#pragma once
#include <array>
#include <cstdint>
#include <cstdlib>
#include <cstring>

namespace windcalm {
// Prototype profile only. No measured speeds or real fan protocol are implied.
struct SpeedProfile {
  std::array<uint8_t, 6> percent{{17, 33, 50, 67, 83, 100}};
  bool valid() const {
    uint8_t previous = 0;
    for (uint8_t p : percent) {
      if (p <= previous || p > 100) return false;
      previous = p;
    }
    return percent.back() == 100;
  }
  uint8_t levelFor(uint8_t requested) const {
    if (requested == 0) return 0;
    for (size_t i = 0; i < percent.size(); ++i)
      if (requested <= percent[i]) return uint8_t(i + 1);
    return 6;
  }
  uint8_t percentFor(uint8_t level) const { return level ? percent[level - 1] : 0; }
};

struct Model {
  SpeedProfile profile;
  uint8_t requestedPercent = 0;
  uint8_t confirmedLevel = 0;
  uint8_t lastNonzero = 50;
  bool requestedLight = false;
  bool confirmedLight = false;
  bool fanPending = false;
  bool lightPending = false;
  uint32_t fanRevision = 0, lightRevision = 0;
  uint32_t commands = 0, confirmations = 0, externalChanges = 0;

  bool requestFan(unsigned percent) {
    if (percent > 100) return false;
    requestedPercent = uint8_t(percent);
    if (percent) lastNonzero = uint8_t(percent);
    fanPending = true;
    ++fanRevision;
    ++commands;
    return true;
  }
  bool requestOnOff(bool on) {
    return requestFan(on ? (requestedPercent ? requestedPercent : lastNonzero) : 0);
  }
  void requestLight(bool on) {
    requestedLight = on;
    lightPending = true;
    ++lightRevision;
    ++commands;
  }
  bool externalFan(unsigned level) {
    if (level > profile.percent.size()) return false;
    confirmedLevel = uint8_t(level);
    requestedPercent = profile.percentFor(confirmedLevel);
    if (requestedPercent) lastNonzero = requestedPercent;
    fanPending = false;
    ++fanRevision;
    ++externalChanges;
    return true;
  }
  void externalLight(bool on) {
    requestedLight = confirmedLight = on;
    lightPending = false;
    ++lightRevision;
    ++externalChanges;
  }
  uint8_t confirmedPercent() const { return profile.percentFor(confirmedLevel); }
};

// In-memory backend only: apply latest request after 150 ms, independently per
// endpoint. An external change cancels only that endpoint's pending request.
class Simulator {
public:
  static constexpr uint32_t latencyMs = 150;
  void tick(Model &m, uint32_t now) {
    if (m.fanPending) {
      if (fanRevision != m.fanRevision) {
        fanRevision = m.fanRevision;
        fanSince = now;
      }
      if (uint32_t(now - fanSince) >= latencyMs) {
        m.confirmedLevel = m.profile.levelFor(m.requestedPercent);
        m.fanPending = false;
        ++m.confirmations;
      }
    }
    if (m.lightPending) {
      if (lightRevision != m.lightRevision) {
        lightRevision = m.lightRevision;
        lightSince = now;
      }
      if (uint32_t(now - lightSince) >= latencyMs) {
        m.confirmedLight = m.requestedLight;
        m.lightPending = false;
        ++m.confirmations;
      }
    }
  }
private:
  uint32_t fanRevision = 0, lightRevision = 0, fanSince = 0, lightSince = 0;
};

enum class ExternalKind { None, Fan, Light };
struct ExternalCommand { ExternalKind kind = ExternalKind::None; unsigned value = 0; };
// USB diagnostic commands only, not a fan UART protocol.
inline ExternalCommand parseExternal(const char *line) {
  ExternalCommand result;
  const char *value = nullptr;
  if (std::strncmp(line, "ext fan ", 8) == 0) {
    result.kind = ExternalKind::Fan; value = line + 8;
  } else if (std::strncmp(line, "ext light ", 10) == 0) {
    result.kind = ExternalKind::Light; value = line + 10;
  } else return result;
  if (*value < '0' || *value > '9') return {};
  char *end = nullptr;
  const unsigned long n = std::strtoul(value, &end, 10);
  if (*end || n > (result.kind == ExternalKind::Fan ? 6UL : 1UL)) return {};
  result.value = unsigned(n);
  return result;
}
} // namespace windcalm
