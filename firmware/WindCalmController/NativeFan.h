#pragma once
#include <Matter.h>
#include <esp_matter_feature.h>
#include "Controller.h"

// Extend the installed Arduino wrapper through the public ESP-Matter APIs.
// No SDK edits. Same endpoint 2 and native FanControl features.
class NativeFan : public MatterFan {
 public:
  using DirectionCallback = std::function<bool(uint8_t)>;
  uint32_t declaredFeatures = 0;
  bool begin(uint8_t percent = 0, FanMode_t mode = FAN_MODE_OFF,
             FanModeSequence_t sequence = FAN_MODE_SEQ_OFF_HIGH) override {
    if (!MatterFan::begin(percent, mode, sequence)) return false;
    auto *cluster = esp_matter::cluster::get(getEndPointId(), chip::app::Clusters::FanControl::Id);
    if (!cluster) return false;
    namespace feature = esp_matter::cluster::fan_control::feature;
    feature::multi_speed::config_t speeds;
    speeds.speed_max = 6; speeds.speed_setting = uint8_t(0); speeds.speed_current = 0;
    if (feature::multi_speed::add(cluster, &speeds) != ESP_OK) return false;
    feature::airflow_direction::config_t direction;
    direction.airflow_direction = 0;
    if (feature::airflow_direction::add(cluster, &direction) != ESP_OK) return false;
    using namespace chip::app::Clusters;
    esp_matter_attr_val_t features, maximum;
    if (!getAttributeVal(FanControl::Id,FanControl::Attributes::FeatureMap::Id,&features) ||
        !getAttributeVal(FanControl::Id,FanControl::Attributes::SpeedMax::Id,&maximum)) return false;
    declaredFeatures = features.val.u32;
    const uint32_t required = feature::multi_speed::get_id() | feature::airflow_direction::get_id();
    return maximum.val.u8 == 6 && (declaredFeatures & required) == required;
  }
  void onDirection(DirectionCallback callback) { directionCallback_ = callback; }
  bool attributeChangeCB(uint16_t ep, uint32_t cluster, uint32_t attribute,
                         esp_matter_attr_val_t *value) override {
    using namespace chip::app::Clusters;
    if (ep == getEndPointId() && cluster == FanControl::Id) {
      if (attribute == FanControl::Attributes::AirflowDirection::Id) {
        if (value->val.u8 > 1) return false;
        return reporting_ || (directionCallback_ && directionCallback_(value->val.u8));
      }
      if (attribute == FanControl::Attributes::SpeedSetting::Id) {
        // No Auto/null speed in this profile; nullable uint8 null is 255.
        if (value->val.u8 > 6) return false;
        return reporting_ || (_onChangeSpeedCB &&
               _onChangeSpeedCB(windcalm::Controller::percentFor(value->val.u8)));
      }
    }
    return MatterFan::attributeChangeCB(ep, cluster, attribute, value);
  }
  // Called only under CHIP lock and only with state reported by the MCU.
  bool reportDetails(uint8_t level, uint8_t direction, bool speedPending, bool directionPending) {
    using namespace chip::app::Clusters;
    bool ok = true; reporting_ = true;
    if (!speedPending) {
      ok &= updateIfDifferent(FanControl::Attributes::SpeedSetting::Id, level, true);
      ok &= updateIfDifferent(FanControl::Attributes::SpeedCurrent::Id, level, false);
    }
    if (!directionPending) ok &= updateIfDifferent(FanControl::Attributes::AirflowDirection::Id, direction, false, true);
    reporting_ = false; return ok;
  }
 private:
  bool reporting_ = false;
  DirectionCallback directionCallback_;
  bool updateIfDifferent(uint32_t attribute, uint8_t value, bool nullable, bool enumeration = false) {
    const uint32_t cluster = chip::app::Clusters::FanControl::Id;
    esp_matter_attr_val_t previous;
    if (!getAttributeVal(cluster, attribute, &previous)) return false;
    if (previous.val.u8 == value) return true;
    auto next = nullable ? esp_matter_nullable_uint8(value) :
                enumeration ? esp_matter_enum8(value) : esp_matter_uint8(value);
    return updateAttributeVal(cluster, attribute, &next);
  }
};
