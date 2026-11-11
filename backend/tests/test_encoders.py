"""Encoder availability, file-format markers, and pixel validation."""

from __future__ import annotations

import sys
import unittest
import zlib
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from img_lab import encoders
from img_lab.content import render


def png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    if not data.startswith(encoders.PNG_SIGNATURE):
        raise AssertionError("missing PNG signature")
    chunks: list[tuple[bytes, bytes]] = []
    position = len(encoders.PNG_SIGNATURE)
    while position < len(data):
        length = int.from_bytes(data[position:position + 4], "big")
        tag = data[position + 4:position + 8]
        payload = data[position + 8:position + 8 + length]
        checksum = int.from_bytes(data[position + 8 + length:position + 12 + length], "big")
        if zlib.crc32(tag + payload) & 0xFFFFFFFF != checksum:
            raise AssertionError(f"bad CRC for {tag!r}")
        chunks.append((tag, payload))
        position += 12 + length
    return chunks


class PngTests(unittest.TestCase):
    def test_png_roundtrip_of_solid_pixels(self) -> None:
        width, height = 8, 6
        colour = bytes((10, 20, 30))
        pixels = colour * (width * height)
        data = encoders.encode_png(pixels, width, height)
        chunks = png_chunks(data)
        self.assertEqual([tag for tag, _ in chunks], [b"IHDR", b"IDAT", b"IEND"])
        header = chunks[0][1]
        self.assertEqual(int.from_bytes(header[0:4], "big"), width)
        self.assertEqual(int.from_bytes(header[4:8], "big"), height)
        self.assertEqual(header[8], 8)
        self.assertEqual(header[9], 2)
        raw = zlib.decompress(chunks[1][1])
        stride = width * 3
        for row in range(height):
            start = row * (stride + 1)
            self.assertEqual(raw[start], 0)
            self.assertEqual(
                raw[start + 1:start + 1 + stride],
                pixels[row * stride:(row + 1) * stride],
            )

    def test_png_encoding_is_deterministic(self) -> None:
        pixels = render(128, 128, content="photo", seed=3)
        self.assertEqual(
            encoders.encode_png(pixels, 128, 128),
            encoders.encode_png(pixels, 128, 128),
        )

    def test_png_rejects_wrong_buffer_size(self) -> None:
        with self.assertRaises(encoders.EncoderError):
            encoders.encode_png(b"\x00" * 10, 8, 8)
        with self.assertRaises(encoders.EncoderError):
            encoders.encode_png(b"", 0, 8)

    def test_jpeg_dimension_parser_rejects_non_jpeg(self) -> None:
        self.assertIsNone(encoders.jpeg_dimensions(b"not a jpeg"))
        self.assertIsNone(encoders.jpeg_dimensions(b""))


class JpegTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = encoders.EncoderRegistry()
        cls.registry.detect()
        cls.capabilities = cls.registry.capabilities()

    def test_jpeg_encoding_has_expected_dimensions(self) -> None:
        if not self.capabilities["jpeg"]["available"]:
            self.skipTest("libjpeg is not available on this host")
        pixels = render(160, 128, content="photo", seed=1)
        data = self.registry.encode("jpeg", pixels, 160, 128, 85)
        self.assertTrue(data.startswith(b"\xff\xd8"))
        self.assertTrue(data.endswith(b"\xff\xd9"))
        self.assertEqual(encoders.jpeg_dimensions(data), (160, 128))

    def test_jpeg_quality_changes_size(self) -> None:
        if not self.capabilities["jpeg"]["available"]:
            self.skipTest("libjpeg is not available on this host")
        pixels = render(256, 192, content="photo", seed=2)
        low = self.registry.encode("jpeg", pixels, 256, 192, 30)
        high = self.registry.encode("jpeg", pixels, 256, 192, 95)
        self.assertLess(len(low), len(high))


class WebpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = encoders.EncoderRegistry()
        cls.registry.detect()
        cls.capabilities = cls.registry.capabilities()

    def test_webp_container_and_dimensions(self) -> None:
        if not self.capabilities["webp"]["available"]:
            self.skipTest("libwebp is not available on this host")
        pixels = render(160, 128, content="photo", seed=3)
        data = self.registry.encode("webp", pixels, 160, 128, 85)
        self.assertEqual(data[:4], b"RIFF")
        self.assertEqual(data[8:12], b"WEBP")
        self.assertEqual(int.from_bytes(data[4:8], "little") + 8, len(data))

    def test_webp_quality_changes_size(self) -> None:
        if not self.capabilities["webp"]["available"]:
            self.skipTest("libwebp is not available on this host")
        pixels = render(256, 192, content="photo", seed=4)
        low = self.registry.encode("webp", pixels, 256, 192, 20)
        high = self.registry.encode("webp", pixels, 256, 192, 95)
        self.assertLess(len(low), len(high))


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = encoders.EncoderRegistry()

    def test_capabilities_shape(self) -> None:
        capabilities = self.registry.capabilities()
        self.assertEqual(set(capabilities), {"jpeg", "png", "webp"})
        self.assertTrue(capabilities["png"]["available"])
        self.assertEqual(capabilities["png"]["detail"], "zlib")
        self.assertTrue(self.registry.supports("png"))
        self.assertFalse(self.registry.supports("gif"))

    def test_unknown_format_cannot_be_encoded(self) -> None:
        with self.assertRaises(encoders.EncoderError):
            self.registry.encode("gif", b"\x00" * 12, 2, 2, 85)

    def test_preview_falls_back_to_png(self) -> None:
        pixels = render(128, 128, content="photo", seed=5)
        data, image_format = self.registry.encode_preview(pixels, 128, 128)
        self.assertTrue(data)
        if self.registry.supports("jpeg"):
            self.assertEqual(image_format, "jpeg")
            self.assertEqual(encoders.jpeg_dimensions(data), (128, 128))
        else:
            self.assertEqual(image_format, "png")
            self.assertTrue(data.startswith(encoders.PNG_SIGNATURE))


if __name__ == "__main__":
    unittest.main()
