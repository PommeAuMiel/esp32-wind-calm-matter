"""Host upload/build checks; fake artifacts and ports, never touch hardware."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location("project", Path(__file__).resolve().parents[1] / "tools/project.py")
project = importlib.util.module_from_spec(spec)
spec.loader.exec_module(project)


class UploadGuards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sketch = self.root / "firmware" / "WindCalmController"
        self.build = self.root / "build" / "WindCalmController"
        self.sketch.mkdir(parents=True)
        self.build.mkdir(parents=True)
        self.source = self.sketch / "WindCalmController.ino"
        self.source.write_bytes(b"source")
        self.data = {"core": project.VERSION, "fqbn": project.BASE_FQBN, "profile": "debug",
                     "sources": {"firmware/WindCalmController/WindCalmController.ino": hashlib.sha256(b"source").hexdigest()},
                     "binaries": {}}
        for name in ("WindCalmController.ino.bin", "WindCalmController.ino.bootloader.bin",
                     "WindCalmController.ino.partitions.bin", "boot_app0.bin"):
            (self.build / name).write_bytes(b"binary")
            self.data["binaries"][name] = {"bytes": 6, "sha256": hashlib.sha256(b"binary").hexdigest()}
        self.save_manifest()
        for name, value in (("ROOT", self.root), ("SKETCH", self.sketch), ("BUILD", self.build),
                            ("PROFILE", "debug"), ("FQBN", project.BASE_FQBN)):
            p = patch.object(project, name, value); p.start(); self.addCleanup(p.stop)
        self.valid = ["upload", "--port", "/dev/fake", "--confirm-xiao", "--confirm-usb-only", "--expected-id", "test-card"]

    def save_manifest(self):
        (self.build / "manifest.json").write_text(json.dumps(self.data))

    def invoke(self, arguments, reason=None):
        with patch("sys.argv", ["project.py", *arguments]), patch.object(project, "configure"), \
                patch.object(project, "check_sdk"), patch.object(project, "run") as run, \
                patch.object(project, "execute") as execute, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as failure:
                project.main()
            if reason: self.assertIn(reason, str(failure.exception))
            run.assert_not_called(); execute.assert_not_called()

    def test_protocol_probe_cannot_be_uploaded(self):
        self.invoke([*self.valid, "--sketch", "WindCalmProtocolTest"])

    def test_requires_explicit_port(self):
        self.invoke(["upload", "--confirm-xiao", "--confirm-usb-only"])

    def test_requires_both_hardware_confirmations(self):
        for flags in ([], ["--confirm-xiao"], ["--confirm-usb-only"]):
            self.invoke(["upload", "--port", "/dev/fake", *flags])

    def test_every_loader_requires_device_identity(self):
        for loader in ("arduino", "rom"):
            self.invoke(self.valid[:-2] + ["--loader", loader])

    def test_refuses_changed_source_or_binary(self):
        for changed in (self.source, self.build / "WindCalmController.ino.bin"):
            with self.subTest(changed=changed.name):
                original = changed.read_bytes(); changed.write_bytes(b"changed")
                self.invoke(self.valid, "changed")
                changed.write_bytes(original)

    def test_refuses_added_source(self):
        (self.sketch / "Added.h").write_text("added")
        self.invoke(self.valid, "inventory changed")

    def test_refuses_profile_mismatch(self):
        self.data["fqbn"] = project.BASE_FQBN.replace("DebugLevel=error", "DebugLevel=none")
        self.save_manifest(); self.invoke(self.valid, "profile / SDK mismatch")

    def test_refuses_missing_required_binary(self):
        del self.data["binaries"]["boot_app0.bin"]
        self.save_manifest(); self.invoke(self.valid, "Required binary missing")

    def test_each_loader_refuses_wrong_device(self):
        for loader in ("arduino", "rom"):
            ports = {"detected_ports": [{"port": {"address": "/dev/fake", "properties": {
                "serialNumber": "different-card", "vid": "0x303A", "pid": "0x1001"}}}]}
            with patch.object(project, "cli_path", return_value="never-run"), \
                    patch.object(project.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(ports))):
                self.invoke([*self.valid, "--loader", loader], "identity mismatch")

    def test_identity_requires_unique_port_and_correct_vid_pid(self):
        valid = {"port": {"address": "/dev/fake", "properties": {
            "serialNumber": "test-card", "vid": "0x303A", "pid": "0x1001"}}}
        for entries in ([], [valid, valid], [{"port": {"address": "/dev/fake", "properties": {
                "serialNumber": "test-card", "vid": "0xffff", "pid": "0x1001"}}}]):
            with patch.object(project, "cli_path", return_value="never-run"), \
                    patch.object(project.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps({"detected_ports": entries}))):
                with self.assertRaises(SystemExit): project.verify_usb_identity("/dev/fake", "test-card")
        with patch.object(project, "cli_path", return_value="never-run"), \
                patch.object(project.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps({"detected_ports": [valid]}))):
            project.verify_usb_identity("/dev/fake", "test-card")

    def test_valid_arduino_upload_passes_only_after_checks(self):
        with patch("sys.argv", ["project.py", *self.valid]), patch.object(project, "configure"), \
                patch.object(project, "check_sdk"), patch.object(project, "verify_usb_identity") as identity, \
                patch.object(project, "run") as run:
            project.main()
            identity.assert_called_once_with("/dev/fake", "test-card")
            self.assertEqual(run.call_args.args[0][0], "upload")
            self.assertNotIn("EraseFlash=all", " ".join(run.call_args.args[0]))


class BuildLogs(unittest.TestCase):
    def test_log_survives_compiler_cleaning_build_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "compile" / "compile.log"
            command = [sys.executable, "-c",
                       "import shutil,sys; shutil.rmtree(sys.argv[1]); print('compiler cleaned build')",
                       str(log.parent)]
            with contextlib.redirect_stdout(io.StringIO()): project.execute(command, log)
            self.assertIn("compiler cleaned build", log.read_text())

    def test_optimized_build_is_separate_and_has_no_flash_action(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("sys.argv", ["project.py", "build", "--sketch", "WindCalmController", "--profile", "optimized"]), \
                patch.object(project, "ROOT", Path(directory)), patch.object(project, "BUILD"), patch.object(project, "SKETCH"), \
                patch.object(project, "FQBN", project.BASE_FQBN), patch.object(project, "PROFILE", "debug"), \
                patch.object(project, "configure"), patch.object(project, "check_sdk"), \
                patch.object(project, "manifest"), patch.object(project, "run") as run:
            project.main()
            command = run.call_args.args[0]
            self.assertEqual(command[0], "compile")
            self.assertIn("DebugLevel=none", command[2])
            self.assertEqual(command[4], str(Path(directory) / "build/WindCalmController-optimized"))


if __name__ == "__main__": unittest.main()
