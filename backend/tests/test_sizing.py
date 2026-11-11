"""Two-level byte-size solver and calibration checks."""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from img_lab.encoders import EncoderRegistry
from img_lab.sizing import (
    FINAL_TOLERANCE,
    LEVEL1_TOLERANCE,
    Cancelled,
    ImageSolver,
    TargetSpec,
    prior_bpp,
    priors_payload,
)

MIB = 1024 * 1024

# Calibration references from the product specification (JPEG q85, 4:3).
REFERENCES = {
    1: (2412, 1810, 1_043_372),
    5: (5726, 4294, 5_283_396),
    8: (7362, 5522, 8_445_481),
}


class SolverTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = EncoderRegistry()
        cls.registry.detect()
        cls.capabilities = cls.registry.capabilities()
        cls.solver = ImageSolver(cls.registry)

    def require(self, image_format: str) -> None:
        if not self.registry.supports(image_format):
            self.skipTest(f"{image_format} encoder is not available on this host")


class PriorTests(SolverTestBase):
    def test_priors_are_positive_and_quality_sensitive(self) -> None:
        self.assertGreater(prior_bpp("jpeg", "photo", 85), prior_bpp("jpeg", "photo", 50))
        self.assertGreater(prior_bpp("jpeg", "photo", 95), prior_bpp("jpeg", "photo", 85))
        self.assertGreater(prior_bpp("webp", "photo", 90), prior_bpp("webp", "photo", 70))
        self.assertEqual(prior_bpp("png", "photo", 50), prior_bpp("png", "photo", 95))
        self.assertGreater(prior_bpp("jpeg", "photo", 85), prior_bpp("jpeg", "gradient", 85))
        self.assertGreater(prior_bpp("jpeg", "gradient", 85), prior_bpp("jpeg", "solid", 85))

    def test_priors_payload_matches_backend_constants(self) -> None:
        payload = priors_payload()
        self.assertIn("jpegQualityCurve", payload)
        self.assertIn("webpQualityCurve", payload)
        self.assertIn("contentFactors", payload)
        self.assertIn("pngBpp", payload)
        self.assertEqual(payload["level1Tolerance"], LEVEL1_TOLERANCE)
        self.assertEqual(payload["finalTolerance"], FINAL_TOLERANCE)
        self.assertEqual(
            payload["formatMaxPixels"]["png"],
            25_000_000,
        )
        self.assertIn([85, 0.239], payload["jpegQualityCurve"])


