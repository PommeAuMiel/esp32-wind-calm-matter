// Synthetic credential validation; never print or read a real device PIN.
#include "Security.h"
#include <assert.h>
#include <stdio.h>
using namespace windcalm;
int main() {
  CommissioningCredentials out;
  const uint32_t invalid[] = {0, 99999999, 100000000, UINT32_MAX, 12345678, 87654321, 20202021,
    11111111, 22222222, 33333333, 44444444, 55555555, 66666666, 77777777, 88888888};
  for (auto pin : invalid) {
    assert(!validPasscode(pin));
    assert(!credentialsFromWords(pin, 0, out));
    assert(!validCredentials(out)); // No partially applied credential.
  }
  assert(credentialsFromWords(12340000, 0xffff, out) && validCredentials(out));
  assert(out.discriminator == 4095);
  for (auto pin : invalid) {
    auto bad = out; bad.passcode = pin; assert(!validCredentials(bad));
  }
  auto bad = out; bad.version ^= 1; assert(!validCredentials(bad));
  bad = out; bad.reserved = 1; assert(!validCredentials(bad));
  bad = out; bad.discriminator = 4096; assert(!validCredentials(bad));
  uint32_t random = 0x83abc123;
  for (unsigned i = 0; i < 100000; ++i) {
    random ^= random << 13; random ^= random >> 17; random ^= random << 5;
    CommissioningCredentials candidate;
    if (credentialsFromWords(random, random >> 12, candidate)) {
      assert(validCredentials(candidate) && candidate.passcode == random);
    } else assert(!validCredentials(candidate));
  }
  puts("Security: PIN exclusions, atomic candidate, schema bounds and 100000 synthetic RNG words passed");
}
