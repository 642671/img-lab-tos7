"""Response-speed measurement with network and server time separated.

Each request is timed as connect -> send -> wait for headers -> download body.
Server processing time is read from ``Server-Timing`` (``dur`` in ms) or
``X-Response-Time`` / ``X-Runtime`` response headers when the target provides
them.  Redirects are followed up to three times.
"""

from __future__ import annotations

import http.client
import math
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urljoin, urlsplit

MAX_CONCURRENCY = 16
MAX_ROUNDS = 20
MAX_REQUESTS = 160
MAX_REDIRECTS = 3
MAX_BODY_BYTES = 32 * 1024 * 1024
USER_AGENT = "img-lab-speedtest/0.1"

ERROR_KINDS = ("timeout", "dns", "connect", "tls", "protocol", "http", "other")


class SpeedTestError(ValueError):
    """Invalid speed-test parameters or URL."""


def validate_url(url: str) -> str:
    if not isinstance(url, str):
        raise SpeedTestError("目标地址必须是字符串")
    url = url.strip()
    if not url:
        raise SpeedTestError("请输入目标地址")
    if len(url) > 2048:
        raise SpeedTestError("目标地址过长")
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise SpeedTestError(f"目标地址无法解析：{exc}") from exc
    if parsed.scheme.lower() not in ("http", "https"):
        raise SpeedTestError("目标地址只支持 http:// 或 https://")
    if not parsed.hostname:
        raise SpeedTestError("目标地址缺少主机名")
    if parsed.username or parsed.password:
        raise SpeedTestError("目标地址不能包含账号密码")
    try:
        parsed.port
    except ValueError as exc:
        raise SpeedTestError("目标地址端口无效") from exc
    return url


def _request_target(parsed) -> str:
    path = parsed.path or "/"
    safe = "/%:@!$&'()*+,;=-._~"
    target = quote(path, safe=safe)
    if parsed.query:
        target += "?" + quote(parsed.query, safe=safe + "?")
    return target


def _server_ms(headers: http.client.HTTPMessage) -> float | None:
    value = headers.get("Server-Timing")
    if value:
        for metric in value.split(","):
            parts = metric.split(";")
            for part in parts[1:]:
                key, _, raw = part.strip().partition("=")
                if key.strip().lower() == "dur":
                    try:
                        return max(0.0, float(raw.strip().strip('"')))
                    except ValueError:
                        continue
    value = headers.get("X-Response-Time")
    if value:
        text = value.strip().lower()
        try:
            if text.endswith("ms"):
                return max(0.0, float(text[:-2]))
            if text.endswith("s"):
                return max(0.0, float(text[:-1]) * 1000)
            return max(0.0, float(text))
        except ValueError:
            pass
    value = headers.get("X-Runtime")
    if value:
        try:
            return max(0.0, float(value.strip()) * 1000)
        except ValueError:
            pass
    return None


def _classify(exc: BaseException) -> str:
    if isinstance(exc, (socket.gaierror,)):
        return "dns"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, ssl.SSLError):
        return "tls"
    if isinstance(exc, ConnectionError):
        return "connect"
    if isinstance(exc, http.client.HTTPException):
        return "protocol"
    return "other"


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{exc.__class__.__name__}: {text}" if text else exc.__class__.__name__


