#include "Simulation.h"
#include <cassert>
#include <iostream>
using namespace windcalm;

int main() {
  SpeedProfile p;
  assert(p.valid());
  assert(p.levelFor(0) == 0);
  for (unsigned n = 1; n <= 100; ++n) {
    auto level = p.levelFor(uint8_t(n));
    assert(level >= 1 && level <= 6);
    assert(p.percentFor(level) >= n);
    if (level > 1) assert(p.percentFor(level - 1) < n);
  }
  for (unsigned level = 0; level <= 6; ++level) assert(p.levelFor(p.percentFor(level)) == level);
  auto custom = p; custom.percent = {{10, 20, 40, 60, 80, 100}};
  assert(custom.valid() && custom.levelFor(11) == 2);
  custom.percent[1] = 10; assert(!custom.valid());
  custom = p; custom.percent[5] = 99; assert(!custom.valid());
  std::cout << "PASS profile: all 101 percentages, levels and invalid/custom configurations\n";

  Model m; Simulator s;
  assert(!m.requestFan(101)); assert(m.commands == 0 && !m.fanPending);
  assert(m.requestFan(51)); s.tick(m, 1000);
  assert(m.requestedPercent == 51 && m.confirmedLevel == 0 && m.fanPending);
  s.tick(m, 1149); assert(m.fanPending);
  s.tick(m, 1150); assert(!m.fanPending && m.confirmedLevel == 4 && m.confirmedPercent() == 67);
  assert(m.requestedPercent == 51); // request is kept distinct from quantized confirmation
  auto commands = m.commands;
  for (unsigned n = 1151; n < 2000; ++n) s.tick(m, n);
  assert(m.commands == commands && m.confirmations == 1); // confirmations do not create requests
  assert(m.requestOnOff(false)); s.tick(m, 2000); s.tick(m, 2150); assert(m.confirmedLevel == 0);
  assert(m.requestOnOff(true)); assert(m.requestedPercent == 51);
  s.tick(m, 2200); s.tick(m, 2350); assert(m.confirmedLevel == 4);
  std::cout << "PASS delayed confirmation, requested/confirmed separation, off/on restoration and no command feedback\n";

  Model fast; Simulator rapid;
  for (unsigned n = 0; n <= 100; ++n) { assert(fast.requestFan(n)); rapid.tick(fast, n); }
  rapid.tick(fast, 249); assert(fast.confirmedLevel == 0);
  rapid.tick(fast, 250); assert(fast.confirmedLevel == 6 && fast.confirmations == 1);
  assert(fast.commands == 101);
  std::cout << "PASS rapid commands: last request wins with a single confirmation\n";

  Model mixed; Simulator sim;
  mixed.requestFan(100); mixed.requestLight(true); sim.tick(mixed, 0);
  assert(mixed.externalFan(2)); sim.tick(mixed, 150);
  assert(mixed.confirmedLevel == 2 && mixed.requestedPercent == 33 && mixed.confirmedLight);
  assert(mixed.commands == 2 && mixed.externalChanges == 1 && mixed.confirmations == 1);
  assert(!mixed.externalFan(7)); assert(mixed.confirmedLevel == 2);
  mixed.requestLight(false); sim.tick(mixed, 200); mixed.externalLight(true); sim.tick(mixed, 500);
  assert(mixed.confirmedLight && !mixed.lightPending);
  // No network state in the backend: loss/reconnection cannot replay a command.
  const auto count = mixed.commands;
  for (unsigned t = 501; t < 20000; t += 10) sim.tick(mixed, t);
  assert(mixed.commands == count && mixed.confirmedLevel == 2 && mixed.confirmedLight);
  Model reboot; assert(reboot.confirmedLevel == 0 && !reboot.confirmedLight && reboot.commands == 0);
  std::cout << "PASS external changes cancel stale commands independently; idle/reconnection and safe boot\n";

  Model wrap; Simulator timer;
  wrap.requestFan(1); timer.tick(wrap, UINT32_MAX - 100);
  timer.tick(wrap, 48); assert(wrap.fanPending);
  timer.tick(wrap, 49); assert(wrap.confirmedLevel == 1);
  std::cout << "PASS millis rollover\n";

  assert(parseExternal("ext fan 6").kind == ExternalKind::Fan);
  assert(parseExternal("ext light 1").kind == ExternalKind::Light);
  for (const char *bad : {"", "ext fan -1", "ext fan 7", "ext fan 100000000000000000000", "ext light 2", "ext light 1x", "ext fan ", "fan 2", "ext fan 1 "})
    assert(parseExternal(bad).kind == ExternalKind::None);
  std::cout << "PASS bounded USB command values and malformed inputs\n";
}
