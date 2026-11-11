// Wind Calm milestone 1: USB-only XIAO ESP32-C6 LED test.
// Initialization follows Espressif Arduino-ESP32 3.3.12 CHIPoBLEThread example
// (Apache-2.0). No fan connection or application UART is configured.
#include <Arduino.h>
#include <Matter.h>
#include <atomic>

#if !defined(ARDUINO_XIAO_ESP32C6)
#error "Select XIAO_ESP32C6"
#endif
#if !defined(CONFIG_ENABLE_MATTER_OVER_THREAD) || !CONFIG_ENABLE_MATTER_OVER_THREAD
#error "The SDK must support Matter over Thread"
#endif
#if !defined(CONFIG_ENABLE_CHIPOBLE) || !CONFIG_ENABLE_CHIPOBLE
#error "BLE commissioning is required"
#endif
#if defined(ZIGBEE_MODE_ED) || defined(ZIGBEE_MODE_ZCZR)
#error "Disable Zigbee for this Thread test"
#endif

MatterOnOffLight testLight;
MatterButton resetButton;
// Active-low user LED. Check the visible polarity on the actual board.
constexpr uint8_t LED_ON = LOW;
constexpr uint8_t LED_OFF = HIGH;
// The callback runs on the CHIP task. Only transfer state here; loop owns GPIO.
std::atomic<bool> requestedLed{false};
bool ready = false;
bool appliedLed = false;
uint32_t lastStatus = 0;

static void fail(const char *reason) {
  Serial.printf("ERROR: %s\n", reason);
  // No reset/erase: keep the reason readable when USB is opened later.
  while (true) {
    delay(2000);
    Serial.printf("ERROR: %s\n", reason);
  }
}

void setup() {
  Serial.begin(115200);
  // Do not wait for Serial: Home Assistant must work without a USB monitor.
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LED_OFF);
  resetButton.setLongHoldMs(5000);
  if (!resetButton.begin(BOOT_PIN)) fail("BOOT timer initialization failed");
  if (!Matter.isNetworkSupported(MATTER_NETWORK_THREAD)) fail("Thread unsupported");
  if (!Matter.isBLECommissioningEnabled()) fail("BLE commissioning unavailable");
  if (!Matter.selectNetwork(MATTER_NETWORK_THREAD)) fail("Thread selection failed");
  if (!Matter.setDeviceName("Wind Calm USB LED")) fail("Device name failed");
  if (!Matter.setProductName("Wind Calm Thread Test")) fail("Product name failed");
  if (!testLight.begin(false)) fail("Light endpoint creation failed");
  testLight.onChange([](bool state) {
    requestedLed.store(state, std::memory_order_relaxed);
    return true;
  });
  Matter.begin();
  ready = true;
  Serial.printf("Wind Calm USB LED: LED=%u BOOT=%u THREAD_ENDPOINT=%u\n",
                LED_BUILTIN, BOOT_PIN, Matter.getNetworkEndPointId(MATTER_NETWORK_THREAD));
}

void loop() {
  if (!ready) return;
  matterButtonEvent_t event;
  while ((event = resetButton.poll()) != MATTER_BUTTON_NONE) {
    if (event == MATTER_BUTTON_LONG_HOLD) {
      Serial.println("RESET: intentional Matter factory reset after 5s BOOT hold");
      Matter.decommission();
      // SDK schedules factory reset/reboot. No repeated request while held.
    }
  }
  const bool led = requestedLed.load(std::memory_order_relaxed);
  if (led != appliedLed) {
    digitalWrite(LED_BUILTIN, led ? LED_ON : LED_OFF);
    appliedLed = led;
    Serial.printf("LED_APPLIED: %s\n", led ? "ON" : "OFF");
  }
  const uint32_t now = millis();
  if (now - lastStatus >= 5000) {
    lastStatus = now;
    const bool commissioned = Matter.isDeviceCommissioned();
    Serial.printf("STATUS: commissioned=%u thread=%u case_session=%u selected_network=%d led=%u heap=%u\n",
                  commissioned, Matter.isThreadConnected(), Matter.isOnline(),
                  int(Matter.getSelectedNetwork()), appliedLed, ESP.getFreeHeap());
    if (!commissioned) {
      // Available only on local USB; never save these in shared reports.
      Serial.printf("MANUAL_CODE: %s\n", Matter.getManualPairingCode().c_str());
    }
  }
  delay(10);
}
