# SPDX-License-Identifier: MIT
"""Installation guard regressions; no downloads or SDK installation."""
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import bootstrap_sdk as bootstrap
import thread_sdk as sdk


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def tar(self, name, kind=tarfile.REGTYPE, link=""):
        archive = self.root / "tool.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            entry = tarfile.TarInfo(name)
            entry.type = kind
            entry.linkname = link
            entry.size = 3 if kind == tarfile.REGTYPE else 0
            bundle.addfile(entry, io.BytesIO(b"gcc") if entry.size else None)
        return archive

    def test_archive_paths_never_escape_staging(self):
        for name in ("../escape", "/escape", "a/../../escape", "a\\escape", "C:escape"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                bootstrap.safe_path(self.root, name)

    def test_compiler_rejects_device_files_and_escaping_links(self):
        for kind, link in ((tarfile.CHRTYPE, ""), (tarfile.SYMTYPE, "../../escape"),
                           (tarfile.LNKTYPE, "/escape")):
            with self.subTest(kind=kind):
                archive = self.tar("bin/gcc", kind, link)
                target = self.root / ("out-" + kind.decode())
                with self.assertRaises(ValueError):
                    bootstrap.extract_compiler(archive, target)
                self.assertFalse((self.root / "escape").exists())

    def test_regular_executable_and_internal_link_are_preserved(self):
        archive = self.root / "tool.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            entry = tarfile.TarInfo("riscv32-esp-elf/bin/gcc")
            entry.size = 3; entry.mode = 0o755
            bundle.addfile(entry, io.BytesIO(b"gcc"))
            entry = tarfile.TarInfo("riscv32-esp-elf/bin/cc")
            entry.type = tarfile.SYMTYPE; entry.linkname = "gcc"
            bundle.addfile(entry)
        target = self.root / "compiler"
        bootstrap.extract_compiler(archive, target)
        self.assertEqual((target / "riscv32-esp-elf/bin/cc").read_bytes(), b"gcc")
        self.assertTrue((target / "riscv32-esp-elf/bin/gcc").stat().st_mode & 0o111)

    def test_zip_traversal_is_rejected_even_outside_selected_core_files(self):
        archive = self.root / "core.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("esp32-core-3.3.12/tools/../../escape", b"bad")
        with self.assertRaises(ValueError):
            bootstrap.extract_core(archive, self.root / "arduino")

    def test_core_copies_component_only_and_declares_compatible_idf(self):
        archive = self.root / "core.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("esp32-core-3.3.12/cores/esp32/test.h", "header")
            bundle.writestr("esp32-core-3.3.12/tools/unneeded.exe", "unused")
        target = self.root / "arduino"
        bootstrap.extract_core(archive, target)
        self.assertEqual((target / "cores/esp32/test.h").read_text(), "header")
        self.assertFalse((target / "tools").exists())
        self.assertIn(">=5.5.5,<5.6", (target / "idf_component.yml").read_text())

    def test_failed_download_never_replaces_or_leaves_partial_archive(self):
        target = self.root / "core.zip"
        with patch.object(bootstrap.urllib.request, "urlopen", return_value=io.BytesIO(b"corrupt")):
            with self.assertRaises(ValueError):
                bootstrap.download("https://example.invalid/core.zip", "0" * 64, target)
        self.assertFalse(target.exists())
        self.assertFalse(target.with_suffix(".zip.partial").exists())

    def test_cache_checksum_is_verified_before_any_network_request(self):
        target = self.root / "core.zip"; target.write_bytes(b"corrupt")
        with patch.object(bootstrap.urllib.request, "urlopen") as request:
            with self.assertRaises(ValueError):
                bootstrap.download("https://example.invalid/core.zip", "0" * 64, target)
            request.assert_not_called()
            self.assertEqual(target.read_bytes(), b"corrupt")

    def test_existing_foreign_idf_is_never_overwritten(self):
        with patch.object(sdk, "LOCAL", self.root), patch.object(sdk, "IDF", self.root / "idf"), \
             patch.object(bootstrap.platform, "system", return_value="Linux"), \
             patch.object(bootstrap.platform, "machine", return_value="x86_64"), \
             patch.object(bootstrap.subprocess, "check_output", return_value="foreign\n"), \
             patch.object(bootstrap, "run") as run:
            sdk.IDF.mkdir()
            with self.assertRaises(SystemExit):
                bootstrap.install()
            run.assert_not_called()

    def test_linux_environment_discovers_native_tools_and_python(self):
        local = self.root / "dedicated"
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        python_env = local / f"idf-tools/python_env/idf5.5_py{version}_env"
        compiler = local / "toolchains/esp-rv32/2601/riscv32-esp-elf/bin"
        for path in (python_env / "bin", local / "idf-tools/tools/cmake/3.30.2/bin",
                     local / "idf-tools/tools/ninja/1.12.1", compiler):
            path.mkdir(parents=True)
        previous = sdk.PYTHON_ENV
        self.addCleanup(setattr, sdk, "PYTHON_ENV", previous)
        with patch.object(sdk, "LOCAL", local), patch.object(sdk, "ROOT", self.root), \
             patch.object(sdk.subprocess, "check_output", side_effect=[sdk.IDF_COMMIT, "esp-14.2.0_20260121"]):
            env = sdk.environment()
        self.assertEqual(env["IDF_PYTHON_ENV_PATH"], str(python_env))
        self.assertIn(str(local / "idf-tools/tools/cmake/3.30.2/bin"), env["PATH"])
        self.assertNotIn("CMake.app", env["PATH"])
        self.assertEqual(env["IDF_TARGET"], "esp32c6")

    def test_ambiguous_python_environments_are_refused(self):
        local = self.root / "dedicated"
        for version in ("3.7", "3.8"):
            (local / f"idf-tools/python_env/idf5.5_py{version}_env").mkdir(parents=True)
        with patch.object(sdk, "LOCAL", local), self.assertRaises(SystemExit):
            sdk.environment()


if __name__ == "__main__":
    unittest.main()
