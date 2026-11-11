#pragma once
#include "OtaPolicy.h"
#include <algorithm>
#include <app/clusters/ota-requestor/OTARequestorInterface.h>
#include <crypto/CHIPCryptoPAL.h>
#include <esp_app_format.h>
#include <esp_image_format.h>
#include <esp_matter_ota.h>
#include <esp_ota_ops.h>
#include <lib/core/OTAImageHeader.h>
#include <platform/DeviceInstanceInfoProvider.h>
#include <platform/ESP32/OTAImageProcessorImpl.h>

// ESP-Matter wires this singleton to its BDX downloader at startup. A separate
// native processor would have no downloader. Keep native flash/reboot behavior.
extern chip::OTAImageProcessorImpl gImageProcessor;
namespace windcalm_ota {
void matterVersionConfirmed();
class VerifiedOta final : public chip::OTAImageProcessorInterface {
 public:
  void setPrepareAllowed(bool (*allowed)()) { prepareAllowed = allowed; }
  CHIP_ERROR PrepareDownload() override {
    reset();
    if (prepareAllowed && !prepareAllowed()) return guard(CHIP_ERROR_INCORRECT_STATE);
    return guard(prepare());
  }
  CHIP_ERROR ProcessBlock(chip::ByteSpan &block) override {
    return failed ? CHIP_ERROR_INCORRECT_STATE : guard(process(block));
  }
  CHIP_ERROR Finalize() override {
    return failed ? CHIP_ERROR_INCORRECT_STATE : guard(finalize());
  }
  CHIP_ERROR Apply() override {
    return failed ? CHIP_ERROR_INCORRECT_STATE : guard(apply());
  }
  CHIP_ERROR Abort() override {
    reset();
    return guard(gImageProcessor.Abort());
  }
  bool IsFirstImageRun() override { return gImageProcessor.IsFirstImageRun(); }
  CHIP_ERROR ConfirmCurrentImage() override {
    const CHIP_ERROR result = gImageProcessor.ConfirmCurrentImage();
    if (result == CHIP_NO_ERROR) matterVersionConfirmed();
    return result;
  }
  // Native ESP32 counters accumulate across interrupted downloads; our inherited
  // mParams is reset per attempt and counts only accepted payload bytes.
 private:
  struct Header {
    char version[32]{}; uint8_t digest[chip::Crypto::kSHA256_Hash_Length]{};
    void clear() { memset(version,0,sizeof(version)); memset(digest,0,sizeof(digest)); }
  } header;
  ImagePolicy policy;
  chip::OTAImageHeaderParser parser;
  chip::Crypto::Hash_SHA256_stream hash;
  bool failed = false, cancelQueued = false;
  bool (*prepareAllowed)() = nullptr;

