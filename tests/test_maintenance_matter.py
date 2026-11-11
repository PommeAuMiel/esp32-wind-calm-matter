# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Wind Calm contributors
"""Compile the real maintenance adapter with fault-injecting SDK boundaries.

These tests never open a serial port, socket, fabric or physical actuator.
"""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HEADERS = ROOT / "firmware/WindCalmThreadOTA/main"

FAKE = r"""
#pragma once
#include <cstdint>
#include <string>
#include <vector>
#include <optional>
#include <tuple>
using CHIP_ERROR = int;
using esp_err_t = int;
constexpr int CHIP_NO_ERROR=0, CHIP_ERROR_INVALID_ARGUMENT=1, CHIP_ERROR_ACCESS_DENIED=2;
constexpr int ESP_OK=0, ESP_ERR_INVALID_ARG=-1;
namespace esp_matter {
constexpr int CLUSTER_FLAG_SERVER=1, ATTRIBUTE_FLAG_MANAGED_INTERNALLY=0x200, COMMAND_FLAG_ACCEPTED=2;
}
struct esp_matter_attr_val_t { bool boolean; uint32_t value; };
inline esp_matter_attr_val_t esp_matter_bool(bool value) { return {true,uint32_t(value)}; }
inline esp_matter_attr_val_t esp_matter_uint32(uint32_t value) { return {false,value}; }
namespace chip {
using EndpointId=uint16_t; using ClusterId=uint32_t; using EventNumber=uint64_t;
constexpr uint8_t kUndefinedFabricIndex=0;
template<class T> class Optional {
 std::optional<T> value;
 public: Optional()=default; Optional(T v):value(v){}
 bool HasValue() const { return value.has_value(); } T Value() const { return value.value(); }
 void ClearValue() { value.reset(); }
};
template<class T> Optional<T> MakeOptional(T value) { return Optional<T>(value); }
struct CharSpan { const char *text; static CharSpan fromCharString(const char *s) { return {s}; } };
template<class T> struct BitMask {
 uint16_t raw=0; void Set(T value) { raw|=uint16_t(value); } uint16_t Raw() const { return raw; }
};
namespace Protocols::InteractionModel {
enum class Status { Success,Failure,UnsupportedCommand,UnsupportedAccess,InvalidCommand,NotFound,Busy,InvalidInState };
}
namespace Access {
enum class AuthMode { kNone,kCase,kPase,kGroup };
enum class Privilege { kOperate,kAdminister };
enum class RequestType { kRequestTypeUnknown,kCommandInvokeRequest };
struct SubjectDescriptor { uint8_t fabricIndex=1; AuthMode authMode=AuthMode::kCase; bool isCommissioning=false; };
struct RequestPath { ClusterId cluster=0; EndpointId endpoint=0; RequestType requestType=RequestType::kRequestTypeUnknown; std::optional<uint32_t> entityId; };
inline bool grant=true; inline unsigned checks=0; inline RequestPath lastRequest; inline Privilege lastPrivilege;
struct AccessControl {
 CHIP_ERROR Check(const SubjectDescriptor &, const RequestPath &request, Privilege privilege) {
  ++checks; lastRequest=request; lastPrivilege=privilege; return grant ? CHIP_NO_ERROR : CHIP_ERROR_ACCESS_DENIED;
 }
};
inline AccessControl &GetAccessControl() { static AccessControl control; return control; }
}
namespace TLV { struct TLVReader { bool valid=true; uint16_t action=1; Optional<uint32_t> invoke; }; }
namespace app {
struct ConcreteReadAttributePath { EndpointId mEndpointId; ClusterId mClusterId; uint32_t mAttributeId; };
using ConcreteDataAttributePath=ConcreteReadAttributePath;
struct ConcreteCommandPath { EndpointId mEndpointId; ClusterId mClusterId; uint32_t mCommandId; };
namespace DataModel {
template<class T> struct List {
 const T *data; unsigned count;
 template<unsigned N> List(const T (&values)[N]):data(values),count(N){}
 List():data(nullptr),count(0){}
};
template<class T> CHIP_ERROR Decode(TLV::TLVReader &reader, T &out) {
 if (!reader.valid) return CHIP_ERROR_INVALID_ARGUMENT;
 out.actionID=reader.action; out.invokeID=reader.invoke; return CHIP_NO_ERROR;
}
}
namespace Clusters::Actions {
constexpr ClusterId Id=37;
enum class ActionTypeEnum { kOther };
enum class EndpointListTypeEnum { kOther };
enum class ActionStateEnum { kInactive,kActive };
enum class ActionErrorEnum { kInterrupted };
enum class CommandBits { kInstantAction=1 };
namespace Attributes { namespace ActionList { constexpr uint32_t Id=0; } namespace EndpointLists { constexpr uint32_t Id=1; } }
namespace Structs {
namespace ActionStruct { struct Type {
 uint16_t actionID=0; CharSpan name{nullptr}; ActionTypeEnum type{}; uint16_t endpointListID=0;
 BitMask<CommandBits> supportedCommands; ActionStateEnum state{};
}; }
namespace EndpointListStruct { struct Type {
 uint16_t endpointListID=0; CharSpan name{nullptr}; EndpointListTypeEnum type{}; DataModel::List<const EndpointId> endpoints;
}; }
}
namespace Commands::InstantAction { constexpr uint32_t Id=0; struct DecodableType { uint16_t actionID=0; Optional<uint32_t> invokeID; }; }
namespace Events {
namespace StateChanged { struct Type { uint16_t actionID=0; uint32_t invokeID=0; ActionStateEnum newState{}; }; }
namespace ActionFailed { struct Type { uint16_t actionID=0; uint32_t invokeID=0; ActionStateEnum newState{}; ActionErrorEnum error{}; }; }
}
}
struct AttributeValueEncoder {
 bool encoded=false,boolean=false; uint32_t scalar=0; std::vector<Clusters::Actions::Structs::ActionStruct::Type> actions;
 std::vector<EndpointId> endpoints; uint16_t endpointListID=0;
 CHIP_ERROR Encode(bool value) { encoded=true; boolean=true; scalar=value; return CHIP_NO_ERROR; }
 CHIP_ERROR Encode(uint32_t value) { encoded=true; boolean=false; scalar=value; return CHIP_NO_ERROR; }
 struct ListEncoder {
  AttributeValueEncoder &owner;
  CHIP_ERROR Encode(const Clusters::Actions::Structs::ActionStruct::Type &value) const { owner.actions.push_back(value); return CHIP_NO_ERROR; }
  CHIP_ERROR Encode(const Clusters::Actions::Structs::EndpointListStruct::Type &value) const {
   owner.endpointListID=value.endpointListID;
   owner.endpoints.assign(value.endpoints.data,value.endpoints.data+value.endpoints.count); return CHIP_NO_ERROR;
  }
 };
 template<class F> CHIP_ERROR EncodeList(F callback) { encoded=true; return callback(ListEncoder{*this}); }
};
struct AttributeValueDecoder {};
struct AttributeAccessInterface {
 EndpointId endpoint; ClusterId cluster;
 AttributeAccessInterface(Optional<EndpointId> ep,ClusterId cl):endpoint(ep.Value()),cluster(cl){}
 virtual ~AttributeAccessInterface()=default;
 virtual CHIP_ERROR Read(const ConcreteReadAttributePath &,AttributeValueEncoder &)=0;
 virtual CHIP_ERROR Write(const ConcreteDataAttributePath &,AttributeValueDecoder &) { return CHIP_NO_ERROR; }
};
struct AttributeAccessInterfaceRegistry {
 std::vector<AttributeAccessInterface*> entries; unsigned attempts=0,failAt=0,unregisters=0;
 static AttributeAccessInterfaceRegistry &Instance() { static AttributeAccessInterfaceRegistry registry; return registry; }
 bool Register(AttributeAccessInterface *entry) { ++attempts; if (attempts==failAt) return false; entries.push_back(entry); return true; }
 void Unregister(AttributeAccessInterface *entry) { ++unregisters; for (auto it=entries.begin();it!=entries.end();++it) if (*it==entry) { entries.erase(it);break; } }
};
struct CommandHandler {
 Access::SubjectDescriptor subject; std::vector<Protocols::InteractionModel::Status> statuses;
 Access::SubjectDescriptor GetSubjectDescriptor() const { return subject; }
 void AddStatus(const ConcreteCommandPath &,Protocols::InteractionModel::Status status) { statuses.push_back(status); }
};
inline std::vector<Clusters::Actions::Events::StateChanged::Type> stateEvents;
inline std::vector<Clusters::Actions::Events::ActionFailed::Type> failureEvents;
inline CHIP_ERROR LogEvent(const Clusters::Actions::Events::StateChanged::Type &event,EndpointId ep,EventNumber &) {
 if (ep!=0) return CHIP_ERROR_INVALID_ARGUMENT;
 stateEvents.push_back(event); return CHIP_NO_ERROR;
}
inline CHIP_ERROR LogEvent(const Clusters::Actions::Events::ActionFailed::Type &event,EndpointId ep,EventNumber &) {
 if (ep!=0) return CHIP_ERROR_INVALID_ARGUMENT;
 failureEvents.push_back(event); return CHIP_NO_ERROR;
}
}
}
namespace fake {
inline unsigned creates=0,failCreateAt=0;
inline bool rootExists=true,clusterExists=false;
inline std::vector<std::tuple<uint32_t,uint32_t,int,bool>> attrs;
inline std::vector<std::tuple<uint16_t,uint32_t,uint32_t>> dirty;
inline unsigned commands=0,events=0; inline int commandFlags=0;
inline bool allocationAllowed() { return ++creates!=failCreateAt; }
}
inline void MatterReportingAttributeChangeCallback(chip::EndpointId ep,chip::ClusterId cl,uint32_t id) { fake::dirty.emplace_back(ep,cl,id); }
namespace esp_matter {
struct endpoint_t {}; struct cluster_t { uint32_t id; }; struct attribute_t {}; struct command_t {}; struct event_t {};
namespace node { inline void *get() { return nullptr; } }
namespace endpoint { inline endpoint_t *get(void *,uint16_t ep) { static endpoint_t root; return ep==0 && fake::rootExists ? &root : nullptr; } }
namespace cluster {
inline cluster_t *get(endpoint_t *,uint32_t) { static cluster_t collision{}; return fake::clusterExists ? &collision : nullptr; }
inline cluster_t *create(endpoint_t *,uint32_t id,int) {
 if (!fake::allocationAllowed()) return nullptr;
 static cluster_t health{0xFFF1FC00},actions{37}; return id==37 ? &actions : &health;
}
namespace global::attribute {
inline attribute_t *create_cluster_revision(cluster_t *,uint16_t) { static attribute_t value; return fake::allocationAllowed() ? &value : nullptr; }
inline attribute_t *create_feature_map(cluster_t *,uint32_t) { static attribute_t value; return fake::allocationAllowed() ? &value : nullptr; }
}
namespace actions {
namespace attribute {
inline attribute_t *create_action_list(cluster_t *,uint8_t *,uint16_t,uint16_t) { static attribute_t value; return fake::allocationAllowed() ? &value : nullptr; }
inline attribute_t *create_endpoint_lists(cluster_t *,uint8_t *,uint16_t,uint16_t) { static attribute_t value; return fake::allocationAllowed() ? &value : nullptr; }
}
namespace event {
inline event_t *create_state_changed(cluster_t *) { ++fake::events; static event_t value; return fake::allocationAllowed() ? &value : nullptr; }
inline event_t *create_action_failed(cluster_t *) { ++fake::events; static event_t value; return fake::allocationAllowed() ? &value : nullptr; }
}
}
}
namespace attribute {
inline attribute_t *create(cluster_t *cluster,uint32_t id,int flags,esp_matter_attr_val_t value) {
 fake::attrs.emplace_back(cluster->id,id,flags,value.boolean);
 static attribute_t attr; return fake::allocationAllowed() ? &attr : nullptr;
}
}
namespace command {
using callback_t=esp_err_t(*)(const chip::app::ConcreteCommandPath &,chip::TLV::TLVReader &,void *);
inline callback_t callback=nullptr;
inline command_t *create(cluster_t *,uint32_t,int flags,callback_t cb) {
 ++fake::commands; fake::commandFlags=flags; callback=cb; static command_t value;
 return fake::allocationAllowed() ? &value : nullptr;
}
}
}
"""

