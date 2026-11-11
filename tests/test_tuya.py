"""Offline synthetic protocol tests; never open a serial port."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class TuyaTests(unittest.TestCase):
    def test_cpp_commissioning(self):
        self.compile_and_run("commissioning_test.cpp", "WindCalmController")

    def test_cpp_security(self):
        self.compile_and_run("security_test.cpp", "WindCalmController")

    def test_cpp_diagnostics(self):
        self.compile_and_run("diagnostics_test.cpp", "WindCalmController")

    def test_cpp_controller(self):
        self.compile_and_run("controller_test.cpp", "WindCalmController")

    def test_cpp_protocol(self):
        self.compile_and_run("tuya_test.cpp", "WindCalmProtocolTest")

    def compile_and_run(self, source, sketch):
        compiler = shutil.which("clang++") or shutil.which("g++")
        self.assertIsNotNone(compiler)
        with tempfile.TemporaryDirectory() as directory:
            binary = str(pathlib.Path(directory) / "tuya_test")
            fake_includes = ["-I", str(ROOT / "tests/commissioning_fakes")] if source == "commissioning_test.cpp" else []
            subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", *fake_includes,
                            "-fsanitize=address,undefined", "-g", "-I",
                            str(ROOT / "firmware" / sketch),
                            str(ROOT / "tests" / source), "-o", binary], check=True)
            result = subprocess.run([binary], check=True, capture_output=True, text=True)
            print(result.stdout, end="")


if __name__ == "__main__":
    unittest.main()
