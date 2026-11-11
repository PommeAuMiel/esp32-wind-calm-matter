"""Real publication scheduler: retries, idle work, pending completion and wrap."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = r'''
#include "PublicationPolicy.h"
#include "MaintenancePolicy.h"
#include <cassert>
int main() {
 using windcalm::PublicationPolicy;
 PublicationPolicy idle;
 unsigned attempts=0;
 for (uint32_t now=0;now<3000;now+=2) {
  idle.observe(1,1,0);
  if (idle.due(now)) { ++attempts; idle.complete(now,true); }
 }
 assert(attempts==3); // Thousands of loop iterations, bounded reconciliation.
 idle.observe(1,2,0); assert(idle.due(3000));
 // A failed CHIP try-lock does not complete/consume the publication.
 assert(idle.due(3002)); idle.complete(3002,false);
 assert(!idle.due(3051)); assert(idle.due(3052));
 idle.complete(3052,true); assert(!idle.due(3054));
 idle.observe(1,2,2); assert(idle.due(3102)); idle.complete(3102,true);
 idle.observe(1,2,0); assert(idle.due(3152)); // Timeout/ACK with no new revision.
 idle.complete(3152,true);
 idle.observe(2,2,0); assert(idle.due(3202)); // Reconnect invalidates old epoch.
 PublicationPolicy wrap;
 const uint32_t begin=UINT32_MAX-20;
 wrap.complete(begin,false); assert(!wrap.due(begin+49)); assert(wrap.due(begin+50));
 wrap.complete(begin+50,true); assert(!wrap.due(begin+1049)); assert(wrap.due(begin+1050));
 using namespace windcalm_maintenance;
 Snapshot initial,next;
 HealthReportCadence cadence; cadence.begin(begin,initial);
 next.loopProgress=100; next.freeHeap=50000; next.lightConfirmed=true;
 assert(!cadence.changed(9,initial,next,begin+5000));
 assert(!cadence.changed(16,initial,next,begin+5000));
 assert(cadence.changed(20,initial,next,begin+5000));
 next.uartRejected=1; assert(cadence.changed(4,initial,next,begin+5000));
 cadence.complete(begin+5000,next);
 // Compare slow fields against their last notification, not last 5s probe.
 assert(cadence.changed(9,next,next,begin+30000));
 assert(cadence.changed(16,next,next,begin+30000));
 cadence.complete(begin+30000,next);
 assert(!cadence.changed(9,next,next,begin+60000));
}
'''


class PublicationPolicyTests(unittest.TestCase):
    def test_idle_retry_pending_reconnect_and_wrapping_clocks(self):
        compiler = shutil.which('clang++') or shutil.which('c++')
        self.assertTrue(compiler)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'scheduler.cpp'; source.write_text(SOURCE)
            binary = root / 'scheduler'
            subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=address,undefined', '-I'+str(ROOT/'firmware/WindCalmController'),
                            '-I'+str(ROOT/'firmware/WindCalmThreadOTA/main'), str(source), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)
