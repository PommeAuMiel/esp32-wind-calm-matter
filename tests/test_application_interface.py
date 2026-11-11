# SPDX-License-Identifier: MIT
"""Compile the real shared health adapter against the actual UART controller."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_usb_backpressure import extract_function

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "firmware/WindCalmController"


class ApplicationInterfaceTests(unittest.TestCase):
    def test_health_snapshot_keeps_actual_counters_flags_and_endpoint_identity(self):
        compiler = shutil.which("clang++") or shutil.which("g++")
        if not compiler:
            self.skipTest("C++ compiler unavailable")
        source = (APP / "Application.cpp").read_text()
        adapter = extract_function(source, r"Snapshot\s+snapshot\s*\(\s*\)")
        endpoints = extract_function(source, r"bool\s+endpointsMatch\s*\(\s*\)")
        fixture = r'''
#include "Application.h"
#include "Controller.h"
#include <cassert>
namespace windcalm::application {
Controller controller;
bool observedLightAttributeKnown = true, observedLightAttribute = false;
uint32_t lightPublishErrors = 17, lightPublishAttempts = 23;
struct Endpoint { unsigned id; unsigned getEndPointId() { return id; } };
Endpoint light{1}, fan{2}, beeps{3};
'''+adapter+'\n'+endpoints+r'''
}
int main() {
  using namespace windcalm::application;
  controller.session.confirmed.known = 63;
  controller.session.confirmed.light = true;
  controller.lightPending = true;
  controller.session.generation = 19;
  controller.session.parser.stats.checksumErrors = 2;
  controller.session.rejected = 3;
  controller.session.recoveries = 5;
  controller.session.reportRevision = 7;
  controller.timeouts = 11;
  const auto before = snapshot();
  assert(before.ready == controller.session.ready());
  assert(before.known == 63 && before.lightKnown && before.lightConfirmed && before.lightPending);
  assert(before.lightAttributeKnown && !before.lightAttribute);
  assert(before.generation == 19 && before.checksumErrors == 2 && before.rejected == 3);
  assert(before.recoveries == 5 && before.reports == 7 && before.timeouts == 11);
  assert(before.publicationErrors == 17 && before.publicationAttempts == 23);
  controller.session.confirmed.light = false;
  controller.session.confirmed.known = 0;
  controller.lightPending = false;
  const auto after = snapshot();
  assert(!after.lightKnown && !after.lightConfirmed && !after.lightPending && !after.known);
  assert(before.lightConfirmed && before.known == 63); // Independent value copy.
  assert(endpointsMatch());
  light.id = 4; assert(!endpointsMatch()); light.id = 1;
  fan.id = 4; assert(!endpointsMatch()); fan.id = 2;
  beeps.id = 4; assert(!endpointsMatch());
}
'''
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary); (path / "test.cpp").write_text(fixture)
            subprocess.run([compiler, "-std=c++20", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", "-I", str(APP),
                            str(path / "test.cpp"), "-o", str(path / "test")], check=True)
            subprocess.run([str(path / "test")], check=True)

    def test_distribution_contract_matches_client_and_minimum(self):
        import sys
        sys.path.insert(0, str(ROOT / "tools"))
        import ha_ota
        import thread_ota
        version, label = thread_ota.project_version()
        minimum, maximum = thread_ota.validate_release_config(
            (ROOT / "firmware/WindCalmThreadOTA/sdkconfig.defaults").read_text())
        self.assertEqual(ha_ota.TARGET_VERSION, version)
        self.assertEqual(ha_ota.BASE_VERSION, minimum)
        self.assertEqual(ha_ota.TARGET_STRING, label)
        self.assertEqual(ha_ota.BASE_STRING, f"{minimum // 10000}.{(minimum // 100) % 100}.{minimum % 100}-thread-ota")
        self.assertIsNone(maximum)
        self.assertEqual(label, f"{version // 10000}.{(version // 100) % 100}.{version % 100}-thread-ota")


if __name__ == "__main__":
    unittest.main()
