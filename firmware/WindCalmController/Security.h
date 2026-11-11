#pragma once
#include <stdint.h>

namespace windcalm {
// Device-local commissioning data. This is not the operational Matter fabric key.
struct CommissioningCredentials {
  uint32_t version = 0x57430101;
  uint32_t passcode = 0;
  uint16_t discriminator = 0;
  uint16_t reserved = 0;
};
static_assert(sizeof(CommissioningCredentials) == 12, "NVS credential schema size");
inline bool validPasscode(uint32_t code) {
  if (!code || code > 99999998 || code == 12345678 || code == 87654321 || code == 20202021) return false;
  for (uint32_t repeated = 11111111; repeated <= 88888888; repeated += 11111111)
    if (code == repeated) return false;
  return true;
}
inline bool validCredentials(const CommissioningCredentials &c) {
  return c.version == 0x57430101 && !c.reserved && c.discriminator <= 4095 && validPasscode(c.passcode);
}
// Rejection sampling: no modulo bias in the PIN. No deterministic fallback.
inline bool credentialsFromWords(uint32_t pin, uint32_t discriminator, CommissioningCredentials &out) {
  if (!validPasscode(pin)) return false;
  CommissioningCredentials next;
  next.passcode = pin; next.discriminator = uint16_t(discriminator & 4095);
  out = next; return true;
}
} // namespace windcalm
