// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Wind Calm contributors
#pragma once
#include <cstdint>

// A deliberately small wire contract. Never add pairing codes, fabric data,
// network addresses, raw UART frames or arbitrary strings to this snapshot.
namespace windcalm_maintenance {
constexpr uint32_t HealthClusterId = 0xFFF1FC00;
constexpr uint16_t RestartActionId = 1;
constexpr uint32_t HealthSchema = 2;
constexpr uint32_t LastHealthAttribute = 26;

struct Snapshot {
  bool tuyaReady = false;
  uint32_t knownMask = 0;
  uint32_t uartChecksumErrors = 0, uartRejected = 0, tuyaRecoveries = 0;
  uint32_t reportRevision = 0, commandTimeouts = 0, lightPublicationErrors = 0;
  uint32_t loopProgress = 0, matterProgress = 0;
  uint32_t supervisorState = 0, lastRecoveryReason = 0, recoveryBudgetCount = 0;
  bool restartPending = false, imageHealthy = false, otaIdle = false;
  bool chipResponsive = false;
  uint32_t freeHeap = 0, minimumFreeHeap = 0, largestFreeBlock = 0;
  bool lightKnown = false, lightConfirmed = false, lightPending = false;
  bool lightAttributeKnown = false, lightAttribute = false, reportSeen = false;
  uint32_t reportAgeMs = UINT32_MAX, lightPublicationAttempts = 0;
};

inline bool restartAllowed(const Snapshot &s) {
  return s.imageHealthy && s.otaIdle && s.chipResponsive && !s.restartPending;
}

// Every field has a fixed TLV scalar type and fixed size. Counters are uint32
// modulo 2^32; polling clients compare samples rather than assume no wrap.
inline bool healthValue(const Snapshot &s, uint32_t attribute, uint32_t &value) {
  switch (attribute) {
    case 0: value = HealthSchema; break;
    case 1: value = s.tuyaReady; break;
    case 2: value = s.knownMask & 0x3Fu; break;
    case 3: value = s.uartChecksumErrors; break;
    case 4: value = s.uartRejected; break;
    case 5: value = s.tuyaRecoveries; break;
    case 6: value = s.reportRevision; break;
    case 7: value = s.commandTimeouts; break;
    case 8: value = s.lightPublicationErrors; break;
    case 9: value = s.loopProgress; break;
    case 10: value = s.matterProgress; break;
    case 11: value = s.supervisorState; break;
    case 12: value = s.lastRecoveryReason; break;
    case 13: value = restartAllowed(s); break;
    case 14: value = s.recoveryBudgetCount; break;
    case 15: value = s.restartPending; break;
    case 16: value = s.freeHeap; break;
    case 17: value = s.minimumFreeHeap; break;
    case 18: value = s.largestFreeBlock; break;
    case 19: value = s.lightKnown; break;
    case 20: value = s.lightConfirmed; break;
    case 21: value = s.lightPending; break;
    case 22: value = s.lightAttributeKnown; break;
    case 23: value = s.lightAttribute; break;
    case 24: value = s.reportSeen; break;
    case 25: value = s.reportAgeMs; break;
    case 26: value = s.lightPublicationAttempts; break;
    default: return false;
  }
  return true;
}

inline bool booleanHealthAttribute(uint32_t attribute) {
  return attribute == 1 || attribute == 13 || attribute == 15 || (attribute >= 19 && attribute <= 24);
}

inline bool slowHealthAttribute(uint32_t attribute) {
  return attribute == 9 || attribute == 10 || (attribute >= 16 && attribute <= 18) || attribute == 25 || attribute == 26;
}

class HealthReportCadence {
 public:
  static constexpr uint32_t IntervalMs = 30000;
  void begin(uint32_t now, const Snapshot &s) { initialized_ = true; sentMs_ = now; sent_ = s; }
  bool slowDue(uint32_t now) const { return initialized_ && uint32_t(now - sentMs_) >= IntervalMs; }
  bool changed(uint32_t id, const Snapshot &previous, const Snapshot &next, uint32_t now) const {
    uint32_t before{}, after{};
    if (slowHealthAttribute(id) && !slowDue(now)) return false;
    return healthValue(slowHealthAttribute(id) ? sent_ : previous, id, before) &&
           healthValue(next, id, after) && before != after;
  }
  void complete(uint32_t now, const Snapshot &next) { if (slowDue(now)) { sentMs_ = now; sent_ = next; } }
 private:
  bool initialized_ = false;
  uint32_t sentMs_ = 0;
  Snapshot sent_{};
};

enum class RestartDecision : uint8_t { Accepted, Unauthorized, UnknownAction, Busy, Unhealthy };

inline RestartDecision authorizeRestart(uint16_t action, bool caseSession, bool validFabric,
                                       bool administerAllowed, const Snapshot &s) {
  // PASE/commissioning and group sessions cannot request a restart even if a
  // broadly configured ACL would otherwise grant the standard Operate right.
  if (!caseSession || !validFabric || !administerAllowed) return RestartDecision::Unauthorized;
  if (action != RestartActionId) return RestartDecision::UnknownAction;
  if (!s.otaIdle || s.restartPending) return RestartDecision::Busy;
  if (!s.imageHealthy || !s.chipResponsive) return RestartDecision::Unhealthy;
  return RestartDecision::Accepted;
}
} // namespace windcalm_maintenance
