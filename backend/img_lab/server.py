"""HTTP service for Image Lab, reachable through /var/api/img-lab.sock."""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import secrets
import signal
import socket
import stat
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

from . import APP_NAME, __version__
from .content import (
    CONTENT_TYPES,
    MAX_DIMENSION,
    MAX_PIXELS,
    MIN_DIMENSION,
    NOISE_LEVELS,
    PROBE_BLOCK,
    PROBE_STRIPES,
    render,
)
from .encoders import (
    DISPLAY_NAMES,
    EXTENSIONS,
    MIME_TYPES,
    EncoderError,
    EncoderRegistry,
)
from .library import Library, LibraryError, MAX_ENTRIES, MAX_TOTAL_BYTES
from .sizing import (
    FINAL_TOLERANCE,
    FORMAT_MAX_PIXELS,
    LEVEL1_TOLERANCE,
    MAX_OUTPUT_BYTES,
    Cancelled,
    ImageSolver,
    Outcome,
    TargetSpec,
    priors_payload,
)
from .speedtest import (
    MAX_BODY_BYTES as MAX_SPEED_BODY_BYTES,
    MAX_CONCURRENCY,
    MAX_REDIRECTS,
    MAX_REQUESTS,
    MAX_ROUNDS,
    SpeedTestError,
    SpeedTester,
    validate_url,
)

APP_ID = APP_NAME
DEFAULT_SOCKET_PATH = "/var/api/img-lab.sock"
MAX_BODY_BYTES = 64 * 1024
MAX_BATCH_ITEMS = 50
AF_UNIX = getattr(socket, "AF_UNIX", None)

CONTENT_LABELS = {
    "photo": "照片纹理",
    "gradient": "平滑渐变",
    "solid": "纯色块",
    "noise": "高频噪声",
}
CONTENT_DESCRIPTIONS = {
    "photo": "渐变底色 + 随机色块 + 中频纹理 + 高频噪声，最接近真实照片",
    "gradient": "线性渐变，压缩率很高，适合测试低码率场景",
    "solid": "单一纯色，压缩率极高，用于极端下限测试",
    "noise": "逐像素随机噪声，不可压缩，用于上限和压力测试",
}

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors *"
)

logger = logging.getLogger("img-lab")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def platform_id() -> str:
    value = os.environ.get("IMG_LAB_PLATFORM", "").strip().lower()
    if value in {"x86_64", "aarch64"}:
        return value
    machine = platform.machine().lower()
    if machine in {"aarch64", "arm64"}:
        return "aarch64"
    return "x86_64"


def normalize_route(path: str, app_id: str = APP_ID) -> str:
    """Strip the TOS proxy prefixes so every deployment shape reaches one router."""

    raw = str(path or "/")
    raw = raw.split("?", 1)[0].split("#", 1)[0]
    if not raw.startswith("/"):
        raw = "/" + raw
    parts = [part for part in unquote(raw).split("/") if part]
    if len(parts) >= 2 and parts[0] == "v2" and parts[1] == "proxy":
        parts = parts[2:]
    if parts and parts[0] == app_id:
        parts = parts[1:]
    if not parts:
        return "/"
    return "/" + "/".join(parts)


def parse_int_query(
    query: dict[str, list[str]],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = (query.get(name) or [""])[0].strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    return max(minimum, min(maximum, value))


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def prepare_socket(path: str) -> None:
    parent = os.path.dirname(path)
    if not parent:
        raise RuntimeError(f"socket 路径无效：{path}")
    if not os.path.isdir(parent):
        raise RuntimeError(
            f"目录 {parent} 不存在；请确认 TOS 已创建 /var/api 并重新安装应用"
        )
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(f"无法检查 socket 路径 {path}：{exc}") from exc
    if not stat.S_ISSOCK(info.st_mode):
        raise RuntimeError(f"{path} 已存在且不是 socket，拒绝覆盖")
    os.unlink(path)


def socket_identity(path: str) -> tuple[int, int] | None:
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISSOCK(info.st_mode):
        return None
    return (info.st_dev, info.st_ino)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


class JobBusy(RuntimeError):
    def __init__(self, message: str, job_id: str) -> None:
        super().__init__(message)
        self.job_id = job_id


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str = ""
    error_code: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)

    def to_dict(self, include_result: bool = True) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": dict(self.progress),
            "error": self.error,
            "error_code": self.error_code,
        }
        if include_result:
            payload["result"] = self.result
        return payload


