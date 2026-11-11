"""Image encoders backed by the Python standard library and system libraries.

PNG is pure Python (zlib).  JPEG and WebP bind the system ``libjpeg`` and
``libwebp`` through ctypes; neither library is a hard dependency, and the
service reports which formats are available instead of failing at runtime.

``libjpeg`` exposes a C struct ABI, and the struct layout varies between
library builds.  Instead of guessing offsets, the encoder forks a short-lived
probe child, encodes a 16x16 image, and parses the resulting SOF marker to
confirm that the chosen ABI/offset combination is correct.  A wrong guess
kills only the probe child.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import signal
import struct
import threading
import zlib

from .content import MAX_PIXELS

FORMATS = ("jpeg", "png", "webp")
EXTENSIONS = {"jpeg": "jpg", "png": "png", "webp": "webp"}
MIME_TYPES = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}
DISPLAY_NAMES = {"jpeg": "JPEG", "png": "PNG", "webp": "WebP"}

JCS_RGB = 2

# (JPEG_LIB_VERSION, sizeof(struct jpeg_compress_struct), field offsets)
# Offsets are (image_width, image_height, input_components, in_color_space).
ABI_CANDIDATES = (
    (80, 584, (48, 52, 56, 60)),
    (62, 520, (40, 44, 48, 52)),
    (80, 584, (40, 44, 48, 52)),
    (62, 520, (48, 52, 56, 60)),
)

JPEG_LIBRARY_CANDIDATES = ("libjpeg.so.8", "libjpeg.so.62", "libjpeg.so")
WEBP_LIBRARY_CANDIDATES = ("libwebp.so.7", "libwebp.so.6", "libwebp.so")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class EncoderError(RuntimeError):
    """The requested encoding cannot be produced."""


def _validate_pixels(pixels: bytes | bytearray, width: int, height: int) -> None:
    expected = width * height * 3
    if len(pixels) != expected:
        raise EncoderError(
            f"像素缓冲区大小不匹配：期望 {expected} 字节，实际 {len(pixels)} 字节"
        )
    if width <= 0 or height <= 0:
        raise EncoderError("宽高必须为正数")
    if width * height > MAX_PIXELS:
        raise EncoderError("像素总量超过限制")


def encode_png(pixels: bytes | bytearray, width: int, height: int) -> bytes:
    """Encode 8-bit RGB pixels as a PNG with filter type 0."""

    _validate_pixels(pixels, width, height)
    stride = width * 3
    view = memoryview(pixels)
    compressor = zlib.compressobj(6)
    body = bytearray()
    for y in range(height):
        body += compressor.compress(b"\x00")
        body += compressor.compress(view[y * stride:(y + 1) * stride])
    body += compressor.flush()

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"".join(
        (
            PNG_SIGNATURE,
            _png_chunk(b"IHDR", header),
            _png_chunk(b"IDAT", bytes(body)),
            _png_chunk(b"IEND", b""),
        )
    )


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(tag + payload) & 0xFFFFFFFF
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", checksum)
    )


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Parse the first SOF marker of a JPEG stream (used by the ABI probe)."""

    if not data.startswith(b"\xff\xd8"):
        return None
    position = 2
    while position + 4 <= len(data):
        if data[position] != 0xFF:
            position += 1
            continue
        marker = data[position + 1]
        position += 2
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            continue
        if marker == 0xD9:
            return None
        length = int.from_bytes(data[position:position + 2], "big")
        if length < 2:
            return None
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            if position + 7 > len(data):
                return None
            height = int.from_bytes(data[position + 3:position + 5], "big")
            width = int.from_bytes(data[position + 5:position + 7], "big")
            return width, height
        position += length
    return None


def _bind_jpeg(lib: ctypes.CDLL) -> None:
    lib.jpeg_std_error.restype = ctypes.c_void_p
    lib.jpeg_std_error.argtypes = [ctypes.c_void_p]
    lib.jpeg_CreateCompress.restype = None
    lib.jpeg_CreateCompress.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t]
    lib.jpeg_destroy_compress.restype = None
    lib.jpeg_destroy_compress.argtypes = [ctypes.c_void_p]
    lib.jpeg_set_defaults.restype = None
    lib.jpeg_set_defaults.argtypes = [ctypes.c_void_p]
    lib.jpeg_set_quality.restype = None
    lib.jpeg_set_quality.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    lib.jpeg_mem_dest.restype = None
    lib.jpeg_mem_dest.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_ulong),
    ]
    lib.jpeg_start_compress.restype = None
    lib.jpeg_start_compress.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.jpeg_write_scanlines.restype = ctypes.c_uint
    lib.jpeg_write_scanlines.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint,
    ]
    lib.jpeg_finish_compress.restype = None
    lib.jpeg_finish_compress.argtypes = [ctypes.c_void_p]


