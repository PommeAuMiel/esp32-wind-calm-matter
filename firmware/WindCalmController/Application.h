// SPDX-License-Identifier: MIT
#pragma once
#include <cstdint>
namespace windcalm::application {
struct Config {
  const char *firmwareVersion;
  bool (*beforeMatterBegin)() = nullptr;
};
// Mutable controller state is owned by the Arduino loop. This snapshot must
// only be read by that same task; cross-task consumers use atomic copies.
struct Snapshot {
  bool ready, lightKnown, lightConfirmed, lightPending, lightAttributeKnown, lightAttribute;
  uint32_t known, checksumErrors, rejected, recoveries, reports, timeouts;
  uint32_t publicationErrors, publicationAttempts, generation;
};
void begin(const Config &config);
void tick();
Snapshot snapshot();
bool endpointsMatch();
}