class SpeedTester:
    def run(
        self,
        url: str,
        concurrency: int = 4,
        rounds: int = 3,
        timeout: float = 10.0,
        cancel: threading.Event | None = None,
        progress=None,
    ) -> dict[str, object]:
        url = validate_url(url)
        concurrency = int(concurrency)
        rounds = int(rounds)
        if not 1 <= concurrency <= MAX_CONCURRENCY:
            raise SpeedTestError(f"并发数必须在 1 到 {MAX_CONCURRENCY} 之间")
        if not 1 <= rounds <= MAX_ROUNDS:
            raise SpeedTestError(f"轮次必须在 1 到 {MAX_ROUNDS} 之间")
        if concurrency * rounds > MAX_REQUESTS:
            raise SpeedTestError(f"总请求数不能超过 {MAX_REQUESTS}")
        timeout = float(timeout)
        if not 1.0 <= timeout <= 60.0:
            raise SpeedTestError("单请求超时必须介于 1 到 60 秒之间")

        total = concurrency * rounds
        started = time.perf_counter()
        samples: list[dict[str, object]] = []
        state_lock = threading.Lock()
        done = 0

        def notify() -> None:
            if progress is None:
                return
            with state_lock:
                snapshot = {
                    "done": done,
                    "total": total,
                    "ok": sum(1 for item in samples if item.get("ok")),
                    "failed": sum(1 for item in samples if not item.get("ok")),
                }
            try:
                progress(snapshot)
            except Exception:
                pass

        def worker(worker_index: int) -> None:
            nonlocal done
            for round_index in range(1, rounds + 1):
                if cancel is not None and cancel.is_set():
                    return
                sample = self._once(url, timeout)
                sample["worker"] = worker_index
                sample["round"] = round_index
                with state_lock:
                    samples.append(sample)
                    done += 1
                notify()
                if cancel is not None and cancel.is_set():
                    return

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(worker, index + 1) for index in range(concurrency)]
            for future in futures:
                future.result()
        wall_ms = (time.perf_counter() - started) * 1000

        cancelled = bool(cancel is not None and cancel.is_set() and len(samples) < total)
        samples.sort(key=lambda item: (int(item["round"]), int(item["worker"])))
        completed = [item for item in samples if item.get("completed")]
        ok = [item for item in samples if item.get("ok")]
        failed = len(samples) - len(ok)
        statuses: dict[str, int] = {}
        for item in samples:
            key = str(item.get("status") or item.get("error_kind") or "error")
            statuses[key] = statuses.get(key, 0) + 1

        result = {
            "url": url,
            "concurrency": concurrency,
            "rounds": rounds,
            "total": total,
            "completed": len(completed),
            "ok": len(ok),
            "failed": failed,
            "fail_rate": (failed / total) if total else 0.0,
            "wall_ms": round(wall_ms, 2),
            "cancelled": cancelled,
            "server_timing": any(item.get("server_ms") is not None for item in completed),
            "statuses": statuses,
            "stats": self._stats(completed),
            "samples": samples,
        }
        return result

    def _once(self, url: str, timeout: float) -> dict[str, object]:
        started = time.perf_counter()
        connect_ms = send_ms = wait_ms = download_ms = 0.0
        received = 0
        truncated = False
        redirects = 0
        current = url
        status = None
        server_ms = None
        error = ""
        error_kind = ""
        completed = False
        final_url = url

        try:
            for hop in range(MAX_REDIRECTS + 1):
                parsed = urlsplit(current)
                host = parsed.hostname or ""
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                if parsed.scheme == "https":
                    connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                        host, port, timeout=timeout
                    )
                else:
                    connection = http.client.HTTPConnection(host, port, timeout=timeout)
                try:
                    begin = time.perf_counter()
                    connection.connect()
                    connect_ms += (time.perf_counter() - begin) * 1000

                    begin = time.perf_counter()
                    connection.request(
                        "GET",
                        _request_target(parsed),
                        headers={
                            "User-Agent": USER_AGENT,
                            "Accept": "*/*",
                            "Accept-Encoding": "identity",
                            "Cache-Control": "no-cache",
                            "Connection": "close",
                        },
                    )
                    send_ms += (time.perf_counter() - begin) * 1000

                    begin = time.perf_counter()
                    response = connection.getresponse()
                    wait_ms += (time.perf_counter() - begin) * 1000
                    status = response.status
                    final_url = current
                    server_ms = _server_ms(response.headers)

                    location = response.getheader("Location")
                    if status in (301, 302, 303, 307, 308) and location and hop < MAX_REDIRECTS:
                        redirects += 1
                        target = urljoin(current, location)
                        try:
                            next_parsed = urlsplit(target)
                        except ValueError as exc:
                            raise SpeedTestError(f"重定向地址无法解析：{exc}") from exc
                        if next_parsed.scheme.lower() not in ("http", "https"):
                            raise SpeedTestError("重定向到了不支持的协议")
                        current = target
                        continue

                    begin = time.perf_counter()
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        received += len(chunk)
                        if received >= MAX_BODY_BYTES:
                            truncated = True
                            break
                    download_ms += (time.perf_counter() - begin) * 1000
                    completed = True
                    break
                finally:
                    connection.close()

            if status is None:
                raise http.client.HTTPException("没有收到响应")
            if status >= 400:
                error = f"HTTP {status}"
                error_kind = "http"
        except BaseException as exc:  # noqa: BLE001 - turn every failure into data
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            error = _error_text(exc)
            error_kind = _classify(exc)
            # Headers already received: keep the sample for latency stats, but
            # it stays a failure for the fail rate.
            completed = status is not None
        total_ms = (time.perf_counter() - started) * 1000
        ok = completed and not error

        return {
            "url": final_url,
            "status": status,
            "ok": ok,
            "completed": completed or status is not None,
            "error": error,
            "error_kind": error_kind,
            "redirects": redirects,
            "bytes": received,
            "truncated": truncated,
            "total_ms": round(total_ms, 2),
            "connect_ms": round(connect_ms, 2),
            "send_ms": round(send_ms, 2),
            "wait_ms": round(wait_ms, 2),
            "download_ms": round(download_ms, 2),
            "server_ms": None if server_ms is None else round(server_ms, 2),
        }

    @staticmethod
    def _stats(samples: list[dict[str, object]]) -> dict[str, object]:
        def values(key: str) -> list[float]:
            return [
                float(item[key])
                for item in samples
                if item.get(key) is not None
            ]

        def summarise(key: str) -> dict[str, float] | None:
            data = values(key)
            if not data:
                return None
            ordered = sorted(data)
            return {
                "mean": round(sum(ordered) / len(ordered), 2),
                "p50": round(_percentile(ordered, 50), 2),
                "p95": round(_percentile(ordered, 95), 2),
                "min": round(ordered[0], 2),
                "max": round(ordered[-1], 2),
            }

        return {
            "total": summarise("total_ms"),
            "connect": summarise("connect_ms"),
            "send": summarise("send_ms"),
            "wait": summarise("wait_ms"),
            "download": summarise("download_ms"),
            "server": summarise("server_ms"),
        }


def _percentile(ordered: list[float], percentile: float) -> float:
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, math.ceil(percentile / 100 * len(ordered)) - 1))
    return ordered[index]
