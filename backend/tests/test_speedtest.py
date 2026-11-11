"""Response-speed measurement, timing headers, and local HTTP behaviour."""

from __future__ import annotations

import email.message
import http.server
import sys
import threading
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from img_lab import speedtest
from img_lab.speedtest import (
    SpeedTestError,
    SpeedTester,
    _percentile,
    _request_target,
    _server_ms,
    validate_url,
)


class UrlTests(unittest.TestCase):
    def test_valid_urls_are_returned_trimmed(self) -> None:
        self.assertEqual(
            validate_url("  https://example.com/a?b=1  "),
            "https://example.com/a?b=1",
        )
        self.assertEqual(validate_url("http://127.0.0.1:8080/"), "http://127.0.0.1:8080/")

    def test_invalid_urls_raise_speed_test_error(self) -> None:
        cases = [
            "",
            "example.com",
            "ftp://example.com/file",
            "http://",
            "http://user:password@example.com/",
            "http://example.com:not-a-port/",
            "https://" + "a" * 2100 + ".example/",
            None,
        ]
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(SpeedTestError):
                    validate_url(value)

    def test_request_target_preserves_path_and_query(self) -> None:
        parsed = speedtest.urlsplit("http://example.com/a b/c?q=x y&z=1")
        target = _request_target(parsed)
        self.assertIn("/a%20b/c", target)
        self.assertIn("q=x%20y", target)


class TimingHeaderTests(unittest.TestCase):
    def headers(self, **values: str) -> email.message.Message:
        message = email.message.Message()
        for key, value in values.items():
            message[key.replace("_", "-")] = value
        return message

    def test_server_timing_dur_is_parsed(self) -> None:
        self.assertAlmostEqual(
            _server_ms(self.headers(Server_Timing='cache;desc="hit", db;dur=12.5')),
            12.5,
        )
        self.assertAlmostEqual(
            _server_ms(self.headers(Server_Timing='app;dur="8.25"')),
            8.25,
        )

    def test_response_time_and_runtime_headers(self) -> None:
        self.assertAlmostEqual(_server_ms(self.headers(X_Response_Time="45ms")), 45.0)
        self.assertAlmostEqual(_server_ms(self.headers(X_Response_Time="0.5s")), 500.0)
        self.assertAlmostEqual(_server_ms(self.headers(X_Response_Time="17")), 17.0)
        self.assertAlmostEqual(_server_ms(self.headers(X_Runtime="0.25")), 250.0)

    def test_missing_or_malformed_headers_return_none(self) -> None:
        self.assertIsNone(_server_ms(self.headers()))
        self.assertIsNone(_server_ms(self.headers(Server_Timing="db;dur=fast")))
        self.assertIsNone(_server_ms(self.headers(X_Response_Time="soon")))
        self.assertIsNone(_server_ms(self.headers(X_Runtime="")))

    def test_percentile_uses_nearest_rank(self) -> None:
        data = list(range(1, 101))
        self.assertEqual(_percentile(data, 50), 50)
        self.assertEqual(_percentile(data, 95), 95)
        self.assertEqual(_percentile([], 95), 0.0)
        self.assertEqual(_percentile([4.0], 95), 4.0)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/ok":
            body = b"hello"
            self.send_response(200)
            self.send_header("Server-Timing", "app;dur=12.5")
        elif self.path == "/big":
            body = b"B" * 10_000
            self.send_response(200)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            body = b""
        elif self.path == "/notfound":
            self.send_response(404)
            body = b"missing"
        else:
            self.send_response(500)
            body = b"error"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LocalServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def test_successful_measurement_separates_phases(self) -> None:
        result = SpeedTester().run(self.url("/ok"), concurrency=2, rounds=2, timeout=5)
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["completed"], 4)
        self.assertEqual(result["ok"], 4)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["fail_rate"], 0.0)
        self.assertTrue(result["server_timing"])
        self.assertFalse(result["cancelled"])
        self.assertEqual(result["statuses"], {"200": 4})
        self.assertEqual(len(result["samples"]), 4)
        self.assertEqual(result["stats"]["server"]["p50"], 12.5)
        for key in ("total", "connect", "send", "wait", "download", "server"):
            self.assertIn(key, result["stats"])
            self.assertIn("p50", result["stats"][key])
            self.assertIn("p95", result["stats"][key])
            self.assertIn("mean", result["stats"][key])
        sample = result["samples"][0]
        self.assertEqual(sample["status"], 200)
        self.assertTrue(sample["ok"])
        self.assertIn("connect_ms", sample)
        self.assertIn("wait_ms", sample)
        self.assertIn("download_ms", sample)
        self.assertEqual(sample["server_ms"], 12.5)

    def test_http_errors_count_towards_fail_rate(self) -> None:
        result = SpeedTester().run(self.url("/notfound"), concurrency=1, rounds=2, timeout=5)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["ok"], 0)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(result["fail_rate"], 1.0)
        self.assertEqual(result["statuses"], {"404": 2})
        self.assertEqual(result["samples"][0]["error"], "HTTP 404")

    def test_redirect_is_followed(self) -> None:
        result = SpeedTester().run(self.url("/redirect"), concurrency=1, rounds=1, timeout=5)
        sample = result["samples"][0]
        self.assertEqual(sample["status"], 200)
        self.assertEqual(sample["redirects"], 1)
        self.assertTrue(sample["url"].endswith("/ok"))
        self.assertTrue(result["ok"])

    def test_body_is_downloaded(self) -> None:
        result = SpeedTester().run(self.url("/big"), concurrency=1, rounds=1, timeout=5)
        self.assertEqual(result["samples"][0]["bytes"], 10_000)
        self.assertFalse(result["samples"][0]["truncated"])

    def test_cancel_stops_before_requests(self) -> None:
        cancel = threading.Event()
        cancel.set()
        result = SpeedTester().run(
            self.url("/ok"),
            concurrency=2,
            rounds=2,
            timeout=5,
            cancel=cancel,
        )
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["samples"], [])
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["fail_rate"], 0.0)

    def test_invalid_parameters_are_rejected(self) -> None:
        tester = SpeedTester()
        with self.assertRaises(SpeedTestError):
            tester.run(self.url("/ok"), concurrency=0)
        with self.assertRaises(SpeedTestError):
            tester.run(self.url("/ok"), concurrency=1, rounds=21)
        with self.assertRaises(SpeedTestError):
            tester.run(self.url("/ok"), concurrency=16, rounds=20)
        with self.assertRaises(SpeedTestError):
            tester.run(self.url("/ok"), concurrency=1, rounds=1, timeout=0.5)
        with self.assertRaises(SpeedTestError):
            tester.run("ftp://example.com/file")


if __name__ == "__main__":
    unittest.main()