def _jpeg_encode(
    lib: ctypes.CDLL,
    abi_version: int,
    struct_size: int,
    offsets: tuple[int, int, int, int],
    pixels: bytes | bytearray,
    width: int,
    height: int,
    quality: int,
) -> bytes:
    _validate_pixels(pixels, width, height)
    quality = max(1, min(100, int(quality)))
    off_width, off_height, off_components, off_color_space = offsets

    cinfo = ctypes.create_string_buffer(struct_size)
    error = ctypes.create_string_buffer(1024)
    error_pointer = lib.jpeg_std_error(ctypes.byref(error))
    struct.pack_into("<Q", cinfo, 0, error_pointer or 0)
    lib.jpeg_CreateCompress(ctypes.byref(cinfo), abi_version, struct_size)
    struct.pack_into("<I", cinfo, off_width, width)
    struct.pack_into("<I", cinfo, off_height, height)
    struct.pack_into("<i", cinfo, off_components, 3)
    struct.pack_into("<i", cinfo, off_color_space, JCS_RGB)

    lib.jpeg_set_defaults(ctypes.byref(cinfo))
    lib.jpeg_set_quality(ctypes.byref(cinfo), quality, 1)

    output = ctypes.c_void_p()
    output_size = ctypes.c_ulong()
    lib.jpeg_mem_dest(ctypes.byref(cinfo), ctypes.byref(output), ctypes.byref(output_size))
    lib.jpeg_start_compress(ctypes.byref(cinfo), 1)

    if isinstance(pixels, bytearray):
        source = (ctypes.c_char * len(pixels)).from_buffer(pixels)
    else:
        source = ctypes.create_string_buffer(pixels, len(pixels))
    try:
        stride = width * 3
        base = ctypes.addressof(source)
        rows = (ctypes.c_void_p * height)()
        for y in range(height):
            rows[y] = base + y * stride
        written = 0
        while written < height:
            chunk = lib.jpeg_write_scanlines(
                ctypes.byref(cinfo),
                ctypes.cast(
                    ctypes.byref(rows, written * ctypes.sizeof(ctypes.c_void_p)),
                    ctypes.POINTER(ctypes.c_void_p),
                ),
                height - written,
            )
            if chunk == 0:
                raise EncoderError("libjpeg 没有消费扫描线")
            written += chunk
        lib.jpeg_finish_compress(ctypes.byref(cinfo))
        if not output.value:
            raise EncoderError("libjpeg 没有返回编码数据")
        data = ctypes.string_at(output.value, output_size.value)
    finally:
        lib.jpeg_destroy_compress(ctypes.byref(cinfo))
        del source
        if output.value:
            ctypes.CDLL(None).free(output)
    return data


def _probe_jpeg(path: str, abi_version: int, struct_size: int, offsets: tuple[int, int, int, int]) -> bool:
    """Fork a child, try the ABI, and verify the encoded SOF dimensions."""

    if not hasattr(os, "fork"):
        return False
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child process
        try:
            null = os.open(os.devnull, os.O_WRONLY)
            os.dup2(null, 1)
            os.dup2(null, 2)
            signal.alarm(15)
            lib = ctypes.CDLL(path)
            _bind_jpeg(lib)
            data = _jpeg_encode(
                lib,
                abi_version,
                struct_size,
                offsets,
                bytearray(16 * 16 * 3),
                16,
                16,
                85,
            )
            os._exit(0 if jpeg_dimensions(data) == (16, 16) else 1)
        except BaseException:
            os._exit(1)
    try:
        _, status = os.waitpid(pid, 0)
    except OSError:
        return False
    return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


class JpegEncoder:
    def __init__(self, path: str, lib: ctypes.CDLL, abi_version: int, struct_size: int, offsets: tuple[int, int, int, int]) -> None:
        self.path = path
        self.lib = lib
        self.abi_version = abi_version
        self.struct_size = struct_size
        self.offsets = offsets

    def encode(self, pixels: bytes | bytearray, width: int, height: int, quality: int) -> bytes:
        return _jpeg_encode(
            self.lib,
            self.abi_version,
            self.struct_size,
            self.offsets,
            pixels,
            width,
            height,
            quality,
        )


class WebPEncoder:
    def __init__(self, path: str, lib: ctypes.CDLL) -> None:
        self.path = path
        self.lib = lib

    def encode(self, pixels: bytes | bytearray, width: int, height: int, quality: int) -> bytes:
        _validate_pixels(pixels, width, height)
        quality = max(1, min(100, int(quality)))
        if isinstance(pixels, bytearray):
            source = (ctypes.c_char * len(pixels)).from_buffer(pixels)
        else:
            source = ctypes.create_string_buffer(pixels, len(pixels))
        try:
            pointer = ctypes.cast(source, ctypes.POINTER(ctypes.c_uint8))
            output = ctypes.POINTER(ctypes.c_uint8)()
            size = self.lib.WebPEncodeRGB(
                pointer,
                width,
                height,
                width * 3,
                ctypes.c_float(quality),
                ctypes.byref(output),
            )
            if not size:
                raise EncoderError("libwebp 编码失败")
            try:
                return ctypes.string_at(output, size)
            finally:
                self.lib.WebPFree(output)
        finally:
            del source


