"""Deterministic synthetic image content and downsampling probes.

The content model is a four-layer photo texture: a low-frequency gradient
base, a fixed grid of random colour cells, mid-frequency cell structure, and
high-frequency noise.  The noise layer is the continuous variable used by the
byte-size solver: its amplitude is chosen per horizontal band, so the mixed
amplitude changes in small, deterministic steps instead of jumping between
images.

Pure standard library only.  Building a 48 MP RGB buffer takes about a second
and peaks at roughly two full-size buffers, which the systemd unit keeps
within its memory limit.
"""

from __future__ import annotations

import math
import random

MIN_DIMENSION = 128
MAX_DIMENSION = 16384
MAX_PIXELS = 60_000_000
ALIGN = 8

PROBE_BLOCK = 64
PROBE_STRIPES = {"jpeg": 4, "webp": 4, "png": 1}

CONTENT_TYPES = ("photo", "gradient", "solid", "noise")

# Fixed photo texture grid.  The cell count does not depend on resolution, so
# a large image has proportionally larger cells and more smooth area per
# pixel.  That reproduces the measured photo bpp curve (about 0.24 at 4.4 MP,
# 0.215 at 24.6 MP and 0.21 at 40.7 MP for JPEG q85).
PHOTO_CELLS_X = 96
PHOTO_CELLS_Y = 72
PHOTO_JITTER = 24

# Per-content noise amplitude states.  ``mix`` in [-1, 1] selects how many
# bands flip from ``base`` to ``high`` (positive mix) or to ``low`` (negative
# mix).  The band count controls the granularity of the byte-size search.
# The photo floor (24) is calibrated so JPEG q85 lands near the reference
# photo entropy; the high/low states give level 2 a usable continuous lever.
NOISE_LEVELS = {
    "photo": {"base": 24, "high": 32, "low": 16, "bands": 64},
    "gradient": {"base": 0, "high": 4, "low": 0, "bands": 64},
    "solid": {"base": 0, "high": 6, "low": 0, "bands": 64},
    "noise": {"base": 0, "high": 0, "low": 0, "bands": 64},
}

# Sizes used while probing: four 64x64 corners in JPEG/WebP, 1 px stripes in
# lossless PNG so any resampling immediately produces grey pixels.


def clamp_dimension(value: int) -> int:
    """Round a requested side to the 8 px grid used by DCT blocks and probes."""

    number = int(value)
    if number < MIN_DIMENSION:
        return MIN_DIMENSION
    if number > MAX_DIMENSION:
        return MAX_DIMENSION
    aligned = int(round(number / ALIGN)) * ALIGN
    aligned = max(MIN_DIMENSION, min(MAX_DIMENSION, aligned))
    return aligned


def dimensions_for_pixels(pixels: float, aspect: float) -> tuple[int, int]:
    """Return 8 px aligned dimensions of the given aspect ratio and area."""

    ratio = float(aspect)
    if not math.isfinite(ratio) or ratio <= 0:
        ratio = 4 / 3
    area = max(float(MIN_DIMENSION * MIN_DIMENSION), float(pixels))
    height = math.sqrt(area / ratio)
    width = height * ratio
    width_i = clamp_dimension(round(width))
    height_i = clamp_dimension(round(height))
    return width_i, height_i


