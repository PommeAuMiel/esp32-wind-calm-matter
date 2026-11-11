// SPDX-License-Identifier: MIT
// Compile the accessory with maintenance confined to this OTA profile.
#include "RuntimeSupervisor.h"
#include "Application.h"
#include <Arduino.h>
#include <atomic>
#include <esp_app_desc.h>
#include "MaintenanceMatter.h"
#include "VerifiedOta.h"
#include <esp_heap_caps.h>
#include <esp_task_wdt.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#if !CONFIG_ENABLE_OTA_REQUESTOR || !CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE || !CONFIG_ESP_TASK_WDT_PANIC
#error "OTA, boot rollback and a resetting task watchdog are required"
#endif
extern "C" bool verifyRollbackLater() { return true; }
namespace windcalm_ota {
namespace {
std::atomic<bool> localImageHealthy{false}, matterVersionMatches{false}, notificationQueued{false};
void notifyApplied(intptr_t) {
  auto *requestor = chip::GetRequestorInstance();
  if (localImageHealthy.load() && matterVersionMatches.load() && requestor &&
      requestor->GetCurrentUpdateState() == chip::OTARequestorInterface::OTAUpdateStateEnum::kApplying)
    requestor->NotifyUpdateApplied();
}
void scheduleNotification() {
  if (localImageHealthy.load() && matterVersionMatches.load() && !notificationQueued.exchange(true)) {
    if (chip::DeviceLayer::PlatformMgr().ScheduleWork(notifyApplied,0) != CHIP_NO_ERROR) notificationQueued.store(false);
  }
}
}
void matterVersionConfirmed() { matterVersionMatches.store(true); scheduleNotification(); }
void bootHealthConfirmed() { localImageHealthy.store(true); scheduleNotification(); }
void retryNotification() { scheduleNotification(); }
}
namespace {
using windcalm_ota::RecoveryReason;
using windcalm_ota::SupervisorState;
using windcalm_ota::RuntimeSupervisor;
windcalm_ota::VerifiedOta verifiedOta;
chip::DeviceLayer::ExtendedOTARequestorDriver gatedDriver;
std::atomic<bool> localStartupComplete{false}, otaIdle{false}, chipProbePending{false};
std::atomic<bool> restartPending{false}, restartCommitting{false};
std::atomic<uint32_t> loopProgress{0}, matterProgress{0}, lastMatterProgressMs{0}, restartRequestedMs{0};
std::atomic<uint32_t> supervisorState{0}, lastRecoveryReason{0}, recoveryBudgetCount{0};
esp_task_wdt_user_handle_t runtimeWatchdog = nullptr;

struct AtomicHealth {
  std::atomic<uint32_t> ready{0}, known{0}, checksumErrors{0}, rejected{0}, recoveries{0};
  std::atomic<uint32_t> reports{0}, timeouts{0}, publicationErrors{0};
  std::atomic<uint32_t> freeHeap{0}, minimumFreeHeap{0}, largestFreeBlock{0};
  std::atomic<bool> lightKnown{false}, lightConfirmed{false}, lightPending{false};
  std::atomic<bool> lightAttributeKnown{false}, lightAttribute{false}, reportSeen{false};
  std::atomic<uint32_t> reportAgeMs{UINT32_MAX}, publicationAttempts{0};
} health;
uint32_t healthReportRevision = 0, healthGeneration = 0;
uint64_t healthReportMs = 0;
struct RecoveryRetention { uint32_t magic, count, reason, checksum; };
RTC_NOINIT_ATTR RecoveryRetention retainedRecovery;
constexpr uint32_t RetentionMagic = 0x57434D32;
uint32_t nowMs() { return uint32_t(esp_timer_get_time() / 1000); }
void retainRecovery(uint32_t count, RecoveryReason reason) {
  retainedRecovery.magic = RetentionMagic;
  retainedRecovery.count = count;
  retainedRecovery.reason = uint32_t(reason);
  retainedRecovery.checksum = RetentionMagic ^ count ^ uint32_t(reason) ^ 0x8A706C51;
  recoveryBudgetCount.store(count); lastRecoveryReason.store(uint32_t(reason));
}
void restoreRecovery() {
  const auto resetReason = esp_reset_reason();
  const bool warmReset = resetReason == ESP_RST_SW || resetReason == ESP_RST_TASK_WDT ||
                         resetReason == ESP_RST_INT_WDT || resetReason == ESP_RST_WDT;
  const bool valid = retainedRecovery.magic == RetentionMagic && retainedRecovery.count <= RuntimeSupervisor::MaxBurstRecoveries &&
    retainedRecovery.reason <= uint32_t(RecoveryReason::ManualRestart) &&
    retainedRecovery.checksum == (RetentionMagic ^ retainedRecovery.count ^ retainedRecovery.reason ^ 0x8A706C51);
  if (!warmReset || !valid) retainRecovery(0, RecoveryReason::None);
  else { recoveryBudgetCount.store(retainedRecovery.count); lastRecoveryReason.store(retainedRecovery.reason); }
}
bool chipResponsive(uint32_t now) { return matterProgress.load() && uint32_t(now - lastMatterProgressMs.load()) <= 10000; }
windcalm_maintenance::Snapshot readHealth() {
  windcalm_maintenance::Snapshot s;
  s.tuyaReady = health.ready.load(); s.knownMask = health.known.load();
  s.uartChecksumErrors = health.checksumErrors.load(); s.uartRejected = health.rejected.load();
  s.tuyaRecoveries = health.recoveries.load(); s.reportRevision = health.reports.load();
  s.commandTimeouts = health.timeouts.load(); s.lightPublicationErrors = health.publicationErrors.load();
  s.loopProgress = loopProgress.load(); s.matterProgress = matterProgress.load();
  s.supervisorState = supervisorState.load(); s.lastRecoveryReason = lastRecoveryReason.load();
  s.recoveryBudgetCount = recoveryBudgetCount.load(); s.restartPending = restartPending.load();
  s.imageHealthy = localStartupComplete.load() && windcalm_ota::localImageHealthy.load();
  s.otaIdle = otaIdle.load() && !restartCommitting.load(); s.chipResponsive = chipResponsive(nowMs());
  s.freeHeap = health.freeHeap.load(); s.minimumFreeHeap = health.minimumFreeHeap.load();
  s.largestFreeBlock = health.largestFreeBlock.load();
  s.lightKnown = health.lightKnown.load(); s.lightConfirmed = health.lightConfirmed.load();
  s.lightPending = health.lightPending.load(); s.lightAttributeKnown = health.lightAttributeKnown.load();
  s.lightAttribute = health.lightAttribute.load(); s.reportSeen = health.reportSeen.load();
  s.reportAgeMs = health.reportAgeMs.load(); s.lightPublicationAttempts = health.publicationAttempts.load();
  return s;
}
bool liveOtaIdle() {
  auto *requestor = chip::GetRequestorInstance();
  return requestor && requestor->GetCurrentUpdateState() == chip::OTARequestorInterface::OTAUpdateStateEnum::kIdle;
}
bool queueRestart() {
  const auto state = readHealth();
  // Called in the CHIP command callback; check the actual state as well as the
  // snapshot so a newly started OTA cannot be hidden by the five-second probe.
  if (!windcalm_maintenance::restartAllowed(state) || !liveOtaIdle()) return false;
  if (restartPending.load()) return false;
  restartRequestedMs.store(nowMs());
  bool expected = false;
  return restartPending.compare_exchange_strong(expected, true);
}
bool maintenanceBegin() {
  auto *root = esp_matter::endpoint::get(esp_matter::node::get(),0);
  auto *general = root ? esp_matter::cluster::get(root,0x0033) : nullptr;
  // The native data provider supplies this optional scalar on read. It is not
  // an application-maintained reason or a fabricated value from the HA cache.
  if (!general || !esp_matter::cluster::general_diagnostics::attribute::create_boot_reason(general,0)) return false;
  return windcalm_maintenance::createBeforeMatterBegin(readHealth, queueRestart);
}
bool allowNewDownload() { return !restartCommitting.load(); }
void chipProbe(intptr_t) {
  lastMatterProgressMs.store(nowMs()); matterProgress.fetch_add(1);
  otaIdle.store(liveOtaIdle());
  windcalm_maintenance::publishSnapshot(nowMs());
  chipProbePending.store(false);
}
void tryRequestProbe() {
  bool expected = false;
  if (chipProbePending.compare_exchange_strong(expected, true) &&
      chip::DeviceLayer::PlatformMgr().ScheduleWork(chipProbe, 0) != CHIP_NO_ERROR) chipProbePending.store(false);
}
void finishRestart(uint32_t now) {
  if (!restartPending.load()) return;
  const uint32_t age = now - restartRequestedMs.load();
  if (age < 2000) return; // Allow the authenticated status response to leave.
  if (age > 30000 || !chipResponsive(now) || !windcalm_ota::localImageHealthy.load()) {
    restartPending.store(false); return;
  }
  // Never wait for a CHIP lock. Recheck at commit, prevent a new downloader
  // from starting, then release the lock before any restart/system operation.
  auto &platform = chip::DeviceLayer::PlatformMgr();
  if (!platform.TryLockChipStack()) return;
  const bool allowed = liveOtaIdle() && localStartupComplete.load();
  if (allowed) restartCommitting.store(true);
  platform.UnlockChipStack();
  if (!allowed) { restartPending.store(false); return; }
  retainRecovery(recoveryBudgetCount.load(), RecoveryReason::ManualRestart);
  esp_restart();
}
void runtimeSupervisor(void *) {
  RuntimeSupervisor policy;
  policy.begin(nowMs(), recoveryBudgetCount.load());
  uint32_t nextProbe = nowMs(); bool recoveryRecorded = false;
  RecoveryRetention previousRetention{};
  while (true) {
    const uint32_t now = nowMs();
    if (localStartupComplete.load() && int32_t(now - nextProbe) >= 0) {
      // Sample outside CHIP lock, never scan the heap in the UART loop.
      health.freeHeap.store(heap_caps_get_free_size(MALLOC_CAP_8BIT));
      health.minimumFreeHeap.store(heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT));
      health.largestFreeBlock.store(heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
      tryRequestProbe(); nextProbe = now + 5000;
    }
    const auto decision = policy.tick(now, loopProgress.load(), matterProgress.load(),
                                     localStartupComplete.load(), !otaIdle.load());
    supervisorState.store(uint32_t(decision.state));
    if (decision.feedWatchdog) {
      if (recoveryRecorded) {
        // A task can recover before watchdog expiry. Do not count a restart
        // that never happened or replace the preceding boot's recovery reason.
        retainedRecovery = previousRetention;
        recoveryBudgetCount.store(previousRetention.count);
        lastRecoveryReason.store(previousRetention.reason);
      }
      if (decision.clearRecoveryBudget) retainRecovery(0, RecoveryReason(lastRecoveryReason.load()));
      if (esp_task_wdt_reset_user(runtimeWatchdog) != ESP_OK) esp_restart();
      recoveryRecorded = false;
      finishRestart(now);
    } else if (!recoveryRecorded) {
      // Retain only bounded reason/count in RTC; no NVS writes or actuator
      // replay. A user watchdog forces reset even when the blocked task yields.
      previousRetention = retainedRecovery;
      retainRecovery(policy.nextRecoveryCount(), decision.reason);
      recoveryRecorded = true;
    }
    vTaskDelay(pdMS_TO_TICKS(100));
  }
}
void startRuntimeSupervisor() {
  restoreRecovery();
  esp_task_wdt_config_t config{};
  config.timeout_ms = 15000; config.idle_core_mask = (1u << portNUM_PROCESSORS) - 1; config.trigger_panic = true;
  if (esp_task_wdt_reconfigure(&config) != ESP_OK ||
      esp_task_wdt_add_user("windcalm-runtime", &runtimeWatchdog) != ESP_OK ||
      xTaskCreate(runtimeSupervisor,"runtime-health",4096,nullptr,2,nullptr) != pdPASS) esp_restart();
}
void bootSupervisor(void *) {
  const auto *running = esp_ota_get_running_partition();
  esp_ota_img_states_t state{};
  if (!running || esp_ota_get_state_partition(running,&state) != ESP_OK) esp_restart();
  if (state == ESP_OTA_IMG_VALID || state == ESP_OTA_IMG_UNDEFINED) {
    windcalm_ota::bootHealthConfirmed(); vTaskDelete(nullptr); return;
  }
  if (state != ESP_OTA_IMG_PENDING_VERIFY) esp_restart();
  const int64_t begin = esp_timer_get_time(); uint32_t previous = loopProgress.load(), successfulSamples = 0;
  while (esp_timer_get_time() - begin < 60000000) {
    vTaskDelay(pdMS_TO_TICKS(100));
    const uint32_t progress = loopProgress.load();
    successfulSamples = progress != previous ? successfulSamples + 1 : 0; previous = progress;
    // Local task progress only, never HA availability or external UART state.
    if (localStartupComplete.load() && successfulSamples >= 100 && matterProgress.load() >= 2 &&
        chipResponsive(nowMs()) && heap_caps_check_integrity_all(false)) {
      if (esp_ota_mark_app_valid_cancel_rollback() == ESP_OK) {
        windcalm_ota::bootHealthConfirmed(); vTaskDelete(nullptr); return;
      }
      break;
    }
  }
  if (esp_ota_mark_app_invalid_rollback_and_reboot() != ESP_OK) esp_restart();
  vTaskDelete(nullptr);
}
}
void setup() {
  startRuntimeSupervisor();
  if (xTaskCreate(bootSupervisor,"ota-health",3072,nullptr,1,nullptr) != pdPASS) esp_restart();
  gatedDriver.SetSendNotifyUpdateApplied(false);
  verifiedOta.setPrepareAllowed(allowNewDownload);
  esp_matter_ota_requestor_impl_t implementation{};
  implementation.image_processor = &verifiedOta; implementation.driver = &gatedDriver;
  esp_matter_ota_config_t config{}; config.impl = &implementation;
  if (esp_matter_ota_requestor_set_config(config) != ESP_OK) esp_restart();
  windcalm::application::begin({esp_app_get_description()->version, maintenanceBegin});
  auto *root = esp_matter::endpoint::get(esp_matter::node::get(),0);
  const bool endpoints = windcalm::application::endpointsMatch();
  const bool clusters = root && esp_matter::cluster::get(root,0x002A) && esp_matter::cluster::get(root,0x0029) &&
    esp_matter::cluster::get(root,windcalm_maintenance::HealthClusterId) && esp_matter::cluster::get(root,0x0025);
  localStartupComplete.store(endpoints && clusters);
}
void loop() {
  windcalm::application::tick();
  const auto state = windcalm::application::snapshot();
  // Only the Arduino loop reads the controller's mutable UART/counter state.
  health.ready.store(state.ready); health.known.store(state.known);
  health.checksumErrors.store(state.checksumErrors); health.rejected.store(state.rejected);
  health.recoveries.store(state.recoveries); health.reports.store(state.reports);
  health.timeouts.store(state.timeouts); health.publicationErrors.store(state.publicationErrors);
  health.lightKnown.store(state.lightKnown);
  health.lightConfirmed.store(state.lightConfirmed); health.lightPending.store(state.lightPending);
  health.lightAttributeKnown.store(state.lightAttributeKnown); health.lightAttribute.store(state.lightAttribute);
  health.publicationAttempts.store(state.publicationAttempts);
  const uint64_t timeMs = uint64_t(esp_timer_get_time()) / 1000;
  if (healthGeneration != state.generation) {
    healthGeneration = state.generation; health.reportSeen.store(false);
  }
  if (healthReportRevision != state.reports) {
    healthReportRevision = state.reports; healthReportMs = timeMs; health.reportSeen.store(true);
  }
  const uint64_t age = health.reportSeen.load() ? timeMs - healthReportMs : UINT32_MAX;
  health.reportAgeMs.store(age > UINT32_MAX ? UINT32_MAX : uint32_t(age));
  loopProgress.fetch_add(1); windcalm_ota::retryNotification();
}
