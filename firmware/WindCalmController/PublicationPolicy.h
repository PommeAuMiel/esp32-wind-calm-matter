// SPDX-License-Identifier: MIT
#pragma once
#include <cstdint>

namespace windcalm {
// No actuator orders: schedule confirmed-state reconciliation under CHIP lock.
// Failed publications stay dirty; lock contention must not consume an attempt.
class PublicationPolicy {
 public:
  static constexpr uint32_t ReconcileMs = 1000, RetryMs = 50;
  void markDirty() { dirty_ = true; }
  void observe(uint32_t generation, uint32_t revision, uint8_t pending) {
    if (!observed_ || generation != generation_ || revision != revision_ || pending != pending_) markDirty();
    observed_ = true; generation_ = generation; revision_ = revision; pending_ = pending;
  }
  bool due(uint32_t now) const {
    return !attempted_ || (dirty_ ? uint32_t(now - attemptMs_) >= RetryMs
                                : uint32_t(now - reconciledMs_) >= ReconcileMs);
  }
  void complete(uint32_t now, bool success) {
    attempted_ = true; attemptMs_ = now; dirty_ = !success;
    if (success) reconciledMs_ = now;
  }
 private:
  bool dirty_ = true, attempted_ = false, observed_ = false;
  uint32_t attemptMs_ = 0, reconciledMs_ = 0, generation_ = 0, revision_ = 0;
  uint8_t pending_ = 0;
};
}
