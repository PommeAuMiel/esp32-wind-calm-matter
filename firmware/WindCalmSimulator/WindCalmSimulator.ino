// Milestone 2. USB-only simulation; no fan GPIO, UART, PWM or Tuya frames.
#include <Arduino.h>
#include <Matter.h>
#include <platform/PlatformManager.h>
#include "Simulation.h"

#if !defined(ARDUINO_XIAO_ESP32C6)
#error "Select XIAO_ESP32C6"
#endif
#if !CONFIG_ENABLE_MATTER_OVER_THREAD || !CONFIG_ENABLE_CHIPOBLE
#error "Thread and BLE commissioning are required"
#endif
#if defined(ZIGBEE_MODE_ED) || defined(ZIGBEE_MODE_ZCZR)
#error "Disable Zigbee"
#endif

// Keep light first (endpoint 1), as in the validated milestone 1 sketch.
MatterOnOffLight light;
MatterFan fan;
MatterButton button;
windcalm::Model model;
windcalm::Simulator simulator;
// Access model and endpoint caches only under the CHIP stack lock. Callbacks
// already run under that lock. All Serial/GPIO processing is outside it.
bool publishing = false;
bool reportFailed = false;
uint32_t lastStatus = 0;
char inputLine[40] = {};
size_t inputLength = 0;
bool inputOverflow = false;

static void fail(const char *reason) {
  while (true) { Serial.printf("ERROR: %s\n", reason); delay(2000); }
}

static windcalm::ExternalCommand readExternal() {
  while (Serial.available()) {
    char c = char(Serial.read());
    if (c == '\r') continue;
    if (c == '\n') {
      inputLine[inputLength] = '\0';
      const auto command = inputOverflow ? windcalm::ExternalCommand{} : windcalm::parseExternal(inputLine);
      if (inputLength || inputOverflow) {
        Serial.println(command.kind == windcalm::ExternalKind::None ? "USB_COMMAND: rejected" : "USB_COMMAND: accepted");
      }
      inputLength = 0; inputOverflow = false;
      return command;
    }
    if (inputLength < sizeof(inputLine) - 1 && !inputOverflow) inputLine[inputLength++] = c;
    else inputOverflow = true;
  }
  return {};
}

// Reporting may synchronously invoke the same endpoint callbacks. Guarding
// these reports prevents them becoming new commands to the simulated backend.
static bool reportConfirmed() {
  publishing = true;
  bool ok = true;
  if (!model.fanPending) {
    const auto mode = model.confirmedLevel ? MatterFan::FAN_MODE_HIGH : MatterFan::FAN_MODE_OFF;
    ok &= fan.setMode(mode, fan.ATTR_UPDATE);
    ok &= fan.setSpeedPercent(model.confirmedPercent(), fan.ATTR_UPDATE);
  }
  if (!model.lightPending) ok &= light.setOnOff(model.confirmedLight);
  publishing = false;
  return ok;
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, HIGH); // active-low LED reflects confirmed light only
  if (!model.profile.valid()) fail("Invalid simulated speed profile");
  button.setLongHoldMs(5000);
  button.setDoubleClickGapMs(350);
  if (!button.begin(BOOT_PIN)) fail("Button timer initialization failed");
  if (!Matter.isNetworkSupported(MATTER_NETWORK_THREAD) || !Matter.isBLECommissioningEnabled()) fail("Thread/BLE unavailable");
  if (!Matter.selectNetwork(MATTER_NETWORK_THREAD)) fail("Thread selection failed");
  if (!Matter.setDeviceName("Wind Calm Simulator") || !Matter.setProductName("Wind Calm Fan Light Simulator")) fail("Identity initialization failed");
  if (!light.begin(false)) fail("Light initialization failed");
  if (!fan.begin(0, MatterFan::FAN_MODE_OFF, MatterFan::FAN_MODE_SEQ_OFF_HIGH)) fail("Fan initialization failed");
  light.onChange([](bool on) {
    if (!publishing) model.requestLight(on);
    return true;
  });
  fan.onChangeSpeedPercent([](uint8_t percent) {
    return publishing || model.requestFan(percent);
  });
  fan.onChangeMode([](MatterFan::FanMode_t mode) {
    if (publishing) return true;
    if (mode == MatterFan::FAN_MODE_OFF) return model.requestOnOff(false);
    if (mode == MatterFan::FAN_MODE_HIGH) return model.requestOnOff(true);
    return false; // Auto/Smart not offered by this sequence
  });
  Matter.begin();
  Serial.printf("SIMULATOR_ONLY: light_endpoint=%u fan_endpoint=%u thread_endpoint=%u\n",
                light.getEndPointId(), fan.getEndPointId(), Matter.getNetworkEndPointId(MATTER_NETWORK_THREAD));
  Serial.println("USB: ext fan 0..6 | ext light 0..1. BOOT click: next simulated fan level; double click: light; hold 5s: Matter reset.");
}

