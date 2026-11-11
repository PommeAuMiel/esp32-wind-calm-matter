#pragma once
// Offline protocol core, original implementation. No Arduino, UART, GPIO or Matter.
#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace windcalm {
constexpr size_t MaxPayload = 256;
constexpr size_t MaxFrame = MaxPayload + 7;
constexpr uint32_t InterByteTimeoutMs = 250;
constexpr uint32_t FrameTimeoutMs = 1000;

struct Frame {
  uint8_t bytes[MaxFrame]{};
  size_t size = 0;
  uint8_t command() const { return bytes[3]; }
  size_t length() const { return size - 7; }
  const uint8_t *payload() const { return bytes + 6; }
};

inline bool encode(uint8_t command, const uint8_t *payload, size_t length, Frame &out,
                   uint8_t version = 0) {
  if (length > MaxPayload || (length && !payload)) return false;
  out.bytes[0] = 0x55; out.bytes[1] = 0xaa; out.bytes[2] = version;
  out.bytes[3] = command; out.bytes[4] = uint8_t(length >> 8); out.bytes[5] = uint8_t(length);
  if (length) memcpy(out.bytes + 6, payload, length);
  uint8_t sum = 0;
  for (size_t i = 0; i < length + 6; ++i) sum = uint8_t(sum + out.bytes[i]);
  out.bytes[length + 6] = sum; out.size = length + 7;
  return true;
}

struct ParserStats { uint32_t frames = 0, checksumErrors = 0, lengthErrors = 0, timeouts = 0; };
class Parser {
 public:
  using Handler = void (*)(void *, const Frame &);
  Parser(Handler handler, void *context) : handler_(handler), context_(context) {}
  // Callback must not recursively feed this parser. Call tick even when no bytes arrive.
  void feed(uint8_t byte, uint32_t now) {
    tick(now);
    if (used_ == 0) started_ = now;
    buffer_[used_++] = byte; last_ = now;
    process(now, false);
  }
  void tick(uint32_t now) {
    if (used_ && (uint32_t(now - last_) >= InterByteTimeoutMs ||
                  uint32_t(now - started_) >= FrameTimeoutMs)) {
      ++stats.timeouts;
      // A truncated leading frame must not hide a complete subsequent frame.
      drop(1); process(now, true); used_ = 0;
    }
  }
  void reset() { used_ = 0; }
  size_t buffered() const { return used_; }
  ParserStats stats;
 private:
  uint8_t buffer_[MaxFrame]{};
  size_t used_ = 0;
  uint32_t last_ = 0, started_ = 0;
  Handler handler_;
  void *context_;
  void drop(size_t n) { used_ -= n; memmove(buffer_, buffer_ + n, used_); }
  void process(uint32_t now, bool expired) {
    while (used_) {
      if (buffer_[0] != 0x55 || (used_ >= 2 && buffer_[1] != 0xaa)) { drop(1); continue; }
      if (used_ < 6) { if (expired) { drop(1); continue; } break; }
      const size_t length = (size_t(buffer_[4]) << 8) | buffer_[5];
      if (length > MaxPayload) { ++stats.lengthErrors; drop(1); continue; }
      const size_t total = length + 7;
      if (used_ < total) { if (expired) { drop(1); continue; } break; }
      uint8_t sum = 0;
      for (size_t i = 0; i < total - 1; ++i) sum = uint8_t(sum + buffer_[i]);
      if (sum != buffer_[total - 1]) { ++stats.checksumErrors; drop(1); continue; }
      Frame frame; frame.size = total; memcpy(frame.bytes, buffer_, total);
      drop(total); ++stats.frames;
      if (handler_) handler_(context_, frame);
      started_ = now;
    }
  }
};

