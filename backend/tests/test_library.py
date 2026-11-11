"""Library persistence, CSV manifests, ZIP bundles, and pruning."""

from __future__ import annotations

import csv
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import img_lab.library as library_module
from img_lab.library import CSV_COLUMNS, INDEX_VERSION, Library, LibraryError
from img_lab.sizing import Outcome, TargetSpec


def make_outcome(
    data: bytes = b"image-bytes",
    *,
    image_format: str = "png",
    width: int = 128,
    height: int = 128,
    target_bytes: int | None = None,
    converged: bool = True,
    deviation: float | None = None,
    note: str = "",
    seed: int = 42,
    mix: float = 0.0,
) -> Outcome:
    spec = TargetSpec(
        mode="bytes" if target_bytes is not None else "resolution",
        format=image_format,
        quality=85,
        content="photo",
        seed=seed,
        target_bytes=target_bytes,
        width=None if target_bytes is not None else width,
        height=None if target_bytes is not None else height,
        aspect=4 / 3,
    )
    return Outcome(
        spec=spec,
        width=width,
        height=height,
        byte_size=len(data),
        mix=mix,
        converged=converged,
        deviation=deviation,
        iterations=2,
        elapsed=0.125,
        note=note,
        data=data,
    )


class LibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = Library(self.root)
        self.addCleanup(self.temp.cleanup)

    def test_add_writes_file_and_reads_exact_bytes(self) -> None:
        data = b"P" * 1234
        entry = self.library.add(make_outcome(data))
        path = self.root / f"{entry['id']}.png"
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_size, len(data))
        self.assertEqual(entry["bytes"], len(data))
        self.assertRegex(str(entry["id"]), r"^[0-9a-f]+-[0-9a-f]+$")

    def test_index_persists_and_uses_disk_size(self) -> None:
        data = b"Q" * 500
        entry = self.library.add(make_outcome(data))
        path = self.root / f"{entry['id']}.png"
        with open(path, "ab") as handle:
            handle.write(b"extra")

        reloaded = Library(self.root)
        entries = reloaded.list_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["bytes"], 505)
        self.assertEqual(reloaded.stats(), {"count": 1, "bytes": 505})

    def test_manifest_csv_has_bom_crlf_and_exact_sizes(self) -> None:
        first = self.library.add(make_outcome(b"a" * 10, target_bytes=1000, deviation=-0.0123))
        second = self.library.add(make_outcome(b"b" * 20, width=256, height=192))

        body = self.library.manifest_csv()
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", body)
        text = body.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[0], list(CSV_COLUMNS))
        self.assertEqual(len(rows), 3)
        actual = CSV_COLUMNS.index("actual_bytes")
        filename = CSV_COLUMNS.index("filename")
        self.assertEqual(int(rows[1][actual]), 20)
        self.assertEqual(int(rows[2][actual]), 10)
        self.assertEqual(
            {rows[1][filename], rows[2][filename]},
            {first["filename"], second["filename"]},
        )
        deviation = CSV_COLUMNS.index("deviation_percent")
        self.assertEqual(rows[1][deviation], "")
        self.assertEqual(rows[2][deviation], "-1.230")

    def test_manifest_can_select_ids(self) -> None:
        first = self.library.add(make_outcome(b"a" * 4))
        self.library.add(make_outcome(b"b" * 4))
        body = self.library.manifest_csv([str(first["id"]), "missing"])
        rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], first["filename"])

    def test_bundle_contains_manifest_and_stored_files(self) -> None:
        data = b"Z" * 64
        entry = self.library.add(make_outcome(data))
        bundle = self.library.create_bundle()
        self.addCleanup(self.library.discard_bundle, bundle)
        with zipfile.ZipFile(bundle) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"manifest.csv", entry["filename"]},
            )
            self.assertEqual(archive.read(entry["filename"]), data)
            manifest = archive.read("manifest.csv")
        self.assertEqual(manifest, self.library.manifest_csv())

    def test_bundle_without_entries_raises(self) -> None:
        with self.assertRaises(LibraryError):
            self.library.create_bundle()

    def test_delete_and_clear_remove_files(self) -> None:
        first = self.library.add(make_outcome(b"a" * 3))
        second = self.library.add(make_outcome(b"b" * 3))
        self.assertTrue((self.root / f"{first['id']}.png").is_file())

        self.assertTrue(self.library.delete(str(first["id"])))
        self.assertFalse((self.root / f"{first['id']}.png").exists())
        self.assertEqual(self.library.stats()["count"], 1)

        self.assertEqual(self.library.clear(), 1)
        self.assertEqual(self.library.stats(), {"count": 0, "bytes": 0})
        self.assertFalse((self.root / f"{second['id']}.png").exists())

    def test_invalid_ids_are_rejected(self) -> None:
        self.assertIsNone(self.library.get("../../etc/passwd"))
        self.assertFalse(self.library.delete("../../etc/passwd"))
        self.assertIsNone(self.library.get("not-an-id"))

    def test_missing_files_are_dropped_on_reload(self) -> None:
        entry = self.library.add(make_outcome(b"c" * 8))
        (self.root / f"{entry['id']}.png").unlink()
        reloaded = Library(self.root)
        self.assertEqual(reloaded.stats(), {"count": 0, "bytes": 0})

    def test_malformed_index_starts_empty(self) -> None:
        (self.root / "index.json").write_text("{not json", encoding="utf-8")
        library = Library(self.root)
        self.assertEqual(library.stats()["count"], 0)
        self.assertEqual(library.manifest_csv().decode("utf-8-sig").splitlines()[0:1], [",".join(CSV_COLUMNS)])

    def test_duplicate_filenames_get_a_suffix(self) -> None:
        first = self.library.add(make_outcome(b"a"), filename="same.png")
        second = self.library.add(make_outcome(b"b"), filename="same.png")
        self.assertEqual(first["filename"], "same.png")
        self.assertEqual(second["filename"], "same-2.png")

    def test_index_version_is_written(self) -> None:
        self.library.add(make_outcome(b"x"))
        payload = (self.root / "index.json").read_text(encoding="utf-8")
        self.assertIn(f'"version":{INDEX_VERSION}', payload)

    def test_entry_limit_prunes_oldest_files(self) -> None:
        original_entries = library_module.MAX_ENTRIES
        library_module.MAX_ENTRIES = 3
        self.addCleanup(setattr, library_module, "MAX_ENTRIES", original_entries)
        library = Library(self.root / "pruned")
        entries = [library.add(make_outcome(bytes([index]) * 8)) for index in range(5)]
        self.assertEqual(library.stats()["count"], 3)
        self.assertFalse((self.root / "pruned" / f"{entries[0]['id']}.png").exists())
        remaining = {entry["id"] for entry in library.list_entries()}
        self.assertEqual(remaining, {entry["id"] for entry in entries[2:]})

    def test_total_byte_limit_prunes_oldest_files(self) -> None:
        original_bytes = library_module.MAX_TOTAL_BYTES
        library_module.MAX_TOTAL_BYTES = 2500
        self.addCleanup(setattr, library_module, "MAX_TOTAL_BYTES", original_bytes)
        library = Library(self.root / "bytes")
        library.add(make_outcome(b"a" * 1000))
        library.add(make_outcome(b"b" * 1000))
        library.add(make_outcome(b"c" * 1000))
        self.assertEqual(library.stats()["count"], 2)


if __name__ == "__main__":
    unittest.main()
