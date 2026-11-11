"""Compile the real NativeLight against a small, fault-injecting SDK boundary.

No serial port or radio is opened. The fake attribute::update invokes both
command callbacks, while attribute::report follows the SDK's callback-free API.
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
#include <cstring>
#include <functional>
constexpr int ESP_OK = 0, ESP_FAIL = -1, CLUSTER_FLAG_SERVER = 1;
constexpr int ESP_MATTER_VAL_TYPE_BOOLEAN = 1, ESP_MATTER_VAL_TYPE_UINT8 = 2;
struct esp_matter_attr_val_t { int type; union { bool b; uint8_t u8; } val; };
inline esp_matter_attr_val_t esp_matter_bool(bool v) {
  esp_matter_attr_val_t result{}; result.type = ESP_MATTER_VAL_TYPE_BOOLEAN; result.val.b = v; return result;
}
inline esp_matter_attr_val_t esp_matter_uint8(uint8_t v) {
  esp_matter_attr_val_t result{}; result.type = ESP_MATTER_VAL_TYPE_UINT8; result.val.u8 = v; return result;
}
namespace chip {
using EndpointId = uint16_t;
struct CharSpan { const char *text = nullptr; static CharSpan fromCharString(const char *s) { return {s}; } };
namespace Protocols::InteractionModel { enum class Status { Success, InvalidValue }; }
namespace app::Clusters {
namespace OnOff { constexpr uint32_t Id = 6; namespace Attributes::OnOff { constexpr uint32_t Id = 0; } }
namespace ModeSelect {
constexpr uint32_t Id = 0x50;
namespace Attributes::CurrentMode { constexpr uint32_t Id = 0; }
namespace Structs::ModeOptionStruct { struct Type { CharSpan label; uint8_t mode = 0; }; }
}
}
}
class MatterOnOffLight;
namespace fake {
inline MatterOnOffLight *endpoint = nullptr;
inline esp_matter_attr_val_t on_off = esp_matter_bool(false);
inline esp_matter_attr_val_t mode = esp_matter_uint8(1);
inline unsigned reads = 0, reports = 0, updates = 0, dirty = 0;
inline bool fail_get = false, fail_report = false;
}
namespace esp_matter {
struct endpoint_t {};
namespace node { inline void *get() { return nullptr; } }
namespace endpoint { inline endpoint_t *get(void *, uint16_t) { static endpoint_t ep; return &ep; } }
namespace cluster::mode_select {
struct config_t { char description[40]{}; uint8_t current_mode = 0; void *delegate = nullptr; };
inline void *create(endpoint_t *ep, config_t *, int) { return ep; }
}
namespace attribute {
inline int report(uint16_t, uint32_t cluster, uint32_t attr, esp_matter_attr_val_t *value) {
  ++fake::reports;
  if (fake::fail_report || cluster != chip::app::Clusters::OnOff::Id ||
      attr != chip::app::Clusters::OnOff::Attributes::OnOff::Id) return ESP_FAIL;
  fake::on_off = *value; ++fake::dirty; return ESP_OK;
}
int update(uint16_t ep, uint32_t cluster, uint32_t attr, esp_matter_attr_val_t *value);
}
}
class MatterOnOffLight {
 public:
  using EndPointCB = std::function<bool(bool)>;
  virtual ~MatterOnOffLight() = default;
  virtual bool begin(bool initial = false) {
    started = true; onOffState = initial; fake::on_off = esp_matter_bool(initial); fake::endpoint = this; return true;
  }
  uint16_t getEndPointId() { return started ? 1 : 0; }
  void onChange(EndPointCB cb) { _onChangeCB = cb; }
  void onChangeOnOff(EndPointCB cb) { _onChangeOnOffCB = cb; }
  bool getOnOff() { return onOffState; }
  virtual bool attributeChangeCB(uint16_t ep, uint32_t cluster, uint32_t attr, esp_matter_attr_val_t *value) {
    if (!started) return false;
    if (ep != 1 || cluster != chip::app::Clusters::OnOff::Id ||
        attr != chip::app::Clusters::OnOff::Attributes::OnOff::Id) return true;
    bool ok = true;
    if (_onChangeCB) ok &= _onChangeCB(value->val.b);
    if (_onChangeOnOffCB) ok &= _onChangeOnOffCB(value->val.b);
    if (ok) onOffState = value->val.b;
    return ok;
  }
  bool getAttributeVal(uint32_t cluster, uint32_t attr, esp_matter_attr_val_t *value) {
    ++fake::reads;
    if (fake::fail_get || attr != 0) return false;
    *value = cluster == chip::app::Clusters::OnOff::Id ? fake::on_off : fake::mode;
    return true;
  }
  bool updateAttributeVal(uint32_t cluster, uint32_t attr, esp_matter_attr_val_t *value) {
    return esp_matter::attribute::update(getEndPointId(), cluster, attr, value) == ESP_OK;
  }
 protected:
  bool started = false, onOffState = false;
  EndPointCB _onChangeCB, _onChangeOnOffCB;
};
inline int esp_matter::attribute::update(uint16_t ep, uint32_t cluster, uint32_t attr, esp_matter_attr_val_t *value) {
  ++fake::updates;
  if (!fake::endpoint || !fake::endpoint->attributeChangeCB(ep, cluster, attr, value)) return ESP_FAIL;
  if (cluster == chip::app::Clusters::OnOff::Id) fake::on_off = *value;
  else fake::mode = *value;
  ++fake::dirty; return ESP_OK;
}
"""

