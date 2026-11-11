"""Content model, determinism, and downsampling-probe checks."""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from img_lab import content


class DimensionTests(unittest.TestCase):
    def test_clamp_dimension_aligns_to_eight_pixels(self) -> None:
        self.assertEqual(content.clamp_dimension(100), content.MIN_DIMENSION)
        self.assertEqual(content.clamp_dimension(129), 128)
        self.assertEqual(content.clamp_dimension(133), 136)
        self.assertEqual(content.clamp_dimension(512), 512)
        self.assertEqual(content.clamp_dimension(20000), content.MAX_DIMENSION)
        for value in (128, 129, 517, 4096, 16384):
            self.assertEqual(content.clamp_dimension(value) % content.ALIGN, 0)

    def test_dimensions_for_pixels_keeps_area_and_aspect(self) -> None:
        width, height = content.dimensions_for_pixels(2_000_000, 4 / 3)
        self.assertEqual(width % content.ALIGN, 0)
        self.assertEqual(height % content.ALIGN, 0)
        self.assertLess(abs(width * height - 2_000_000) / 2_000_000, 0.02)
        self.assertLess(abs(width / height - 4 / 3), 0.02)

    def test_dimensions_for_pixels_rejects_bad_aspect(self) -> None:
        width, height = content.dimensions_for_pixels(500_000, 0)
        self.assertEqual(width % content.ALIGN, 0)
        self.assertEqual(height % content.ALIGN, 0)
        self.assertGreater(width, 0)

    def test_max_area_for_aspect_respects_limits(self) -> None:
        area = content.max_area_for_aspect(4 / 3, content.MAX_PIXELS)
        self.assertLessEqual(area, content.MAX_PIXELS)
        self.assertGreater(area, content.MAX_PIXELS * 0.98)


class RenderTests(unittest.TestCase):
    def test_render_is_deterministic(self) -> None:
        first = content.render(160, 128, content="photo", seed=1234, mix=0.35, probe=True)
        second = content.render(160, 128, content="photo", seed=1234, mix=0.35, probe=True)
        self.assertEqual(bytes(first), bytes(second))
        self.assertEqual(len(first), 160 * 128 * 3)

    def test_seed_and_content_change_pixels(self) -> None:
        base = content.render(160, 128, content="photo", seed=1)
        other_seed = content.render(160, 128, content="photo", seed=2)
        other_content = content.render(160, 128, content="noise", seed=1)
        self.assertNotEqual(bytes(base), bytes(other_seed))
        self.assertNotEqual(bytes(base), bytes(other_content))

    def test_solid_content_starts_uniform(self) -> None:
        image = content.render(160, 128, content="solid", seed=9, mix=0.0)
        first = bytes(image[0:3])
        for offset in range(0, len(image), 3):
            self.assertEqual(bytes(image[offset:offset + 3]), first)

    def test_gradient_varies_horizontally(self) -> None:
        image = content.render(192, 128, content="gradient", seed=9)
        left = image[0:3]
        right = image[(192 - 1) * 3:192 * 3]
        self.assertNotEqual(bytes(left), bytes(right))

    def test_photo_mix_changes_encoded_entropy(self) -> None:
        low = content.render(512, 384, content="photo", seed=7, mix=-1.0)
        high = content.render(512, 384, content="photo", seed=7, mix=1.0)
        self.assertNotEqual(bytes(low), bytes(high))

    def test_invalid_content_and_size_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            content.render(160, 128, content="unknown")
        with self.assertRaises(ValueError):
            content.render(64, 128, content="photo")
        with self.assertRaises(ValueError):
            content.build_base(16384, 16384, "photo", 1)

    def test_twenty_four_megapixels_render_completely(self) -> None:
        width, height = 6000, 4000
        image = content.render(width, height, content="photo", seed=3, mix=0.0)
        self.assertEqual(len(image), width * height * 3)

    @unittest.skipUnless(
        os.environ.get("IMG_LAB_SLOW_TESTS") == "1",
        "set IMG_LAB_SLOW_TESTS=1 to run the 48 MP timing test",
    )
    def test_forty_eight_megapixels_is_under_ten_seconds(self) -> None:
        width, height = 8000, 6000
        started = time.perf_counter()
        image = content.render(width, height, content="photo", seed=424242, mix=0.0)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(image), width * height * 3)
        self.assertLess(elapsed, 10.0)