class JobManager:
    def __init__(
        self,
        solver: ImageSolver,
        library: Library,
        speedtester: SpeedTester,
        max_jobs: int = 20,
    ) -> None:
        self._solver = solver
        self._library = library
        self._speedtester = speedtester
        self._max_jobs = max_jobs
        self._lock = threading.RLock()
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._active: dict[str, str] = {}
        self._threads: set[threading.Thread] = set()

    # -- lifecycle -------------------------------------------------------

    def start_generate(self, specs: list[TargetSpec]) -> dict[str, Any]:
        with self._lock:
            self._reject_active_locked("generate")
            job = Job(id=secrets.token_hex(8), kind="generate")
            self._jobs[job.id] = job
            self._active["generate"] = job.id
            thread = threading.Thread(
                target=self._run_generate,
                args=(job, specs),
                name=f"img-lab-generate-{job.id}",
                daemon=True,
            )
            self._threads.add(thread)
            thread.start()
            self._prune_locked()
            return job.to_dict()

    def start_speed(self, options: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._reject_active_locked("speed")
            job = Job(id=secrets.token_hex(8), kind="speed")
            self._jobs[job.id] = job
            self._active["speed"] = job.id
            thread = threading.Thread(
                target=self._run_speed,
                args=(job, options),
                name=f"img-lab-speed-{job.id}",
                daemon=True,
            )
            self._threads.add(thread)
            thread.start()
            self._prune_locked()
            return job.to_dict()

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.to_dict() if job else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                job.to_dict(include_result=False)
                for job in reversed(self._jobs.values())
            ]

    def active_summary(self) -> dict[str, str]:
        with self._lock:
            return dict(self._active)

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.status in {"queued", "running"}:
                job.cancel.set()
                if job.status == "queued":
                    job.status = "cancelled"
                    job.finished_at = utc_now()
            return job.to_dict()

    def shutdown(self) -> None:
        with self._lock:
            for job in self._jobs.values():
                if job.status in {"queued", "running"}:
                    job.cancel.set()
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout=5)

    # -- workers ---------------------------------------------------------

    def _run_generate(self, job: Job, specs: list[TargetSpec]) -> None:
        started = time.monotonic()
        entries: list[dict[str, Any]] = []
        with self._lock:
            job.status = "running"
            job.started_at = utc_now()
            job.progress = {
                "batch_index": 1,
                "batch_total": len(specs),
                "stage": "starting",
            }
        try:
            total = len(specs)
            for index, spec in enumerate(specs, start=1):
                def on_progress(detail: dict[str, Any], index=index) -> None:
                    payload = {
                        "batch_index": index,
                        "batch_total": total,
                        **detail,
                    }
                    with self._lock:
                        job.progress = payload

                outcome: Outcome = self._solver.solve(
                    spec,
                    cancel=job.cancel,
                    progress=on_progress,
                )
                entry = self._library.add(outcome)
                entries.append(entry)
                with self._lock:
                    job.progress = {
                        "batch_index": index,
                        "batch_total": total,
                        "stage": "saved",
                        "bytes": entry.get("bytes"),
                        "filename": entry.get("filename"),
                    }
            elapsed_ms = int(round((time.monotonic() - started) * 1000))
            with self._lock:
                job.status = "done"
                job.result = {
                    "entries": entries,
                    "stats": self._library.stats(),
                    "elapsed_ms": elapsed_ms,
                }
                job.progress = {
                    "batch_index": len(specs),
                    "batch_total": len(specs),
                    "stage": "done",
                }
        except Cancelled as exc:
            self._fail_or_cancel(job, "cancelled", str(exc) or "已取消", "cancelled", entries)
        except (EncoderError, ValueError, LibraryError) as exc:
            self._fail_or_cancel(job, "failed", str(exc), "generation_failed", entries)
        except Exception:  # noqa: BLE001
            logger.exception("生成任务 %s 失败", job.id)
            self._fail_or_cancel(job, "failed", "生成任务内部错误", "internal", entries)
        finally:
            with self._lock:
                job.finished_at = utc_now()
                if self._active.get("generate") == job.id:
                    self._active.pop("generate", None)
                self._threads = {
                    thread for thread in self._threads if thread.is_alive() and thread is not threading.current_thread()
                }

    def _run_speed(self, job: Job, options: dict[str, Any]) -> None:
        started = time.monotonic()
        with self._lock:
            job.status = "running"
            job.started_at = utc_now()

        def on_progress(snapshot: dict[str, Any]) -> None:
            with self._lock:
                job.progress = dict(snapshot)

        try:
            result = self._speedtester.run(
                options["url"],
                concurrency=options["concurrency"],
                rounds=options["rounds"],
                timeout=options["timeout"],
                cancel=job.cancel,
                progress=on_progress,
            )
            result["elapsed_ms"] = int(round((time.monotonic() - started) * 1000))
            with self._lock:
                if result.get("cancelled"):
                    job.status = "cancelled"
                else:
                    job.status = "done"
                job.result = result
        except SpeedTestError as exc:
            self._fail_or_cancel(job, "failed", str(exc), "invalid_speedtest", [])
        except Exception:  # noqa: BLE001
            logger.exception("测速任务 %s 失败", job.id)
            self._fail_or_cancel(job, "failed", "测速任务内部错误", "internal", [])
        finally:
            with self._lock:
                job.finished_at = utc_now()
                if self._active.get("speed") == job.id:
                    self._active.pop("speed", None)
                self._threads = {
                    thread for thread in self._threads if thread.is_alive() and thread is not threading.current_thread()
                }

    def _fail_or_cancel(
        self,
        job: Job,
        status: str,
        message: str,
        code: str,
        entries: list[dict[str, Any]],
    ) -> None:
        with self._lock:
            job.status = status
            job.error = message
            job.error_code = code
            job.result = {
                "entries": entries,
                "partial": status != "done",
                "stats": self._library.stats(),
            }

    def _reject_active_locked(self, kind: str) -> None:
        active_id = self._active.get(kind)
        if not active_id:
            return
        job = self._jobs.get(active_id)
        if job and job.status in {"queued", "running"}:
            raise JobBusy("已有同类任务在运行", active_id)
        self._active.pop(kind, None)

    def _prune_locked(self) -> None:
        while len(self._jobs) > self._max_jobs:
            oldest = next(iter(self._jobs.values()))
            if oldest.status in {"queued", "running"}:
                break
            self._jobs.pop(oldest.id, None)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


