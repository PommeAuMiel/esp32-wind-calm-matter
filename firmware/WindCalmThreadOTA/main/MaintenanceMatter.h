// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Wind Calm contributors
#pragma once
#include "MaintenancePolicy.h"
#include <esp_matter.h>
#include <access/AccessControl.h>
#include <app/AttributeAccessInterface.h>
#include <app/AttributeAccessInterfaceRegistry.h>
#include <app/CommandHandler.h>
#include <app/EventLogging.h>
#include <app/data-model/Decode.h>
#include <app/reporting/reporting.h>
#include <clusters/Actions/Attributes.h>
#include <clusters/Actions/Commands.h>
#include <clusters/Actions/Events.h>

namespace windcalm_maintenance {
using ReadSnapshot = Snapshot (*)();
using QueueRestart = bool (*)();
using Status = chip::Protocols::InteractionModel::Status;
namespace Actions = chip::app::Clusters::Actions;
inline ReadSnapshot readSnapshot = nullptr;
inline QueueRestart queueRestart = nullptr;
inline Snapshot published{};
inline HealthReportCadence reportCadence;
inline bool initialized = false;
inline chip::Optional<uint32_t> restartInvokeId;

// Only CHIP-thread functions below access published, the access registry and
// event logging. The reader supplied by main.cpp only loads atomics; its queue
// callback must be bounded and must never restart, sleep or take a CHIP lock.
inline Snapshot currentSnapshot() { return readSnapshot ? readSnapshot() : Snapshot{}; }

class HealthAttributes final : public chip::app::AttributeAccessInterface {
 public:
  HealthAttributes() : AttributeAccessInterface(chip::MakeOptional<chip::EndpointId>(0), HealthClusterId) {}
  CHIP_ERROR Read(const chip::app::ConcreteReadAttributePath &path,
                  chip::app::AttributeValueEncoder &encoder) override {
    if (path.mEndpointId != 0 || path.mClusterId != HealthClusterId) return CHIP_ERROR_INVALID_ARGUMENT;
    uint32_t value{};
    if (!healthValue(currentSnapshot(), path.mAttributeId, value)) return CHIP_NO_ERROR; // SDK global attributes.
    return booleanHealthAttribute(path.mAttributeId) ? encoder.Encode(bool(value)) : encoder.Encode(value);
  }
  CHIP_ERROR Write(const chip::app::ConcreteDataAttributePath &,
                   chip::app::AttributeValueDecoder &) override { return CHIP_ERROR_ACCESS_DENIED; }
};

class ActionAttributes final : public chip::app::AttributeAccessInterface {
 public:
  ActionAttributes() : AttributeAccessInterface(chip::MakeOptional<chip::EndpointId>(0), Actions::Id) {}
  CHIP_ERROR Read(const chip::app::ConcreteReadAttributePath &path,
                  chip::app::AttributeValueEncoder &encoder) override {
    if (path.mEndpointId != 0 || path.mClusterId != Actions::Id) return CHIP_ERROR_INVALID_ARGUMENT;
    if (path.mAttributeId == Actions::Attributes::ActionList::Id) {
      Actions::Structs::ActionStruct::Type action;
      action.actionID = RestartActionId;
      action.name = chip::CharSpan::fromCharString("Restart controller");
      action.type = Actions::ActionTypeEnum::kOther;
      action.endpointListID = 1;
      action.supportedCommands.Set(Actions::CommandBits::kInstantAction);
      action.state = currentSnapshot().restartPending ? Actions::ActionStateEnum::kActive : Actions::ActionStateEnum::kInactive;
      return encoder.EncodeList([&](const auto &list) { return list.Encode(action); });
    }
    if (path.mAttributeId == Actions::Attributes::EndpointLists::Id) {
      const chip::EndpointId controllerEndpoint[] = {0};
      Actions::Structs::EndpointListStruct::Type endpoints;
      endpoints.endpointListID = 1;
      endpoints.name = chip::CharSpan::fromCharString("Controller");
      endpoints.type = Actions::EndpointListTypeEnum::kOther;
      endpoints.endpoints = chip::app::DataModel::List<const chip::EndpointId>(controllerEndpoint);
      return encoder.EncodeList([&](const auto &list) { return list.Encode(endpoints); });
    }
    return CHIP_NO_ERROR;
  }
  CHIP_ERROR Write(const chip::app::ConcreteDataAttributePath &,
                   chip::app::AttributeValueDecoder &) override { return CHIP_ERROR_ACCESS_DENIED; }
};

inline HealthAttributes healthAttributes;
inline ActionAttributes actionAttributes;

inline void stateEvent(Actions::ActionStateEnum state) {
  if (!restartInvokeId.HasValue()) return;
  Actions::Events::StateChanged::Type event;
  event.actionID = RestartActionId; event.invokeID = restartInvokeId.Value(); event.newState = state;
  chip::EventNumber number{};
  (void)chip::app::LogEvent(event, 0, number);
}

inline Status decisionStatus(RestartDecision decision) {
  switch (decision) {
    case RestartDecision::Accepted: return Status::Success;
    case RestartDecision::Unauthorized: return Status::UnsupportedAccess;
    case RestartDecision::UnknownAction: return Status::NotFound;
    case RestartDecision::Busy: return Status::Busy;
    case RestartDecision::Unhealthy: return Status::InvalidInState;
  }
  return Status::Failure;
}

inline esp_err_t handleRestartCommand(const chip::app::ConcreteCommandPath &path,
                                     chip::TLV::TLVReader &reader, void *opaque) {
  auto *handler = static_cast<chip::app::CommandHandler *>(opaque);
  if (!handler) return ESP_ERR_INVALID_ARG;
  Status status = Status::UnsupportedCommand;
  if (path.mEndpointId == 0 && path.mClusterId == Actions::Id &&
      path.mCommandId == Actions::Commands::InstantAction::Id) {
    const auto subject = handler->GetSubjectDescriptor();
    chip::Access::RequestPath request;
    request.endpoint = 0; request.cluster = Actions::Id;
    request.requestType = chip::Access::RequestType::kCommandInvokeRequest;
    request.entityId = Actions::Commands::InstantAction::Id;
    const bool caseSession = subject.authMode == chip::Access::AuthMode::kCase;
    const bool validFabric = subject.fabricIndex != chip::kUndefinedFabricIndex && !subject.isCommissioning;
    // Actions normally needs Operate. A maintenance restart additionally needs
    // Administer on this exact path; never assume the HA controller is admin.
    const bool admin = caseSession && validFabric &&
      chip::Access::GetAccessControl().Check(subject, request, chip::Access::Privilege::kAdminister) == CHIP_NO_ERROR;
    if (!caseSession || !validFabric || !admin) status = Status::UnsupportedAccess;
    else {
      Actions::Commands::InstantAction::DecodableType command;
      if (chip::app::DataModel::Decode(reader, command) != CHIP_NO_ERROR) status = Status::InvalidCommand;
      else {
        const auto decision = authorizeRestart(command.actionID, caseSession, validFabric, admin, currentSnapshot());
        status = decisionStatus(decision);
        if (decision == RestartDecision::Accepted) {
          if (!queueRestart || !queueRestart()) status = Status::Busy;
          else {
            restartInvokeId = command.invokeID;
            published.restartPending = true;
            stateEvent(Actions::ActionStateEnum::kActive);
            MatterReportingAttributeChangeCallback(0, HealthClusterId, 13);
            MatterReportingAttributeChangeCallback(0, HealthClusterId, 15);
            MatterReportingAttributeChangeCallback(0, Actions::Id, Actions::Attributes::ActionList::Id);
          }
        }
      }
    }
  }
  // This is a standard command with an explicit status response, not a custom
  // command with the SDK's automatic generic response. No reset runs here.
  handler->AddStatus(path, status);
  return ESP_OK;
}

inline bool createBeforeMatterBegin(ReadSnapshot reader, QueueRestart queue) {
  if (initialized || !reader || !queue) return false;
  auto *root = esp_matter::endpoint::get(esp_matter::node::get(), 0);
  if (!root || esp_matter::cluster::get(root, HealthClusterId) || esp_matter::cluster::get(root, Actions::Id)) return false;
  auto *health = esp_matter::cluster::create(root, HealthClusterId, esp_matter::CLUSTER_FLAG_SERVER);
  auto *actions = esp_matter::cluster::create(root, Actions::Id, esp_matter::CLUSTER_FLAG_SERVER);
  if (!health || !actions) return false;
  if (!esp_matter::cluster::global::attribute::create_cluster_revision(health, 1) ||
      !esp_matter::cluster::global::attribute::create_feature_map(health, 0) ||
      !esp_matter::cluster::global::attribute::create_cluster_revision(actions, 1) ||
      !esp_matter::cluster::global::attribute::create_feature_map(actions, 0)) return false;
  for (uint32_t id = 0; id <= LastHealthAttribute; ++id) {
    const auto value = booleanHealthAttribute(id) ? esp_matter_bool(false) : esp_matter_uint32(0);
    if (!esp_matter::attribute::create(health, id, esp_matter::ATTRIBUTE_FLAG_MANAGED_INTERNALLY, value)) return false;
  }
  if (!esp_matter::cluster::actions::attribute::create_action_list(actions, nullptr, 0, 0) ||
      !esp_matter::cluster::actions::attribute::create_endpoint_lists(actions, nullptr, 0, 0) ||
      !esp_matter::cluster::actions::event::create_state_changed(actions) ||
      !esp_matter::cluster::actions::event::create_action_failed(actions) ||
      !esp_matter::command::create(actions, Actions::Commands::InstantAction::Id, esp_matter::COMMAND_FLAG_ACCEPTED, handleRestartCommand)) return false;
  auto &registry = chip::app::AttributeAccessInterfaceRegistry::Instance();
  if (!registry.Register(&healthAttributes)) return false;
  if (!registry.Register(&actionAttributes)) { registry.Unregister(&healthAttributes); return false; }
  readSnapshot = reader; queueRestart = queue; published = reader(); initialized = true;
  reportCadence.begin(0, published);
  return true;
}

inline void publishSnapshot(uint32_t now = 0) {
  if (!initialized) return;
  const Snapshot next = currentSnapshot();
  // A queued request cancelled by the parent's OTA/liveness recheck is exposed
  // as an interrupted Action when the caller supplied the optional InvokeID.
  if (published.restartPending && !next.restartPending && restartInvokeId.HasValue()) {
    Actions::Events::ActionFailed::Type event;
    event.actionID = RestartActionId; event.invokeID = restartInvokeId.Value();
    event.newState = Actions::ActionStateEnum::kInactive; event.error = Actions::ActionErrorEnum::kInterrupted;
    chip::EventNumber number{}; (void)chip::app::LogEvent(event, 0, number);
    stateEvent(Actions::ActionStateEnum::kInactive); restartInvokeId.ClearValue();
  }
  const Snapshot previous = published; published = next;
  for (uint32_t id = 0; id <= LastHealthAttribute; ++id) {
    if (reportCadence.changed(id, previous, next, now)) MatterReportingAttributeChangeCallback(0, HealthClusterId, id);
  }
  reportCadence.complete(now, next);
  if (previous.restartPending != next.restartPending)
    MatterReportingAttributeChangeCallback(0, Actions::Id, Actions::Attributes::ActionList::Id);
}
} // namespace windcalm_maintenance
