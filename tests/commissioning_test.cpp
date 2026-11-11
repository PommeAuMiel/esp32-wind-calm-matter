#include "Commissioning.h"
#include <stdio.h>
int main() {
  using windcalm::CommissioningCredentials;
  CommissioningCredentials first;
  assert(configureDeviceCommissioning(first));
  assert(Fake::state.fills == 2 && !Fake::state.entropy && Fake::state.pinCalls == 1);
  assert(windcalm::validCredentials(first) && verifyDeviceCommissioning(first));
  assert(!Fake::state.closed && !Fake::state.locks);
  auto original = Fake::state.blob;
  // Reboot reuses the same credential without drawing new randomness.
  Fake::state = Fake::State{}; Fake::state.blob = original;
  CommissioningCredentials reboot;
  assert(configureDeviceCommissioning(reboot));
  assert(!Fake::state.fills && reboot.passcode == first.passcode && reboot.discriminator == first.discriminator);
  // Broken/partial schema must fail before changing a PIN or replacing storage.
  for (bool truncated : {false, true}) {
    Fake::state = Fake::State{}; Fake::state.blob = original;
    if (truncated) Fake::state.blob.pop_back(); else Fake::state.blob[0] ^= 1;
    auto bad = Fake::state.blob;
    assert(!configureDeviceCommissioning(reboot));
    assert(Fake::state.blob == bad && !Fake::state.fills && !Fake::state.pinCalls && !Fake::state.entropy);
  }
  Fake::state = Fake::State{}; Fake::state.beginOK = false;
  assert(!configureDeviceCommissioning(reboot) && !Fake::state.fills);
  for (bool corruptRead : {false, true}) {
    Fake::state = Fake::State{};
    Fake::state.writeOK = corruptRead; Fake::state.corruptReadback = corruptRead;
    assert(!configureDeviceCommissioning(reboot));
    assert(!Fake::state.entropy && !Fake::state.pinCalls);
  }
  Fake::state = Fake::State{}; Fake::state.pinOK = false;
  assert(!configureDeviceCommissioning(reboot) && !Fake::state.entropy);
  // SDK fallback to a test PIN closes commissioning and always releases the lock.
  Fake::state = Fake::State{}; assert(configureDeviceCommissioning(first));
  Fake::state.pin = 20202021;
  assert(!verifyDeviceCommissioning(first) && Fake::state.closed && !Fake::state.locks);
  puts("Commissioning: synthetic first boot, reuse, corrupt NVS, write/read failures, entropy cleanup and provider fallback passed");
}