@dataclass
class AppContext:
    frontend_dir: str
    library: Library
    encoders: EncoderRegistry
    solver: ImageSolver
    speedtester: SpeedTester
    jobs: JobManager
    allowed_origins: set[str]
    socket_path: str
    preview_cache: "PreviewCache"


class PreviewCache:
    def __init__(self, capacity: int = 24) -> None:
        self._capacity = capacity
        self._lock = threading.Lock()
        self._items: OrderedDict[tuple[Any, ...], tuple[bytes, str]] = OrderedDict()

    def get(self, key: tuple[Any, ...]) -> tuple[bytes, str] | None:
        with self._lock:
            item = self._items.get(key)
            if item is not None:
                self._items.move_to_end(key)
            return item

    def put(self, key: tuple[Any, ...], data: bytes, mime: str) -> None:
        with self._lock:
            self._items[key] = (data, mime)
            self._items.move_to_end(key)
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)


class AppHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        server_address: Any,
        handler: type[BaseHTTPRequestHandler],
        context: AppContext,
        address_family: int = socket.AF_INET,
    ) -> None:
        self.address_family = address_family
        self.context = context
        super().__init__(server_address, handler)

    def server_bind(self) -> None:
        if AF_UNIX is not None and self.address_family == AF_UNIX:
            self.socket.bind(self.server_address)
            self.server_address = self.socket.getsockname()
            self.server_name = "localhost"
            self.server_port = 0
            return
        super().server_bind()


