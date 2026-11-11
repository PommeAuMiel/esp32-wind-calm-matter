"""Run the production monotonic policy with stalled-task and wrap injections."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RuntimeSupervisorTests(unittest.TestCase):
    def test_actual_policy_under_sanitizers(self):
        source = r'''
#include "firmware/WindCalmThreadOTA/main/RuntimeSupervisor.h"
#include <cassert>
#include <cstdint>
using namespace windcalm_ota;
int main() {
  RuntimeSupervisor boot;
  boot.begin(100, 0);
  assert(boot.tick(60099, 0, 0, false, false).feedWatchdog);
  auto r = boot.tick(60100, 0, 0, false, false);
  assert(!r.feedWatchdog && r.reason == RecoveryReason::StartupStalled);
  RuntimeSupervisor loop;
  loop.begin(0, 0); loop.tick(0, 1, 1, true, false);
  assert(loop.tick(14999, 1, 2, true, false).feedWatchdog);
  r = loop.tick(15000, 1, 3, true, false);
  assert(!r.feedWatchdog && r.reason == RecoveryReason::LoopStalled);
  // CHIP can stall while the UART loop continues to yield and progress.
  RuntimeSupervisor matter;
  matter.begin(0, 0); matter.tick(0, 1, 1, true, false);
  assert(matter.tick(44999, 2, 1, true, false).feedWatchdog);
  r = matter.tick(45000, 3, 1, true, false);
  assert(!r.feedWatchdog && r.reason == RecoveryReason::MatterStalled);
  RuntimeSupervisor update;
  update.begin(0, 0); update.tick(0, 1, 1, true, true);
  assert(update.tick(59999, 1, 2, true, true).feedWatchdog);
  assert(!update.tick(60000, 1, 3, true, true).feedWatchdog);
  RuntimeSupervisor updateMatter;
  updateMatter.begin(0, 0); updateMatter.tick(0, 1, 1, true, true);
  assert(updateMatter.tick(179999, 2, 1, true, true).feedWatchdog);
  assert(updateMatter.tick(180000, 3, 1, true, true).reason == RecoveryReason::MatterStalled);
  // Three closely spaced recoveries cause a ten-minute holdoff, not a storm.
  RuntimeSupervisor limited;
  limited.begin(0, 3); limited.tick(0, 1, 1, true, false);
  r = limited.tick(15000, 1, 2, true, false);
  assert(r.feedWatchdog && r.state == SupervisorState::Holdoff);
  assert(limited.tick(614999, 1, 3, true, false).feedWatchdog);
  assert(!limited.tick(615000, 1, 4, true, false).feedWatchdog);
  assert(limited.nextRecoveryCount() == 1);
  RuntimeSupervisor recovered;
  recovered.begin(0, 3); recovered.tick(0, 1, 1, true, false);
  assert(recovered.tick(15000, 1, 2, true, false).state == SupervisorState::Holdoff);
  assert(recovered.tick(15001, 2, 3, true, false).state == SupervisorState::Healthy);
  RuntimeSupervisor stable;
  stable.begin(0, 2);
  bool cleared = false;
  for (uint32_t t = 0; t <= 600000; t += 1000)
    cleared |= stable.tick(t, t + 1, t / 5000 + 1, true, false).clearRecoveryBudget;
  assert(cleared && stable.nextRecoveryCount() == 1);
  // Monotonic millisecond and progress counters may both wrap normally.
  RuntimeSupervisor wrap;
  const uint32_t start = UINT32_MAX - 999;
  wrap.begin(start, 0); wrap.tick(start, UINT32_MAX, UINT32_MAX, true, false);
  assert(wrap.tick(start + 1000, 0, 0, true, false).feedWatchdog);
  assert(wrap.tick(start + 15999, 0, 1, true, false).feedWatchdog);
  assert(!wrap.tick(start + 16000, 0, 2, true, false).feedWatchdog);
}
'''
        compiler = shutil.which("clang++") or shutil.which("g++")
        self.assertIsNotNone(compiler)
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "runtime.cpp"
            file.write_text(source)
            executable = Path(directory) / "runtime"
            subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                            "-I", str(ROOT), str(file), "-o", str(executable)], check=True)
            subprocess.run([str(executable)], check=True)


if __name__ == "__main__":
    unittest.main()