def _library_candidates(name: str, configured: str) -> list[str]:
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    env_name = os.environ.get("IMG_LAB_JPEG_LIB" if name == "jpeg" else "IMG_LAB_WEBP_LIB", "").strip()
    if env_name:
        candidates.append(env_name)
    candidates.extend(JPEG_LIBRARY_CANDIDATES if name == "jpeg" else WEBP_LIBRARY_CANDIDATES)
    found = ctypes.util.find_library(name)
    if found:
        candidates.append(found)
    seen: set[str] = set()
    unique: list[str] = []
    for item in candidates:
        if item and item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def load_jpeg_encoder() -> tuple[JpegEncoder | None, str]:
    if not hasattr(os, "fork"):
        return None, "当前平台不支持 fork，无法安全探测 libjpeg ABI"
    errors: list[str] = []
    for path in _library_candidates("jpeg", ""):
        if not os.path.isabs(path):
            # CDLL resolves sonames through the loader; probe needs a prefix
            # that CDLL accepts, so keep the name as-is.
            pass
        for abi_version, struct_size, offsets in ABI_CANDIDATES:
            if not _probe_jpeg(path, abi_version, struct_size, offsets):
                continue
            try:
                lib = ctypes.CDLL(path)
                _bind_jpeg(lib)
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                continue
            return (
                JpegEncoder(path, lib, abi_version, struct_size, offsets),
                f"{os.path.basename(path)} · ABI {abi_version}",
            )
        errors.append(f"{path}: 没有匹配的 ABI")
    return None, "未找到可用的 libjpeg（" + "；".join(errors[:4] or ["没有候选库"]) + "）"


def load_webp_encoder() -> tuple[WebPEncoder | None, str]:
    errors: list[str] = []
    for path in _library_candidates("webp", ""):
        try:
            lib = ctypes.CDLL(path)
            lib.WebPEncodeRGB.restype = ctypes.c_size_t
            lib.WebPEncodeRGB.argtypes = [
                ctypes.POINTER(ctypes.c_uint8),
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_float,
                ctypes.POINTER(ctypes.POINTER(ctypes.c_uint8)),
            ]
            lib.WebPFree.restype = None
            lib.WebPFree.argtypes = [ctypes.c_void_p]
        except (OSError, AttributeError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        return WebPEncoder(path, lib), os.path.basename(path)
    return None, "未找到可用的 libwebp（" + "；".join(errors[:4] or ["没有候选库"]) + "）"


class EncoderRegistry:
    """Lazy encoder detection with thread-safe caching."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._detected = False
        self._jpeg: JpegEncoder | None = None
        self._jpeg_detail = "尚未检测"
        self._webp: WebPEncoder | None = None
        self._webp_detail = "尚未检测"

    def detect(self) -> None:
        with self._lock:
            if self._detected:
                return
            self._detected = True
            self._jpeg, self._jpeg_detail = load_jpeg_encoder()
            self._webp, self._webp_detail = load_webp_encoder()

    def capabilities(self) -> dict[str, dict[str, object]]:
        self.detect()
        return {
            "jpeg": {
                "available": self._jpeg is not None,
                "detail": self._jpeg_detail,
                "quality": [1, 100],
            },
            "png": {"available": True, "detail": "zlib", "quality": None},
            "webp": {
                "available": self._webp is not None,
                "detail": self._webp_detail,
                "quality": [1, 100],
            },
        }

    def supports(self, image_format: str) -> bool:
        self.detect()
        if image_format == "png":
            return True
        if image_format == "jpeg":
            return self._jpeg is not None
        if image_format == "webp":
            return self._webp is not None
        return False

    def encode(
        self,
        image_format: str,
        pixels: bytes | bytearray,
        width: int,
        height: int,
        quality: int,
    ) -> bytes:
        self.detect()
        if image_format == "png":
            return encode_png(pixels, width, height)
        if image_format == "jpeg":
            if self._jpeg is None:
                raise EncoderError(f"JPEG 编码器不可用：{self._jpeg_detail}")
            return self._jpeg.encode(pixels, width, height, quality)
        if image_format == "webp":
            if self._webp is None:
                raise EncoderError(f"WebP 编码器不可用：{self._webp_detail}")
            return self._webp.encode(pixels, width, height, quality)
        raise EncoderError(f"不支持的图片格式：{image_format}")

    def encode_preview(self, pixels: bytes | bytearray, width: int, height: int) -> tuple[bytes, str]:
        self.detect()
        if self._jpeg is not None:
            return self._jpeg.encode(pixels, width, height, 82), "jpeg"
        return encode_png(pixels, width, height), "png"