SCENARIOS = r"""
#include "MaintenanceMatter.h"
#include <cassert>
#include <limits>
using namespace windcalm_maintenance;
Snapshot sample;
unsigned reads=0,queues=0;
bool queueAvailable=true;
Snapshot reader() { ++reads; return sample; }
bool queue() { ++queues; if (!queueAvailable) return false; sample.restartPending=true; return true; }
void healthy() { sample.imageHealthy=sample.otaIdle=sample.chipResponsive=true; }
void command(chip::app::CommandHandler &handler,chip::TLV::TLVReader &tlv,
             Status expected,uint16_t ep=0,uint32_t cluster=37,uint32_t id=0) {
 const size_t previous=handler.statuses.size();
 assert(handleRestartCommand({ep,cluster,id},tlv,&handler)==ESP_OK);
 assert(handler.statuses.size()==previous+1 && handler.statuses.back()==expected);
}
int main(int argc,char **argv) {
 assert(argc==2); const std::string scenario=argv[1];
 if (scenario=="initialization_failure") {
  fake::rootExists=false; assert(!createBeforeMatterBegin(reader,queue) && !initialized);
  fake::rootExists=true; fake::clusterExists=true;
  assert(!createBeforeMatterBegin(reader,queue) && fake::creates==0 && !initialized);
  fake::clusterExists=false; assert(!createBeforeMatterBegin(nullptr,queue));
  fake::failCreateAt=7; assert(!createBeforeMatterBegin(reader,queue) && !initialized);
  return 0;
 }
 if (scenario=="registration_failure") {
  auto &registry=chip::app::AttributeAccessInterfaceRegistry::Instance(); registry.failAt=2;
  assert(!createBeforeMatterBegin(reader,queue) && !initialized);
  assert(registry.entries.empty() && registry.unregisters==1); return 0;
 }
 assert(createBeforeMatterBegin(reader,queue));
 assert(fake::attrs.size()==LastHealthAttribute+1 && fake::commands==1 && fake::events==2);
 assert(fake::commandFlags==esp_matter::COMMAND_FLAG_ACCEPTED);
 assert(!createBeforeMatterBegin(reader,queue));
 for (const auto &attr:fake::attrs) {
  assert(std::get<0>(attr)==HealthClusterId && std::get<2>(attr)==esp_matter::ATTRIBUTE_FLAG_MANAGED_INTERNALLY);
  assert(std::get<3>(attr)==booleanHealthAttribute(std::get<1>(attr)));
 }
 chip::app::CommandHandler handler; chip::TLV::TLVReader tlv;
 if (scenario=="authorization") {
  healthy(); handler.subject.authMode=chip::Access::AuthMode::kPase;
  command(handler,tlv,Status::UnsupportedAccess); assert(chip::Access::checks==0);
  handler.subject.authMode=chip::Access::AuthMode::kGroup;
  command(handler,tlv,Status::UnsupportedAccess); assert(chip::Access::checks==0);
  handler.subject.authMode=chip::Access::AuthMode::kCase; handler.subject.fabricIndex=0;
  command(handler,tlv,Status::UnsupportedAccess); assert(chip::Access::checks==0);
  handler.subject.fabricIndex=1; handler.subject.isCommissioning=true;
  command(handler,tlv,Status::UnsupportedAccess); assert(chip::Access::checks==0);
  handler.subject.isCommissioning=false; chip::Access::grant=false;
  command(handler,tlv,Status::UnsupportedAccess); assert(chip::Access::checks==1 && queues==0);
  assert(chip::Access::lastRequest.endpoint==0 && chip::Access::lastRequest.cluster==37 &&
         chip::Access::lastRequest.entityId==0 && chip::Access::lastPrivilege==chip::Access::Privilege::kAdminister);
 } else if (scenario=="invalid_command") {
  healthy(); command(handler,tlv,Status::UnsupportedCommand,1);
  command(handler,tlv,Status::UnsupportedCommand,0,6);
  command(handler,tlv,Status::UnsupportedCommand,0,37,1);
  tlv.valid=false; command(handler,tlv,Status::InvalidCommand);
  tlv.valid=true; tlv.action=2; command(handler,tlv,Status::NotFound);
  assert(queues==0 && fake::dirty.empty());
  assert(handleRestartCommand({0,37,0},tlv,nullptr)==ESP_ERR_INVALID_ARG);
 } else if (scenario=="ota_and_liveness") {
  command(handler,tlv,Status::Busy); healthy(); sample.otaIdle=false;
  command(handler,tlv,Status::Busy); sample.otaIdle=true; sample.imageHealthy=false;
  command(handler,tlv,Status::InvalidInState); sample.imageHealthy=true; sample.chipResponsive=false;
  command(handler,tlv,Status::InvalidInState); sample.chipResponsive=true; sample.restartPending=true;
  command(handler,tlv,Status::Busy); sample.restartPending=false;
  queueAvailable=false; command(handler,tlv,Status::Busy); assert(queues==1 && !sample.restartPending);
  queueAvailable=true; command(handler,tlv,Status::Success); assert(queues==2 && sample.restartPending);
  command(handler,tlv,Status::Busy); assert(queues==2);
 } else if (scenario=="snapshot_scalars") {
  healthy(); sample.tuyaReady=true; sample.knownMask=0xFFFF; sample.uartChecksumErrors=17;
  sample.commandTimeouts=19; sample.lightPublicationErrors=23; sample.loopProgress=29;
  sample.recoveryBudgetCount=2; sample.reportRevision=std::numeric_limits<uint32_t>::max();
  publishSnapshot(); const unsigned readsBefore=reads;
  for (uint32_t id=0;id<=LastHealthAttribute;++id) {
   chip::app::AttributeValueEncoder encoder; uint32_t expected=0;
   assert(healthValue(sample,id,expected));
   assert(healthAttributes.Read({0,HealthClusterId,id},encoder)==CHIP_NO_ERROR);
   assert(encoder.encoded && encoder.scalar==expected && encoder.boolean==booleanHealthAttribute(id));
  }
  assert(reads==readsBefore+LastHealthAttribute+1 && queues==0);
  sample.freeHeap=42000; sample.lightKnown=true; sample.lightConfirmed=false;
  chip::app::AttributeValueEncoder live;
  assert(healthAttributes.Read({0,HealthClusterId,16},live)==CHIP_NO_ERROR && live.scalar==42000);
  // Direct reads stay fresh even when slow notifications have not run.
  sample.freeHeap=43000;
  chip::app::AttributeValueEncoder fresh;
  assert(healthAttributes.Read({0,HealthClusterId,16},fresh)==CHIP_NO_ERROR && fresh.scalar==43000);
  publishSnapshot();
  uint32_t unchanged=123; assert(!healthValue(sample,LastHealthAttribute+1,unchanged) && unchanged==123);
  assert(!healthValue(sample,UINT32_MAX,unchanged));
  chip::app::AttributeValueEncoder global;
  assert(healthAttributes.Read({0,HealthClusterId,0xFFFD},global)==CHIP_NO_ERROR && !global.encoded);
  assert(healthAttributes.Read({1,HealthClusterId,0},global)==CHIP_ERROR_INVALID_ARGUMENT);
  chip::app::AttributeValueDecoder decoder;
  assert(healthAttributes.Write({0,HealthClusterId,1},decoder)==CHIP_ERROR_ACCESS_DENIED);
  assert(actionAttributes.Write({0,37,0},decoder)==CHIP_ERROR_ACCESS_DENIED);
  fake::dirty.clear(); publishSnapshot(); assert(fake::dirty.empty());
  sample.reportRevision=0; publishSnapshot();
  assert(fake::dirty.size()==1 && std::get<2>(fake::dirty.front())==6);
 } else if (scenario=="action_discovery") {
  chip::app::AttributeValueEncoder actions,endpoints;
  assert(actionAttributes.Read({0,37,0},actions)==CHIP_NO_ERROR && actions.actions.size()==1);
  const auto &action=actions.actions.front();
  assert(action.actionID==1 && std::string(action.name.text)=="Restart controller" && action.endpointListID==1 &&
         action.type==Actions::ActionTypeEnum::kOther && action.supportedCommands.Raw()==1 &&
         action.state==Actions::ActionStateEnum::kInactive);
  assert(actionAttributes.Read({0,37,1},endpoints)==CHIP_NO_ERROR);
  assert(endpoints.endpointListID==1 && endpoints.endpoints==std::vector<chip::EndpointId>{0});
  assert(queues==0);
 } else if (scenario=="events_and_cancel") {
  healthy(); tlv.invoke=chip::MakeOptional<uint32_t>(42);
  command(handler,tlv,Status::Success); assert(queues==1 && chip::app::stateEvents.size()==1);
  assert(chip::app::stateEvents.front().invokeID==42 && chip::app::stateEvents.front().newState==Actions::ActionStateEnum::kActive);
  sample.restartPending=false; sample.otaIdle=false; publishSnapshot();
  assert(chip::app::failureEvents.size()==1 && chip::app::stateEvents.size()==2 && !restartInvokeId.HasValue());
  assert(chip::app::failureEvents.front().error==Actions::ActionErrorEnum::kInterrupted);
  publishSnapshot(); assert(chip::app::failureEvents.size()==1);
 } else if (scenario=="optional_invoke_id") {
  healthy(); command(handler,tlv,Status::Success); assert(queues==1 && chip::app::stateEvents.empty());
  sample.restartPending=false; publishSnapshot(); assert(chip::app::failureEvents.empty());
 } else return 3;
 return 0;
}
"""


class MaintenanceMatterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("c++") or shutil.which("clang++") or shutil.which("g++")
        if not compiler:
            raise unittest.SkipTest("A C++17 compiler is required")
        cls.temp = tempfile.TemporaryDirectory(prefix="windcalm-maintenance-")
        cls.addClassCleanup(cls.temp.cleanup)
        folder = pathlib.Path(cls.temp.name)
        (folder / "esp_matter.h").write_text(FAKE, encoding="utf-8")
        for name in (
            "access/AccessControl.h", "app/AttributeAccessInterface.h",
            "app/AttributeAccessInterfaceRegistry.h", "app/CommandHandler.h",
            "app/EventLogging.h", "app/data-model/Decode.h", "app/reporting/reporting.h",
            "clusters/Actions/Attributes.h", "clusters/Actions/Commands.h", "clusters/Actions/Events.h",
        ):
            header = folder / name
            header.parent.mkdir(parents=True, exist_ok=True)
            header.write_text("#pragma once\n#include <esp_matter.h>\n", encoding="utf-8")
        source = folder / "scenarios.cpp"
        source.write_text(SCENARIOS, encoding="utf-8")
        cls.binary = folder / "scenarios"
        result = subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                                 "-fsanitize=address,undefined", "-g",
                                 "-I", str(folder), "-I", str(HEADERS), str(source), "-o", str(cls.binary)],
                                capture_output=True, text=True)
        if result.returncode:
            raise AssertionError("Maintenance adapter compilation failed:\n" + result.stdout + result.stderr)

    def run_scenario(self, name):
        subprocess.run([str(self.binary), name], check=True, capture_output=True, text=True)

    def test_initialization_fails_closed(self):
        self.run_scenario("initialization_failure")

    def test_registration_conflict_unregisters_first_override(self):
        self.run_scenario("registration_failure")

    def test_requires_case_fabric_and_administer_on_exact_command(self):
        self.run_scenario("authorization")

    def test_rejects_wrong_path_malformed_command_and_unknown_action(self):
        self.run_scenario("invalid_command")

    def test_rejects_ota_unconfirmed_image_stale_matter_and_duplicate_requests(self):
        self.run_scenario("ota_and_liveness")

    def test_fixed_readonly_scalars_wrap_and_changed_attribute_reporting(self):
        self.run_scenario("snapshot_scalars")

    def test_action_discovery_targets_only_controller_root(self):
        self.run_scenario("action_discovery")

    def test_optional_invoke_id_events_report_queued_request_and_cancellation(self):
        self.run_scenario("events_and_cancel")

    def test_no_events_without_optional_invoke_id(self):
        self.run_scenario("optional_invoke_id")


if __name__ == "__main__":
    unittest.main()
