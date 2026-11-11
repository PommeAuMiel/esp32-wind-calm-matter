"""The frozen-input verifier rejects corruption and files outside the archive."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("release_archive", Path(__file__).resolve().parents[1] / "tools/verify_release_archive.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ReleaseArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "archive"
        self.root.mkdir()
        self.record = {"profile": "thread-ota", "ota": {"version": 40002}, "sources": {}, "sdk_inputs": {}, "binaries": {}}
        for category, prefix, relative in (("sources", "sources", "firmware/main.cpp"), ("sdk_inputs", "sdk-inputs", ".local/sdk.c"), ("binaries", "", "app.bin")):
            path = self.root / prefix / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.record[category][relative] = {"bytes": 7, "sha256": digest} if category == "binaries" else digest
        self.save()

    def save(self):
        (self.root / "manifest.json").write_text(json.dumps(self.record))

    def test_verified_frozen_inputs_do_not_need_current_sources(self):
        self.assertEqual(MODULE.verify_archive(self.root), {"version": 40002, "verified": True, "sources": 1, "sdk_inputs": 1, "binaries": 1})

    def test_changed_input_rejected(self):
        (self.root / "sources/firmware/main.cpp").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            MODULE.verify_archive(self.root)

    def test_changed_binary_size_rejected(self):
        (self.root / "app.bin").write_bytes(b"short")
        with self.assertRaises(ValueError):
            MODULE.verify_archive(self.root)

    def test_missing_input_rejected(self):
        (self.root / "sdk-inputs/.local/sdk.c").unlink()
        with self.assertRaises(ValueError):
            MODULE.verify_archive(self.root)

    def test_parent_and_absolute_paths_rejected(self):
        for path in ("../outside", "/outside", "firmware/../../outside"):
            with self.subTest(path=path):
                self.record["sources"] = {path: "0" * 64}
                self.save()
                with self.assertRaises(ValueError):
                    MODULE.verify_archive(self.root)

    def test_escaping_symlink_rejected(self):
        outside = self.root.parent / "outside"
        outside.write_bytes(b"fixture")
        source = self.root / "sources/firmware/main.cpp"
        source.unlink()
        source.symlink_to(outside)
        with self.assertRaises(ValueError):
            MODULE.verify_archive(self.root)

    def test_empty_inputs_and_invalid_digest_rejected(self):
        self.record["sources"] = {}
        self.save()
        with self.assertRaises(ValueError):
            MODULE.verify_archive(self.root)
        self.record["sources"] = {"firmware/main.cpp": "not-a-sha"}
        self.save()
        with self.assertRaises(ValueError):
            MODULE.verify_archive(self.root)


if __name__ == "__main__":
    unittest.main()
