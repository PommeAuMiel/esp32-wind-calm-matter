#pragma once
// SPDX-License-Identifier: MIT
#include <cstdint>

namespace windcalm_ota {
enum class RecoveryReason : uint32_t { None, StartupStalled, LoopStalled, MatterStalled, SupervisorStalled, ManualRestart };
enum class SupervisorState : uint32_t { Starting, Healthy, Updating, Suspect, Holdoff, Recovering };

// Pure monotonic policy. Connectivity and actuator state are deliberately not
// inputs: an absent controller/network must not trigger restart storms.
class RuntimeSupervisor {
 public:
  static constexpr uint32_t StartupGraceMs = 60000;
  static constexpr uint32_t LoopStaleMs = 15000;
  static constexpr uint32_t MatterStaleMs = 45000;
  static constexpr uint32_t OtaLoopStaleMs = 60000;
  static constexpr uint32_t OtaMatterStaleMs = 180000;
  static constexpr uint32_t HoldoffMs = 600000;
  static constexpr uint32_t StableResetBudgetMs = 600000;
  static constexpr uint32_t MaxBurstRecoveries = 3;

  struct Result { SupervisorState state; RecoveryReason reason; bool feedWatchdog; bool clearRecoveryBudget; };
  void begin(uint32_t now, uint32_t recoveryCount) {
    started_ = loopSeen_ = matterSeen_ = healthySince_ = now;
    recoveryCount_ = recoveryCount > MaxBurstRecoveries ? MaxBurstRecoveries : recoveryCount;
    initialized_ = true;
  }
  Result tick(uint32_t now, uint32_t loopProgress, uint32_t matterProgress, bool setupComplete, bool otaBusy) {
    if (!initialized_) begin(now, 0);
    if (loopProgress != loopProgress_) { loopSeen_ = now; loopProgress_ = loopProgress; }
    if (matterProgress != matterProgress_) { matterSeen_ = now; matterProgress_ = matterProgress; }
    RecoveryReason reason = RecoveryReason::None;
    if (!setupComplete) {
      if (elapsed(now, started_) < StartupGraceMs) return {SupervisorState::Starting, reason, true, false};
      reason = RecoveryReason::StartupStalled;
    } else if (!armed_) {
      armed_ = true; loopSeen_ = matterSeen_ = healthySince_ = now;
    }
    if (setupComplete) {
      if (elapsed(now, loopSeen_) >= (otaBusy ? OtaLoopStaleMs : LoopStaleMs)) reason = RecoveryReason::LoopStalled;
      else if (elapsed(now, matterSeen_) >= (otaBusy ? OtaMatterStaleMs : MatterStaleMs)) reason = RecoveryReason::MatterStalled;
    }
    if (reason == RecoveryReason::None) {
      failing_ = false;
      const bool clear = armed_ && recoveryCount_ && elapsed(now, healthySince_) >= StableResetBudgetMs;
      if (clear) recoveryCount_ = 0;
      return {otaBusy ? SupervisorState::Updating : SupervisorState::Healthy, reason, true, clear};
    }
    healthySince_ = now;
    if (!failing_) { failing_ = true; failureSince_ = now; }
    if (recoveryCount_ >= MaxBurstRecoveries && elapsed(now, failureSince_) < HoldoffMs)
      return {SupervisorState::Holdoff, reason, true, false};
    return {SupervisorState::Recovering, reason, false, false};
  }
  uint32_t nextRecoveryCount() const { return recoveryCount_ >= MaxBurstRecoveries ? 1 : recoveryCount_ + 1; }
 private:
  static uint32_t elapsed(uint32_t now, uint32_t then) { return now - then; }
  bool initialized_ = false, armed_ = false, failing_ = false;
  uint32_t started_ = 0, loopSeen_ = 0, matterSeen_ = 0, healthySince_ = 0;
  uint32_t failureSince_ = 0, loopProgress_ = 0, matterProgress_ = 0, recoveryCount_ = 0;
};
} // namespace windcalm_ota