MODES_FAKE = r"""
#pragma once
#include <Matter.h>
namespace chip::app::Clusters::ModeSelect {
class SupportedModesManager {
 public:
  using ModeOption = Structs::ModeOptionStruct::Type;
  struct ModeOptionsProvider {
    ModeOptionsProvider() = default;
    ModeOptionsProvider(const ModeOption *, const ModeOption *) {}
  };
  virtual ~SupportedModesManager() = default;
  virtual ModeOptionsProvider getModeOptionsProvider(EndpointId) const = 0;
  virtual Protocols::InteractionModel::Status getModeOptionByMode(EndpointId, uint8_t, const ModeOption **) const = 0;
};
}
"""

SCENARIOS = r"""
#include "NativeLight.h"
#include <cassert>
#include <string>
class Probe : public NativeLight { public: void cache(bool value) { onOffState = value; } };
int main(int argc, char **argv) {
  assert(argc == 2);
  const std::string scenario = argv[1];
  Probe light;
  unsigned general_commands = 0, onoff_commands = 0;
  light.onChange([&](bool) { ++general_commands; return true; });
  light.onChangeOnOff([&](bool) { ++onoff_commands; return true; });
  if (scenario == "not_started") {
    bool readback = true;
    assert(!light.readOnOffAttribute(readback) && readback);
    assert(!light.reportOnOff(false));
    assert(fake::reads == 0 && fake::reports == 0 && fake::updates == 0);
    return 0;
  }
  assert(light.begin(true));
  if (scenario == "get_failure") {
    fake::fail_get = true;
    bool readback = false;
    assert(!light.readOnOffAttribute(readback) && !readback);
    assert(!light.reportOnOff(false));
    assert(light.getOnOff() && fake::on_off.val.b && fake::reports == 0);
    fake::fail_get = false;
    assert(light.reportOnOff(false));
    assert(!light.getOnOff() && !fake::on_off.val.b && fake::dirty == 1);
  } else if (scenario == "report_failure_retry") {
    fake::fail_report = true;
    assert(!light.reportOnOff(false));
    assert(light.getOnOff() && fake::on_off.val.b && fake::dirty == 0);
    assert(!light.reportOnOff(false));
    assert(light.getOnOff() && fake::on_off.val.b && fake::dirty == 0);
    fake::fail_report = false;
    assert(light.reportOnOff(false));
    assert(!light.getOnOff() && !fake::on_off.val.b && fake::reports == 3 && fake::dirty == 1);
  } else if (scenario == "cache_matches_attribute_diverges") {
    light.cache(false); // A previous failed SDK publication can leave this cache.
    assert(fake::on_off.val.b && !light.getOnOff());
    assert(light.reportOnOff(false));
    assert(!fake::on_off.val.b && !light.getOnOff() && fake::reports == 1 && fake::dirty == 1);
  } else if (scenario == "attribute_matches_cache_diverges") {
    fake::on_off = esp_matter_bool(false);
    assert(light.getOnOff());
    bool readback = true;
    assert(light.readOnOffAttribute(readback) && !readback && light.getOnOff());
    assert(light.reportOnOff(false));
    assert(!light.getOnOff() && fake::reports == 0 && fake::dirty == 0);
  } else if (scenario == "invalid_type") {
    fake::on_off = esp_matter_uint8(0);
    bool readback = true;
    assert(!light.readOnOffAttribute(readback) && readback);
    assert(!light.reportOnOff(false));
    assert(light.getOnOff() && fake::reports == 0 && fake::dirty == 0);
  } else if (scenario == "ha_on_remote_off_no_echo") {
    assert(light.reportOnOff(false));
    auto ha_on = esp_matter_bool(true);
    assert(esp_matter::attribute::update(1, chip::app::Clusters::OnOff::Id, 0, &ha_on) == ESP_OK);
    assert(light.getOnOff() && fake::on_off.val.b);
    assert(general_commands == 1 && onoff_commands == 1);
    assert(light.reportOnOff(false)); // Confirmed RF report, not another command.
    assert(!light.getOnOff() && !fake::on_off.val.b);
    for (unsigned repeat = 0; repeat < 1000; ++repeat) assert(light.reportOnOff(false));
    assert(fake::reports == 2 && fake::updates == 1 && fake::dirty == 3);
    assert(general_commands == 1 && onoff_commands == 1);
    return 0;
  } else {
    assert(false && "unknown scenario");
  }
  assert(general_commands == 0 && onoff_commands == 0 && fake::updates == 0);
}
"""


class LightPublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("clang++") or shutil.which("g++")
        if not compiler:
            raise RuntimeError("A host C++ compiler is required")
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        temp = pathlib.Path(cls.directory.name)
        (temp / "Matter.h").write_text(MATTER_FAKE)
        modes = temp / "app/clusters/mode-select-server/supported-modes-manager.h"
        modes.parent.mkdir(parents=True)
        modes.write_text(MODES_FAKE)
        source = temp / "light_publication.cpp"
        source.write_text(SCENARIOS)
        cls.binary = temp / "light_publication"
        subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                        "-fsanitize=address,undefined", "-g", "-I", str(temp),
                        "-I", str(ROOT / "firmware/WindCalmController"), str(source),
                        "-o", str(cls.binary)], check=True)

    def run_scenario(self, scenario):
        subprocess.run([str(self.binary), scenario], check=True, capture_output=True, text=True)

    def test_not_started(self):
        self.run_scenario("not_started")

    def test_get_failure_preserves_cache_and_retries(self):
        self.run_scenario("get_failure")

    def test_report_failure_preserves_cache_and_retries(self):
        self.run_scenario("report_failure_retry")

    def test_matching_cache_does_not_hide_wrong_attribute(self):
        self.run_scenario("cache_matches_attribute_diverges")

    def test_matching_attribute_repairs_cache_without_reporting(self):
        self.run_scenario("attribute_matches_cache_diverges")

    def test_wrong_attribute_type_is_rejected(self):
        self.run_scenario("invalid_type")

    def test_ha_on_then_remote_off_and_repeats_never_echo(self):
        self.run_scenario("ha_on_remote_off_no_echo")


if __name__ == "__main__":
    unittest.main()
