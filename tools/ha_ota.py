#!/usr/bin/env python3
"""Inspect and deliver one reviewed Wind Calm OTA through verified SSH access.

No remote packages or files are installed. Upload and installation are distinct
explicit commands; neither operation is automatically retried after an error.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import http.client
import inspect
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import struct
import subprocess
import sys
import time

NODE = None  # Loaded from private/access/ha_ssh.json before network access.
VID, PID = 0xFFF1, 0x8000
BASE_VERSION, TARGET_VERSION = 40004, 40005
BASE_STRING, TARGET_STRING = (
    f"{version // 10000}.{(version // 100) % 100}.{version % 100}-thread-ota"
    for version in (BASE_VERSION, TARGET_VERSION)
)
PROJECT_NAME = "windcalm-thread-ota"
PRODUCT_NAME = "Wind Calm Thread Fan Light"
MAX_IMAGE_BYTES = 2 * 1024 * 1024
SLOT_BYTES = 0x1F0000
OPERATIONS = frozenset(("preflight", "validate-image", "import-image", "check-update", "install-update", "status", "diagnostics", "watch-update", "monitor-health", "restart-controller"))
BASIC_PATHS = ("0/40/9", "0/40/10", "0/40/2", "0/40/4")
# Scalar health attributes only: no fabric data, addresses, topology tables,
# credentials, reset commands or diagnostic test triggers are requested.
DIAGNOSTIC_FIELDS = {
    "0/51/1": ("reboot_count", 0xFFFF),
    "0/51/2": ("uptime_seconds", 0xFFFFFFFFFFFFFFFF),
    "0/51/3": ("total_operational_hours", 0xFFFFFFFF),
    "0/51/4": ("boot_reason", 0xFF),
    "0/53/0": ("thread_channel", 0xFFFF),
    "0/53/1": ("thread_routing_role", 0xFF),
}
BOOT_REASONS = {0: "unspecified", 1: "power_on", 2: "brown_out",
                3: "software_watchdog", 4: "hardware_watchdog",
                5: "software_update_completed", 6: "software_reset"}
THREAD_ROLES = {0: "unspecified", 1: "unassigned", 2: "sleepy_end_device",
                3: "end_device", 4: "reed", 5: "router", 6: "leader"}
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
UPLOAD_ID = re.compile(r"[0-9a-f]{32}\Z")


class GuardError(ValueError):
    """A fixed safety/compatibility boundary was not met."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def tlv_parser():
    import thread_ota
    return thread_ota.read_tlv_header


def validate_image_bytes(data, expected_sha256=None, parser=None):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise GuardError("image_size")
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and (not SHA256.fullmatch(expected_sha256) or
                                       not hmac.compare_digest(expected_sha256, digest)):
        raise GuardError("image_sha256")
    try:
        fields, payload = (parser or tlv_parser())(data)
    except (ValueError, UnicodeError, struct.error) as error:
        raise GuardError("matter_container") from error
    if (fields.get(0), fields.get(1), fields.get(2), fields.get(3), fields.get(5)) != (
            VID, PID, TARGET_VERSION, TARGET_STRING, BASE_VERSION):
        raise GuardError("matter_identity_version_range")
    maximum = fields.get(6)
    if maximum is not None and (type(maximum) is not int or not BASE_VERSION <= maximum < TARGET_VERSION):
        raise GuardError("matter_maximum_version")
    if not 288 <= len(payload) <= SLOT_BYTES or fields.get(4) != len(payload) or fields.get(8) != 1:
        raise GuardError("matter_payload_size_digest_type")
    payload_digest = hashlib.sha256(payload).digest()
    if not isinstance(fields.get(9), bytes) or not hmac.compare_digest(fields[9], payload_digest):
        raise GuardError("matter_payload_sha256")
    if payload[0] != 0xE9 or not 1 <= payload[1] <= 16 or struct.unpack_from("<H", payload, 12)[0] != 13:
        raise GuardError("esp32_c6_image")
    if payload[23] != 1 or struct.unpack_from("<II", payload, 32) != (0xABCD5432, 0):
        raise GuardError("esp_descriptor_secure_version_digest")
    try:
        app_version = payload[48:80].split(b"\0", 1)[0].decode("ascii")
        app_project = payload[80:112].split(b"\0", 1)[0].decode("ascii")
    except UnicodeError as error:
        raise GuardError("esp_descriptor_encoding") from error
    if app_version != TARGET_STRING or app_project != PROJECT_NAME:
        raise GuardError("esp_descriptor_version_project")
    # Verify the ESP segment checksum and appended image SHA as well as the
    # Matter payload SHA. A container-only header edit cannot pass this check.
    offset, checksum = 24, 0xEF
    for segment in range(payload[1]):
        if offset + 8 > len(payload):
            raise GuardError("esp_segment_header")
        _address, count = struct.unpack_from("<II", payload, offset)
        offset += 8
        if count > SLOT_BYTES or offset + count > len(payload) or (segment == 0 and count < 256):
            raise GuardError("esp_segment_size")
        for value in memoryview(payload)[offset:offset + count]:
            checksum ^= value
        offset += count
    checksum_end = (offset + 16) & ~15
    if checksum_end + 32 != len(payload) or payload[checksum_end - 1] != checksum:
        raise GuardError("esp_checksum_length")
    if not hmac.compare_digest(hashlib.sha256(payload[:checksum_end]).digest(), payload[checksum_end:]):
        raise GuardError("esp_image_sha256")
    return {"vendor_id": VID, "product_id": PID, "version": TARGET_VERSION, "version_string": TARGET_STRING,
            "min_applicable_version": BASE_VERSION, "max_applicable_version": maximum,
            "ota_bytes": len(data), "ota_sha256": digest, "payload_bytes": len(payload),
            "payload_sha256": payload_digest.hex(), "project_name": PROJECT_NAME, "chip": "ESP32-C6"}