class ProbeTests(unittest.TestCase):
    def _corner_blocks(self, image: bytearray, width: int, height: int):
        block = content.PROBE_BLOCK
        right = ((width - block) // content.ALIGN) * content.ALIGN
        bottom = ((height - block) // content.ALIGN) * content.ALIGN
        stride = width * 3
        for x0, y0 in ((0, 0), (right, 0), (0, bottom), (right, bottom)):
            yield x0, y0, [
                bytes(image[(y0 + y) * stride + x0 * 3:(y0 + y) * stride + (x0 + block) * 3])
                for y in range(block)
            ]

    def test_png_probe_uses_one_pixel_stripes_of_pure_black_and_white(self) -> None:
        width, height = 650, 485
        image = content.render(
            width,
            height,
            content="photo",
            seed=11,
            probe=True,
            stripe=content.PROBE_STRIPES["png"],
        )
        for x0, y0, rows in self._corner_blocks(image, width, height):
            self.assertEqual(x0 % content.ALIGN, 0)
            self.assertEqual(y0 % content.ALIGN, 0)
            observed: set[int] = set()
            for row in rows:
                self.assertEqual(len(row), content.PROBE_BLOCK * 3)
                for offset in range(0, len(row), 3):
                    red, green, blue = row[offset:offset + 3]
                    self.assertEqual(red, green)
                    self.assertEqual(green, blue)
                    self.assertIn(red, (0, 255))
                    observed.add(red)
            self.assertEqual(observed, {0, 255})

    def test_jpeg_probe_stripes_are_four_pixels_and_block_aligned(self) -> None:
        width, height = 650, 485
        image = content.render(
            width,
            height,
            content="photo",
            seed=11,
            probe=True,
            stripe=content.PROBE_STRIPES["jpeg"],
        )
        for x0, y0, rows in self._corner_blocks(image, width, height):
            self.assertEqual(x0 % content.ALIGN, 0, "probe origin must be 8 px aligned")
            self.assertEqual(y0 % content.ALIGN, 0)
            row = rows[0]
            values = [row[index * 3] for index in range(content.PROBE_BLOCK)]
            self.assertEqual(
                values[:8],
                [0, 0, 0, 0, 255, 255, 255, 255],
                "the pattern must start black on a DCT block boundary",
            )
            for index, value in enumerate(values):
                expected = 0 if (index // content.PROBE_STRIPES["jpeg"]) % 2 == 0 else 255
                self.assertEqual(value, expected)
            for index in range(content.PROBE_BLOCK):
                offset = index * 3
                self.assertEqual(row[offset], row[offset + 1])
                self.assertEqual(row[offset], row[offset + 2])

    def test_probe_does_not_touch_pixels_outside_the_blocks(self) -> None:
        width, height = 650, 485
        plain = content.render(width, height, content="photo", seed=11, probe=False)
        probed = content.render(
            width, height, content="photo", seed=11, probe=True, stripe=1
        )
        stride = width * 3
        block = content.PROBE_BLOCK
        right = ((width - block) // content.ALIGN) * content.ALIGN
        bottom = ((height - block) // content.ALIGN) * content.ALIGN
        probe_columns = set(range(0, block)) | set(range(right, right + block))
        for y in range(height):
            if y < block or y >= bottom:
                continue
            for x in range(width):
                if x in probe_columns:
                    continue
                self.assertEqual(plain[y * stride + x * 3], probed[y * stride + x * 3])


if __name__ == "__main__":
    unittest.main()