class CalibrationTests(SolverTestBase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.outcomes = {}
        if cls.registry.supports("jpeg"):
            for mib in (1, 5, 8):
                spec = TargetSpec(
                    mode="bytes",
                    format="jpeg",
                    quality=85,
                    content="photo",
                    seed=424242,
                    target_bytes=mib * MIB,
                    aspect=4 / 3,
                )
                cls.outcomes[mib] = cls.solver.solve(spec)

    def test_reference_sizes_converge_within_one_and_a_half_percent(self) -> None:
        self.require("jpeg")
        for mib, (ref_width, ref_height, ref_bytes) in REFERENCES.items():
            with self.subTest(mib=mib):
                outcome = self.outcomes[mib]
                self.assertTrue(outcome.converged, outcome.note)
                self.assertLessEqual(abs(outcome.deviation or 0.0), FINAL_TOLERANCE)
                self.assertEqual(outcome.byte_size, len(outcome.data))
                self.assertLess(
                    abs(outcome.width - ref_width) / ref_width,
                    0.08,
                    (outcome.width, ref_width),
                )
                self.assertLess(
                    abs(outcome.height - ref_height) / ref_height,
                    0.08,
                    (outcome.height, ref_height),
                )
                bpp = outcome.byte_size / outcome.pixels
                self.assertGreater(bpp, 0.19)
                self.assertLess(bpp, 0.27)

    def test_reference_byte_counts_are_close_to_the_specification(self) -> None:
        self.require("jpeg")
        for mib, (_, _, ref_bytes) in REFERENCES.items():
            with self.subTest(mib=mib):
                outcome = self.outcomes[mib]
                self.assertLess(
                    abs(outcome.byte_size - ref_bytes) / ref_bytes,
                    0.06,
                )

    def test_result_payload_reports_measured_values(self) -> None:
        self.require("jpeg")
        payload = self.outcomes[1].as_dict()
        self.assertEqual(payload["width"], self.outcomes[1].width)
        self.assertEqual(payload["bytes"], len(self.outcomes[1].data))
        self.assertEqual(payload["target_bytes"], MIB)
        self.assertTrue(payload["converged"])
        self.assertIn("deviation", payload)
        self.assertIn("iterations", payload)


class ByteModeTests(SolverTestBase):
    def test_png_byte_mode_converges_and_returns_exact_bytes(self) -> None:
        target = 64 * 1024
        spec = TargetSpec(
            mode="bytes",
            format="png",
            quality=85,
            content="photo",
            seed=2026,
            target_bytes=target,
            aspect=4 / 3,
        )
        outcome = self.solver.solve(spec)
        self.assertTrue(outcome.converged, outcome.note)
        self.assertLessEqual(abs(outcome.deviation or 0.0), FINAL_TOLERANCE)
        self.assertEqual(outcome.byte_size, len(outcome.data))
        self.assertGreater(outcome.iterations, 1)
        self.assertEqual(outcome.width % 8, 0)
        self.assertEqual(outcome.height % 8, 0)

    def test_byte_mode_is_deterministic(self) -> None:
        spec = TargetSpec(
            mode="bytes",
            format="png",
            quality=85,
            content="photo",
            seed=77,
            target_bytes=32 * 1024,
            aspect=1.0,
        )
        first = self.solver.solve(spec)
        second = self.solver.solve(spec)
        self.assertEqual((first.width, first.height), (second.width, second.height))
        self.assertEqual(first.byte_size, second.byte_size)
        self.assertEqual(first.mix, second.mix)
        self.assertEqual(first.data, second.data)

    def test_unreachable_small_target_is_reported_not_silently_accepted(self) -> None:
        spec = TargetSpec(
            mode="bytes",
            format="png",
            quality=85,
            content="photo",
            seed=5,
            target_bytes=1024,
            aspect=4 / 3,
        )
        outcome = self.solver.solve(spec)
        self.assertFalse(outcome.converged)
        self.assertIsNotNone(outcome.deviation)
        self.assertGreater(outcome.byte_size, 1024)
        self.assertTrue(outcome.note)
        self.assertIn("低于最小可编码体积", outcome.note)

    def test_progress_callback_sees_real_measurements(self) -> None:
        events = []
        spec = TargetSpec(
            mode="bytes",
            format="png",
            quality=85,
            content="photo",
            seed=8,
            target_bytes=32 * 1024,
            aspect=4 / 3,
        )
        self.solver.solve(spec, progress=events.append)
        self.assertTrue(events)
        self.assertIn(events[0]["phase"], {"level1", "level2"})
        self.assertEqual(events[0]["target_bytes"], 32 * 1024)
        for event in events:
            self.assertIn("width", event)
            self.assertIn("height", event)
            self.assertIn("bytes", event)
            self.assertGreater(event["bytes"], 0)

    def test_cancelled_before_generation(self) -> None:
        cancel = threading.Event()
        cancel.set()
        spec = TargetSpec(
            mode="bytes",
            format="png",
            target_bytes=32 * 1024,
        )
        with self.assertRaises(Cancelled):
            self.solver.solve(spec, cancel=cancel)

    def test_cancelled_resolution_mode(self) -> None:
        cancel = threading.Event()
        cancel.set()
        spec = TargetSpec(
            mode="resolution",
            format="png",
            width=128,
            height=128,
        )
        with self.assertRaises(Cancelled):
            self.solver.solve(spec, cancel=cancel)

    def test_resolution_mode_is_exact_and_deterministic(self) -> None:
        spec = TargetSpec(
            mode="resolution",
            format="png",
            quality=85,
            content="photo",
            seed=99,
            width=256,
            height=192,
        )
        first = self.solver.solve(spec)
        second = self.solver.solve(spec)
        self.assertTrue(first.converged)
        self.assertIsNone(first.deviation)
        self.assertEqual(first.width, 256)
        self.assertEqual(first.height, 192)
        self.assertEqual(first.byte_size, len(first.data))
        self.assertEqual(first.data, second.data)


class SpecValidationTests(unittest.TestCase):
    def test_valid_specs_pass(self) -> None:
        TargetSpec(mode="resolution", width=128, height=128).validate()
        TargetSpec(mode="bytes", target_bytes=1024, aspect=1.0).validate()

    def test_invalid_specs_raise_value_error(self) -> None:
        cases = [
            TargetSpec(mode="unknown", width=128, height=128),
            TargetSpec(mode="resolution", format="gif", width=128, height=128),
            TargetSpec(mode="resolution", width=128, height=128, quality=0),
            TargetSpec(mode="resolution", width=100, height=128),
            TargetSpec(mode="resolution", width=20000, height=128),
            TargetSpec(mode="bytes", target_bytes=1023),
            TargetSpec(mode="bytes", target_bytes=300 * 1024 * 1024),
            TargetSpec(mode="bytes", target_bytes=4096, aspect=0),
        ]
        for spec in cases:
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError):
                    spec.validate()


if __name__ == "__main__":
    unittest.main()
