#pragma once
#include "TuyaProtocol.h"

namespace windcalm {
// Real UART backend logic, also tested on host with synthetic MCU packets.
// All calls are confined to Arduino loop; Matter callbacks use a separate mailbox.
class Controller {
 public:
  Session session;
  bool fanPending = false, lightPending = false, directionPending = false;
  bool temperaturePending = false, beepsPending = false;
  static constexpr uint32_t StopDwellMs = 10000;
  uint32_t confirmations = 0, cancellations = 0, timeouts = 0, requests = 0;
  uint8_t lastLevel = 3;
  void start(uint32_t now) { session.start(now); reconcile(); monitorStop(now); }
  void feed(uint8_t byte, uint32_t now) { session.feed(byte, now); reconcile(); monitorStop(now); }
  void tick(uint32_t now) {
    session.tick(now); reconcile(); monitorStop(now);
    // Prototype emulation: Tuya local network available (03), no cloud claim (04).
    // Independent of HA. Confirm actual MCU accepts this status on the real unit.
    if (session.phase == Phase::Network) session.prepareNetworkStatus(3);
    if ((fanPending && uint32_t(now - fanAt_) >= 3000) ||
        (lightPending && uint32_t(now - lightAt_) >= 3000) ||
        (directionPending && uint32_t(now - directionAt_) >= 3000) ||
        (temperaturePending && uint32_t(now - temperatureAt_) >= 3000) ||
        (beepsPending && uint32_t(now - beepsAt_) >= 3000)) {
      if (fanPending && uint32_t(now - fanAt_) >= 3000) { fanPending = false; ++timeouts; }
      if (lightPending && uint32_t(now - lightAt_) >= 3000) { lightPending = false; ++timeouts; }
      if (directionPending && uint32_t(now - directionAt_) >= 3000) {
        directionPending = false; ++timeouts;
        // Direction is uncertain: require a fresh DP63 before allowing controls again.
        session.confirmed.known &= uint8_t(~8u); session.phase = Phase::Datapoints;
        stoppedTracked_ = false;
      }
      if (temperaturePending && uint32_t(now - temperatureAt_) >= 3000) {
        temperaturePending = false; session.confirmed.known &= uint8_t(~16u); ++timeouts;
      }
      if (beepsPending && uint32_t(now - beepsAt_) >= 3000) {
        beepsPending = false; session.confirmed.known &= uint8_t(~32u); ++timeouts;
      }
      // Readback only; NEVER automatically retry or replay a motor/light command.
      Frame query; encode(8, nullptr, 0, query); extraQuery_ = query; queryPending_ = true;
    }
  }
  bool requestFan(uint8_t level, uint32_t now) {
    if (level > 6 || !session.ready() || directionPending) return false;
    // Speed first, then switch. No command is generated at startup/reconnect.
    if (level && !session.prepareControl(62, level, true)) { reconcile(); return false; }
    if (!session.prepareControl(60, level ? 1 : 0, true)) { reconcile(); return false; }
    targetFan_ = level; fanWaiting_ = level ? 6 : 2; fanAt_ = now;
    fanPending = true; stoppedTracked_ = false;
    if (level) lastLevel = level;
    ++requests; return true;
  }
  bool requestLight(bool on, uint32_t now) {
    if (!session.prepareControl(20, on ? 1 : 0, true)) { reconcile(); return false; }
    targetLight_ = on; lightAt_ = now; lightPending = true; ++requests; return true;
  }
  // Explicit diagnostic readback; never infer or replay a light/fan command.
  // One extra-query slot coalesces requests, with at most one acceptance/s.
  bool requestReadback(uint32_t now) {
    if (!session.ready() || queryPending_ ||
        (readbackTracked_ && uint32_t(now - readbackAt_) < 1000)) return false;
    Frame query;
    if (!encode(8, nullptr, 0, query)) return false;
    extraQuery_ = query; queryPending_ = true;
    readbackAt_ = now; readbackTracked_ = true; return true;
  }
  bool requestTemperature(uint16_t value, uint32_t now) {
    if (!(session.confirmed.known & 16) || !session.prepareControl(23,value,true)) return false;
    targetTemperature_ = value; temperatureAt_ = now; temperaturePending = true; ++requests; return true;
  }
  bool requestBeeps(bool enabled, uint32_t now) {
    if (!(session.confirmed.known & 32) || !session.prepareControl(66,enabled ? 1 : 0,true)) return false;
    targetBeeps_ = enabled; beepsAt_ = now; beepsPending = true; ++requests; return true;
  }
  bool mayReverse(uint32_t now) const {
    return session.ready() && !session.confirmed.fan && !fanPending && !directionPending &&
           stoppedTracked_ && uint32_t(now - stoppedAt_) >= StopDwellMs;
  }
  bool requestDirection(uint8_t direction, uint32_t now) {
    if (direction > 1 || !mayReverse(now)) return false;
    if (session.confirmed.direction == direction) return true;
    if (!session.prepareControl(63, direction, true)) { reconcile(); return false; }
    targetDirection_ = direction; directionAt_ = now; directionPending = true; ++requests;
    return true; // No restart command; caller must explicitly turn on later.
  }
  bool pop(Frame &frame) {
    if (session.pop(frame)) return true;
    if (queryPending_ && (session.ready() || session.phase == Phase::Datapoints)) {
      queryPending_ = false; frame = extraQuery_; return true;
    }
    return false;
  }
  uint8_t level() const { return session.confirmed.fan ? session.confirmed.speed : 0; }
  static uint8_t levelFor(uint8_t percent) {
    return percent == 0 ? 0 : percent <= 17 ? 1 : percent <= 33 ? 2 :
           percent <= 50 ? 3 : percent <= 67 ? 4 : percent <= 83 ? 5 : 6;
  }
  static uint8_t percentFor(uint8_t level) {
    const uint8_t values[] = {0,17,33,50,67,83,100}; return level <= 6 ? values[level] : 0;
  }
 private:
  uint32_t generation_ = 0, revision_ = 0, fanAt_ = 0, lightAt_ = 0, directionAt_ = 0, stoppedAt_ = 0;
  uint32_t temperatureAt_ = 0, beepsAt_ = 0;
  uint32_t readbackAt_ = 0;
  uint16_t targetTemperature_ = 500;
  bool targetBeeps_ = false;
  uint8_t targetFan_ = 0, fanWaiting_ = 0;
  bool targetLight_ = false, queryPending_ = false;
  bool stoppedTracked_ = false;
  bool readbackTracked_ = false;
  uint8_t targetDirection_ = 0;
  Frame extraQuery_;
  void monitorStop(uint32_t now) {
    if (!session.ready() || session.confirmed.fan || fanPending) stoppedTracked_ = false;
    else if (!stoppedTracked_) { stoppedTracked_ = true; stoppedAt_ = now; }
  }
  void reconcile() {
    if (generation_ != session.generation) {
      generation_ = session.generation;
      if (fanPending) ++cancellations;
      if (lightPending) ++cancellations;
      if (directionPending) ++cancellations;
      if (temperaturePending) ++cancellations;
      if (beepsPending) ++cancellations;
      temperaturePending = beepsPending = false;
      fanPending = lightPending = directionPending = queryPending_ = stoppedTracked_ = readbackTracked_ = false;
      revision_ = session.reportRevision;
    }
    if (revision_ == session.reportRevision) return;
    revision_ = session.reportRevision;
    const auto &s = session.confirmed;
    const uint8_t mask = session.reportMask;
    if (temperaturePending && (mask & 16)) {
      if (s.temperature == targetTemperature_) ++confirmations; else ++cancellations;
      temperaturePending = false;
    }
    if (beepsPending && (mask & 32)) {
      if (s.beeps == targetBeeps_) ++confirmations; else ++cancellations;
      beepsPending = false;
    }
    if (directionPending && (mask & 8)) {
      if (s.direction == targetDirection_) ++confirmations; else ++cancellations;
      directionPending = false;
    }
    if ((mask & 4) && s.speed) lastLevel = s.speed;
    if (lightPending && (mask & 1)) {
      if (s.light == targetLight_) ++confirmations; else ++cancellations;
      lightPending = false;
    }
    if (fanPending) {
      bool mismatch = false;
      if (mask & 2) {
        mismatch |= s.fan != (targetFan_ != 0); fanWaiting_ &= uint8_t(~2u);
      }
      if ((mask & 4) && targetFan_) {
        mismatch |= s.speed != targetFan_; fanWaiting_ &= uint8_t(~4u);
      }
      if (mismatch) { fanPending = false; ++cancellations; }
      else if (!fanWaiting_) { fanPending = false; ++confirmations; }
    }
  }
};
} // namespace windcalm
