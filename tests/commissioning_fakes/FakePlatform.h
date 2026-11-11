#pragma once
#include <assert.h>
#include <stdint.h>
#include <cstring>
#include <vector>

// Test-only SDK interfaces. No real hardware, RNG, NVS, radio or secret data.
namespace Fake {
struct State {
  std::vector<uint8_t> blob;
  bool beginOK = true, writeOK = true, corruptReadback = false, pinOK = true;
  bool entropy = false, closed = false;
  unsigned fills = 0, pinCalls = 0, locks = 0;
  uint32_t pin = 0; uint16_t discriminator = 0;
};
inline State state;
}
inline constexpr int CHIP_NO_ERROR = 0;
class Preferences {
 public:
  bool begin(const char *name, bool) { assert(!strcmp(name,"wc-security")); return Fake::state.beginOK; }
  void end() {}
  bool isKey(const char *) { return !Fake::state.blob.empty(); }
  size_t getBytesLength(const char *) { return Fake::state.blob.size(); }
  size_t getBytes(const char *, void *out, size_t size) {
    if (size < Fake::state.blob.size()) return 0;
    memcpy(out,Fake::state.blob.data(),Fake::state.blob.size());
    if (Fake::state.corruptReadback && Fake::state.fills) static_cast<uint8_t *>(out)[0] ^= 1;
    return Fake::state.blob.size();
  }
  size_t putBytes(const char *, const void *data, size_t size) {
    if (!Fake::state.writeOK) return 0;
    auto *bytes = static_cast<const uint8_t *>(data);
    Fake::state.blob.assign(bytes,bytes+size); return size;
  }
};
inline void bootloader_random_enable() { assert(!Fake::state.entropy); Fake::state.entropy = true; }
inline void bootloader_random_disable() { assert(Fake::state.entropy); Fake::state.entropy = false; }
inline void esp_fill_random(void *out, size_t size) {
  assert(Fake::state.entropy && size == 8);
  const uint32_t words[] = {++Fake::state.fills == 1 ? 0u : 12340000u, 0xfffu};
  memcpy(out,words,size);
}
class FakeMatter {
 public:
  bool setSetupPasscode(uint32_t value) {
    assert(!Fake::state.entropy); ++Fake::state.pinCalls;
    Fake::state.pin = value; return Fake::state.pinOK;
  }
  bool setSetupDiscriminator(uint16_t value) { Fake::state.discriminator = value; return true; }
  bool setBLECommissioningEnabled(bool enabled) { assert(enabled); return true; }
  bool setBLEMemoryReleaseEnabled(bool enabled) { assert(enabled); return true; }
};
inline FakeMatter Matter;
namespace chip {
namespace DeviceLayer {
class Manager {
 public:
  void LockChipStack() { ++Fake::state.locks; }
  void UnlockChipStack() { assert(Fake::state.locks); --Fake::state.locks; }
};
inline Manager &PlatformMgr() { static Manager manager; return manager; }
class Provider {
 public:
  int GetSetupPasscode(uint32_t &pin) { assert(Fake::state.locks); pin = Fake::state.pin; return 0; }
  int GetSetupDiscriminator(uint16_t &value) { assert(Fake::state.locks); value = Fake::state.discriminator; return 0; }
};
inline Provider *GetCommissionableDataProvider() { static Provider provider; return &provider; }
}
class Window {
 public:
  void CloseCommissioningWindow() { assert(Fake::state.locks); Fake::state.closed = true; }
};
class Server {
 public:
  static Server &GetInstance() { static Server server; return server; }
  Window &GetCommissioningWindowManager() { return window; }
 private:
  Window window;
};
}