def read_local_image(path, expected_sha256=None):
    path = Path(path)
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
        raise GuardError("image_file_size")
    with path.open("rb") as stream:
        data = stream.read(MAX_IMAGE_BYTES + 1)
    return data, validate_image_bytes(data, expected_sha256)


def filter_node(node):
    if not isinstance(node, dict) or type(node.get("node_id")) is not int or node["node_id"] != NODE:
        raise GuardError("target_node")
    attributes = node.get("attributes")
    if not isinstance(attributes, dict):
        raise GuardError("node_attributes")
    allowed = BASIC_PATHS + ("0/40/3", "0/40/5", "0/42/1", "0/42/2", "0/42/3")
    selected = {}
    for key in allowed:
        value = attributes.get(key)
        if value is None or type(value) in (bool, int) or isinstance(value, str) and len(value) <= 128:
            selected[key] = value
    return {"node_id": NODE, "available": node.get("available") is True,
            "ota_requestor_root": any(isinstance(key, str) and key.startswith("0/42/") for key in attributes),
            "attributes": selected}


def require_base(node):
    attributes = node["attributes"]
    if not node["available"] or not node["ota_requestor_root"]:
        raise GuardError("base_unavailable_or_ota_missing")
    if (attributes.get("0/40/9"), attributes.get("0/40/10"), attributes.get("0/40/2"),
            attributes.get("0/40/4"), attributes.get("0/40/3")) != (
            BASE_VERSION, BASE_STRING, VID, PID, PRODUCT_NAME):
        raise GuardError("base_identity_version")


