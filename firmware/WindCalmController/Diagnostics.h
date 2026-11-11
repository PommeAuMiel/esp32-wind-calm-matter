#pragma once
#include "TuyaProtocol.h"

namespace windcalm {
struct DiagnosticEvent { Frame frame; uint32_t time = 0, sequence = 0; };
class Diagnostics {
 public:
  bool enabled = false;
  uint32_t dropped = 0;
  static constexpr uint32_t LifetimeMs = 120000;
  void setEnabled(bool on, uint32_t now = 0) { enabled = on; startedAt_ = now; head_ = count_ = 0; }
  void tick(uint32_t now) {
    if (enabled && uint32_t(now - startedAt_) >= LifetimeMs) setEnabled(false, now);
  }
  void capture(const Frame &frame, uint32_t now) {
    if (!enabled) return;
    if (count_ == Capacity) { ++dropped; return; }
    auto &event = events_[(head_ + count_) % Capacity];
    event.frame = frame; event.time = now; event.sequence = ++sequence_; ++count_;
  }
  bool pop(DiagnosticEvent &event) {
    if (!count_) return false;
    event = events_[head_]; head_ = (head_ + 1) % Capacity; --count_; return true;
  }
 private:
  static constexpr size_t Capacity = 4;
  DiagnosticEvent events_[Capacity];
  size_t head_ = 0, count_ = 0;
  uint32_t sequence_ = 0;
  uint32_t startedAt_ = 0;
};
struct DatapointView { uint8_t id = 0, type = 0; size_t length = 0; const uint8_t *value = nullptr; };
class DatapointCursor {
 public:
  DatapointCursor(const uint8_t *data, size_t length) : data_(data), length_(length) {}
  bool malformed = false;
  bool next(DatapointView &view) {
    if (malformed || offset_ == length_) return false;
    if (length_ - offset_ < 4) { malformed = true; return false; }
    view.id = data_[offset_]; view.type = data_[offset_+1];
    view.length = (size_t(data_[offset_+2]) << 8) | data_[offset_+3];
    offset_ += 4;
    if (view.length > length_ - offset_) { malformed = true; return false; }
    view.value = data_ + offset_; offset_ += view.length; return true;
  }
 private:
  const uint8_t *data_;
  size_t length_, offset_ = 0;
};
} // namespace windcalm
