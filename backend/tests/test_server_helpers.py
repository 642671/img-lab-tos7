"""HTTP routing, job lifecycle, preview, and socket checks."""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from img_lab import __version__
from img_lab import server as backend_server
from img_lab.encoders import EncoderRegistry
from img_lab.library import Library
from img_lab.sizing import ImageSolver
from img_lab.speedtest import SpeedTester


class Harness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.frontend = root / "webui"
        self.frontend.mkdir()
        (self.frontend / "index.html").write_text(
            "<!doctype html><title>Image Lab</title><p>test-index</p>",
            encoding="utf-8",
        )
        (self.frontend / "app.js").write_text("window.testApp = true;\n", encoding="utf-8")
        (root / "secret.txt").write_text("secret", encoding="utf-8")

        self.registry = EncoderRegistry()
        self.registry.detect()
        self.library = Library(root / "data")
        self.solver = ImageSolver(self.registry)
        self.speedtester = SpeedTester()
        self.jobs = backend_server.JobManager(self.solver, self.library, self.speedtester)
        self.context = backend_server.AppContext(
            frontend_dir=str(self.frontend),
            library=self.library,
            encoders=self.registry,
            solver=self.solver,
            speedtester=self.speedtester,
            jobs=self.jobs,
            allowed_origins={"http://tos.example"},
            socket_path="",
            preview_cache=backend_server.PreviewCache(),
        )
        self.server = backend_server.create_server(
            self.context,
            tcp_address=("127.0.0.1", 0),
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.jobs.shutdown()

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            payload = response.read()
            return response.status, dict(response.getheaders()), payload
        finally:
            connection.close()

    def json_request(
        self,
        method: str,
        path: str,
        payload: object,
    ) -> tuple[int, dict[str, str], bytes]:
        return self.request(
            method,
            path,
            body=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )


class HttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.harness = Harness(self.root)
        self.addCleanup(self.harness.close)

    def test_health_identifies_the_app(self) -> None:
        status, headers, body = self.harness.request("GET", "/health")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["app"], "img-lab")
        self.assertEqual(payload["name"], "img-lab")
        self.assertEqual(payload["version"], __version__)
        self.assertTrue(payload["capabilities"]["png"]["available"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])

    def test_health_and_options_through_proxy_prefixes(self) -> None:
        for path in (
            "/v2/proxy/img-lab/health",
            "/img-lab/health",
            "/img-lab/api/health",
            "/v2/proxy/img-lab/api/health",
        ):
            with self.subTest(path=path):
                status, _, body = self.harness.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["app"], "img-lab")

        status, _, body = self.harness.request("GET", "/img-lab/api/options")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(
            [item["id"] for item in payload["contents"]],
            ["photo", "gradient", "solid", "noise"],
        )
        self.assertIn("jpegQualityCurve", payload["priors"])
        self.assertIn("format_max_pixels", payload["limits"])
        self.assertEqual(payload["defaults"]["quality"], 85)
        self.assertEqual(payload["defaults"]["seed"], 424242)
        self.assertIn("max_concurrency", payload["speed"])

    def test_index_head_and_static_files(self) -> None:
        status, headers, body = self.harness.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"test-index", body)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertEqual(headers["Cache-Control"], "no-store")

        status, headers, body = self.harness.request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"window.testApp", body)
        self.assertTrue(headers["Content-Type"].startswith("text/javascript"))

        status, headers, body = self.harness.request("HEAD", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertGreater(int(headers["Content-Length"]), 0)

    def test_traversal_and_unknown_routes_are_rejected(self) -> None:
        for path in ("/%2e%2e/secret.txt", "/../secret.txt", "/missing.js"):
            with self.subTest(path=path):
                status, _, body = self.harness.request("GET", path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"secret", body)

        status, _, body = self.harness.request("GET", "/api/missing")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["code"], "not_found")

    def test_preview_is_generated_and_cached(self) -> None:
        status, headers, body = self.harness.request(
            "GET", "/api/preview?width=800&height=600&content=photo&seed=1"
        )
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("image/"))
        self.assertIn("X-Preview-Size", headers)
        self.assertTrue(body)

        status, _, _ = self.harness.request(
            "GET", "/api/preview?width=800&height=600&content=invalid"
        )
        self.assertEqual(status, 400)
        status, _, _ = self.harness.request("GET", "/api/preview?width=bad")
        self.assertEqual(status, 400)

    def test_generate_job_library_download_manifest_and_bundle(self) -> None:
        status, _, body = self.harness.json_request(
            "POST",
            "/api/generate",
            {
                "specs": [
                    {
                        "mode": "resolution",
                        "format": "png",
                        "width": 128,
                        "height": 128,
                        "content": "solid",
                        "seed": 7,
                    }
                ]
            },
        )
        self.assertEqual(status, 200)
        job_id = json.loads(body)["job"]["id"]
        job = self._wait_for_job(job_id)
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(len(job["result"]["entries"]), 1)
        entry = job["result"]["entries"][0]
        self.assertEqual(entry["format"], "png")
        self.assertEqual(entry["width"], 128)
        self.assertEqual(entry["height"], 128)
        self.assertGreater(entry["bytes"], 0)
        self.assertTrue(entry["converged"])
        stored = self.harness.library.get(str(entry["id"]))
        self.assertIsNotNone(stored)
        self.assertEqual(stored[1].stat().st_size, entry["bytes"])

        status, headers, data = self.harness.request(
            "GET", f"/api/files/{entry['id']}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(data), entry["bytes"])
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertIn("attachment", headers["Content-Disposition"])

        status, headers, body = self.harness.request("GET", "/api/manifest.csv")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
        self.assertIn(entry["filename"].encode("utf-8"), body)
        self.assertTrue(headers["Content-Type"].startswith("text/csv"))

        status, headers, body = self.harness.request("GET", "/api/bundle.zip")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        archive_path = self.root / "bundle.zip"
        archive_path.write_bytes(body)
        with zipfile.ZipFile(archive_path) as archive:
            self.assertIn("manifest.csv", archive.namelist())
            self.assertIn(entry["filename"], archive.namelist())
            self.assertEqual(archive.read(entry["filename"]), data)

        status, _, body = self.harness.request("GET", "/api/library")
        self.assertEqual(status, 200)
        library = json.loads(body)
        self.assertEqual(library["stats"]["count"], 1)
        self.assertEqual(library["limits"]["max_entries"], 200)

        status, _, body = self.harness.json_request(
            "POST", "/api/library/delete", {"id": entry["id"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["stats"]["count"], 0)
        status, _, _ = self.harness.request("GET", f"/api/files/{entry['id']}")
        self.assertEqual(status, 404)

    def _wait_for_job(self, job_id: str) -> dict:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status, _, body = self.harness.request("GET", f"/api/jobs/{job_id}")
            self.assertEqual(status, 200)
            job = json.loads(body)["job"]
            if job["status"] in {"done", "failed", "cancelled"}:
                return job
            time.sleep(0.05)
        self.fail(f"job {job_id} did not finish")

    def test_generate_validation_errors(self) -> None:
        status, _, body = self.harness.request(
            "POST", "/api/generate", body=b"{}", headers={"Content-Type": "text/plain"}
        )
        self.assertEqual(status, 415)
        self.assertEqual(json.loads(body)["code"], "unsupported_media_type")

        status, _, body = self.harness.request(
            "POST",
            "/api/generate",
            body=b"{",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "invalid_json")

        status, _, body = self.harness.json_request(
            "POST", "/api/generate", {"mode": "resolution"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "invalid_spec")

        status, _, body = self.harness.json_request(
            "POST",
            "/api/generate",
            {"specs": [{"mode": "resolution", "width": 128, "height": 128}] * 51},
        )
        self.assertEqual(status, 400)
        self.assertIn("50", json.loads(body)["error"])

    def test_speedtest_validation_error(self) -> None:
        status, _, body = self.harness.json_request(
            "POST", "/api/speedtest", {"url": "ftp://example.com/"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "invalid_speedtest")

    def test_job_routes_and_library_errors(self) -> None:
        status, _, body = self.harness.request("GET", "/api/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["jobs"], [])

        status, _, _ = self.harness.request("GET", "/api/jobs/not-found")
        self.assertEqual(status, 404)
        status, _, _ = self.harness.request(
            "POST", "/api/jobs/not-found/cancel", body=b"{}",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 404)

        status, _, body = self.harness.json_request(
            "POST", "/api/library/delete", {}
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "invalid_id")

        status, _, _ = self.harness.json_request(
            "POST", "/api/library/delete", {"id": "abcdef-1234"}
        )
        self.assertEqual(status, 404)
        status, _, _ = self.harness.request("GET", "/api/files/abcdef-1234")
        self.assertEqual(status, 404)

    def test_clear_library_accepts_empty_post(self) -> None:
        status, _, body = self.harness.request("POST", "/api/library/clear")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["removed"], 0)

    def test_cors_is_limited_to_configured_origins(self) -> None:
        status, headers, _ = self.harness.request(
            "OPTIONS", "/api/generate", headers={"Origin": "http://tos.example"}
        )
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "http://tos.example")
        self.assertEqual(headers["Access-Control-Allow-Credentials"], "true")

        status, headers, _ = self.harness.request(
            "OPTIONS", "/api/generate", headers={"Origin": "http://evil.example"}
        )
        self.assertEqual(status, 204)
        self.assertNotIn("Access-Control-Allow-Origin", headers)


class RouteTests(unittest.TestCase):
    def test_normalize_route(self) -> None:
        cases = {
            "/": "/",
            "/img-lab": "/",
            "/img-lab/": "/",
            "/img-lab/index.html": "/index.html",
            "/v2/proxy/img-lab/health": "/health",
            "/v2/proxy/img-lab/api/jobs/abc": "/api/jobs/abc",
            "/api/options?x=1": "/api/options",
            "health": "/health",
            "/img-lab/styles.css?v=0.1.0": "/styles.css",
        }
        for raw, expected in cases.items():
            self.assertEqual(backend_server.normalize_route(raw), expected, raw)

    def test_parse_int_query_clamps(self) -> None:
        query = {"width": ["-5"], "height": ["99999"]}
        self.assertEqual(
            backend_server.parse_int_query(query, "width", 1600, 16, 20000), 16
        )
        self.assertEqual(
            backend_server.parse_int_query(query, "height", 1200, 16, 20000), 20000
        )
        self.assertEqual(
            backend_server.parse_int_query({}, "width", 1600, 16, 20000), 1600
        )
        with self.assertRaises(ValueError):
            backend_server.parse_int_query({"width": ["x"]}, "width", 1600, 16, 20000)

    def test_platform_id_environment_override(self) -> None:
        original = os.environ.get("IMG_LAB_PLATFORM")
        try:
            os.environ["IMG_LAB_PLATFORM"] = "aarch64"
            self.assertEqual(backend_server.platform_id(), "aarch64")
            os.environ["IMG_LAB_PLATFORM"] = "x86_64"
            self.assertEqual(backend_server.platform_id(), "x86_64")
            os.environ["IMG_LAB_PLATFORM"] = "sparc"
            self.assertIn(backend_server.platform_id(), {"x86_64", "aarch64"})
        finally:
            if original is None:
                os.environ.pop("IMG_LAB_PLATFORM", None)
            else:
                os.environ["IMG_LAB_PLATFORM"] = original

    def test_as_bool(self) -> None:
        for value in (True, 1, "1", "true", "YES", "on"):
            self.assertTrue(backend_server._as_bool(value))
        for value in (False, 0, "0", "false", "off", ""):
            self.assertFalse(backend_server._as_bool(value))
        self.assertTrue(backend_server._as_bool("unknown", default=True))
        self.assertFalse(backend_server._as_bool(None))


class PayloadTests(unittest.TestCase):
    def test_spec_defaults(self) -> None:
        spec = backend_server.spec_from_payload(
            {"mode": "resolution", "width": 192, "height": 128}
        )
        self.assertEqual(spec.format, "jpeg")
        self.assertEqual(spec.quality, 85)
        self.assertEqual(spec.content, "photo")
        self.assertEqual(spec.seed, 424242)
        self.assertFalse(spec.probe)
        self.assertEqual(spec.width, 192)
        self.assertEqual(spec.height, 128)

    def test_spec_full_options(self) -> None:
        spec = backend_server.spec_from_payload(
            {
                "mode": "bytes",
                "format": "png",
                "content": "solid",
                "quality": 40,
                "seed": 7,
                "target_bytes": 1024 * 1024,
                "aspect": 1.5,
                "probe": "yes",
            }
        )
        self.assertEqual(spec.format, "png")
        self.assertEqual(spec.content, "solid")
        self.assertEqual(spec.quality, 40)
        self.assertEqual(spec.seed, 7)
        self.assertEqual(spec.target_bytes, 1024 * 1024)
        self.assertAlmostEqual(spec.aspect, 1.5)
        self.assertTrue(spec.probe)

    def test_invalid_specs_raise(self) -> None:
        cases = [
            {"mode": "other", "width": 128, "height": 128},
            {"mode": "resolution", "width": 128, "height": 128, "format": "gif"},
            {"mode": "resolution", "width": 128, "height": 128, "content": "x"},
            {"mode": "resolution", "width": 128, "height": 128, "quality": 101},
            {"mode": "resolution", "width": 64, "height": 128},
            {"mode": "resolution", "width": 128, "height": 128, "aspect": 0.1},
            {"mode": "bytes", "target_bytes": 1023},
            {"mode": "bytes", "target_bytes": 1024, "seed": -1},
            {"mode": "resolution", "width": "x", "height": 128},
            [],
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises((ValueError, TypeError)):
                    backend_server.spec_from_payload(payload)

    def test_specs_from_payload_variants(self) -> None:
        specs = backend_server.specs_from_payload(
            {
                "specs": [
                    {"mode": "resolution", "width": 128, "height": 128},
                    {"mode": "bytes", "target_bytes": 2048},
                ]
            }
        )
        self.assertEqual(len(specs), 2)

        single = backend_server.specs_from_payload(
            {"spec": {"mode": "resolution", "width": 128, "height": 128}}
        )
        self.assertEqual(len(single), 1)

        bare = backend_server.specs_from_payload(
            {"mode": "resolution", "width": 128, "height": 128}
        )
        self.assertEqual(len(bare), 1)

        with self.assertRaises(ValueError):
            backend_server.specs_from_payload({})
        with self.assertRaises(ValueError):
            backend_server.specs_from_payload({"specs": []})
        with self.assertRaises(ValueError):
            backend_server.specs_from_payload(
                {"specs": [{"mode": "resolution", "width": 128, "height": 128}] * 51}
            )

    def test_speed_options(self) -> None:
        options = backend_server.speed_options_from_payload(
            {"url": "http://127.0.0.1:8080/"}
        )
        self.assertEqual(options["concurrency"], 4)
        self.assertEqual(options["rounds"], 3)
        self.assertEqual(options["timeout"], 10.0)

        for payload in (
            {"url": "ftp://example.com/"},
            {"url": "http://example.com/", "concurrency": 17},
            {"url": "http://example.com/", "rounds": 21},
            {"url": "http://example.com/", "concurrency": 16, "rounds": 11},
            {"url": "http://example.com/", "timeout": 0.5},
            {"url": "http://example.com/", "timeout": "soon"},
            {"url": "http://example.com/", "concurrency": ""},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    backend_server.speed_options_from_payload(payload)


@unittest.skipUnless(os.name == "posix", "Unix sockets are validated on Linux")
class SocketLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_stale_socket_is_removed(self) -> None:
        path = self.root / "api.sock"
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(path))
        stale.close()
        self.assertIsNotNone(backend_server.socket_identity(str(path)))
        backend_server.prepare_socket(str(path))
        self.assertFalse(os.path.exists(path))

    def test_regular_file_is_not_replaced(self) -> None:
        path = self.root / "not-a-socket"
        path.write_text("keep", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            backend_server.prepare_socket(str(path))
        self.assertEqual(path.read_text(encoding="utf-8"), "keep")

    def test_missing_parent_is_reported(self) -> None:
        with self.assertRaises(RuntimeError):
            backend_server.prepare_socket(str(self.root / "missing" / "api.sock"))


if __name__ == "__main__":
    unittest.main()