// Reference profile NOT VALIDATED ON OUR CB3S / GL-FS-XR018 unit.
// Presence flags distinguish unknown startup state from OFF; speed may remain 1..6 while OFF.
struct Snapshot {
  bool light = false, fan = false, beeps = false;
  uint16_t temperature = 500;
  uint8_t speed = 0, direction = 0, known = 0;
  bool complete() const { return (known & 15) == 15; }
};
inline bool decodeDatapoints(const uint8_t *data, size_t length, Snapshot &state, uint8_t *present = nullptr) {
  if (length > MaxPayload || (length && !data)) return false;
  Snapshot next = state;
  uint8_t mask = 0;
  for (size_t offset = 0; offset < length;) {
    if (length - offset < 4) return false;
    const uint8_t id = data[offset], type = data[offset + 1];
    const size_t n = (size_t(data[offset + 2]) << 8) | data[offset + 3];
    offset += 4;
    if (n > length - offset) return false;
    const uint8_t *value = data + offset;
    if (type > 5 || (type == 1 && (n != 1 || value[0] > 1)) ||
        (type == 2 && n != 4) || (type == 4 && n != 1) ||
        (type == 5 && n != 1 && n != 2 && n != 4)) return false;
    if (id == 20 || id == 60 || id == 66) {
      if (type != 1) return false;
      if (id == 20) { next.light = value[0]; next.known |= 1; mask |= 1; }
      else if (id == 60) { next.fan = value[0]; next.known |= 2; mask |= 2; }
      else { next.beeps = value[0]; next.known |= 32; mask |= 32; }
    } else if (id == 62) {
      if (type != 2) return false;
      const uint32_t v = (uint32_t(value[0]) << 24) | (uint32_t(value[1]) << 16) |
                         (uint32_t(value[2]) << 8) | value[3];
      if (v < 1 || v > 6) return false;
      next.speed = uint8_t(v); next.known |= 4; mask |= 4;
    } else if (id == 23) {
      if (type != 2) return false;
      const uint32_t v = (uint32_t(value[0]) << 24) | (uint32_t(value[1]) << 16) |
                         (uint32_t(value[2]) << 8) | value[3];
      if (v > 1000) return false;
      next.temperature = uint16_t(v); next.known |= 16; mask |= 16;
    } else if (id == 63) {
      if (type != 4 || value[0] > 1) return false;
      next.direction = value[0]; next.known |= 8; mask |= 8;
    }
    // DP64 timer and unknown IDs: structurally validate, then ignore.
    offset += n;
  }
  state = next; // Atomic: malformed trailing DP never applies earlier DPs.
  if (present) *present = mask;
  return true;
}

