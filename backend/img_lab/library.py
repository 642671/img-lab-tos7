"""Generated-file library with an atomic JSON index.

Every entry stores the exact byte count read back from disk, so the CSV
manifest and the files in the ZIP always agree with what the browser
downloads.  The library keeps the newest 200 entries within 1.5 GiB.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .encoders import EXTENSIONS
from .sizing import Outcome

INDEX_VERSION = 1
MAX_ENTRIES = 200
MAX_TOTAL_BYTES = int(1.5 * 1024 ** 3)
ID_PATTERN = re.compile(r"^[0-9a-f]{6,32}-[0-9a-f]{4,16}$")

CSV_COLUMNS = (
    "filename",
    "width",
    "height",
    "megapixels",
    "format",
    "quality",
    "target_bytes",
    "actual_bytes",
    "deviation_percent",
    "converged",
    "content",
    "probe",
    "seed",
    "mix",
    "iterations",
    "elapsed_ms",
    "created_at",
)


class LibraryError(RuntimeError):
    """Library operation failed."""


class Library:
    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.data_dir / "index.json"
        self._lock = threading.RLock()
        self._entries: list[dict[str, object]] = []
        self._load()

    # -- persistence -----------------------------------------------------

    def _load(self) -> None:
        entries: list[dict[str, object]] = []
        try:
            raw = self._index_path.read_text(encoding="utf-8")
            payload = json.loads(raw)
            if isinstance(payload, dict) and payload.get("version") == INDEX_VERSION:
                candidate = payload.get("entries")
                if isinstance(candidate, list):
                    entries = [item for item in candidate if self._valid_entry(item)]
        except FileNotFoundError:
            entries = []
        except (OSError, ValueError, TypeError):
            entries = []
        self._entries = entries
        self._verify_locked()

    @staticmethod
    def _valid_entry(item: object) -> bool:
        if not isinstance(item, dict):
            return False
        return (
            isinstance(item.get("id"), str)
            and isinstance(item.get("filename"), str)
            and isinstance(item.get("format"), str)
            and item.get("ext") in EXTENSIONS.values()
        )

    def _verify_locked(self) -> None:
        """Drop missing files and force byte counts to match disk exactly."""

        verified: list[dict[str, object]] = []
        for entry in self._entries:
            path = self._entry_path(entry)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            entry["bytes"] = size
            verified.append(entry)
        self._entries = verified

    def _write_index_locked(self) -> None:
        payload = {"version": INDEX_VERSION, "entries": self._entries}
        temp_path = self._index_path.with_suffix(".json.tmp")
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, self._index_path)

    def _entry_path(self, entry: dict[str, object]) -> Path:
        return self.data_dir / f"{entry['id']}.{entry['ext']}"

    # -- public API ------------------------------------------------------

    def add(self, outcome: Outcome, filename: str | None = None) -> dict[str, object]:
        ext = EXTENSIONS[outcome.spec.format]
        with self._lock:
            entry_id = self._new_id_locked()
            final_name = self._unique_filename_locked(filename or self._default_filename(outcome, ext))
            path = self.data_dir / f"{entry_id}.{ext}"
            temp_path = path.with_suffix(path.suffix + ".tmp")
            try:
                with open(temp_path, "wb") as handle:
                    handle.write(outcome.data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, path)
            except OSError as exc:
                try:
                    temp_path.unlink()
                except OSError:
                    pass
                raise LibraryError(f"写入生成文件失败：{exc}") from exc

            entry = {
                "id": entry_id,
                "filename": final_name,
                "ext": ext,
                "width": outcome.width,
                "height": outcome.height,
                "pixels": outcome.pixels,
                "megapixels": round(outcome.pixels / 1_000_000, 3),
                "format": outcome.spec.format,
                "quality": outcome.spec.quality,
                "content": outcome.spec.content,
                "probe": bool(outcome.spec.probe),
                "seed": outcome.spec.seed,
                "target_bytes": outcome.spec.target_bytes,
                "bytes": path.stat().st_size,
                "deviation": outcome.deviation,
                "converged": bool(outcome.converged),
                "mix": round(outcome.mix, 4),
                "iterations": outcome.iterations,
                "elapsed_ms": int(round(outcome.elapsed * 1000)),
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "note": outcome.note,
            }
            self._entries.insert(0, entry)
            self._prune_locked()
            self._write_index_locked()
            return dict(entry)

    def list_entries(self) -> list[dict[str, object]]:
        with self._lock:
            self._verify_locked()
            return [dict(entry) for entry in self._entries]

    def stats(self) -> dict[str, int]:
        with self._lock:
            self._verify_locked()
            return {
                "count": len(self._entries),
                "bytes": sum(int(entry["bytes"]) for entry in self._entries),
            }

    def get(self, entry_id: str) -> tuple[dict[str, object], Path] | None:
        if not ID_PATTERN.fullmatch(entry_id or ""):
            return None
        with self._lock:
            for entry in self._entries:
                if entry["id"] == entry_id:
                    path = self._entry_path(entry)
                    try:
                        size = path.stat().st_size
                    except OSError:
                        return None
                    entry["bytes"] = size
                    return dict(entry), path
        return None

    def delete(self, entry_id: str) -> bool:
        if not ID_PATTERN.fullmatch(entry_id or ""):
            return False
        with self._lock:
            for index, entry in enumerate(self._entries):
                if entry["id"] != entry_id:
                    continue
                path = self._entry_path(entry)
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise LibraryError(f"删除文件失败：{exc}") from exc
                del self._entries[index]
                self._write_index_locked()
                return True
        return False

    def clear(self) -> int:
        with self._lock:
            removed = 0
            for entry in self._entries:
                try:
                    self._entry_path(entry).unlink()
                    removed += 1
                except FileNotFoundError:
                    pass
                except OSError:
                    continue
            self._entries = []
            self._write_index_locked()
            return removed

    def manifest_csv(self, entry_ids: list[str] | None = None) -> bytes:
        with self._lock:
            self._verify_locked()
            entries = self._select_locked(entry_ids)
            buffer = io.StringIO()
            buffer.write("﻿")
            writer = csv.writer(buffer, lineterminator="\r\n")
            writer.writerow(CSV_COLUMNS)
            for entry in entries:
                writer.writerow(self._csv_row(entry))
            return buffer.getvalue().encode("utf-8")

    def create_bundle(self, entry_ids: list[str] | None = None) -> Path:
        with self._lock:
            self._verify_locked()
            entries = self._select_locked(entry_ids)
            if not entries:
                raise LibraryError("没有可打包的文件")
            handle, temp_name = tempfile.mkstemp(
                prefix="img-lab-bundle-", suffix=".zip", dir=self.data_dir
            )
            os.close(handle)
            manifest = self.manifest_csv([str(entry["id"]) for entry in entries])
            try:
                with zipfile.ZipFile(temp_name, "w", allowZip64=True) as archive:
                    archive.writestr(
                        "manifest.csv",
                        manifest,
                        compress_type=zipfile.ZIP_DEFLATED,
                    )
                    for entry in entries:
                        path = self._entry_path(entry)
                        archive.write(
                            path,
                            arcname=str(entry["filename"]),
                            compress_type=zipfile.ZIP_STORED,
                        )
            except OSError as exc:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise LibraryError(f"打包失败：{exc}") from exc
            return Path(temp_name)

    def discard_bundle(self, path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    # -- helpers ---------------------------------------------------------

    def _select_locked(self, entry_ids: list[str] | None) -> list[dict[str, object]]:
        if entry_ids is None:
            return list(self._entries)
        wanted = [item for item in entry_ids if isinstance(item, str)]
        if not wanted:
            return list(self._entries)
        lookup = {entry["id"]: entry for entry in self._entries}
        return [lookup[item] for item in wanted if item in lookup]

    @staticmethod
    def _csv_row(entry: dict[str, object]) -> list[object]:
        deviation = entry.get("deviation")
        return [
            entry.get("filename", ""),
            entry.get("width", ""),
            entry.get("height", ""),
            entry.get("megapixels", ""),
            str(entry.get("format", "")).upper(),
            entry.get("quality", ""),
            "" if entry.get("target_bytes") is None else entry.get("target_bytes"),
            entry.get("bytes", ""),
            "" if deviation is None else f"{float(deviation) * 100:.3f}",
            "true" if entry.get("converged") else "false",
            entry.get("content", ""),
            "true" if entry.get("probe") else "false",
            entry.get("seed", ""),
            entry.get("mix", ""),
            entry.get("iterations", ""),
            entry.get("elapsed_ms", ""),
            entry.get("created_at", ""),
        ]

    def _default_filename(self, outcome: Outcome, ext: str) -> str:
        probe = "-probe" if outcome.spec.probe else ""
        base = (
            f"img-{outcome.width}x{outcome.height}-{outcome.spec.format}"
            f"-q{outcome.spec.quality}-{outcome.spec.content}{probe}"
            f"-{outcome.byte_size}"
        )
        return f"{base}.{ext}"

    def _unique_filename_locked(self, filename: str) -> str:
        used = {entry["filename"] for entry in self._entries}
        if filename not in used:
            return filename
        stem, dot, suffix = filename.rpartition(".")
        counter = 2
        while True:
            candidate = f"{stem}-{counter}{dot}{suffix}"
            if candidate not in used:
                return candidate
            counter += 1

    def _new_id_locked(self) -> str:
        existing = {entry["id"] for entry in self._entries}
        while True:
            candidate = f"{int(time.time() * 1000):x}-{secrets.token_hex(3)}"
            if candidate not in existing:
                return candidate

    def _prune_locked(self) -> None:
        while len(self._entries) > MAX_ENTRIES:
            entry = self._entries.pop()
            try:
                self._entry_path(entry).unlink()
            except OSError:
                pass
        total = sum(int(entry["bytes"]) for entry in self._entries)
        while total > MAX_TOTAL_BYTES and len(self._entries) > 1:
            entry = self._entries.pop()
            try:
                self._entry_path(entry).unlink()
            except OSError:
                pass
            total -= int(entry["bytes"])
