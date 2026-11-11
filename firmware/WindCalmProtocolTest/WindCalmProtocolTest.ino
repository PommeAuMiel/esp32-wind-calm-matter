// Compile-only milestone 3 probe. Synthetic in-memory input; no UART or GPIO.
#include "TuyaProtocol.h"
windcalm::Session protocol;
windcalm::Frame outgoing;
void setup() {
  protocol.start(0);
  // Synthetic MCU heartbeat (not a captured frame).
  const uint8_t heartbeat[] = {0x55, 0xaa, 0x03, 0x00, 0x00, 0x01, 0x00, 0x03};
  for (uint8_t byte : heartbeat) protocol.feed(byte, 1);
}
void loop() {
  protocol.tick(millis());
  while (protocol.pop(outgoing)) { /* Offline output deliberately discarded. */ }
  delay(1);
}