def filter_update(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise GuardError("update_record")
    result = {}
    for key in ("vid", "pid", "software_version", "software_version_string",
                "min_applicable_software_version", "max_applicable_software_version", "update_source"):
        item = value.get(key)
        if item is None or type(item) is int or isinstance(item, str) and len(item) <= 128:
            result[key] = item
    return result


def require_update(value):
    filtered = filter_update(value)
    if filtered is None or (filtered.get("vid"), filtered.get("pid"), filtered.get("software_version"),
                           filtered.get("software_version_string"), filtered.get("min_applicable_software_version"),
                           filtered.get("update_source")) != (
            VID, PID, TARGET_VERSION, TARGET_STRING, BASE_VERSION, "local"):
        raise GuardError("update_identity_version_range")
    maximum = filtered.get("max_applicable_software_version")
    if maximum is not None and (type(maximum) is not int or not BASE_VERSION <= maximum < TARGET_VERSION):
        raise GuardError("update_maximum_version")
    return filtered


def require_imported_image(image, metadata):
    if not isinstance(metadata, dict) or metadata.get("import_receipt_verified") is not True:
        raise GuardError("matching_import_receipt_required")
    digest = metadata.get("ota_sha256")
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise GuardError("candidate_image_digest")
    actual = validate_image_bytes(image, digest, globals().get("read_tlv_header"))
    if any(metadata.get(key) != item for key, item in actual.items()):
        raise GuardError("candidate_image_metadata")


def require_candidate(value, image, metadata):
    """Bind server candidate metadata to the exact locally imported image.

    Server 1.4.0 cannot recover the minimum from SoftwareUpdateInfo and
    publishes zero for check/update responses. The upload receipt must still
    contain the actual base minimum; zero is not trusted as an image range.
    """
    require_imported_image(image, metadata)
    filtered = filter_update(value)
    if filtered is None or type(filtered.get("min_applicable_software_version")) is not int or \
            filtered["min_applicable_software_version"] not in (0, BASE_VERSION):
        raise GuardError("candidate_minimum_version")
    require_update(dict(filtered, min_applicable_software_version=BASE_VERSION))
    return filtered


def validate_ticket(value, image_bytes):
    if not isinstance(value, dict) or not isinstance(value.get("upload_id"), str) or not UPLOAD_ID.fullmatch(value["upload_id"]):
        raise GuardError("upload_ticket_id")
    expires, maximum = value.get("expires_in"), value.get("max_size")
    if type(expires) is not int or not 1 <= expires <= 60 or type(maximum) is not int or maximum < image_bytes:
        raise GuardError("upload_ticket_limits")
    return value["upload_id"], expires


class MatterRPC:
    def __init__(self, client, operation):
        self.client, self.operation, self.counter = client, operation, 0
        self.mutation_started = False
        self.last_command = None
        self.last_attribute_path = None
        self.last_started = None

    def call(self, command, args=None, timeout=20):
        args = {} if args is None else args
        if type(NODE) is not int or not 1 <= NODE <= 0x1FFFFFFFFFFFFF:
            raise GuardError("target_not_configured")
        if not isinstance(args, dict) or "node_id" in args and type(args["node_id"]) is not int:
            raise GuardError("request_arguments")
        if self.operation in ("preflight", "status", "diagnostics", "monitor-health", "watch-update") and \
                command not in ("get_node", "read_attribute", "start_listening"):
            raise GuardError("read_only_operation")
        exact = {"get_node": {"node_id": NODE}, "check_node_update": {"node_id": NODE},
                 "start_listening": {}, "initiate_ota_upload": {}, "update_node": {"node_id": NODE, "software_version": TARGET_VERSION},
                 "device_command": {"node_id": NODE, "endpoint_id": 0, "cluster_id": 37,
                                    "command_name": "instantAction", "payload": {"actionID": 1}}}
        if command == "read_attribute":
            if self.operation == "monitor-health":
                from ha_health import READ_PATHS
                paths = args.get("attribute_path")
                valid_paths = isinstance(paths, list) and 1 <= len(paths) <= 9 and \
                    all(isinstance(path, str) and path in READ_PATHS for path in paths) and len(set(paths)) == len(paths)
            else:
                allowed = BASIC_PATHS + tuple(DIAGNOSTIC_FIELDS) if self.operation == "diagnostics" else BASIC_PATHS
                if self.operation == "restart-controller":
                    allowed += ("0/37/0", "0/37/1", "0/42/2", "0/4294048768/0", "0/4294048768/13", "0/4294048768/15")
                valid_paths = isinstance(args.get("attribute_path"), str) and args["attribute_path"] in allowed
            if type(args.get("node_id")) is not int or args["node_id"] != NODE or not valid_paths or set(args) != {"node_id", "attribute_path"}:
                raise GuardError("read_allowlist")
        elif command not in exact or args != exact[command] or \
                command == "device_command" and (type(args.get("node_id")) is not int or
                    type(args.get("endpoint_id")) is not int or type(args.get("cluster_id")) is not int or
                    type(args.get("payload", {}).get("actionID")) is not int):
            raise GuardError("request_allowlist")
        if command == "device_command" and self.operation != "restart-controller" or \
                command == "initiate_ota_upload" and self.operation != "import-image" or \
                command == "update_node" and self.operation != "install-update" or \
                command == "read_attribute" and self.operation not in ("import-image", "install-update", "status", "diagnostics", "watch-update", "monitor-health", "restart-controller") or \
                command == "start_listening" and self.operation != "watch-update":
            raise GuardError("operation_allowlist")
        self.counter += 1
        identifier = "windcalm-ota-{}".format(self.counter)
        request = {"message_id": identifier, "command": command}
        if args:
            request["args"] = args
        started = time.monotonic()
        self.last_command = command
        self.last_attribute_path = args.get("attribute_path") if command == "read_attribute" else None
        self.last_started = started
        if command in ("initiate_ota_upload", "update_node", "device_command"):
            self.mutation_started = True
        self.client.send_json(request)
        deadline = started + timeout
        while time.monotonic() < deadline:
            try:
                value = self.client.recv_json(timeout=min(15, max(0.01, deadline - time.monotonic())))
            except TimeoutError:
                continue  # Receive only; the command is never sent a second time.
            if not isinstance(value, dict) or value.get("message_id") != identifier:
                continue
            if "error_code" in value or "result" not in value:
                raise GuardError("server_rejected")
            return value["result"], round((time.monotonic() - started) * 1000, 1)
        raise TimeoutError("Matter request outcome not confirmed")


def http_json(host, port, method, path, body=None, headers=None, timeout=15):
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024 or not 200 <= response.status < 300:
            raise GuardError("http_status_or_size")
        return json.loads(raw)
    finally:
        connection.close()


def supervisor_preflight(http=http_json):
    value = http("supervisor", 80, "GET", "/addons/core_matter_server/info",
                 headers={"Authorization": "Bearer " + os.environ["SUPERVISOR_TOKEN"]})
    data = value.get("data") if isinstance(value, dict) else None
    if not isinstance(data, dict):
        raise GuardError("supervisor_response")
    result = {}
    for key in ("state", "version", "version_latest"):
        if isinstance(data.get(key), str) and len(data[key]) <= 128:
            result[key] = data[key]
    options = data.get("options")
    result["enable_test_net_dcl"] = options.get("enable_test_net_dcl") is True if isinstance(options, dict) else None
    return result


def live_information(rpc):
    actual, timings = {}, {}
    for path in BASIC_PATHS:
        values, elapsed = rpc.call("read_attribute", {"node_id": NODE, "attribute_path": path}, timeout=60)
        if not isinstance(values, dict) or path not in values:
            raise GuardError("live_basic_information_missing")
        value = values[path]
        if path == "0/40/10":
            if not isinstance(value, str) or len(value) > 128:
                raise GuardError("live_basic_information_type")
        elif type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
            raise GuardError("live_basic_information_type")
        actual[path], timings[path] = value, elapsed
    return actual, timings


def live_diagnostics(rpc):
    result = {}
    for path, (name, maximum) in DIAGNOSTIC_FIELDS.items():
        try:
            values, elapsed = rpc.call("read_attribute", {"node_id": NODE, "attribute_path": path}, timeout=20)
        except TimeoutError:
            result[name] = {"path": path, "available": False, "reason": "timeout"}
            continue
        except GuardError as error:
            if str(error) != "server_rejected":
                raise
            result[name] = {"path": path, "available": False, "reason": "server_rejected"}
            continue
        if not isinstance(values, dict) or values.get(path) is None:
            result[name] = {"path": path, "available": False, "reason": "missing_or_null", "read_ms": elapsed}
            continue
        value = values[path]
        if type(value) is not int or not 0 <= value <= maximum:
            raise GuardError("live_diagnostic_type_range")
        item = {"path": path, "available": True, "value": value, "read_ms": elapsed}
        labels = BOOT_REASONS if name == "boot_reason" else THREAD_ROLES if name == "thread_routing_role" else None
        if labels is not None:
            item["label"] = labels.get(value, "unknown")
        result[name] = item
    return result


def watch_update(rpc, seconds, refresh=None):
    if type(seconds) is not int or not 30 <= seconds <= 900:
        raise GuardError("watch_duration")
    nodes, _elapsed = rpc.call("start_listening")
    if not isinstance(nodes, list):
        raise GuardError("watch_nodes")
    initial = next((filter_node(node) for node in nodes if isinstance(node, dict) and node.get("node_id") == NODE), None)
    if initial is None:
        raise GuardError("watch_target_missing")
    def emit(kind, **fields):
        print(json.dumps(dict(kind=kind, utc=utc_now(), node_id=NODE, **fields)), flush=True)
    emit("ota_watch_ready", node=initial, seconds=seconds, automatic_update_commands=False)
    started, next_heartbeat = time.monotonic(), time.monotonic() + 10
    confirmed = False
    pending_confirmation = initial["attributes"].get("0/40/9") == TARGET_VERSION
    allowed_paths = ("0/40/9", "0/40/10", "0/42/1", "0/42/2", "0/42/3")
    while time.monotonic() - started < seconds:
        if pending_confirmation and not confirmed:
            actual, timings = live_information(rpc)
            if tuple(actual[path] for path in BASIC_PATHS) != (TARGET_VERSION, TARGET_STRING, VID, PID):
                raise GuardError("post_update_live_identity_version")
            node, _elapsed = rpc.call("get_node", {"node_id": NODE})
            node = filter_node(node)
            if not node["available"] or not node["ota_requestor_root"]:
                raise GuardError("post_update_node_unavailable_or_ota_missing")
            emit("ota_live_confirmed", live_basic_information=actual, live_read_ms=timings, node=node)
            confirmed = True
            break
        try:
            value = rpc.client.recv_json(timeout=min(10, max(0.01, seconds - (time.monotonic() - started))))
        except TimeoutError:
            value = None
        if isinstance(value, dict):
            event, data = value.get("event"), value.get("data")
            if event == "attribute_updated" and isinstance(data, list) and len(data) == 3 and \
                    type(data[0]) is int and data[0] == NODE and data[1] in allowed_paths:
                item = data[2]
                if item is None or type(item) in (bool, int) or isinstance(item, str) and len(item) <= 128:
                    emit("ota_attribute", attribute_path=data[1], value=item)
                    if data[1] == "0/40/9" and type(item) is int and item == TARGET_VERSION:
                        pending_confirmation = True
            elif event in ("node_updated", "node_added") and isinstance(data, dict) and data.get("node_id") == NODE:
                node = filter_node(data)
                emit("ota_node", node=node)
                if node["attributes"].get("0/40/9") == TARGET_VERSION and node["available"]:
                    pending_confirmation = True
            elif event == "node_removed" and type(data) is int and data == NODE:
                emit("ota_available", available=False)
        if time.monotonic() >= next_heartbeat:
            emit("ota_watch_heartbeat", elapsed_seconds=round(time.monotonic() - started, 1))
            next_heartbeat = time.monotonic() + 10
    result = {"node_id": NODE, "live_target_version_confirmed": confirmed,
              "elapsed_seconds": round(time.monotonic() - started, 1), "device_changes": False}
    if confirmed and refresh is not None:
        result["ha_firmware_refresh"] = refresh()
    return result


def checked_live_base(rpc):
    value, _elapsed = rpc.call("get_node", {"node_id": NODE})
    require_base(filter_node(value))
    actual, timings = live_information(rpc)
    if tuple(actual[path] for path in BASIC_PATHS) != (BASE_VERSION, BASE_STRING, VID, PID):
        raise GuardError("live_base_identity_version")
    fresh, _elapsed = rpc.call("get_node", {"node_id": NODE})
    fresh = filter_node(fresh)
    require_base(fresh)
    return fresh, actual, timings


def checked_restart(rpc):
    """Discover the explicitly advertised controller action before one invoke."""
    actual, timings = live_information(rpc)
    if tuple(actual[path] for path in BASIC_PATHS) != (TARGET_VERSION, TARGET_STRING, VID, PID):
        raise GuardError("restart_live_identity_version")
    node, _elapsed = rpc.call("get_node", {"node_id": NODE})
    node = filter_node(node)
    if not node["available"] or not node["ota_requestor_root"]:
        raise GuardError("restart_unavailable_or_ota_missing")
    def read(path):
        values, _elapsed = rpc.call("read_attribute", {"node_id": NODE, "attribute_path": path}, timeout=10)
        if not isinstance(values, dict) or path not in values:
            raise GuardError("restart_attribute_missing")
        return values[path]
    state, schema, allowed, pending = (read(path) for path in (
        "0/42/2", "0/4294048768/0", "0/4294048768/13", "0/4294048768/15"))
    if type(state) is not int or state != 1 or type(schema) is not int or schema not in (1, 2) or allowed is not True or pending is not False:
        raise GuardError("restart_not_allowed_or_ota_active")
    actions, endpoints = read("0/37/0"), read("0/37/1")
    if not isinstance(actions, list) or len(actions) != 1 or not isinstance(endpoints, list) or len(endpoints) != 1:
        raise GuardError("restart_action_discovery")
    action, endpoint_list = actions[0], endpoints[0]
    if not isinstance(action, dict) or set(action) != {"0", "1", "2", "3", "4", "5"} or \
            not all(type(action.get(key)) is int for key in ("0", "2", "3", "4", "5")) or \
            tuple(action[key] for key in ("0", "1", "2", "3", "4", "5")) != (1, "Restart controller", 0, 1, 1, 0):
        raise GuardError("restart_action_metadata")
    if not isinstance(endpoint_list, dict) or set(endpoint_list) != {"0", "1", "2", "3"} or \
            type(endpoint_list.get("0")) is not int or endpoint_list["0"] != 1 or \
            endpoint_list.get("1") != "Controller" or type(endpoint_list.get("2")) is not int or endpoint_list["2"] != 0 or \
            not isinstance(endpoint_list.get("3"), list) or len(endpoint_list["3"]) != 1 or \
            type(endpoint_list["3"][0]) is not int or endpoint_list["3"][0] != 0:
        raise GuardError("restart_endpoint_metadata")
    return actual, timings


def run_remote(operation, image, metadata, rpc, http=http_json):
    if operation not in OPERATIONS - {"validate-image"}:
        raise GuardError("operation")
    if operation == "preflight":
        node, elapsed = rpc.call("get_node", {"node_id": NODE})
        return {"node": filter_node(node), "cache_request_ms": elapsed, "addon": supervisor_preflight(http),
                "cache_only": True, "device_changes": False}
    if operation in ("status", "diagnostics"):
        actual, timings = live_information(rpc)
        if (actual["0/40/2"], actual["0/40/4"]) != (VID, PID):
            raise GuardError("live_identity")
        node, elapsed = rpc.call("get_node", {"node_id": NODE})
        result = {"node": filter_node(node), "live_basic_information": actual, "live_read_ms": timings,
                  "cache_request_ms": elapsed, "device_changes": False}
        if operation == "diagnostics":
            result["live_diagnostics"] = live_diagnostics(rpc)
            result["diagnostics_complete"] = all(item["available"] for item in result["live_diagnostics"].values())
            result["continuous_monitoring"] = False
        return result
    if operation == "restart-controller":
        actual, timings = checked_restart(rpc)
        _response, elapsed = rpc.call("device_command", {
            "node_id": NODE, "endpoint_id": 0, "cluster_id": 37,
            "command_name": "instantAction", "payload": {"actionID": 1}}, timeout=20)
        # The server already validates a successful command status. Its raw
        # response is intentionally omitted; acceptance is not proof of reboot.
        return {"requested_node": NODE, "controller_restart_requested": True,
                "command_accepted": True, "request_ms": elapsed, "automatic_retry": False,
                "live_basic_information": actual, "live_read_ms": timings,
                "reboot_confirmation_required": True, "pairing_reset_requested": False}
    if operation == "import-image":
        # The complete image was read, revalidated and hashed before this ticket.
        node, actual, timings = checked_live_base(rpc)
        ticket, _elapsed = rpc.call("initiate_ota_upload")
        upload_id, expires = validate_ticket(ticket, len(image))
        issued = time.monotonic()
        record = http("core-matter-server", 5580, "POST", "/ota-upload/" + upload_id, body=image,
                      headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(image))},
                      timeout=min(15, expires))
        imported = require_update(record)
        return {"imported": imported, "image": metadata, "upload_ms": round((time.monotonic() - issued) * 1000, 1),
                "installed": False, "server_exposes_digest": False, "node": node,
                "live_basic_information": actual, "live_read_ms": timings}
    node, _elapsed = rpc.call("get_node", {"node_id": NODE})
    node = filter_node(node)
    require_base(node)
    if operation == "check-update":
        require_imported_image(image, metadata)
        update, elapsed = rpc.call("check_node_update", {"node_id": NODE})
        return {"node": node, "update": filter_update(update), "check_ms": elapsed,
                "expected_update": bool(update is not None and require_candidate(update, image, metadata)),
                "image": metadata, "server_exposes_digest": False, "device_changes": False}
    if not metadata or not metadata.get("import_receipt_verified"):
        raise GuardError("matching_import_receipt_required")
    actual, timings = live_information(rpc)
    if tuple(actual[path] for path in BASIC_PATHS) != (BASE_VERSION, BASE_STRING, VID, PID):
        raise GuardError("live_base_identity_version")
    fresh, _elapsed = rpc.call("get_node", {"node_id": NODE})
    require_base(filter_node(fresh))
    update, check_ms = rpc.call("check_node_update", {"node_id": NODE})
    candidate = require_candidate(update, image, metadata)
    result, elapsed = rpc.call("update_node", {"node_id": NODE, "software_version": TARGET_VERSION}, timeout=60)
    return {"requested_node": NODE, "requested_version": TARGET_VERSION, "live_basic_information": actual,
            "live_read_ms": timings, "candidate": candidate, "check_ms": check_ms, "update_response_ms": elapsed,
            "update_response": require_candidate(result, image, metadata), "image": metadata,
            "device_reboot_and_version_confirmation_required": True, "server_exposes_digest": False}