void loop() {
  const auto external = readExternal();
  const auto event = button.poll();
  if (event == MATTER_BUTTON_LONG_HOLD) {
    Serial.println("RESET: intentional Matter reset");
    Matter.decommission();
  }
  const uint32_t now = millis();
  bool commissioned = false, connected = false, online = false, confirmedLight = false;
  bool printStatus = false, reportOK = true;
  windcalm::Model snapshot;
  String pairing;
  // Nonblocking acquisition: give the Matter task priority; retain a pending USB
  // command until acquired, rather than dropping it on a busy stack.
  static windcalm::ExternalCommand pendingFanExternal, pendingLightExternal;
  static unsigned pendingFanClicks = 0, pendingLightClicks = 0;
  if (external.kind == windcalm::ExternalKind::Fan) pendingFanExternal = external;
  if (external.kind == windcalm::ExternalKind::Light) pendingLightExternal = external;
  if (event == MATTER_BUTTON_CLICK) ++pendingFanClicks;
  if (event == MATTER_BUTTON_DOUBLE_CLICK) ++pendingLightClicks;
  if (chip::DeviceLayer::PlatformMgr().TryLockChipStack()) {
    if (pendingFanExternal.kind != windcalm::ExternalKind::None) model.externalFan(pendingFanExternal.value);
    if (pendingLightExternal.kind != windcalm::ExternalKind::None) model.externalLight(pendingLightExternal.value != 0);
    pendingFanExternal = {}; pendingLightExternal = {};
    while (pendingFanClicks) { model.externalFan((model.confirmedLevel + 1) % 7); --pendingFanClicks; }
    if (pendingLightClicks % 2) model.externalLight(!model.confirmedLight);
    pendingLightClicks = 0;
    simulator.tick(model, now);
    reportOK = reportConfirmed();
    confirmedLight = model.confirmedLight;
    if (uint32_t(now - lastStatus) >= 5000) {
      lastStatus = now; printStatus = true;
      snapshot = model;
      commissioned = Matter.isDeviceCommissioned(); connected = Matter.isThreadConnected(); online = Matter.isOnline();
      if (!commissioned) pairing = Matter.getManualPairingCode();
    }
    chip::DeviceLayer::PlatformMgr().UnlockChipStack();
  } else { delay(10); return; }
  digitalWrite(LED_BUILTIN, confirmedLight ? LOW : HIGH);
  if (!reportOK && !reportFailed) Serial.println("ERROR: Matter state report failed; retrying");
  reportFailed = !reportOK;
  if (printStatus) {
    Serial.printf("STATUS: commissioned=%u thread=%u case_session=%u transport=THREAD simulator=1\n", commissioned, connected, online);
    Serial.printf("SIM_STATE: requested_pct=%u confirmed_level=%u confirmed_pct=%u fan_pending=%u requested_light=%u confirmed_light=%u light_pending=%u commands=%lu confirmations=%lu external=%lu\n",
                  snapshot.requestedPercent, snapshot.confirmedLevel, snapshot.confirmedPercent(), snapshot.fanPending,
                  snapshot.requestedLight, snapshot.confirmedLight, snapshot.lightPending,
                  (unsigned long)snapshot.commands, (unsigned long)snapshot.confirmations, (unsigned long)snapshot.externalChanges);
    if (!commissioned) Serial.printf("MANUAL_CODE: %s\n", pairing.c_str());
  }
  delay(10);
}