  void reset() {
    failed = cancelQueued = false; policy.reset(); parser.Clear(); header.clear(); mParams = {};
  }
  CHIP_ERROR guard(CHIP_ERROR result) {
    if (result != CHIP_NO_ERROR) {
      failed = true; policy.reject();
      if (!cancelQueued) {
        // SDK BDX's simple error return does not release/reset the requestor.
        // Cancel after unwinding this callback; EndDownload alone uses reason
        // Success and does not reset requestor state. A full queue leaves the
        // latch closed and the native bounded transfer timeout as fallback.
        cancelQueued = true;
        if (chip::DeviceLayer::PlatformMgr().ScheduleWork(cancelFailedDownload,reinterpret_cast<intptr_t>(this)) != CHIP_NO_ERROR)
          cancelQueued = false;
      }
    }
    return result;
  }
  static void cancelFailedDownload(intptr_t context) {
    auto *self = reinterpret_cast<VerifiedOta *>(context);
    if (!self || !self->failed || !self->cancelQueued) return;
    self->cancelQueued = false;
    if (auto *requestor = chip::GetRequestorInstance()) requestor->CancelImageUpdate();
  }
  CHIP_ERROR prepare() {
    parser.Init();
    ReturnErrorOnFailure(hash.Begin());
    return gImageProcessor.PrepareDownload();
  }
  CHIP_ERROR process(chip::ByteSpan &original) {
    chip::ByteSpan payload = original;
    if (parser.IsInitialized()) {
      chip::OTAImageHeader image;
      CHIP_ERROR error = parser.AccumulateAndDecode(payload,image);
      if (error == CHIP_ERROR_BUFFER_TOO_SMALL) return gImageProcessor.ProcessBlock(original);
      ReturnErrorOnFailure(error);
      uint16_t vendor = 0, product = 0; uint32_t version = 0;
      ReturnErrorOnFailure(chip::DeviceLayer::GetDeviceInstanceInfoProvider()->GetVendorId(vendor));
      ReturnErrorOnFailure(chip::DeviceLayer::GetDeviceInstanceInfoProvider()->GetProductId(product));
      ReturnErrorOnFailure(chip::DeviceLayer::ConfigurationMgr().GetSoftwareVersion(version));
      auto *requestor = chip::GetRequestorInstance();
      const auto *slot = esp_ota_get_next_update_partition(nullptr);
      VerifyOrReturnError(requestor && slot && slot != esp_ota_get_running_partition(), CHIP_ERROR_INCORRECT_STATE);
      const ImageMetadata metadata{image.mVendorId,image.mProductId,image.mSoftwareVersion,
        image.mMinApplicableVersion.ValueOr(0),image.mMaxApplicableVersion.ValueOr(0),image.mPayloadSize,
        image.mMinApplicableVersion.HasValue(),image.mMaxApplicableVersion.HasValue(),
        image.mImageDigestType == chip::OTAImageDigestType::kSha256,image.mImageDigest.data(),image.mImageDigest.size()};
      const DeviceMetadata device{vendor,product,version,requestor->GetTargetVersion(),slot->size};
      VerifyOrReturnError(policy.accept(metadata,device), CHIP_ERROR_INVALID_ARGUMENT);
      VerifyOrReturnError(image.mSoftwareVersionString.size() && image.mSoftwareVersionString.size() < sizeof(header.version), CHIP_ERROR_INVALID_ARGUMENT);
      memcpy(header.version,image.mSoftwareVersionString.data(),image.mSoftwareVersionString.size());
      header.version[image.mSoftwareVersionString.size()] = 0;
      memcpy(header.digest,image.mImageDigest.data(),sizeof(header.digest));
      mParams.totalFileBytes = image.mPayloadSize;
      parser.Clear();
    }
    VerifyOrReturnError(policy.append(payload.size()), CHIP_ERROR_INVALID_ARGUMENT);
    ReturnErrorOnFailure(hash.AddData(payload));
    // Native parser must see the original complete stream, not our subspan.
    ReturnErrorOnFailure(gImageProcessor.ProcessBlock(original));
    mParams.downloadedBytes += payload.size();
    return CHIP_NO_ERROR;
  }
  CHIP_ERROR finalize() {
    uint8_t bytes[chip::Crypto::kSHA256_Hash_Length]; chip::MutableByteSpan digest(bytes);
    ReturnErrorOnFailure(hash.Finish(digest));
    VerifyOrReturnError(policy.finish(digest.data(),digest.size()), CHIP_ERROR_INTEGRITY_CHECK_FAILED);
    return gImageProcessor.Finalize();
  }
  CHIP_ERROR apply() {
    VerifyOrReturnError(policy.mayApply(), CHIP_ERROR_INCORRECT_STATE);
    const auto *slot = esp_ota_get_next_update_partition(nullptr);
    VerifyOrReturnError(slot, CHIP_ERROR_INCORRECT_STATE);
    // Native Finalize is scheduled. Independently verify the completed ESP
    // image before allowing the native processor to change the boot selector.
    esp_partition_pos_t position{slot->address,slot->size}; esp_image_metadata_t image{};
    VerifyOrReturnError(esp_image_verify(ESP_IMAGE_VERIFY,&position,&image) == ESP_OK, CHIP_ERROR_INTEGRITY_CHECK_FAILED);
    esp_app_desc_t description{};
    VerifyOrReturnError(esp_ota_get_partition_description(slot,&description) == ESP_OK &&
      strncmp(description.version,header.version,sizeof(description.version)) == 0 &&
      strncmp(description.project_name,"windcalm-thread-ota",sizeof(description.project_name)) == 0,
      CHIP_ERROR_INVALID_ARGUMENT);
    ReturnErrorOnFailure(verifyStoredPayload(slot));
    return gImageProcessor.Apply();
  }
  CHIP_ERROR verifyStoredPayload(const esp_partition_t *slot) {
    // Native queue submissions return success even when a queue is full. Bind
    // the actual inactive flash bytes to this download, so an old valid image
    // with the same version/project cannot be applied accidentally.
    VerifyOrReturnError(mParams.totalFileBytes && mParams.totalFileBytes <= slot->size, CHIP_ERROR_INVALID_ARGUMENT);
    chip::Crypto::Hash_SHA256_stream flashHash;
    ReturnErrorOnFailure(flashHash.Begin());
    uint8_t buffer[512];
    for (size_t offset=0; offset<mParams.totalFileBytes;) {
      const size_t bytes = std::min(sizeof(buffer),size_t(mParams.totalFileBytes-offset));
      VerifyOrReturnError(esp_partition_read(slot,offset,buffer,bytes) == ESP_OK, CHIP_ERROR_READ_FAILED);
      ReturnErrorOnFailure(flashHash.AddData(chip::ByteSpan(buffer,bytes)));
      offset += bytes;
    }
    uint8_t bytes[chip::Crypto::kSHA256_Hash_Length]; chip::MutableByteSpan digest(bytes);
    ReturnErrorOnFailure(flashHash.Finish(digest));
    uint8_t different=0;
    for (size_t i=0;i<sizeof(header.digest);++i) different |= bytes[i]^header.digest[i];
    VerifyOrReturnError(!different,CHIP_ERROR_INTEGRITY_CHECK_FAILED);
    return CHIP_NO_ERROR;
  }
};
} // namespace windcalm_ota
