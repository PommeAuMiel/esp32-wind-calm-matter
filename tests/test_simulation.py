import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class SimulationTests(unittest.TestCase):
    def test_cpp_backend(self):
        compiler = shutil.which("clang++") or shutil.which("g++")
        self.assertIsNotNone(compiler, "A host C++ compiler is required")
        with tempfile.TemporaryDirectory() as directory:
            binary = str(pathlib.Path(directory) / "simulation_test")
            subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", "-g", "-I",
                            str(ROOT / "firmware/WindCalmSimulator"),
                            str(ROOT / "tests/simulation_test.cpp"), "-o", binary], check=True)
            result = subprocess.run([binary], check=True, capture_output=True, text=True)
            print(result.stdout, end="")


if __name__ == "__main__":
    unittest.main()