enum class Phase { Heartbeat, Product, Configuration, Network, NetworkReply, Datapoints, Ready };
class Session {
 public:
  Session() : parser(&Session::receive, this) {}
  Session(const Session &) = delete;
  Session &operator=(const Session &) = delete;
  Parser parser;
  Snapshot confirmed;
  Phase phase = Phase::Heartbeat;
  uint32_t rejected = 0, unknown = 0, queueOverflows = 0, recoveries = 0;
  uint32_t generation = 0, reportRevision = 0;
  uint8_t reportMask = 0;
  void setObserver(Parser::Handler observer, void *context) { observer_ = observer; observerContext_ = context; }
  bool ready() const { return phase == Phase::Ready && confirmed.complete(); }
  // An offline outbound queue, not a serial transport. Always drain promptly.
  bool pop(Frame &frame) {
    if (!count_) return false;
    frame = queue_[head_]; head_ = (head_ + 1) % QueueSize; --count_; return true;
  }
  void start(uint32_t now) {
    parser.reset(); now_ = now; restart();
    send(0x00); heartbeatAt_ = retryAt_ = lastHeartbeat_ = now;
  }
  void feed(uint8_t byte, uint32_t now) { now_ = now; parser.feed(byte, now); }
  void tick(uint32_t now) {
    now_ = now; parser.tick(now);
    if (uint32_t(now - lastHeartbeat_) >= 45000) {
      ++recoveries; start(now); return;
    }
    if (uint32_t(now - heartbeatAt_) >= 15000) { send(0x00); heartbeatAt_ = now; }
    if (uint32_t(now - retryAt_) >= 1000) {
      retryAt_ = now;
      switch (phase) {
        case Phase::Heartbeat: send(0x00); break;
        case Phase::Product: send(0x01); break;
        case Phase::Configuration: send(0x02); break;
        case Phase::NetworkReply: send(0x03, &networkStatus_, 1); break;
        case Phase::Datapoints: send(0x08); break;
        default: break;
      }
    }
  }
  // Empty CONF reply needs an explicitly chosen Tuya Wi-Fi status mapping.
  // Thread commissioning / HA reachability are NOT automatically mapped to cloud status.
  bool prepareNetworkStatus(uint8_t status) {
    if (phase != Phase::Network || status > 4) return false;
    if (!send(0x03, &status, 1)) return false;
    networkStatus_ = status; phase = Phase::NetworkReply; retryAt_ = now_; return true;
  }
  // Explicit opt-in to the reference profile; this does not assert hardware validation.
  // Never called by startup, reports, reconnect or heartbeat; never writes a UART.
  bool prepareControl(uint8_t id, uint16_t value, bool referenceProfileEnabled = false) {
    if (!referenceProfileEnabled || !ready()) return false;
    uint8_t dp[8] = {id, 1, 0, 1, uint8_t(value), 0, 0, 0}; size_t n = 5;
    if (id == 20 || id == 60 || id == 66) { if (value > 1) return false; }
    else if (id == 62) {
      if (value < 1 || value > 6) return false;
      dp[1] = 2; dp[3] = 4; dp[4] = 0; dp[7] = uint8_t(value); n = 8;
    } else if (id == 63) {
      // The caller must also enforce stop dwell and serialize direction/fan requests.
      if (value > 1 || confirmed.fan) return false;
      dp[1] = 4;
    } else if (id == 23) {
      // Only the three presets observed on this physical controller.
      if (value != 0 && value != 500 && value != 1000) return false;
      dp[1] = 2; dp[3] = 4; dp[4] = dp[5] = 0;
      dp[6] = uint8_t(value >> 8); dp[7] = uint8_t(value); n = 8;
    } else return false; // Timer/unknown writes remain disabled.
    return send(0x06, dp, n); // confirmed is updated only by a later report.
  }
 private:
  static constexpr size_t QueueSize = 8;
  Frame queue_[QueueSize];
  size_t head_ = 0, count_ = 0;
  uint32_t now_ = 0, heartbeatAt_ = 0, lastHeartbeat_ = 0, retryAt_ = 0;
  uint8_t networkStatus_ = 0;
  Parser::Handler observer_ = nullptr;
  void *observerContext_ = nullptr;
  void restart() {
    ++generation;
    head_ = count_ = 0; confirmed = Snapshot{}; phase = Phase::Heartbeat; retryAt_ = now_;
  }
  bool send(uint8_t command, const uint8_t *data = nullptr, size_t size = 0) {
    if (count_ == QueueSize) {
      ++queueOverflows; restart(); return false; // No stale queued motor command survives.
    }
    Frame &frame = queue_[(head_ + count_) % QueueSize];
    if (!encode(command, data, size, frame)) return false;
    ++count_; return true;
  }
  static void receive(void *context, const Frame &frame) {
    auto *session = static_cast<Session *>(context);
    session->onFrame(frame);
    if (session->observer_) session->observer_(session->observerContext_, frame);
  }
  void onFrame(const Frame &f) {
    const uint8_t *p = f.payload(); const size_t n = f.length();
    if (f.bytes[2] != 3) { ++rejected; return; } // Supported MCU protocol version only.
    switch (f.command()) {
      case 0x00:
        if (n != 1 || p[0] > 1) { ++rejected; return; }
        lastHeartbeat_ = now_;
        if (p[0] == 0) { ++recoveries; restart(); }
        if (phase == Phase::Heartbeat && send(0x01)) {
          phase = Phase::Product; retryAt_ = now_;
        }
        break;
      case 0x01:
        if (!n) { ++rejected; return; }
        for (size_t i = 0; i < n; ++i) if (p[i] < 32 || p[i] > 126) { ++rejected; return; }
        // Product text accepted for handshake only, not parsed or used to authorize a profile.
        if (phase == Phase::Product && send(0x02)) { phase = Phase::Configuration; retryAt_ = now_; }
        break;
      case 0x02:
        if (n != 0 && n != 2) { ++rejected; return; }
        if (phase == Phase::Configuration) {
          if (!n) phase = Phase::Network;
          else if (send(0x08)) phase = Phase::Datapoints;
          retryAt_ = now_;
        }
        break;
      case 0x03:
        if (n != 0) { ++rejected; return; }
        if (phase == Phase::NetworkReply && send(0x08)) { phase = Phase::Datapoints; retryAt_ = now_; }
        break;
      case 0x07: case 0x22: {
        Snapshot next = confirmed;
        uint8_t present = 0;
        const bool valid = decodeDatapoints(p, n, next, &present);
        // Success = local atomic acceptance, not a claim of Tuya cloud / HA delivery.
        // Reserve ACK before applying, so queue failure cannot silently confirm state.
        if (f.command() == 0x22) {
          const uint8_t success = valid ? 1 : 0;
          if (!send(0x23, &success, 1)) { ++rejected; return; }
        }
        if (!valid) { ++rejected; return; }
        if (phase == Phase::Datapoints || phase == Phase::Ready) {
          confirmed = next;
          reportMask = present; ++reportRevision;
          if (confirmed.complete()) phase = Phase::Ready;
        }
        break;
      }
      default: ++unknown; break;
    }
  }
};
} // namespace windcalm
