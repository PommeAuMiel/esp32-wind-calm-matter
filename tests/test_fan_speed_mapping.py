"""Compile the real NativeFan against a model of the SDK's FanControl coupling.

ESP-Matter 1.5.1 keeps PercentSetting and SpeedSetting consistent by itself:
a PercentSetting write derives SpeedSetting = ceil(SpeedMax * percent / 100),
a SpeedSetting write derives PercentSetting = floor(speed * 100 / SpeedMax),
and every derived write runs the application PRE_UPDATE callback again. Home
Assistant rounds its six steps to 17/33/50/67/83/100, so 17 % and 67 % derive
speeds 2 and 5. The fake below is an original, minimal model of that behaviour;
no radio, serial port or SDK file is used.
"""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

MATTER_FAKE = r"""
#pragma once
#include <cstdint>
#include <functional>
#include <map>
constexpr int ESP_OK = 0, ESP_FAIL = -1;
struct esp_matter_attr_val_t { int type = 0; union { bool b; uint8_t u8; uint32_t u32; } val{}; };
inline esp_matter_attr_val_t esp_matter_uint8(uint8_t v) { esp_matter_attr_val_t r; r.val.u32 = 0; r.val.u8 = v; return r; }
inline esp_matter_attr_val_t esp_matter_nullable_uint8(uint8_t v) { return esp_matter_uint8(v); }
inline esp_matter_attr_val_t esp_matter_enum8(uint8_t v) { return esp_matter_uint8(v); }
namespace chip::app::Clusters::FanControl {
constexpr uint32_t Id = 0x0202;
namespace Attributes {
namespace FanMode { constexpr uint32_t Id = 0; }
namespace PercentSetting { constexpr uint32_t Id = 2; }
namespace PercentCurrent { constexpr uint32_t Id = 3; }
namespace SpeedMax { constexpr uint32_t Id = 4; }
namespace SpeedSetting { constexpr uint32_t Id = 5; }
namespace SpeedCurrent { constexpr uint32_t Id = 6; }
namespace AirflowDirection { constexpr uint32_t Id = 11; }
namespace FeatureMap { constexpr uint32_t Id = 0xFFFC; }
}
}
class MatterFan;
namespace fake {
namespace A = chip::app::Clusters::FanControl::Attributes;
inline MatterFan *endpoint = nullptr;
inline std::map<uint32_t, uint32_t> store;
inline unsigned changes = 0;
inline bool percentBusy = false, speedBusy = false, modeBusy = false;
bool pre(uint32_t attribute, uint32_t value);
bool internalWrite(uint32_t attribute, uint32_t value);
// set_val_internal: unchanged values stop early; the cluster logic always runs.
inline bool commit(uint32_t attribute, uint32_t value, bool callbacks) {
  if (store[attribute] == value) return true;
  if (callbacks && !pre(attribute, value)) return false;
  store[attribute] = value; ++changes;
  if (attribute == A::FanMode::Id) {
    const bool previous = modeBusy; modeBusy = true;
    if (value == 0) {
      internalWrite(A::PercentSetting::Id, 0); internalWrite(A::PercentCurrent::Id, 0);
      internalWrite(A::SpeedSetting::Id, 0); internalWrite(A::SpeedCurrent::Id, 0);
    }
    modeBusy = previous;
  } else if (attribute == A::PercentSetting::Id) {
    const bool previous = percentBusy; percentBusy = true;
    if (value == 0 && store[A::FanMode::Id] != 0) internalWrite(A::FanMode::Id, 0);
    internalWrite(A::SpeedSetting::Id, (store[A::SpeedMax::Id] * value + 99) / 100);
    percentBusy = previous;
  } else if (attribute == A::SpeedSetting::Id) {
    const bool previous = speedBusy; speedBusy = true;
    if (value == 0 && store[A::FanMode::Id] != 0) internalWrite(A::FanMode::Id, 0);
    internalWrite(A::PercentSetting::Id, value * 100 / store[A::SpeedMax::Id]);
    speedBusy = previous;
  }
  return true;
}
// Accessor writes made by the cluster server itself.
inline bool internalWrite(uint32_t attribute, uint32_t value) {
  if ((attribute == A::PercentSetting::Id && percentBusy) || (attribute == A::SpeedSetting::Id && speedBusy) ||
      (attribute == A::FanMode::Id && modeBusy)) return true;
  return commit(attribute, value, true);
}
// A controller write always runs PRE_UPDATE, even for an unchanged value.
inline bool controllerWrite(uint32_t attribute, uint32_t value) {
  if (!pre(attribute, value)) return false;
  return commit(attribute, value, false);
}
}
namespace esp_matter::cluster {
inline void *get(uint16_t, uint32_t) { static int cluster; return &cluster; }
namespace fan_control::feature {
namespace multi_speed {
struct config_t { uint8_t speed_max = 0, speed_setting = 0, speed_current = 0; };
inline uint32_t get_id() { return 1; }
inline int add(void *, config_t *config) {
  fake::store[fake::A::SpeedMax::Id] = config->speed_max; fake::store[fake::A::FeatureMap::Id] |= get_id(); return ESP_OK;
}
}
namespace airflow_direction {
struct config_t { uint8_t airflow_direction = 0; };
inline uint32_t get_id() { return 0x20; }
inline int add(void *, config_t *) { fake::store[fake::A::FeatureMap::Id] |= get_id(); return ESP_OK; }
}
}
}
class MatterFan {
 public:
  enum FanMode_t { FAN_MODE_OFF = 0, FAN_MODE_HIGH = 3 };
  enum FanModeSequence_t { FAN_MODE_SEQ_OFF_HIGH = 5 };
  static constexpr bool ATTR_UPDATE = true;
  using SpeedCB = std::function<bool(uint8_t)>;
  using ModeCB = std::function<bool(FanMode_t)>;
  virtual ~MatterFan() = default;
  virtual bool begin(uint8_t = 0, FanMode_t = FAN_MODE_OFF, FanModeSequence_t = FAN_MODE_SEQ_OFF_HIGH) {
    fake::store.clear(); fake::endpoint = this; started = true; return true;
  }
  uint16_t getEndPointId() { return started ? 2 : 0; }
  void onChangeSpeedPercent(SpeedCB cb) { _onChangeSpeedCB = cb; }
  void onChangeMode(ModeCB cb) { _onChangeModeCB = cb; }
  virtual bool attributeChangeCB(uint16_t, uint32_t, uint32_t attribute, esp_matter_attr_val_t *value) {
    bool ok = true;
    if (attribute == fake::A::FanMode::Id) {
      if (value->val.u8 == currentFanMode) return true;
      currentFanMode = FanMode_t(value->val.u8);
      if (_onChangeModeCB) ok &= _onChangeModeCB(currentFanMode);
    } else if (attribute == fake::A::PercentSetting::Id) {
      if (value->val.u8 > 100) return value->val.u8 == 255;
      if (value->val.u8 != currentPercent) {
        currentPercent = value->val.u8;
        if (_onChangeSpeedCB) ok &= _onChangeSpeedCB(currentPercent);
      }
      if (ok) fake::commit(fake::A::PercentCurrent::Id, currentPercent, true);
    } else if (attribute == fake::A::PercentCurrent::Id) {
      if (value->val.u8 != currentPercent) {
        currentPercent = value->val.u8;
        if (_onChangeSpeedCB) ok &= _onChangeSpeedCB(currentPercent);
      }
    }
    return ok;
  }
  bool setMode(FanMode_t mode, bool) {
    if (currentFanMode == mode) return true;
    if (!fake::commit(fake::A::FanMode::Id, mode, true)) return false;
    currentFanMode = mode;
    if (mode == FAN_MODE_OFF) {
      if (!fake::commit(fake::A::PercentSetting::Id, 0, true)) return false;
      currentPercent = 0; return fake::commit(fake::A::PercentCurrent::Id, 0, true);
    }
    return true;
  }
  bool setSpeedPercent(uint8_t percent, bool) {
    if (currentPercent == percent) return true;
    if (!fake::commit(fake::A::PercentSetting::Id, percent, true)) return false;
    currentPercent = percent; return fake::commit(fake::A::PercentCurrent::Id, percent, true);
  }
 protected:
  bool started = false;
  FanMode_t currentFanMode = FAN_MODE_OFF;
  uint8_t currentPercent = 0;
  SpeedCB _onChangeSpeedCB;
  ModeCB _onChangeModeCB;
  bool getAttributeVal(uint32_t, uint32_t attribute, esp_matter_attr_val_t *value) {
    value->val.u32 = fake::store[attribute]; return true;
  }
  bool updateAttributeVal(uint32_t, uint32_t attribute, esp_matter_attr_val_t *value) {
    return fake::commit(attribute, value->val.u8, true);
  }
};
inline bool fake::pre(uint32_t attribute, uint32_t value) {
  auto next = esp_matter_uint8(uint8_t(value));
  return endpoint->attributeChangeCB(2, chip::app::Clusters::FanControl::Id, attribute, &next);
}
"""

