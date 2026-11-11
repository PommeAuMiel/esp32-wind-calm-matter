# SPDX-License-Identifier: MIT
"""Real subprocess regression: stream progress and preserve errors and full logs."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import thread_sdk as sdk


class BuildLoggingTests(unittest.TestCase):
    def test_live_output_preserves_failure_and_suppresses_generic_flash_advice(self):
        code = "\n".join([
            "import sys",
            "print('compiling shared application', flush=True)",
            "print('compiler error detail', file=sys.stderr, flush=True)",
            "print('-- Component paths: very long list', flush=True)",
            "print('Project build complete. To flash, run:', flush=True)",
            "print('idf.py flash', flush=True)",
            "sys.exit(7)",
        ])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); log = root / "build.log"; output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = sdk.run_logged([sys.executable, "-c", code], log, os.environ.copy(), root, live=True)
            self.assertEqual(result.returncode, 7)
            with self.assertRaises(subprocess.CalledProcessError):
                result.check_returncode()
            self.assertIn("compiling shared application", output.getvalue())
            self.assertIn("compiler error detail", output.getvalue())
            self.assertNotIn("idf.py flash", output.getvalue())
            self.assertNotIn("Component paths:", output.getvalue())
            self.assertIn("idf.py flash", log.read_text())
            self.assertIn("compiler error detail", log.read_text())

    def test_quiet_mode_keeps_complete_successful_log_without_streaming(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); log = root / "build.log"; output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = sdk.run_logged([sys.executable, "-c", "print('compiled')"], log, os.environ.copy(), root)
            result.check_returncode()
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(log.read_text(), "compiled\n")


if __name__ == "__main__":
    unittest.main()
