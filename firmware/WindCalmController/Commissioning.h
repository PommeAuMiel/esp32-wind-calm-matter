#pragma once
#include <Matter.h>
#include <Preferences.h>
#include <bootloader_random.h>
#include <esp_random.h>
#include <platform/CommissionableDataProvider.h>
#include <platform/CHIPDeviceLayer.h>
#include <app/server/Server.h>
#include "Security.h"

// Must run BEFORE Matter/Thread/BLE/ADC initialization. Entropy source is
// always disabled again before starting radio services. Never print the PIN.
inline bool configureDeviceCommissioning(windcalm::CommissioningCredentials &expected) {
  Preferences storage;
  if (!storage.begin("wc-security", false)) return false;
  windcalm::CommissioningCredentials credentials;
  if (storage.isKey("commission")) {
    if (storage.getBytesLength("commission") != sizeof(credentials) ||
        storage.getBytes("commission", &credentials, sizeof(credentials)) != sizeof(credentials) ||
        !windcalm::validCredentials(credentials)) {
      storage.end(); return false; // Corruption must not silently reset the PIN.
    }
  } else {
    bool generated = false;
    bootloader_random_enable();
    for (unsigned attempt = 0; attempt < 4096 && !generated; ++attempt) {
      uint32_t words[2]; esp_fill_random(words, sizeof(words));
      generated = windcalm::credentialsFromWords(words[0], words[1], credentials);
    }
    bootloader_random_disable();
    if (!generated || storage.putBytes("commission", &credentials, sizeof(credentials)) != sizeof(credentials)) {
      storage.end(); return false;
    }
    // One blob/one commit; verify persistence before advertising this credential.
    windcalm::CommissioningCredentials readback;
    if (storage.getBytes("commission", &readback, sizeof(readback)) != sizeof(readback) ||
        !windcalm::validCredentials(readback) || readback.passcode != credentials.passcode ||
        readback.discriminator != credentials.discriminator) {
      storage.end(); return false;
    }
  }
  storage.end();
  expected = credentials;
  return Matter.setSetupPasscode(credentials.passcode) &&
         Matter.setSetupDiscriminator(credentials.discriminator) &&
         Matter.setBLECommissioningEnabled(true) && Matter.setBLEMemoryReleaseEnabled(true);
}

// Arduino applies its provider override while starting the stack. Verify the
// effective provider afterward; close commissioning if the SDK kept defaults.
inline bool verifyDeviceCommissioning(const windcalm::CommissioningCredentials &expected) {
  chip::DeviceLayer::PlatformMgr().LockChipStack();
  auto *provider = chip::DeviceLayer::GetCommissionableDataProvider();
  uint32_t pin = 0; uint16_t discriminator = 0;
  const bool ok = provider && provider->GetSetupPasscode(pin) == CHIP_NO_ERROR &&
      provider->GetSetupDiscriminator(discriminator) == CHIP_NO_ERROR &&
      pin == expected.passcode && discriminator == expected.discriminator;
  if (!ok) chip::Server::GetInstance().GetCommissioningWindowManager().CloseCommissioningWindow();
  chip::DeviceLayer::PlatformMgr().UnlockChipStack();
  return ok;
}