TEST = r"""
#include <cassert>
#include <cstdio>
#include "NativeFan.h"
using windcalm::Controller;
namespace A = fake::A;
static NativeFan fan;
static bool publishing = false, requested = false;
static uint8_t requestedPercent = 0;
// Same order as the publication block of Application.cpp.
static void publish(uint8_t level) {
  publishing = true;
  const uint8_t p = Controller::reportedPercentFor(level);
  assert(fan.setMode(p ? MatterFan::FAN_MODE_HIGH : MatterFan::FAN_MODE_OFF, fan.ATTR_UPDATE));
  assert(fan.setSpeedPercent(p, fan.ATTR_UPDATE));
  assert(fan.reportDetails(level, 0, false, false));
  publishing = false;
}
static void settle(uint8_t level) {
  publish(level); publish(level); fan.forgetDerivedSpeed(); requested = false;
}
int main() {
  assert(fan.begin());
  fan.onChangeSpeedPercent([](uint8_t p) {
    if (publishing) return true;
    requestedPercent = p; requested = true; return true;
  });
  fan.onChangeMode([](MatterFan::FanMode_t mode) {
    if (publishing || mode != MatterFan::FAN_MODE_OFF) return true;
    requestedPercent = 0; requested = true; return true;
  });
  const uint8_t homeAssistant[] = {0, 17, 33, 50, 67, 83, 100};
  for (uint8_t level = 0; level <= 6; ++level) {
    // A confirmed MCU state must be published once and then stay untouched.
    settle(level);
    assert(!requested);
    const unsigned before = fake::changes;
    publish(level);
    assert(fake::changes == before);
    assert(fake::store[A::SpeedSetting::Id] == level && fake::store[A::SpeedCurrent::Id] == level);
    assert(Controller::levelFor(uint8_t(fake::store[A::PercentSetting::Id])) == level);
    assert(fake::store[A::PercentCurrent::Id] == fake::store[A::PercentSetting::Id]);
  }
  // Every Home Assistant step selects its own level from every confirmed state.
  for (uint8_t from = 0; from <= 6; ++from)
    for (uint8_t level = 1; level <= 6; ++level) {
      settle(from);
      assert(fake::controllerWrite(A::PercentSetting::Id, homeAssistant[level]));
      // Re-selecting the confirmed level may be a no-op, never another level.
      assert(requested ? Controller::levelFor(requestedPercent) == level : from == level);
    }
  // SDK-consistent percentages and direct SpeedSetting writes select the same levels.
  for (uint8_t from = 0; from <= 6; ++from)
    for (uint8_t level = 1; level <= 6; ++level) {
      settle(from);
      assert(fake::controllerWrite(A::PercentSetting::Id, Controller::reportedPercentFor(level)));
      assert(requested ? Controller::levelFor(requestedPercent) == level : from == level);
      settle(from);
      assert(fake::controllerWrite(A::SpeedSetting::Id, level));
      assert(requested && Controller::levelFor(requestedPercent) == level);
    }
  // A direct speed write that follows a percent write in the same lock hold is still honoured.
  settle(3);
  assert(fake::controllerWrite(A::PercentSetting::Id, 17));
  assert(Controller::levelFor(requestedPercent) == 1);
  assert(fake::controllerWrite(A::SpeedSetting::Id, 5));
  assert(Controller::levelFor(requestedPercent) == 5);
  for (uint8_t level = 0; level <= 6; ++level) {
    const uint8_t p = Controller::reportedPercentFor(level);
    assert(Controller::levelFor(p) == level && (6u * p + 99) / 100 == level && p == level * 100 / 6);
  }
  puts("Fan: six Home Assistant steps, direct speeds and stable publication against SDK-derived writes passed");
}
"""


class FanSpeedMappingTests(unittest.TestCase):
    def test_native_fan_with_sdk_coupling(self):
        compiler = shutil.which("clang++") or shutil.which("g++")
        self.assertIsNotNone(compiler)
        with tempfile.TemporaryDirectory() as directory:
            folder = pathlib.Path(directory)
            (folder / "Matter.h").write_text(MATTER_FAKE)
            (folder / "esp_matter_feature.h").write_text("#pragma once\n")
            (folder / "test.cpp").write_text(TEST)
            binary = str(folder / "fan_test")
            subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                            "-g", "-I", directory, "-I", str(ROOT / "firmware/WindCalmController"),
                            str(folder / "test.cpp"), "-o", binary], check=True)
            result = subprocess.run([binary], check=True, capture_output=True, text=True)
            print(result.stdout, end="")

    def test_application_publishes_sdk_consistent_percent(self):
        source = (ROOT / "firmware/WindCalmController/Application.cpp").read_text()
        self.assertIn("reportedPercentFor(controller.level())", source)
        self.assertNotIn("percentFor(controller.level())", source.replace("reportedPercentFor", ""))
        self.assertIn("fan.forgetDerivedSpeed()", source)


if __name__ == "__main__":
    unittest.main()