class HttpError(Exception):
    def __init__(self, status: int, message: str, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


class RequestHandler(BaseHTTPRequestHandler):
    server_version = f"img-lab/{__version__}"
    protocol_version = "HTTP/1.1"

    @property
    def context(self) -> AppContext:
        return self.server.context  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        self._run(head=False)

    def do_HEAD(self) -> None:
        self._run(head=True)

    def do_POST(self) -> None:
        try:
            self._handle_post()
        except HttpError as exc:
            self._send_json(exc.status, {"ok": False, "error": exc.message, "code": exc.code})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            logger.exception("POST %s 处理失败", self.path)
            self._safe_error(500, "服务器内部错误")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Csrf-Token")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _run(self, head: bool) -> None:
        try:
            self._handle_get(head)
        except HttpError as exc:
            self._send_json(exc.status, {"ok": False, "error": exc.message, "code": exc.code})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            logger.exception("GET %s 处理失败", self.path)
            self._safe_error(500, "服务器内部错误")

    def _safe_error(self, status: int, message: str) -> None:
        try:
            self._send_json(status, {"ok": False, "error": message, "code": "internal"})
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin", "")
        allowed = self.context.allowed_origins
        if not origin or not allowed or origin not in allowed:
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Vary", "Origin")

    def _send_headers(
        self,
        status: int,
        content_type: str,
        length: int,
        cache_control: str = "no-store",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Security-Policy", CSP)
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self._cors_headers()
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        head: bool = False,
        cache_control: str = "no-store",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._send_headers(status, content_type, len(body), cache_control, extra_headers)
        if not head:
            self.wfile.write(body)

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        head: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(
            status,
            body,
            "application/json; charset=utf-8",
            head=head,
            extra_headers=extra_headers,
        )

    def _send_file(
        self,
        path: str,
        filename: str,
        content_type: str,
        head: bool = False,
        cache_control: str = "no-store",
    ) -> None:
        size = os.path.getsize(path)
        safe_name = filename.replace('"', "")
        self._send_headers(
            200,
            content_type,
            size,
            cache_control,
            {"Content-Disposition": f'attachment; filename="{safe_name}"'},
        )
        if head:
            return
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # -- GET -------------------------------------------------------------

    def _handle_get(self, head: bool) -> None:
        route = normalize_route(self.path)
        query = parse_qs(urlsplit(self.path).query)

        if route in {"/health", "/api/health"}:
            self._serve_health(head)
            return
        if route == "/api/options":
            self._serve_options(head)
            return
        if route == "/api/library":
            context = self.context
            self._send_json(
                200,
                {
                    "ok": True,
                    "entries": context.library.list_entries(),
                    "stats": context.library.stats(),
                    "limits": {
                        "max_entries": MAX_ENTRIES,
                        "max_total_bytes": MAX_TOTAL_BYTES,
                    },
                },
                head=head,
            )
            return
        if route == "/api/jobs":
            self._send_json(
                200,
                {"ok": True, "jobs": self.context.jobs.list()},
                head=head,
            )
            return
        if route.startswith("/api/jobs/"):
            job_id = route[len("/api/jobs/"):].strip("/")
            if not job_id or "/" in job_id:
                raise HttpError(404, "任务不存在", "not_found")
            job = self.context.jobs.get(job_id)
            if job is None:
                raise HttpError(404, "任务不存在", "not_found")
            self._send_json(200, {"ok": True, "job": job}, head=head)
            return
        if route.startswith("/api/files/"):
            entry_id = route[len("/api/files/"):].strip("/")
            self._serve_library_file(entry_id, head)
            return
        if route == "/api/manifest.csv":
            ids = self._query_ids(query)
            body = self.context.library.manifest_csv(ids)
            filename = (
                "img-lab-manifest-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".csv"
            )
            self._send_bytes(
                200,
                body,
                "text/csv; charset=utf-8",
                head=head,
                extra_headers={
                    "Content-Disposition": f'attachment; filename="{filename}"'
                },
            )
            return
        if route == "/api/bundle.zip":
            try:
                bundle = self.context.library.create_bundle(self._query_ids(query))
            except LibraryError as exc:
                raise HttpError(404, str(exc), "no_files") from exc
            try:
                self._send_file(
                    str(bundle),
                    "img-lab-images-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".zip",
                    "application/zip",
                    head=head,
                )
            finally:
                self.context.library.discard_bundle(bundle)
            return
        if route == "/api/preview":
            self._serve_preview(query, head)
            return
        if route.startswith("/api/"):
            raise HttpError(404, "接口不存在", "not_found")

        if route in {"/", "/index.html"}:
            self._serve_static("index.html", head)
            return
        self._serve_static(route.lstrip("/"), head)

    def _serve_health(self, head: bool) -> None:
        context = self.context
        try:
            capabilities = context.encoders.capabilities()
        except Exception:
            logger.exception("编码器检测失败")
            capabilities = {}
        self._send_json(
            200,
            {
                "ok": True,
                "app": APP_ID,
                "name": APP_ID,
                "version": __version__,
                "platform": platform_id(),
                "time": utc_now(),
                "socket": context.socket_path or "tcp",
                "capabilities": capabilities,
                "library": context.library.stats(),
                "jobs": context.jobs.active_summary(),
            },
            head=head,
        )

    def _serve_options(self, head: bool) -> None:
        context = self.context
        self._send_json(
            200,
            {
                "ok": True,
                "app": APP_ID,
                "version": __version__,
                "formats": context.encoders.capabilities(),
                "formatNames": DISPLAY_NAMES,
                "contents": [
                    {
                        "id": item,
                        "label": CONTENT_LABELS[item],
                        "description": CONTENT_DESCRIPTIONS[item],
                        "bands": NOISE_LEVELS[item]["bands"],
                    }
                    for item in CONTENT_TYPES
                ],
                "defaults": {
                    "format": "jpeg",
                    "quality": 85,
                    "content": "photo",
                    "seed": 424242,
                    "aspect": 4 / 3,
                    "concurrency": 4,
                    "rounds": 3,
                    "timeout": 10,
                },
                "limits": {
                    "min_dimension": MIN_DIMENSION,
                    "max_dimension": MAX_DIMENSION,
                    "max_pixels": MAX_PIXELS,
                    "format_max_pixels": FORMAT_MAX_PIXELS,
                    "max_output_bytes": MAX_OUTPUT_BYTES,
                    "max_batch_items": MAX_BATCH_ITEMS,
                    "probe_block": PROBE_BLOCK,
                    "probe_stripes": PROBE_STRIPES,
                    "level1_tolerance": LEVEL1_TOLERANCE,
                    "final_tolerance": FINAL_TOLERANCE,
                },
                "priors": priors_payload(),
                "speed": {
                    "max_concurrency": MAX_CONCURRENCY,
                    "max_rounds": MAX_ROUNDS,
                    "max_requests": MAX_REQUESTS,
                    "max_redirects": MAX_REDIRECTS,
                    "max_body_bytes": MAX_SPEED_BODY_BYTES,
                },
                "library": {
                    "stats": context.library.stats(),
                    "max_entries": MAX_ENTRIES,
                    "max_total_bytes": MAX_TOTAL_BYTES,
                },
            },
            head=head,
        )

    def _serve_library_file(self, entry_id: str, head: bool) -> None:
        found = self.context.library.get(entry_id)
        if found is None:
            raise HttpError(404, "文件不存在或已被清理", "not_found")
        entry, path = found
        ext = str(entry.get("ext", ""))
        content_type = MIME_TYPES.get(str(entry.get("format")), "application/octet-stream")
        if ext not in EXTENSIONS.values():
            raise HttpError(404, "文件不存在", "not_found")
        self._send_file(
            str(path),
            str(entry.get("filename", f"{entry_id}.{ext}")),
            content_type,
            head=head,
            cache_control="private, max-age=3600",
        )

    def _serve_preview(self, query: dict[str, list[str]], head: bool) -> None:
        try:
            width = parse_int_query(query, "width", 1600, 16, 20000)
            height = parse_int_query(query, "height", 1200, 16, 20000)
            seed = parse_int_query(query, "seed", 424242, 0, 2**31 - 1)
        except ValueError as exc:
            raise HttpError(400, str(exc), "invalid_query") from exc
        content = (query.get("content") or ["photo"])[0].strip().lower()
        if content not in CONTENT_TYPES:
            raise HttpError(400, "内容类型无效", "invalid_content")
        aspect = width / max(1, height)
        aspect = max(0.4, min(2.5, aspect))
        if aspect >= 1:
            preview_width = 320
            preview_height = max(128, round(320 / aspect))
        else:
            preview_height = 320
            preview_width = max(128, round(320 * aspect))
        key = (preview_width, preview_height, content, seed)
        cache = self.context.preview_cache
        hit = cache.get(key)
        if hit is None:
            image = render(
                preview_width,
                preview_height,
                content=content,
                seed=seed,
                probe=False,
            )
            data, image_format = self.context.encoders.encode_preview(
                image, preview_width, preview_height
            )
            mime = MIME_TYPES[image_format]
            cache.put(key, data, mime)
            hit = (data, mime)
        data, mime = hit
        self._send_bytes(
            200,
            data,
            mime,
            head=head,
            cache_control="private, max-age=60",
            extra_headers={"X-Preview-Size": f"{preview_width}x{preview_height}"},
        )

    def _query_ids(self, query: dict[str, list[str]]) -> list[str] | None:
        raw = (query.get("ids") or [""])[0].strip()
        if not raw:
            return None
        items = [item.strip() for item in raw.split(",") if item.strip()]
        return items or None

    def _serve_static(self, relative: str, head: bool) -> None:
        parts = [part for part in relative.split("/") if part not in {"", "."}]
        if not parts or any(part == ".." or part.startswith(".") for part in parts):
            raise HttpError(404, "文件不存在", "not_found")
        suffix = os.path.splitext(parts[-1])[1].lower()
        content_type = STATIC_TYPES.get(suffix)
        if content_type is None:
            raise HttpError(404, "文件不存在", "not_found")
        frontend_dir = os.path.realpath(self.context.frontend_dir)
        target = os.path.realpath(os.path.join(frontend_dir, *parts))
        try:
            if os.path.commonpath([target, frontend_dir]) != frontend_dir:
                raise HttpError(404, "文件不存在", "not_found")
        except ValueError as exc:
            raise HttpError(404, "文件不存在", "not_found") from exc
        if not os.path.isfile(target):
            raise HttpError(404, "文件不存在", "not_found")
        try:
            with open(target, "rb") as handle:
                body = handle.read()
        except OSError as exc:
            logger.warning("读取静态文件失败 %s：%s", target, exc)
            raise HttpError(500, "无法读取页面文件", "static_error") from exc
        cache = "no-store" if suffix == ".html" else "public, max-age=3600"
        self._send_bytes(200, body, content_type, head=head, cache_control=cache)

    # -- POST ------------------------------------------------------------

    def _read_json_body(self) -> dict[str, Any]:
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        if content_type.lower() != "application/json":
            self.close_connection = True
            raise HttpError(415, "请求必须使用 application/json", "unsupported_media_type")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self.close_connection = True
            raise HttpError(411, "缺少 Content-Length", "length_required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            self.close_connection = True
            raise HttpError(400, "Content-Length 无效", "invalid_length") from exc
        if length < 0:
            self.close_connection = True
            raise HttpError(400, "Content-Length 无效", "invalid_length")
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            raise HttpError(413, "请求内容过大", "payload_too_large")
        raw = self.rfile.read(length) if length else b""
        if len(raw) != length:
            raise HttpError(400, "请求内容不完整", "truncated_body")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(400, "请求 JSON 无效", "invalid_json") from exc
        if not isinstance(payload, dict):
            raise HttpError(400, "请求内容必须是 JSON 对象", "invalid_json")
        return payload

    def _handle_post(self) -> None:
        route = normalize_route(self.path)
        context = self.context

        if route == "/api/generate":
            payload = self._read_json_body()
            try:
                specs = specs_from_payload(payload)
                job = context.jobs.start_generate(specs)
            except JobBusy as exc:
                raise HttpError(
                    409,
                    f"{exc}（任务 {exc.job_id}）",
                    "already_running",
                ) from exc
            except ValueError as exc:
                raise HttpError(400, str(exc), "invalid_spec") from exc
            self._send_json(200, {"ok": True, "job": job})
            return

        if route == "/api/speedtest":
            payload = self._read_json_body()
            try:
                options = speed_options_from_payload(payload)
                job = context.jobs.start_speed(options)
            except JobBusy as exc:
                raise HttpError(
                    409,
                    f"{exc}（任务 {exc.job_id}）",
                    "already_running",
                ) from exc
            except (ValueError, SpeedTestError) as exc:
                raise HttpError(400, str(exc), "invalid_speedtest") from exc
            self._send_json(200, {"ok": True, "job": job})
            return

        if route.startswith("/api/jobs/") and route.endswith("/cancel"):
            job_id = route[len("/api/jobs/"):-len("/cancel")].strip("/")
            if not job_id or "/" in job_id:
                raise HttpError(404, "任务不存在", "not_found")
            try:
                self._read_json_body()
            except HttpError as exc:
                if exc.status != 415:
                    raise
            job = context.jobs.cancel(job_id)
            if job is None:
                raise HttpError(404, "任务不存在", "not_found")
            self._send_json(200, {"ok": True, "cancelled": True, "job": job})
            return

        if route == "/api/library/delete":
            payload = self._read_json_body()
            entry_id = payload.get("id")
            if not isinstance(entry_id, str) or not entry_id:
                raise HttpError(400, "缺少文件 id", "invalid_id")
            try:
                removed = context.library.delete(entry_id)
            except LibraryError as exc:
                raise HttpError(500, str(exc), "library_error") from exc
            if not removed:
                raise HttpError(404, "文件不存在", "not_found")
            self._send_json(
                200,
                {"ok": True, "stats": context.library.stats()},
            )
            return

        if route == "/api/library/clear":
            try:
                self._read_json_body()
            except HttpError as exc:
                if exc.status != 415:
                    raise
            try:
                removed = context.library.clear()
            except LibraryError as exc:
                raise HttpError(500, str(exc), "library_error") from exc
            self._send_json(
                200,
                {"ok": True, "removed": removed, "stats": context.library.stats()},
            )
            return

        raise HttpError(404, "接口不存在", "not_found")


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def _int_field(
    payload: dict[str, Any],
    name: str,
    default: int | None,
    minimum: int,
    maximum: int,
) -> int | None:
    value = payload.get(name, default)
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return number


def spec_from_payload(payload: dict[str, Any]) -> TargetSpec:
    if not isinstance(payload, dict):
        raise ValueError("生成参数必须是对象")
    mode = str(payload.get("mode") or "resolution").strip().lower()
    image_format = str(payload.get("format") or "jpeg").strip().lower()
    content = str(payload.get("content") or "photo").strip().lower()
    quality = _int_field(payload, "quality", 85, 1, 100)
    seed = _int_field(payload, "seed", 424242, 0, 2**31 - 1)
    width = _int_field(payload, "width", None, MIN_DIMENSION, MAX_DIMENSION)
    height = _int_field(payload, "height", None, MIN_DIMENSION, MAX_DIMENSION)
    target_bytes = _int_field(payload, "target_bytes", None, 1024, MAX_OUTPUT_BYTES)
    try:
        aspect = float(payload.get("aspect", 4 / 3))
    except (TypeError, ValueError) as exc:
        raise ValueError("aspect 必须是数字") from exc
    if not 0.2 <= aspect <= 5.0:
        raise ValueError("aspect 必须在 0.2 到 5.0 之间")
    spec = TargetSpec(
        mode=mode,
        format=image_format,
        quality=int(quality or 85),
        content=content,
        probe=_as_bool(payload.get("probe", False)),
        seed=int(seed if seed is not None else 424242),
        width=width,
        height=height,
        target_bytes=target_bytes,
        aspect=aspect,
    )
    spec.validate()
    if spec.content not in CONTENT_TYPES:
        raise ValueError(f"不支持的内容类型：{spec.content}")
    return spec


def specs_from_payload(payload: dict[str, Any]) -> list[TargetSpec]:
    items = payload.get("specs")
    if items is None:
        single = payload.get("spec")
        if isinstance(single, dict):
            items = [single]
        elif "mode" in payload or "width" in payload or "target_bytes" in payload:
            items = [payload]
        else:
            raise ValueError("缺少生成参数")
    if not isinstance(items, list) or not items:
        raise ValueError("specs 必须是非空数组")
    if len(items) > MAX_BATCH_ITEMS:
        raise ValueError(f"单次批量最多 {MAX_BATCH_ITEMS} 张")
    return [spec_from_payload(item) for item in items]


def speed_options_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    url = validate_url(payload.get("url", ""))
    concurrency = _int_field(payload, "concurrency", 4, 1, MAX_CONCURRENCY)
    rounds = _int_field(payload, "rounds", 3, 1, MAX_ROUNDS)
    if concurrency is None or rounds is None:
        raise ValueError("并发数和轮次不能为空")
    if concurrency * rounds > MAX_REQUESTS:
        raise ValueError(f"总请求数不能超过 {MAX_REQUESTS}")
    try:
        timeout = float(payload.get("timeout", 10))
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout 必须是数字") from exc
    if not 1.0 <= timeout <= 60.0:
        raise ValueError("单请求超时必须介于 1 到 60 秒之间")
    return {
        "url": url,
        "concurrency": concurrency,
        "rounds": rounds,
        "timeout": timeout,
    }


# ---------------------------------------------------------------------------
# Assembly and lifecycle
# ---------------------------------------------------------------------------


def create_server(
    context: AppContext,
    socket_path: str = "",
    tcp_address: tuple[str, int] | None = None,
) -> AppHTTPServer:
    if socket_path:
        if AF_UNIX is None:
            raise RuntimeError("当前 Python 不支持 Unix socket")
        return AppHTTPServer(
            socket_path,
            RequestHandler,
            context,
            address_family=AF_UNIX,
        )
    if tcp_address is None:
        raise ValueError("必须提供 socket 路径或 TCP 地址")
    return AppHTTPServer(
        tcp_address,
        RequestHandler,
        context,
        address_family=socket.AF_INET,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Image Lab backend for TOS 7")
    parser.add_argument(
        "--socket",
        default=os.environ.get("IMG_LAB_SOCKET", DEFAULT_SOCKET_PATH),
        help="Unix socket path (default: %(default)s)",
    )
    parser.add_argument(
        "--tcp-port",
        type=int,
        default=None,
        help="Bind TCP instead of a Unix socket (development only)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="TCP bind host")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("IMG_LAB_DATA_DIR", "/usr/local/img-lab/data"),
    )
    parser.add_argument(
        "--frontend-dir",
        default=os.environ.get("IMG_LAB_FRONTEND_DIR", "/usr/local/img-lab/webui"),
    )
    parser.add_argument(
        "--allowed-origins",
        default=os.environ.get("IMG_LAB_ALLOWED_ORIGINS", ""),
        help="Comma-separated CORS origins (normally unnecessary)",
    )
    parser.add_argument("--log-level", default=os.environ.get("IMG_LAB_LOG_LEVEL", "INFO"))
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        data_dir = os.path.abspath(args.data_dir)
        os.makedirs(data_dir, exist_ok=True)
    except OSError as exc:
        logger.error("无法创建数据目录 %s：%s", args.data_dir, exc)
        return 1

    encoders = EncoderRegistry()
    encoders.detect()
    solver = ImageSolver(encoders)
    library = Library(os.path.join(data_dir, "library"))
    speedtester = SpeedTester()
    jobs = JobManager(solver, library, speedtester)
    origins = [item.strip() for item in str(args.allowed_origins).split(",") if item.strip()]

    tcp_port = args.tcp_port
    if tcp_port is None:
        env_port = os.environ.get("IMG_LAB_TCP_PORT", "").strip()
        if env_port:
            try:
                tcp_port = int(env_port)
            except ValueError:
                tcp_port = None

    socket_path = ""
    if tcp_port is None:
        socket_path = str(args.socket or DEFAULT_SOCKET_PATH)
        try:
            prepare_socket(socket_path)
        except (OSError, RuntimeError) as exc:
            logger.error("无法准备 socket：%s", exc)
            return 1

    context = AppContext(
        frontend_dir=args.frontend_dir,
        library=library,
        encoders=encoders,
        solver=solver,
        speedtester=speedtester,
        jobs=jobs,
        allowed_origins=set(origins),
        socket_path=socket_path,
        preview_cache=PreviewCache(),
    )

    try:
        server = create_server(
            context,
            socket_path=socket_path,
            tcp_address=(args.host, tcp_port) if tcp_port is not None else None,
        )
    except OSError as exc:
        logger.error("无法启动服务：%s", exc)
        return 1

    identity: tuple[int, int] | None = None
    if socket_path:
        try:
            os.chmod(socket_path, 0o660)
        except OSError as exc:
            logger.warning("无法设置 socket 权限：%s", exc)
        identity = socket_identity(socket_path)

    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame: Any) -> None:
        if stop_event.is_set():
            return
        stop_event.set()
        logger.info("收到信号 %s，正在停止服务", signum)
        threading.Thread(
            target=server.shutdown,
            name="img-lab-shutdown",
            daemon=True,
        ).start()

    for name in ("SIGTERM", "SIGINT"):
        number = getattr(signal, name, None)
        if number is not None:
            try:
                signal.signal(number, _handle_signal)
            except (OSError, ValueError):
                pass

    address = socket_path if socket_path else f"{args.host}:{server.server_address[1]}"
    logger.info(
        "%s %s 启动，监听 %s，数据目录 %s",
        APP_ID,
        __version__,
        address,
        data_dir,
    )
    if not encoders.capabilities().get("jpeg", {}).get("available"):
        logger.warning("libjpeg 不可用，JPEG 生成将被拒绝：%s", encoders.capabilities()["jpeg"]["detail"])
    if not encoders.capabilities().get("webp", {}).get("available"):
        logger.warning("libwebp 不可用，WebP 生成将被拒绝")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        logger.info("收到键盘中断")
    finally:
        jobs.shutdown()
        server.server_close()
        if socket_path and identity is not None:
            try:
                if socket_identity(socket_path) == identity:
                    os.unlink(socket_path)
            except OSError:
                pass
        logger.info("服务已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
