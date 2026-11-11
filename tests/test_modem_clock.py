"""Host regressions for the private ESP32-C6 modem clock HAL correction.

The compiled function is extracted from the installed private ESP-IDF source,
not copied into the test. Hardware calls and the silicon revision are stubbed;
this checks reference accounting and assertion ordering, not radio operation.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HAL_SOURCE = (ROOT / ".local/dedicated/esp-idf/components/hal/esp32c6/"
              "modem_clock_hal.c")


def extract_clock_function(source):
    match = re.search(
        r"void modem_clock_hal_enable_wifipwr_clock\s*\([^)]*\)\s*\{", source)
    if match is None:
        raise AssertionError("The installed C6 HAL clock function is missing")
    depth = 1
    end = match.end()
    while depth and end < len(source):
        if source[end] == "{":
            depth += 1
        elif source[end] == "}":
            depth -= 1
        end += 1
    if depth:
        raise AssertionError("The installed C6 HAL clock function is incomplete")
    return source[match.start():end]


STUBS = r"""
#include <cstdio>
#include <stdexcept>
#include <string>
#include <vector>

struct HalAssertionFailure {};
#define HAL_ASSERT(condition) do { if (!(condition)) throw HalAssertionFailure{}; } while (false)
struct modem_clock_hal_context_t { void *lpcon_dev; };
static int chip_revision = 2;
static int device;
static std::vector<bool> clock_calls;
int efuse_hal_chip_revision() { return chip_revision; }
void modem_lpcon_ll_enable_wifipwr_clock(void *dev, bool enable) {
    if (dev != &device) throw std::runtime_error("Incorrect modem HAL device");
    clock_calls.push_back(enable);
}
"""

SCENARIOS = r"""
static void require(bool condition, const char *reason) {
    if (!condition) throw std::runtime_error(reason);
}
static void expect_calls(std::initializer_list<bool> expected) {
    require(clock_calls == std::vector<bool>(expected), "Incorrect hardware clock transitions");
}
int main(int argc, char **argv) {
    require(argc == 2, "A scenario is required");
    modem_clock_hal_context_t hal{&device};
    const std::string scenario = argv[1];
    if (scenario == "balanced") {
        // Returning to zero is valid, and a subsequent owner can use the clock.
        for (int cycle = 0; cycle < 3; ++cycle) {
            modem_clock_hal_enable_wifipwr_clock(&hal, true);
            modem_clock_hal_enable_wifipwr_clock(&hal, false);
        }
        expect_calls({true, false, true, false, true, false});
    } else if (scenario == "nested") {
        // Shared users turn the clock on once and off only after the last release.
        for (int owner = 0; owner < 128; ++owner)
            modem_clock_hal_enable_wifipwr_clock(&hal, true);
        expect_calls({true});
        for (int owner = 0; owner < 127; ++owner)
            modem_clock_hal_enable_wifipwr_clock(&hal, false);
        expect_calls({true});
        modem_clock_hal_enable_wifipwr_clock(&hal, false);
        expect_calls({true, false});
        modem_clock_hal_enable_wifipwr_clock(&hal, true);
        modem_clock_hal_enable_wifipwr_clock(&hal, false);
        expect_calls({true, false, true, false});
    } else if (scenario == "underflow") {
        // A rejected release must leave both hardware and reference count intact.
        for (int cycle = 0; cycle < 3; ++cycle) {
            const auto previous_calls = clock_calls;
            bool rejected = false;
            try { modem_clock_hal_enable_wifipwr_clock(&hal, false); }
            catch (const HalAssertionFailure &) { rejected = true; }
            require(rejected, "Release without an owner was accepted");
            require(clock_calls == previous_calls, "Rejected release touched hardware");
            modem_clock_hal_enable_wifipwr_clock(&hal, true);
            modem_clock_hal_enable_wifipwr_clock(&hal, false);
        }
        expect_calls({true, false, true, false, true, false});
    } else if (scenario == "eco0") {
        chip_revision = 0;
        // ECO0 has direct hardware behavior; repeated writes must remain direct.
        for (bool enable : {false, false, true, true, false})
            modem_clock_hal_enable_wifipwr_clock(&hal, enable);
        expect_calls({false, false, true, true, false});
    } else {
        throw std::runtime_error("Unknown scenario");
    }
    std::printf("C6 modem clock HAL: %s passed\n", argv[1]);
}
"""


class ModemClockHalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not HAL_SOURCE.is_file():
            raise unittest.SkipTest("The private ESP-IDF SDK is not installed")
        compiler = shutil.which("clang++") or shutil.which("g++")
        if not compiler:
            raise unittest.SkipTest("A host C++ compiler is not installed")
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        source = Path(directory.name) / "modem_clock_test.cpp"
        source.write_text(STUBS + extract_clock_function(HAL_SOURCE.read_text()) + SCENARIOS)
        cls.binary = str(Path(directory.name) / "modem_clock_test")
        subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                        "-fsanitize=address,undefined", "-g", str(source),
                        "-o", cls.binary], check=True)

    def run_scenario(self, scenario):
        # Each process starts the actual function's private static counter at zero.
        result = subprocess.run([self.binary, scenario], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout, end="")

    def test_balanced_references_return_to_zero(self):
        self.run_scenario("balanced")

    def test_shared_references_only_toggle_on_boundary(self):
        self.run_scenario("nested")

    def test_underflow_is_rejected_before_state_changes(self):
        self.run_scenario("underflow")

    def test_eco0_keeps_direct_hardware_behavior(self):
        self.run_scenario("eco0")


if __name__ == "__main__":
    unittest.main()
