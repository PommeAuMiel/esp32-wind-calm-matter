#pragma once
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace windcalm_ota {
// Transport authenticity belongs to Matter CASE and its fabric ACL. This policy
// checks compatibility, exact payload length and integrity, never a signature.
struct ImageMetadata {
  uint16_t vendor, product;
  uint32_t version, minimum, maximum;
  uint64_t bytes;
  bool hasMinimum, hasMaximum, sha256;
  const uint8_t *digest;
  size_t digestBytes;
};
struct DeviceMetadata {
  uint16_t vendor, product;
  uint32_t currentVersion, targetVersion;
  uint64_t slotBytes;
};
class ImagePolicy {
 public:
  void reset() { headerAccepted = complete = failed = false; payload = received = 0; }
  bool accept(const ImageMetadata &image, const DeviceMetadata &device) {
    if (headerAccepted || failed || image.vendor != device.vendor || image.product != device.product ||
        image.version <= device.currentVersion || image.version != device.targetVersion ||
        (image.hasMinimum && device.currentVersion < image.minimum) ||
        (image.hasMaximum && device.currentVersion > image.maximum) ||
        !image.bytes || image.bytes > device.slotBytes || !image.sha256 ||
        !image.digest || image.digestBytes != sizeof(expected)) return reject();
    memcpy(expected, image.digest, sizeof(expected)); payload = image.bytes; headerAccepted = true;
    return true;
  }
  bool append(size_t bytes) {
    if (!headerAccepted || complete || failed || uint64_t(bytes) > payload - received) return reject();
    received += bytes; return true;
  }
  bool finish(const uint8_t *actual, size_t bytes) {
    if (!headerAccepted || complete || failed || received != payload || !actual || bytes != sizeof(expected)) return reject();
    uint8_t difference = 0;
    for (size_t i=0; i<sizeof(expected); ++i) difference |= expected[i] ^ actual[i];
    if (difference) return reject();
    complete = true; return true;
  }
  bool mayApply() const { return complete && !failed; }
  bool reject() { failed = true; complete = false; return false; }
 private:
  bool headerAccepted = false, complete = false, failed = false;
  uint64_t payload = 0, received = 0;
  uint8_t expected[32]{};
};
} // namespace windcalm_ota