def remote_main(parser):
    from ha_ws_remote import WebSocket
    client = None
    operation = "unknown"
    rpc = None
    try:
        length_bytes = sys.stdin.buffer.read(4)
        if len(length_bytes) != 4:
            raise GuardError("input_metadata_length")
        length = struct.unpack("!I", length_bytes)[0]
        if not 0 < length <= 16384:
            raise GuardError("input_metadata_size")
        envelope = json.loads(sys.stdin.buffer.read(length))
        operation = envelope.get("operation")
        if operation not in OPERATIONS - {"validate-image"}:
            raise GuardError("input_operation")
        image = sys.stdin.buffer.read(MAX_IMAGE_BYTES + 1)
        metadata = None
        if operation in ("import-image", "check-update", "install-update"):
            supplied = envelope.get("image")
            if not isinstance(supplied, dict) or not SHA256.fullmatch(supplied.get("ota_sha256", "")):
                raise GuardError("input_expected_digest")
            metadata = validate_image_bytes(image, supplied["ota_sha256"], parser)
            if any(metadata.get(key) != supplied.get(key) for key in metadata):
                raise GuardError("input_image_metadata")
            metadata["import_receipt_verified"] = envelope.get("import_receipt_verified") is True
        elif image:
            raise GuardError("unexpected_image_input")
        if operation == "monitor-health":
            from ha_health import monitor_health
            def factory():
                return MatterRPC(WebSocket.connect("core-matter-server", 5580, "/ws", timeout=5), operation)
            result = monitor_health(factory, envelope.get("seconds"), envelope.get("interval"),
                                    envelope.get("stale_after"), envelope.get("tracker_state"),
                                    emit=lambda item: print(json.dumps(item), flush=True))
        else:
            client = WebSocket.connect("core-matter-server", 5580, "/ws", timeout=10)
            rpc = MatterRPC(client, operation)
            if operation == "watch-update":
                from ha_presentation import refresh_firmware
                result = watch_update(rpc, envelope.get("seconds"),
                                      refresh=lambda: refresh_firmware(NODE, TARGET_STRING))
            else:
                result = run_remote(operation, image, metadata, rpc)
        print(json.dumps({"success": True, "operation": operation, "result": result}), flush=True)
    except BaseException as error:
        reason = str(error) if isinstance(error, GuardError) else type(error).__name__
        print(json.dumps({"success": False, "operation": operation, "reason": reason,
                          "outcome_may_be_uncertain": bool(rpc and rpc.mutation_started),
                          "last_command": rpc.last_command if rpc else None,
                          "last_attribute_path": rpc.last_attribute_path if rpc else None,
                          "last_request_elapsed_ms": round((time.monotonic() - rpc.last_started) * 1000, 1) if rpc and rpc.last_started else None,
                          "automatic_retry": False}), flush=True)
        raise SystemExit(1)
    finally:
        if client is not None:
            try:
                client.close()
            except OSError:
                pass