def max_area_for_aspect(aspect: float, max_pixels: int) -> int:
    """Largest pixel count that fits both the per-side and total limits."""

    ratio = float(aspect) if aspect else 4 / 3
    height = min(MAX_DIMENSION, math.sqrt(max_pixels / ratio))
    width = min(MAX_DIMENSION, height * ratio)
    return int(width // ALIGN) * ALIGN * int(height // ALIGN) * ALIGN


def _photo_base(width: int, height: int, seed: int) -> bytearray:
    """Build the four-layer photo texture used by the default content type.

    The image is a horizontal gradient with a fixed grid of jittered colour
    cells on top.  The cells are low/mid frequency at small sizes and become
    larger relative to the image at high resolutions, which is what makes the
    encoded bytes-per-pixel decline slowly as the image grows.  The
    high-frequency noise layer is applied later by ``_apply_chunk_noise`` so
    that ``mix`` can tune it without rebuilding the base.
    """

    rng = random.Random(seed)
    start = [rng.randrange(60, 190) for _ in range(3)]
    end = [rng.randrange(60, 190) for _ in range(3)]

    cells: list[bytearray] = []
    for _ in range(PHOTO_CELLS_Y):
        row = bytearray()
        for gx in range(PHOTO_CELLS_X):
            t = gx / (PHOTO_CELLS_X - 1)
            for channel in range(3):
                value = int(start[channel] * (1 - t) + end[channel] * t)
                value += rng.randrange(-PHOTO_JITTER, PHOTO_JITTER + 1)
                row.append(0 if value < 0 else (255 if value > 255 else value))
        cells.append(row)

    stride = width * 3
    image = bytearray(width * height * 3)
    for gy in range(PHOTO_CELLS_Y):
        y0 = gy * height // PHOTO_CELLS_Y
        y1 = (gy + 1) * height // PHOTO_CELLS_Y
        coarse = cells[gy]
        row = bytearray(stride)
        for gx in range(PHOTO_CELLS_X):
            x0 = gx * width // PHOTO_CELLS_X
            x1 = (gx + 1) * width // PHOTO_CELLS_X
            row[x0 * 3:x1 * 3] = coarse[gx * 3:gx * 3 + 3] * (x1 - x0)
        image[y0 * stride:y1 * stride] = row * (y1 - y0)
    return image


def _gradient_base(width: int, height: int, seed: int) -> bytearray:
    rng = random.Random(seed)
    start_colour = [rng.randrange(24, 210) for _ in range(3)]
    end_colour = [rng.randrange(24, 210) for _ in range(3)]

    row = bytearray(width * 3)
    denominator = max(1, width - 1)
    for x in range(width):
        t = x / denominator
        base = 3 * x
        for channel in range(3):
            value = int(start_colour[channel] * (1 - t) + end_colour[channel] * t)
            row[base + channel] = 0 if value < 0 else (255 if value > 255 else value)

    # 256 vertical steps keep the banding to at most one grey level, which
    # still looks like a smooth gradient at any realistic size.
    bands = 256
    span = rng.randrange(40, 90) * (1 if rng.random() < 0.5 else -1)
    tables = []
    for band in range(bands):
        delta = int(round(-span / 2 + span * band / (bands - 1)))
        tables.append(bytes(max(0, min(255, value + delta)) for value in range(256)))

    image = bytearray(width * height * 3)
    stride = width * 3
    for y in range(height):
        band = min(bands - 1, y * bands // height)
        image[y * stride:(y + 1) * stride] = row.translate(tables[band])
    return image


def _solid_base(width: int, height: int, seed: int) -> bytearray:
    rng = random.Random(seed)
    colour = bytes(rng.randrange(24, 232) for _ in range(3))
    row = colour * width
    return bytearray(row * height)


def _noise_base(width: int, height: int, seed: int) -> bytearray:
    rng = random.Random(seed)
    stride = width * 3
    image = bytearray(width * height * 3)
    for y in range(height):
        image[y * stride:(y + 1) * stride] = rng.randbytes(stride)
    return image


def build_base(width: int, height: int, content: str, seed: int) -> bytearray:
    """Build the noise-free base layer for the requested content type."""

    if content not in CONTENT_TYPES:
        raise ValueError(f"不支持的内容类型：{content}")
    if width < MIN_DIMENSION or height < MIN_DIMENSION:
        raise ValueError(f"宽高不能小于 {MIN_DIMENSION}")
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise ValueError(f"宽高不能大于 {MAX_DIMENSION}")
    if width * height > MAX_PIXELS:
        raise ValueError("像素总量超过限制")
    seed = int(seed)
    if content == "photo":
        return _photo_base(width, height, seed)
    if content == "gradient":
        return _gradient_base(width, height, seed)
    if content == "solid":
        return _solid_base(width, height, seed)
    return _noise_base(width, height, seed)


def _apply_chunk_noise(
    image: bytearray,
    width: int,
    height: int,
    content: str,
    seed: int,
    mix: float,
) -> None:
    levels = NOISE_LEVELS[content]
    bands = levels["bands"]
    chunk_rows = max(1, height // bands)
    count = (height + chunk_rows - 1) // chunk_rows

    order = list(range(count))
    random.Random(seed ^ 0x51ED270B).shuffle(order)

    amplitudes = [levels["base"]] * count
    if mix > 0:
        flipped = int(round(min(1.0, float(mix)) * count))
        for index in order[:flipped]:
            amplitudes[index] = levels["high"]
    elif mix < 0:
        flipped = int(round(min(1.0, -float(mix)) * count))
        for index in order[:flipped]:
            amplitudes[index] = levels["low"]

    rng = random.Random(seed ^ 0x7A11C0DE)
    stride = width * 3
    mask_cache: dict[tuple[int, int], int] = {}
    for index, amplitude in enumerate(amplitudes):
        start_y = index * chunk_rows
        rows = min(chunk_rows, height - start_y)
        length = rows * stride
        # Always consume the same random bytes so that changing ``mix`` only
        # changes the amplitude mask, never the noise sequence.
        noise = rng.randbytes(length)
        if amplitude <= 0:
            continue
        mask = mask_cache.get((length, amplitude))
        if mask is None:
            mask = int.from_bytes(bytes([amplitude]) * length, "little")
            mask_cache[(length, amplitude)] = mask
        start = start_y * stride
        value = int.from_bytes(image[start:start + length], "little")
        value ^= int.from_bytes(noise, "little") & mask
        image[start:start + length] = value.to_bytes(length, "little")


def _probe_pattern(block: int, stripe: int) -> bytearray:
    pattern = bytearray(block * 3)
    for x in range(block):
        value = 0 if (x // stripe) % 2 == 0 else 255
        position = x * 3
        pattern[position] = value
        pattern[position + 1] = value
        pattern[position + 2] = value
    return pattern


def draw_probe(image: bytearray, width: int, height: int, stripe: int) -> None:
    """Paint the four corner stripe blocks in place.

    Block origins are snapped to the 8 px grid so that the 4 px stripes stay
    aligned with JPEG/WebP DCT blocks even when the image itself has an
    arbitrary width or height.
    """

    if stripe <= 0:
        return
    block = min(PROBE_BLOCK, width, height)
    pattern = _probe_pattern(block, stripe)
    stride = width * 3
    right = ((width - block) // ALIGN) * ALIGN
    bottom = ((height - block) // ALIGN) * ALIGN
    corners = (
        (0, 0),
        (right, 0),
        (0, bottom),
        (right, bottom),
    )
    for x0, y0 in corners:
        for y in range(y0, y0 + block):
            start = y * stride + x0 * 3
            image[start:start + len(pattern)] = pattern


def apply_mix(
    base: bytearray,
    width: int,
    height: int,
    content: str,
    seed: int,
    mix: float,
    probe: bool = False,
    stripe: int = 0,
) -> bytearray:
    """Copy the base layer, apply the high-frequency mix, and paint probes."""

    image = bytearray(base)
    if content in {"photo", "gradient", "solid"}:
        _apply_chunk_noise(image, width, height, content, seed, float(mix))
    if probe:
        draw_probe(image, width, height, int(stripe) or PROBE_STRIPES["jpeg"])
    return image


def render(
    width: int,
    height: int,
    *,
    content: str = "photo",
    seed: int = 424242,
    mix: float = 0.0,
    probe: bool = False,
    stripe: int = 0,
) -> bytearray:
    """Render a complete image, used by tests and the preview endpoint."""

    base = build_base(width, height, content, seed)
    return apply_mix(
        base,
        width,
        height,
        content,
        seed,
        mix,
        probe=probe,
        stripe=stripe,
    )
