"""Two-level byte-size solver.

Level 1 estimates bytes-per-pixel, renders, measures, and corrects the
resolution until the file is within 5% of the target.

Level 2 fixes the resolution and tunes the high-frequency noise mix.  The mix
is a quantised continuous variable (one step = one band flip), so the encoded
size changes smoothly instead of jumping between distant resolutions.  A
damped secant extrapolation (0.85) converges to within 1.5%.

Quality never changes during a search; resolution and mix are the only
dependent variables.  If the target cannot be reached inside the format
limits, the result is returned with ``converged = False`` and an explicit
reason instead of being silently accepted.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

from .content import (
    MAX_PIXELS,
    MIN_DIMENSION,
    NOISE_LEVELS,
    PROBE_STRIPES,
    apply_mix,
    build_base,
    dimensions_for_pixels,
)
from .encoders import EncoderError, EncoderRegistry, FORMATS

FORMAT_MAX_PIXELS = {"jpeg": 60_000_000, "webp": 60_000_000, "png": 25_000_000}
MIN_PIXELS = MIN_DIMENSION * MIN_DIMENSION

LEVEL1_TOLERANCE = 0.05
FINAL_TOLERANCE = 0.015
MAX_LEVEL1_STEPS = 6
MAX_LEVEL2_STEPS = 9
DAMPING = 0.85
MAX_OUTPUT_BYTES = 256 * 1024 * 1024

# Bytes per pixel priors measured on the calibrated content model at 4:3 and
# about 4.4 MP.  Only the first level-1 guess uses these; every later step is
# driven by a real encoded measurement.
JPEG_QUALITY_CURVE = (
    (1, 0.017),
    (25, 0.039),
    (50, 0.081),
    (60, 0.101),
    (70, 0.130),
    (75, 0.150),
    (80, 0.185),
    (85, 0.239),
    (90, 0.331),
    (95, 0.524),
    (100, 1.038),
)
WEBP_QUALITY_CURVE = (
    (1, 0.011),
    (25, 0.027),
    (50, 0.066),
    (60, 0.085),
    (70, 0.119),
    (75, 0.134),
    (80, 0.183),
    (85, 0.257),
    (90, 0.346),
    (95, 0.487),
    (100, 0.627),
)
CONTENT_FACTORS = {
    "jpeg": {"photo": 1.0, "gradient": 0.088, "solid": 0.066, "noise": 3.172},
    "webp": {"photo": 1.0, "gradient": 0.014, "solid": 0.007, "noise": 2.928},
    "png": {"photo": 1.0, "gradient": 1.0, "solid": 1.0, "noise": 1.0},
}
PNG_BPP = {"photo": 1.92, "gradient": 0.022, "solid": 0.004, "noise": 3.00}

MODES = ("resolution", "bytes")


class Cancelled(Exception):
    """The job was cancelled by the user."""


def _interpolate(curve: tuple[tuple[int, float], ...], quality: int) -> float:
    quality = max(1, min(100, int(quality)))
    if quality <= curve[0][0]:
        return curve[0][1]
    if quality >= curve[-1][0]:
        return curve[-1][1]
    for (q0, v0), (q1, v1) in zip(curve, curve[1:]):
        if q0 <= quality <= q1:
            ratio = (quality - q0) / (q1 - q0)
            return v0 + (v1 - v0) * ratio
    return curve[-1][1]


def prior_bpp(image_format: str, content: str, quality: int) -> float:
    """Rough bytes-per-pixel estimate used for the first resolution guess."""

    if image_format == "png":
        return PNG_BPP[content]
    curve = JPEG_QUALITY_CURVE if image_format == "jpeg" else WEBP_QUALITY_CURVE
    factor = CONTENT_FACTORS.get(image_format, CONTENT_FACTORS["jpeg"]).get(content, 1.0)
    return max(0.002, _interpolate(curve, quality) * factor)


def priors_payload() -> dict[str, object]:
    return {
        "jpegQualityCurve": [list(item) for item in JPEG_QUALITY_CURVE],
        "webpQualityCurve": [list(item) for item in WEBP_QUALITY_CURVE],
        "contentFactors": CONTENT_FACTORS,
        "pngBpp": PNG_BPP,
        "formatMaxPixels": FORMAT_MAX_PIXELS,
        "level1Tolerance": LEVEL1_TOLERANCE,
        "finalTolerance": FINAL_TOLERANCE,
    }


@dataclass(frozen=True)
class TargetSpec:
    mode: str
    format: str = "jpeg"
    quality: int = 85
    content: str = "photo"
    probe: bool = False
    seed: int = 424242
    width: int | None = None
    height: int | None = None
    target_bytes: int | None = None
    aspect: float = 4 / 3

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"不支持的模式：{self.mode}")
        if self.format not in FORMATS:
            raise ValueError(f"不支持的图片格式：{self.format}")
        if not 1 <= int(self.quality) <= 100:
            raise ValueError("质量必须在 1 到 100 之间")
        if self.mode == "resolution":
            if not self.width or not self.height:
                raise ValueError("固定分辨率模式需要宽和高")
            if self.width < MIN_DIMENSION or self.height < MIN_DIMENSION:
                raise ValueError(f"宽高不能小于 {MIN_DIMENSION}")
            if self.width > 16384 or self.height > 16384:
                raise ValueError("宽高不能大于 16384")
        else:
            if not self.target_bytes or self.target_bytes < 1024:
                raise ValueError("目标体积至少为 1024 字节")
            if self.target_bytes > MAX_OUTPUT_BYTES:
                raise ValueError("目标体积超过 256 MiB 上限")
            if not math.isfinite(self.aspect) or self.aspect <= 0:
                raise ValueError("宽高比无效")


@dataclass
class Outcome:
    spec: TargetSpec
    width: int
    height: int
    byte_size: int
    mix: float
    converged: bool
    deviation: float | None
    iterations: int
    elapsed: float
    note: str = ""
    data: bytes = field(default=b"", repr=False)

    @property
    def pixels(self) -> int:
        return self.width * self.height

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.spec.mode,
            "format": self.spec.format,
            "content": self.spec.content,
            "probe": self.spec.probe,
            "quality": self.spec.quality,
            "seed": self.spec.seed,
            "width": self.width,
            "height": self.height,
            "pixels": self.pixels,
            "megapixels": round(self.pixels / 1_000_000, 3),
            "bytes": self.byte_size,
            "target_bytes": self.spec.target_bytes,
            "deviation": None if self.deviation is None else round(self.deviation, 6),
            "converged": self.converged,
            "mix": round(self.mix, 4),
            "iterations": self.iterations,
            "elapsed_ms": int(round(self.elapsed * 1000)),
            "note": self.note,
        }


@dataclass
class _Candidate:
    width: int
    height: int
    mix: float
    data: bytes
    size: int
    deviation: float | None
    iteration: int

    @property
    def pixels(self) -> int:
        return self.width * self.height


class ImageSolver:
    def __init__(self, encoders: EncoderRegistry) -> None:
        self.encoders = encoders
        self._base_key: tuple[int, int, str, int] | None = None
        self._base: bytearray | None = None

    # -- measurement -----------------------------------------------------

    def _measure(
        self,
        spec: TargetSpec,
        width: int,
        height: int,
        mix: float,
    ) -> tuple[bytes, int]:
        key = (width, height, spec.content, spec.seed)
        if key != self._base_key or self._base is None:
            self._base = build_base(width, height, spec.content, spec.seed)
            self._base_key = key
        image = apply_mix(
            self._base,
            width,
            height,
            spec.content,
            spec.seed,
            mix,
            probe=spec.probe,
            stripe=PROBE_STRIPES.get(spec.format, 1),
        )
        data = self.encoders.encode(
            spec.format,
            image,
            width,
            height,
            spec.quality,
        )
        if len(data) > MAX_OUTPUT_BYTES:
            raise EncoderError("生成文件超过 256 MiB 上限，请降低目标体积或质量")
        return data, len(data)

    # -- public API ------------------------------------------------------

    def solve(
        self,
        spec: TargetSpec,
        cancel: threading.Event | None = None,
        progress=None,
    ) -> Outcome:
        spec.validate()
        started = time.monotonic()
        if not self.encoders.supports(spec.format):
            status = self.encoders.capabilities().get(spec.format, {})
            raise EncoderError(
                f"{spec.format.upper()} 编码器不可用：{status.get('detail', '未知原因')}"
            )

        if spec.mode == "resolution":
            self._check_cancel(cancel)
            width = int(spec.width or 0)
            height = int(spec.height or 0)
            data, size = self._measure(spec, width, height, 0.0)
            elapsed = time.monotonic() - started
            self._emit(
                progress,
                {
                    "phase": "done",
                    "iteration": 1,
                    "width": width,
                    "height": height,
                    "mix": 0.0,
                    "bytes": size,
                    "deviation": None,
                    "target_bytes": None,
                },
            )
            return Outcome(
                spec=spec,
                width=width,
                height=height,
                byte_size=size,
                mix=0.0,
                converged=True,
                deviation=None,
                iterations=1,
                elapsed=elapsed,
                data=data,
            )

        return self._solve_bytes(spec, started, cancel, progress)

    # -- byte-size search ------------------------------------------------

    def _solve_bytes(
        self,
        spec: TargetSpec,
        started: float,
        cancel: threading.Event | None,
        progress,
    ) -> Outcome:
        target = int(spec.target_bytes or 0)
        max_pixels = min(FORMAT_MAX_PIXELS[spec.format], MAX_PIXELS)
        max_pixels = max(MIN_PIXELS, max_pixels)

        level1 = self._level1(spec, target, max_pixels, cancel, progress)
        best = level1["best"]
        iterations = level1["iterations"]

        if abs(best.deviation or 0.0) <= FINAL_TOLERANCE:
            return self._finish(spec, best, iterations, started, True, "")

        levels = NOISE_LEVELS.get(spec.content, {})
        mix_available = levels.get("high", 0) != levels.get("base", 0)
        if not mix_available:
            best, iterations, note = self._fine_resolution(
                spec,
                target,
                best,
                iterations,
                cancel,
                progress,
            )
            converged = abs(best.deviation or 0.0) <= FINAL_TOLERANCE
            return self._finish(spec, best, iterations, started, converged, note)

        best, iterations, note = self._level2(
            spec,
            target,
            best,
            iterations,
            cancel,
            progress,
        )
        converged = abs(best.deviation or 0.0) <= FINAL_TOLERANCE
        return self._finish(spec, best, iterations, started, converged, note)

    def _level1(
        self,
        spec: TargetSpec,
        target: int,
        max_pixels: int,
        cancel: threading.Event | None,
        progress,
    ) -> dict[str, object]:
        bpp = prior_bpp(spec.format, spec.content, spec.quality)
        pixels = min(max_pixels, max(MIN_PIXELS, target / bpp))
        width, height = dimensions_for_pixels(pixels, spec.aspect)
        if width * height > max_pixels:
            width, height = dimensions_for_pixels(max_pixels, spec.aspect)

        best: _Candidate | None = None
        iterations = 0
        for step in range(1, MAX_LEVEL1_STEPS + 1):
            self._check_cancel(cancel)
            data, size = self._measure(spec, width, height, 0.0)
            iterations = step
            deviation = (size - target) / target
            candidate = _Candidate(width, height, 0.0, data, size, deviation, step)
            if best is None or abs(deviation) < abs(best.deviation or 0.0):
                best = candidate
            self._emit(
                progress,
                {
                    "phase": "level1",
                    "iteration": step,
                    "width": width,
                    "height": height,
                    "mix": 0.0,
                    "bytes": size,
                    "deviation": round(deviation, 6),
                    "target_bytes": target,
                },
            )
            self._check_cancel(cancel)
            if abs(deviation) <= LEVEL1_TOLERANCE:
                break

            factor = target / size
            factor = max(0.55, min(1.75, factor))
            next_pixels = candidate.pixels * factor
            next_width, next_height = dimensions_for_pixels(next_pixels, spec.aspect)
            if next_width * next_height > max_pixels:
                next_width, next_height = dimensions_for_pixels(max_pixels, spec.aspect)
            if (next_width, next_height) == (width, height):
                break
            width, height = next_width, next_height

        assert best is not None
        return {"best": best, "iterations": iterations}

    def _level2(
        self,
        spec: TargetSpec,
        target: int,
        best: _Candidate,
        iterations: int,
        cancel: threading.Event | None,
        progress,
    ) -> tuple[_Candidate, int, str]:
        width, height = best.width, best.height
        count = max(1, NOISE_LEVELS[spec.content]["bands"])
        measurements: dict[float, _Candidate] = {0.0: best}
        note = ""

        def measure(mix: float) -> _Candidate:
            nonlocal iterations, best
            quantised = _quantise(mix, count)
            if quantised in measurements:
                return measurements[quantised]
            self._check_cancel(cancel)
            data, size = self._measure(spec, width, height, quantised)
            iterations += 1
            deviation = (size - target) / target
            candidate = _Candidate(width, height, quantised, data, size, deviation, iterations)
            measurements[quantised] = candidate
            if abs(deviation) < abs(best.deviation or 0.0):
                best = candidate
            self._emit(
                progress,
                {
                    "phase": "level2",
                    "iteration": iterations,
                    "width": width,
                    "height": height,
                    "mix": round(quantised, 4),
                    "bytes": size,
                    "deviation": round(deviation, 6),
                    "target_bytes": target,
                },
            )
            self._check_cancel(cancel)
            return candidate

        direction = 1.0 if best.size < target else -1.0
        gap = abs(math.log(target / best.size)) if best.size > 0 else 1.0
        probe_mix = direction * min(0.6, max(0.15, 0.12 + 0.35 * gap))
        probe = measure(probe_mix)

        if abs(probe.deviation or 0.0) <= FINAL_TOLERANCE:
            return probe, iterations, ""
        if abs(best.deviation or 0.0) <= FINAL_TOLERANCE:
            return best, iterations, ""

        # If the first probe did not bracket the target, try the extreme.
        bracketed = _bracketed(measurements, target)
        if not bracketed:
            extreme = measure(direction * 1.0)
            if abs(extreme.deviation or 0.0) <= FINAL_TOLERANCE:
                return extreme, iterations, ""
            bracketed = _bracketed(measurements, target)

        if not bracketed:
            note = _unreachable_note(spec, target, best, direction)
            return best, iterations, note

        for _ in range(MAX_LEVEL2_STEPS):
            if abs(best.deviation or 0.0) <= FINAL_TOLERANCE:
                break
            low = _closest_below(measurements, target)
            high = _closest_above(measurements, target)
            if low is None or high is None:
                break
            mix_low, point_low = low
            mix_high, point_high = high
            log_low = math.log(max(1, point_low.size))
            log_high = math.log(max(1, point_high.size))
            if abs(log_high - log_low) < 1e-9:
                next_mix = (mix_low + mix_high) / 2
            else:
                ratio = (math.log(target) - log_low) / (log_high - log_low)
                secant = mix_low + (mix_high - mix_low) * ratio
                anchor = (
                    point_low
                    if abs(point_low.deviation or 0.0) <= abs(point_high.deviation or 0.0)
                    else point_high
                )
                next_mix = anchor.mix + DAMPING * (secant - anchor.mix)

            next_mix = max(-1.0, min(1.0, next_mix))
            quantised = _quantise(next_mix, count)
            step = 1.0 / count
            candidates = (quantised, quantised + step, quantised - step)
            chosen = None
            for candidate_mix in candidates:
                candidate_mix = round(candidate_mix, 9)
                if (
                    -1.0 <= candidate_mix <= 1.0
                    and candidate_mix not in measurements
                ):
                    chosen = candidate_mix
                    break
            if chosen is None:
                note = "噪声混合权重已无更细的步进空间"
                break

            measure(chosen)

        if abs(best.deviation or 0.0) > FINAL_TOLERANCE and not note:
            note = (
                f"最佳偏差 {best.deviation * 100:+.2f}%，"
                f"未达到 ±{FINAL_TOLERANCE * 100:.1f}% 容差"
            )
        return best, iterations, note

    def _fine_resolution(
        self,
        spec: TargetSpec,
        target: int,
        best: _Candidate,
        iterations: int,
        cancel: threading.Event | None,
        progress,
    ) -> tuple[_Candidate, int, str]:
        """Noise content has no mix lever, so refine the resolution instead."""

        width, height = best.width, best.height
        max_pixels = min(FORMAT_MAX_PIXELS[spec.format], MAX_PIXELS)
        for _ in range(MAX_LEVEL2_STEPS):
            if abs(best.deviation or 0.0) <= FINAL_TOLERANCE:
                break
            self._check_cancel(cancel)
            factor = target / best.size
            factor = max(0.5, min(1.8, factor)) ** DAMPING
            pixels = best.pixels * factor
            pixels = max(MIN_PIXELS, min(max_pixels, pixels))
            next_width, next_height = dimensions_for_pixels(pixels, spec.aspect)
            if next_width * next_height > max_pixels:
                next_width, next_height = dimensions_for_pixels(max_pixels, spec.aspect)
            if (next_width, next_height) == (width, height):
                break
            width, height = next_width, next_height
            data, size = self._measure(spec, width, height, 0.0)
            iterations += 1
            deviation = (size - target) / target
            candidate = _Candidate(width, height, 0.0, data, size, deviation, iterations)
            if abs(deviation) < abs(best.deviation or 0.0):
                best = candidate
            self._emit(
                progress,
                {
                    "phase": "level2",
                    "iteration": iterations,
                    "width": width,
                    "height": height,
                    "mix": 0.0,
                    "bytes": size,
                    "deviation": round(deviation, 6),
                    "target_bytes": target,
                },
            )

        note = ""
        if abs(best.deviation or 0.0) > FINAL_TOLERANCE:
            note = (
                f"噪声内容没有可调混合权重，分辨率细化后最佳偏差 "
                f"{best.deviation * 100:+.2f}%，未达到 ±{FINAL_TOLERANCE * 100:.1f}% 容差"
            )
        return best, iterations, note

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _check_cancel(cancel: threading.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise Cancelled()

    @staticmethod
    def _emit(progress, payload: dict[str, object]) -> None:
        if progress is not None:
            try:
                progress(payload)
            except Exception:
                # Progress reporting must never break generation.
                pass

    def _finish(
        self,
        spec: TargetSpec,
        best: _Candidate,
        iterations: int,
        started: float,
        converged: bool,
        note: str,
    ) -> Outcome:
        return Outcome(
            spec=spec,
            width=best.width,
            height=best.height,
            byte_size=best.size,
            mix=best.mix,
            converged=converged,
            deviation=best.deviation,
            iterations=iterations,
            elapsed=time.monotonic() - started,
            note=note,
            data=best.data,
        )


def _quantise(mix: float, count: int) -> float:
    step = 1.0 / count
    return max(-1.0, min(1.0, round(mix / step) * step))


def _bracketed(measurements: dict[float, _Candidate], target: int) -> bool:
    return _closest_below(measurements, target) is not None and _closest_above(
        measurements, target
    ) is not None


def _closest_below(
    measurements: dict[float, _Candidate], target: int
) -> tuple[float, _Candidate] | None:
    options = [
        (mix, candidate)
        for mix, candidate in measurements.items()
        if candidate.size <= target
    ]
    if not options:
        return None
    return max(options, key=lambda item: item[1].size)


def _closest_above(
    measurements: dict[float, _Candidate], target: int
) -> tuple[float, _Candidate] | None:
    options = [
        (mix, candidate)
        for mix, candidate in measurements.items()
        if candidate.size >= target
    ]
    if not options:
        return None
    return min(options, key=lambda item: item[1].size)


def _unreachable_note(
    spec: TargetSpec,
    target: int,
    best: _Candidate,
    direction: float,
) -> str:
    max_pixels = min(FORMAT_MAX_PIXELS[spec.format], MAX_PIXELS)
    if direction > 0:
        if best.pixels >= max_pixels:
            limit = "已达到格式像素上限"
        else:
            limit = "噪声混合权重已达上限"
        return (
            f"目标 {target} 字节超出可达到的编码体积上限（{limit}，"
            f"最佳 {best.size} 字节，偏差 {best.deviation * 100:+.2f}%）"
        )
    return (
        f"目标 {target} 字节低于最小可编码体积（最佳 {best.size} 字节，"
        f"偏差 {best.deviation * 100:+.2f}%），请提高目标体积"
    )