def ssh_command(root):
    from ha_access import load_access, ssh_prefix
    config = load_access(root)
    ws_source = (root / "tools/ha_ws_remote.py").read_text()
    source = (root / "tools/ha_ota.py").read_text()
    health_source = (root / "tools/ha_health.py").read_text()
    presentation_source = (root / "tools/ha_presentation.py").read_text()
    parser_source = inspect.getsource(tlv_parser())
    payload = (
        "import sys,types\n"
        "m=types.ModuleType('ha_ws_remote')\nexec({!r},m.__dict__)\nsys.modules['ha_ws_remote']=m\n"
        "ota=types.ModuleType('ha_ota')\nsys.modules['ha_ota']=ota\nexec({!r},ota.__dict__)\n"
        "ota.NODE={!r}\nexec({!r},ota.__dict__)\n"
        "health=types.ModuleType('ha_health')\nsys.modules['ha_health']=health\nexec({!r},health.__dict__)\n"
        "presentation=types.ModuleType('ha_presentation')\nsys.modules['ha_presentation']=presentation\nexec({!r},presentation.__dict__)\n"
        "ota.remote_main(ota.read_tlv_header)\n"
    ).format(ws_source, source, config["node_id"], parser_source, health_source, presentation_source)
    return ssh_prefix(config) + ["python3 -u -c " + shlex.quote(payload)]


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def matching_receipt(root, metadata):
    path = root / "private/ha-ota/receipts" / (metadata["ota_sha256"] + ".json")
    if not path.is_file():
        raise GuardError("matching_import_receipt_required")
    receipt = json.loads(path.read_text())
    if receipt.get("success") is not True or receipt.get("operation") != "import-image" or \
            any(receipt.get("result", {}).get("image", {}).get(key) != value for key, value in metadata.items()):
        raise GuardError("import_receipt_mismatch")
    require_update(receipt["result"].get("imported"))
    return True


