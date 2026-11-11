// SPDX-License-Identifier: MIT
// Shared Matter/Thread and Tuya UART application.
#include "Application.h"
#include <Arduino.h>
#include <Matter.h>
#include <platform/PlatformManager.h>
#include <atomic>
#include "Controller.h"
#include "NativeFan.h"
#include "NativeLight.h"
#include "Diagnostics.h"
#include "Commissioning.h"
#include "PublicationPolicy.h"

#if !defined(ARDUINO_XIAO_ESP32C6) || !CONFIG_ENABLE_MATTER_OVER_THREAD || !CONFIG_ENABLE_CHIPOBLE
#error "XIAO ESP32-C6, Thread and BLE commissioning are required"
#endif
#if defined(ZIGBEE_MODE_ED) || defined(ZIGBEE_MODE_ZCZR)
#error "Disable Zigbee"
#endif
namespace windcalm::application {
namespace {
const char *firmwareVersion = "controller-dev";
constexpr int TuyaTx = D4; // GPIO22 -> old CB3S TX line (controller receives)
constexpr int TuyaRx = D5; // GPIO23 <- old CB3S RX line (controller transmits)
HardwareSerial tuya(1);
NativeLight light; // Preserve endpoints: light 1, fan 2.
NativeFan fan;
MatterOnOffPlugin beeps; // Endpoint 3: ON means audible command acknowledgements.
MatterButton button;
windcalm::Controller controller;
std::atomic<bool> ready{false};
std::atomic<uint32_t> generation{0};
std::atomic<bool> reverseAllowed{false};
std::atomic<bool> fanAllowed{false};
std::atomic<uint8_t> known{0};
bool publishing = false;
struct Mailbox {
  bool fan = false, light = false, lightValue = false, direction = false;
  bool temperature = false, beeps = false, beepsValue = false;
  uint16_t temperatureValue = 500;
  uint32_t temperatureGeneration = 0, beepsGeneration = 0;
  uint8_t percent = 0, lastNonzero = 50;
  uint8_t directionValue = 0;
  uint32_t fanGeneration = 0, lightGeneration = 0, directionGeneration = 0;
} mailbox; // CHIP lock protects mailbox, endpoint SDK caches and publishing.
uint32_t lastStatus = 0;
windcalm::PublicationPolicy publication;
bool observedLightAttributeKnown = false, observedLightAttribute = false;
windcalm::Frame txFrame;
bool txPending = false;
uint32_t txGeneration = 0, txCount = 0, controlTx = 0;
windcalm::Diagnostics diagnostics;
windcalm::DiagnosticEvent diagnosticEvent;
bool diagnosticActive = false, diagnosticHeader = false;
size_t diagnosticOffset = 0;
char usbLine[48]{};
size_t usbLength = 0;
bool usbOverflow = false;
bool pairingRequested = false;
bool lightSnapshotRequested = false;
uint32_t lightPublishAttempts = 0, lightPublishErrors = 0;
struct LightSyncSnapshot {
  uint32_t time = 0, revision = 0;
  bool known = false, confirmed = false, cached = false, attributeKnown = false;
  bool attribute = false, pending = false;
};

static void readDiagnosticCommands() {
  for (unsigned budget = 0; budget < 64 && Serial.available(); ++budget) {
    const char c = char(Serial.read());
    if (c == '\r') continue;
    if (c != '\n') {
      if (usbLength < sizeof(usbLine)-1 && !usbOverflow) usbLine[usbLength++] = c;
      else usbOverflow = true;
      continue;
    }
    usbLine[usbLength] = 0;
    if (!usbOverflow && !strcmp(usbLine,"pairing")) {
      pairingRequested = true; // Process under CHIP lock; USB only, never a network command.
    } else if (!usbOverflow && (!strcmp(usbLine,"diag on") || !strcmp(usbLine,"diag off"))) {
      diagnostics.setEnabled(usbLine[5] == 'o' && usbLine[6] == 'n', millis()); diagnosticActive = false;
      Serial.printf("DIAG: enabled=%u dropped=%lu\n", diagnostics.enabled, (unsigned long)diagnostics.dropped);
    } else if (!usbOverflow && !strcmp(usbLine,"diag snapshot")) {
      lightSnapshotRequested = true; // Only read the model under the CHIP lock.
    } else if (!usbOverflow && !strcmp(usbLine,"diag query")) {
      const bool accepted = diagnostics.enabled && controller.requestReadback(millis());
      Serial.printf("DIAG_QUERY: accepted=%u ready=%u enabled=%u read_only=1\n",
                    accepted, controller.session.ready(), diagnostics.enabled);
      lightSnapshotRequested = true;
    } else if (!usbOverflow && !strncmp(usbLine,"diag mark ",10)) {
      bool valid = usbLength > 10;
      for (size_t i=10;i<usbLength;++i) {
        const char v=usbLine[i];
        valid &= (v>='a' && v<='z') || (v>='A' && v<='Z') || (v>='0' && v<='9') || v=='_' || v=='-';
      }
      if (valid) Serial.printf("DIAG_MARK: ms=%lu label=%s\n", (unsigned long)millis(),usbLine+10);
      else Serial.println("DIAG: rejected marker");
    } else Serial.println("DIAG: commands are diag on | diag off | diag snapshot | diag query | diag mark LABEL | pairing");
    usbLength = 0; usbOverflow = false;
  }
}
// At most one short line per loop, only when USB has room. Never print in the
// parser callback or CHIP callback; ring overflow must not delay ACK/heartbeat.
static void printDiagnostics() {
  if (!diagnostics.enabled || !Serial) return;
  if (!diagnosticActive) {
    if (!diagnostics.pop(diagnosticEvent)) return;
    diagnosticActive = diagnosticHeader = true; diagnosticOffset = 0;
  }
  const auto &f = diagnosticEvent.frame;
  char line[160]; size_t advance = 0; bool finish = false;
  if (diagnosticHeader) {
    snprintf(line,sizeof(line),"FRAME_DIAG: seq=%lu ms=%lu version=%02X cmd=%02X len=%u checksum=ok\n",
      (unsigned long)diagnosticEvent.sequence,(unsigned long)diagnosticEvent.time,f.bytes[2],f.command(),unsigned(f.length()));
    finish = f.command()!=7 && f.command()!=0x22;
  } else {
    windcalm::DatapointCursor cursor(f.payload()+diagnosticOffset,f.length()-diagnosticOffset);
    windcalm::DatapointView dp;
    if (!cursor.next(dp)) {
      if (!cursor.malformed) { diagnosticActive = false; return; }
      snprintf(line,sizeof(line),"DP_DIAG: seq=%lu malformed_offset=%u\n",(unsigned long)diagnosticEvent.sequence,unsigned(diagnosticOffset));
      finish = true;
    } else {
      advance = dp.length + 4;
      const bool numeric = ((dp.type==1 || dp.type==4) && dp.length==1) ||
          (dp.type==2 && dp.length==4) || (dp.type==5 && (dp.length==1 || dp.length==2 || dp.length==4));
      if (numeric) {
        uint32_t value=0; for(size_t i=0;i<dp.length;++i) value=(value<<8)|dp.value[i];
        snprintf(line,sizeof(line),"DP_DIAG: seq=%lu id=%u type=%u len=%u value=%lu\n",(unsigned long)diagnosticEvent.sequence,dp.id,dp.type,unsigned(dp.length),(unsigned long)value);
      } else snprintf(line,sizeof(line),"DP_DIAG: seq=%lu id=%u type=%u len=%u value=redacted\n",(unsigned long)diagnosticEvent.sequence,dp.id,dp.type,unsigned(dp.length));
    }
  }
  const size_t n = strlen(line);
  if (Serial.availableForWrite() < int(n)) return;
  Serial.write(reinterpret_cast<const uint8_t *>(line),n);
  diagnosticHeader = false; diagnosticOffset += advance;
  if (finish) diagnosticActive = false;
}

static void fail(const char *reason) {
  while (true) { Serial.printf("ERROR: %s\n", reason); delay(2000); }
}
static bool requestFan(uint8_t percent) {
  if (!ready.load() || !fanAllowed.load() || mailbox.direction || percent > 100) return false;
  mailbox.percent = percent;
  if (percent) mailbox.lastNonzero = percent;
  mailbox.fanGeneration = generation.load(); mailbox.fan = true; return true;
}
static void drainTx() {
  if (txPending && txGeneration != controller.session.generation) txPending = false;
  // Bounded drain: do not block while the serial TX buffer is full.
  for (unsigned i = 0; i < 8; ++i) {
    if (!txPending) {
      if (!controller.pop(txFrame)) return;
      txPending = true; txGeneration = controller.session.generation;
    }
    if (tuya.availableForWrite() < int(txFrame.size)) return;
    if (tuya.write(txFrame.bytes, txFrame.size) != txFrame.size) fail("UART short write");
    ++txCount; if (txFrame.command() == 6) ++controlTx;
    txPending = false;
  }
}
} // namespace

void begin(const Config &config) {
  firmwareVersion = config.firmwareVersion ? config.firmwareVersion : "controller-dev";
  Serial.begin(115200);
  // A connected USB host may stop reading. Never wait for diagnostic TX space:
  // HWCDC's default retries can otherwise stall control for seconds per line.
  Serial.setTxTimeoutMs(0);
  windcalm::CommissioningCredentials expectedCredentials;
  if (!configureDeviceCommissioning(expectedCredentials)) fail("Commissioning credentials unavailable; no default PIN fallback");
  pinMode(LED_BUILTIN, OUTPUT); digitalWrite(LED_BUILTIN, HIGH);
  tuya.setRxBufferSize(1024); tuya.setTxBufferSize(1024);
  tuya.begin(9600, SERIAL_8N1, TuyaRx, TuyaTx);
  button.setLongHoldMs(5000);
  if (!button.begin(BOOT_PIN)) fail("Button initialization");
  if (!Matter.selectNetwork(MATTER_NETWORK_THREAD)) fail("Thread selection");
  if (!Matter.setDeviceName("Wind Calm") || !Matter.setProductName("Wind Calm Thread Fan Light")) fail("Matter identity");
  if (!light.begin(false)) fail("Light endpoint");
  if (!fan.begin(0, MatterFan::FAN_MODE_OFF, MatterFan::FAN_MODE_SEQ_OFF_HIGH)) fail("Fan endpoint");
  if (!beeps.begin(false)) fail("Beeps endpoint");
  light.onTemperature([](uint16_t value) {
    if (publishing) return true;
    if (!ready.load() || !(known.load() & 16)) return false;
    mailbox.temperatureValue = value; mailbox.temperatureGeneration = generation.load(); mailbox.temperature = true;
    return true;
  });
  beeps.onChange([](bool enabled) {
    if (publishing) return true;
    if (!ready.load() || !(known.load() & 32)) return false;
    mailbox.beepsValue = enabled; mailbox.beepsGeneration = generation.load(); mailbox.beeps = true;
    return true;
  });
  light.onChange([](bool on) {
    if (publishing) return true;
    if (!ready.load()) return false;
    mailbox.lightValue = on; mailbox.lightGeneration = generation.load(); mailbox.light = true;
    return true;
  });
  fan.onChangeSpeedPercent([](uint8_t p) { return publishing || requestFan(p); });
  fan.onChangeMode([](MatterFan::FanMode_t mode) {
    if (publishing) return true;
    if (mode == MatterFan::FAN_MODE_OFF) return requestFan(0);
    if (mode == MatterFan::FAN_MODE_HIGH) return requestFan(mailbox.percent ? mailbox.percent : mailbox.lastNonzero);
    return false;
  });
  fan.onDirection([](uint8_t direction) {
    if (publishing) return true;
    if (!ready.load() || !reverseAllowed.load() || mailbox.fan) return false;
    mailbox.directionValue = direction; mailbox.directionGeneration = generation.load(); mailbox.direction = true;
    return true;
  });
  controller.session.setObserver([](void *,const windcalm::Frame &frame) {
    diagnostics.capture(frame,millis());
  },nullptr);
  if (config.beforeMatterBegin && !config.beforeMatterBegin()) fail("Maintenance endpoint initialization");
  Matter.begin();
  if (!verifyDeviceCommissioning(expectedCredentials)) fail("Commissioning override failed; commissioning closed");
  controller.start(millis());
  Serial.printf("TUYA_UART: baud=9600 tx=D4/GPIO%d rx=D5/GPIO%d startup_controls=0\n", TuyaTx, TuyaRx);
  Serial.printf("ENDPOINTS: light=%u fan=%u thread=%u\n", light.getEndPointId(), fan.getEndPointId(), Matter.getNetworkEndPointId(MATTER_NETWORK_THREAD));
  Serial.println("BOOT: hold 5s for intentional Matter reset. Short/double click do not command the fan.");
  Serial.printf("FAN_FEATURES: map=0x%08lx speeds=6 direction=1 reverse_stop_dwell_ms=10000 firmware=%s\n",(unsigned long)fan.declaredFeatures,firmwareVersion);
  Serial.println("SECURITY: device-local commissioning credentials; USB pairing command only before commissioning; diagnostics expire after 120s");
  Serial.println("USB_DIAG: diag on | diag off | diag snapshot | diag query | diag mark LABEL. Query only reads the MCU; no control is sent.");
}
void tick() {
  const uint32_t now = millis();
  controller.tick(now); drainTx();
  diagnostics.tick(now);
  readDiagnosticCommands();
  for (unsigned budget = 0; budget < 128 && tuya.available(); ++budget) {
    controller.feed(uint8_t(tuya.read()), now); drainTx();
  }
  const bool synchronized = controller.session.ready();
  const uint32_t epoch = controller.session.generation;
  ready.store(synchronized); generation.store(epoch); known.store(controller.session.confirmed.known);
  reverseAllowed.store(controller.mayReverse(now));
  fanAllowed.store(synchronized && !controller.directionPending);
  const auto pendingMask = []() -> uint8_t {
    return controller.fanPending | (controller.lightPending << 1) | (controller.directionPending << 2) |
           (controller.temperaturePending << 3) | (controller.beepsPending << 4);
  };
  publication.observe(epoch, controller.session.reportRevision, pendingMask());
  if (button.poll() == MATTER_BUTTON_LONG_HOLD) {
    ready.store(false); Serial.println("RESET: intentional Matter reset"); Matter.decommission();
  }
  bool status = false, commissioned = false, thread = false, pairingResponse = false;
  bool lightSnapshot = false;
  LightSyncSnapshot lightSync;
  String pairing;
  if (chip::DeviceLayer::PlatformMgr().TryLockChipStack()) {
    if (mailbox.direction) {
      if (synchronized && mailbox.directionGeneration == epoch && !mailbox.fan)
        controller.requestDirection(mailbox.directionValue,now);
      mailbox.direction = false; publication.markDirty();
    }
    if (mailbox.fan) {
      if (synchronized && mailbox.fanGeneration == epoch)
        controller.requestFan(windcalm::Controller::levelFor(mailbox.percent), now);
      mailbox.fan = false;
      publication.markDirty();
    }
    if (mailbox.light) {
      if (synchronized && mailbox.lightGeneration == epoch) controller.requestLight(mailbox.lightValue, now);
      mailbox.light = false;
      publication.markDirty();
    }
    if (mailbox.temperature) {
      if (synchronized && mailbox.temperatureGeneration == epoch) controller.requestTemperature(mailbox.temperatureValue, now);
      mailbox.temperature = false; publication.markDirty();
    }
    if (mailbox.beeps) {
      if (synchronized && mailbox.beepsGeneration == epoch) controller.requestBeeps(mailbox.beepsValue, now);
      mailbox.beeps = false; publication.markDirty();
    }
    publication.observe(epoch, controller.session.reportRevision, pendingMask());
    if (synchronized && publication.due(now)) {
      publishing = true; bool ok = true;
      if (!controller.fanPending) {
        const uint8_t p = windcalm::Controller::percentFor(controller.level());
        ok &= fan.setMode(p ? MatterFan::FAN_MODE_HIGH : MatterFan::FAN_MODE_OFF, fan.ATTR_UPDATE);
        ok &= fan.setSpeedPercent(p, fan.ATTR_UPDATE);
        mailbox.percent = p;
        mailbox.lastNonzero = windcalm::Controller::percentFor(controller.lastLevel);
      }
      if (!controller.lightPending) {
        ++lightPublishAttempts;
        const bool lightOk = light.reportOnOff(controller.session.confirmed.light);
        if (!lightOk) ++lightPublishErrors;
        ok &= lightOk;
      }
      if ((controller.session.confirmed.known & 16) && !controller.temperaturePending)
        ok &= light.reportTemperature(controller.session.confirmed.temperature);
      if ((controller.session.confirmed.known & 32) && !controller.beepsPending)
        ok &= beeps.setOnOff(controller.session.confirmed.beeps);
      ok &= fan.reportDetails(controller.level(),controller.session.confirmed.direction,
                              controller.fanPending,controller.directionPending);
      publishing = false;
      observedLightAttributeKnown = light.readOnOffAttribute(observedLightAttribute);
      publication.complete(now, ok);
    }
    if (uint32_t(now - lastStatus) >= 5000) {
      lastStatus = now; status = true;
      commissioned = Matter.isDeviceCommissioned(); thread = Matter.isThreadConnected();
    }
    if (pairingRequested) {
      pairingRequested = false; pairingResponse = true;
      if (!Matter.isDeviceCommissioned()) pairing = Matter.getManualPairingCode();
    }
    if (lightSnapshotRequested || (status && diagnostics.enabled)) {
      lightSnapshotRequested = false; lightSnapshot = true;
      lightSync.time = now; lightSync.revision = controller.session.reportRevision;
      lightSync.known = bool(controller.session.confirmed.known & 1);
      lightSync.confirmed = controller.session.confirmed.light;
      lightSync.pending = controller.lightPending;
      lightSync.cached = light.getOnOff();
      lightSync.attributeKnown = light.readOnOffAttribute(lightSync.attribute);
    }
    chip::DeviceLayer::PlatformMgr().UnlockChipStack();
  }
  drainTx();
  ready.store(controller.session.ready()); generation.store(controller.session.generation);
  reverseAllowed.store(controller.mayReverse(now));
  fanAllowed.store(controller.session.ready() && !controller.directionPending);
  digitalWrite(LED_BUILTIN, controller.session.ready() && controller.session.confirmed.light ? LOW : HIGH);
  if (status && Serial) {
    Serial.printf("STATUS: commissioned=%u thread=%u simulator=0 tuya_ready=%u phase=%u generation=%lu known=0x%02x\n", commissioned, thread, controller.session.ready(), unsigned(controller.session.phase), (unsigned long)controller.session.generation, controller.session.confirmed.known);
    Serial.printf("TUYA: rx_frames=%lu tx_frames=%lu control_tx=%lu checksum_errors=%lu rejected=%lu recoveries=%lu fan=%u speed=%u light=%u fan_pending=%u light_pending=%u confirmations=%lu cancellations=%lu timeouts=%lu\n", (unsigned long)controller.session.parser.stats.frames, (unsigned long)txCount, (unsigned long)controlTx, (unsigned long)controller.session.parser.stats.checksumErrors, (unsigned long)controller.session.rejected, (unsigned long)controller.session.recoveries, controller.session.confirmed.fan, controller.session.confirmed.speed, controller.session.confirmed.light, controller.fanPending, controller.lightPending, (unsigned long)controller.confirmations, (unsigned long)controller.cancellations, (unsigned long)controller.timeouts);
    Serial.printf("LIGHT_DETAILS: temperature=%u temperature_known=%u temperature_pending=%u beeps=%u beeps_known=%u beeps_pending=%u firmware=%s\n",controller.session.confirmed.temperature,bool(controller.session.confirmed.known & 16),controller.temperaturePending,controller.session.confirmed.beeps,bool(controller.session.confirmed.known & 32),controller.beepsPending,firmwareVersion);
    Serial.printf("FAN_DETAILS: level=%u direction=%u direction_pending=%u reverse_allowed=%u diag_enabled=%u diag_dropped=%lu firmware=%s\n",controller.level(),controller.session.confirmed.direction,controller.directionPending,controller.mayReverse(now),diagnostics.enabled,(unsigned long)diagnostics.dropped,firmwareVersion);
  }
  if (pairingResponse && Serial) {
    if (pairing.length()) Serial.printf("MANUAL_CODE: %s\n", pairing.c_str());
    else Serial.println("PAIRING: already commissioned or code unavailable; no secret printed");
  }
  if (lightSnapshot && Serial) {
    Serial.printf("LIGHT_SYNC: ms=%lu revision=%lu known=%u confirmed=%u cache=%u attr_known=%u attr=%u pending=%u attempts=%lu errors=%lu\n",
      (unsigned long)lightSync.time, (unsigned long)lightSync.revision, lightSync.known,
      lightSync.confirmed, lightSync.cached, lightSync.attributeKnown, lightSync.attribute,
      lightSync.pending, (unsigned long)lightPublishAttempts, (unsigned long)lightPublishErrors);
  }
  printDiagnostics();
  delay(2);
}

Snapshot snapshot() {
  return {controller.session.ready(), bool(controller.session.confirmed.known & 1),
    controller.session.confirmed.light, controller.lightPending, observedLightAttributeKnown,
    observedLightAttribute, controller.session.confirmed.known,
    controller.session.parser.stats.checksumErrors, controller.session.rejected,
    controller.session.recoveries, controller.session.reportRevision, controller.timeouts,
    lightPublishErrors, lightPublishAttempts, controller.session.generation};
}
bool endpointsMatch() {
  return light.getEndPointId() == 1 && fan.getEndPointId() == 2 && beeps.getEndPointId() == 3;
}
} // namespace windcalm::application
