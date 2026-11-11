#pragma once
#include <Matter.h>
#include <app/clusters/mode-select-server/supported-modes-manager.h>

// Three observed CCT presets, without claiming unmeasured Kelvin values or dimming.
// Keep the existing OnOffLight endpoint and add the standard ModeSelect cluster.
class NativeLight : public MatterOnOffLight,
                    public chip::app::Clusters::ModeSelect::SupportedModesManager {
 public:
  using ModeOption = chip::app::Clusters::ModeSelect::Structs::ModeOptionStruct::Type;
  using ModeCallback = std::function<bool(uint16_t)>;
  bool begin(bool initial = false) override {
    if (!MatterOnOffLight::begin(initial)) return false;
    modeEndpoint_ = getEndPointId();
    static const char *labels[] = {"Teinte 1", "Teinte 2", "Teinte 3"};
    for (uint8_t i = 0; i < 3; ++i) {
      options_[i].label = chip::CharSpan::fromCharString(labels[i]);
      options_[i].mode = i;
    }
    esp_matter::cluster::mode_select::config_t config;
    strcpy(config.description, "Teinte lumiere");
    config.current_mode = 1;
    config.delegate = static_cast<SupportedModesManager *>(this);
    auto *ep = esp_matter::endpoint::get(esp_matter::node::get(), getEndPointId());
    return esp_matter::cluster::mode_select::create(ep, &config, CLUSTER_FLAG_SERVER) != nullptr;
  }
  ModeOptionsProvider getModeOptionsProvider(chip::EndpointId ep) const override {
    return ep == modeEndpoint_ ? ModeOptionsProvider(options_, options_ + 3) : ModeOptionsProvider();
  }
  chip::Protocols::InteractionModel::Status getModeOptionByMode(
      chip::EndpointId ep, uint8_t mode, const ModeOption **result) const override {
    if (ep != modeEndpoint_ || mode > 2 || !result)
      return chip::Protocols::InteractionModel::Status::InvalidValue;
    *result = &options_[mode];
    return chip::Protocols::InteractionModel::Status::Success;
  }
  void onTemperature(ModeCallback cb) { callback_ = cb; }
  bool attributeChangeCB(uint16_t ep, uint32_t cluster, uint32_t attr,
                         esp_matter_attr_val_t *value) override {
    using namespace chip::app::Clusters;
    if (ep == getEndPointId() && cluster == ModeSelect::Id && attr == ModeSelect::Attributes::CurrentMode::Id) {
      return value->val.u8 <= 2 && (reporting_ || (callback_ && callback_(uint16_t(value->val.u8) * 500)));
    }
    return MatterOnOffLight::attributeChangeCB(ep, cluster, attr, value);
  }
  // Call under the CHIP lock. Read the database, independently of Arduino's cache.
  bool readOnOffAttribute(bool &state) {
    if (!started) return false;
    using namespace chip::app::Clusters;
    esp_matter_attr_val_t value;
    if (!getAttributeVal(OnOff::Id, OnOff::Attributes::OnOff::Id, &value) ||
        value.type != ESP_MATTER_VAL_TYPE_BOOLEAN) return false;
    state = value.val.b;
    return true;
  }
  // A confirmed MCU report must never invoke the network command callbacks.
  // Do not let a failed publication change the cache and suppress a later retry.
  bool reportOnOff(bool state) {
    bool previous;
    if (!readOnOffAttribute(previous)) return false;
    using namespace chip::app::Clusters;
    if (previous != state) {
      auto next = esp_matter_bool(state);
      if (esp_matter::attribute::report(getEndPointId(), OnOff::Id,
                                      OnOff::Attributes::OnOff::Id, &next) != ESP_OK) return false;
    }
    onOffState = state;
    return true;
  }
  bool reportTemperature(uint16_t value) {
    if (value != 0 && value != 500 && value != 1000) return false;
    using namespace chip::app::Clusters;
    esp_matter_attr_val_t previous;
    if (!getAttributeVal(ModeSelect::Id, ModeSelect::Attributes::CurrentMode::Id, &previous)) return false;
    if (previous.val.u8 == value / 500) return true;
    auto next = esp_matter_uint8(uint8_t(value / 500));
    reporting_ = true;
    const bool ok = updateAttributeVal(ModeSelect::Id, ModeSelect::Attributes::CurrentMode::Id, &next);
    reporting_ = false;
    return ok;
  }
 private:
  uint16_t modeEndpoint_ = 0;
  ModeOption options_[3]{};
  ModeCallback callback_;
  bool reporting_ = false;
};