def invoke_ssh(root, envelope, image, folder):
    operation = envelope["operation"]
    header = json.dumps(envelope).encode("utf-8")
    data = struct.pack("!I", len(header)) + header + image
    uncertain = operation in ("import-image", "install-update", "restart-controller")
    def failure(reason):
        return {"success": False, "operation": operation, "reason": reason,
                "outcome_may_be_uncertain": uncertain, "automatic_retry": False}
    if operation not in ("watch-update", "monitor-health"):
        try:
            result = subprocess.run(ssh_command(root), input=data, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=420)
        except subprocess.TimeoutExpired:
            return failure("ssh_timeout")
        if len(result.stdout) > 65536:
            return failure("remote_report_size")
        try:
            record = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            return failure("remote_report_missing")
        if not isinstance(record, dict) or record.get("operation") != operation or type(record.get("success")) is not bool:
            return failure("remote_report_format")
        if result.returncode and record["success"]:
            return failure("remote_exit_status")
        return record
    process = subprocess.Popen(ssh_command(root), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, bufsize=0)
    process.stdin.write(data)
    process.stdin.close()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    pending = bytearray()
    record = None
    started = time.monotonic()
    try:
        with (folder / "events.jsonl").open("x") as events:
            while selector.get_map() and time.monotonic() - started < envelope["seconds"] + 320:
                for key, _mask in selector.select(1):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    pending.extend(chunk)
                    if len(pending) > 131072:
                        return failure("remote_record_size")
                    while b"\n" in pending:
                        line, _, tail = pending.partition(b"\n")
                        pending = bytearray(tail)
                        value = json.loads(line)
                        if not isinstance(value, dict):
                            return failure("remote_record_format")
                        if value.get("operation") == operation and type(value.get("success")) is bool:
                            record = value
                        elif value.get("kind") in ("ota_watch_ready", "ota_watch_heartbeat", "ota_live_confirmed",
                                                   "ota_attribute", "ota_node", "ota_available", "health_sample") and value.get("node_id") == NODE:
                            events.write(json.dumps(value) + "\n")
                            events.flush()
                            if not envelope.get("json_only"):
                                print(json.dumps(value), flush=True)
                        else:
                            return failure("unexpected_remote_record")
            return record or failure("watch_report_missing")
    finally:
        selector.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        process.stdout.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    for operation in sorted(OPERATIONS):
        command = sub.add_parser(operation)
        if operation in ("validate-image", "import-image", "check-update", "install-update"):
            command.add_argument("--image", type=Path, required=True)
            command.add_argument("--expected-sha256", required=operation != "validate-image")
        if operation == "watch-update":
            command.add_argument("--seconds", type=int, default=600)
        if operation == "monitor-health":
            command.add_argument("--seconds", type=int, default=300, help="0 for one bounded live sample, otherwise 30..86400")
            command.add_argument("--interval", type=int, default=60, help="30..3600 seconds between samples")
            command.add_argument("--stale-after", type=int, default=180, help="At least twice the interval, at most21600")
            command.add_argument("--json", action="store_true", help="Print one JSON report, suppress streaming stdout")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    global NODE
    os.umask(0o077)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = root / "private/ha-ota" / stamp
    folder.mkdir(parents=True, mode=0o700)
    report = folder / "report.json"
    image, metadata, receipt = b"", None, False
    monitor_lock = None
    try:
        if args.operation != "validate-image":
            from ha_access import load_access
            NODE = load_access(root)["node_id"]
        if args.operation in ("validate-image", "import-image", "check-update", "install-update"):
            image, metadata = read_local_image(args.image, args.expected_sha256)
        if args.operation in ("check-update", "install-update"):
            receipt = matching_receipt(root, metadata)
        if args.operation == "validate-image":
            record = {"success": True, "operation": args.operation, "result": {"image": metadata, "network_used": False}}
        else:
            if args.operation == "watch-update" and not 30 <= args.seconds <= 900:
                raise GuardError("watch_duration")
            envelope = {"operation": args.operation, "image": metadata, "import_receipt_verified": receipt}
            if args.operation == "watch-update":
                envelope["seconds"] = args.seconds
            if args.operation == "monitor-health":
                from ha_health import acquire_monitor_lock, validate_options, validate_tracker_state
                validate_options(args.seconds, args.interval, args.stale_after)
                monitor_lock = acquire_monitor_lock(root)
                state_path = root / "private/ha-health/state.json"
                state = None
                if state_path.is_file():
                    if state_path.is_symlink() or state_path.stat().st_size > 8192:
                        raise GuardError("health_state_file")
                    state = validate_tracker_state(json.loads(state_path.read_text()))
                envelope.update(seconds=args.seconds, interval=args.interval, stale_after=args.stale_after,
                                tracker_state=state, json_only=args.json)
            record = invoke_ssh(root, envelope, image, folder)
    except (GuardError, OSError, ValueError) as error:
        record = {"success": False, "operation": args.operation,
                  "reason": str(error) if isinstance(error, GuardError) else type(error).__name__, "automatic_retry": False}
    if args.operation == "monitor-health" and record["success"]:
        from ha_health import save_tracker_state
        try:
            save_tracker_state(root, record.get("result", {}).get("tracker_state"))
        except (GuardError, OSError, ValueError):
            record = {"success": False, "operation": args.operation, "reason": "health_state_not_saved", "automatic_retry": False}
    if monitor_lock is not None:
        os.close(monitor_lock)
    record["created_utc"] = utc_now()
    private_json(report, record)
    if args.operation == "import-image" and record["success"]:
        private_json(root / "private/ha-ota/receipts" / (metadata["ota_sha256"] + ".json"), record)
    print(json.dumps(dict(record, private_report=str(report)), indent=2), flush=True)
    if not record["success"]:
        raise SystemExit(1)
    if args.operation == "monitor-health" and record.get("result", {}).get("latest", {}).get("health_state") != "ok":
        raise SystemExit(2)


if __name__ == "__main__":
    # Share the configured target with the health module when this file is
    # launched as a script, rather than creating a second unconfigured import.
    sys.modules.setdefault("ha_ota", sys.modules[__name__])
    main()
