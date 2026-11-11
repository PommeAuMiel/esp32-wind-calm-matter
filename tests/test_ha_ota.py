"""Offline OTA image, request-boundary and redaction checks. No real network."""
import hashlib
import contextlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import ha_ota as ota
ota.NODE = 7  # Synthetic test target; real targets come from private configuration.


def esp_image(version=ota.TARGET_STRING, project=ota.PROJECT_NAME, chip=13):
    data = bytearray(288)
    data[0], data[1], data[23] = 0xE9, 1, 1
    struct.pack_into("<H", data, 12, chip)
    struct.pack_into("<II", data, 24, 0x42000000, 256)
    struct.pack_into("<II", data, 32, 0xABCD5432, 0)
    data[48:48 + len(version)] = version.encode()
    data[80:80 + len(project)] = project.encode()
    checksum = 0xEF
    for value in data[32:]:
        checksum ^= value
    data += b"\0" * 15 + bytes((checksum,))
    return bytes(data) + hashlib.sha256(data).digest()


def container(payload=None, version=ota.TARGET_VERSION, version_string=ota.TARGET_STRING,
              minimum=ota.BASE_VERSION, vendor=ota.VID, digest=None):
    payload = esp_image() if payload is None else payload
    fields = {0: vendor, 1: ota.PID, 2: version, 3: version_string, 4: len(payload),
              5: minimum, 8: 1, 9: digest or hashlib.sha256(payload).digest()}
    tlv = bytearray(b"\x15")
    for tag, value in fields.items():
        if type(value) is int:
            tlv += bytes((0x26, tag)) + struct.pack("<I", value)
        elif isinstance(value, str):
            raw = value.encode()
            tlv += bytes((0x2C, tag, len(raw))) + raw
        else:
            tlv += bytes((0x30, tag, len(value))) + value
    tlv += b"\x18"
    return struct.pack("<IQI", 0x1BEEF11E, 16 + len(tlv) + len(payload), len(tlv)) + tlv + payload


def node(version=ota.BASE_VERSION, ota_root=True):
    attributes = {"0/40/2": ota.VID, "0/40/3": ota.PRODUCT_NAME, "0/40/4": ota.PID,
                  "0/40/5": "Wind Calm", "0/40/9": version, "0/40/10": ota.BASE_STRING,
                  "0/62/0": "private_fabric_secret"}
    if ota_root:
        attributes.update({"0/42/1": True, "0/42/2": 0})
    return {"node_id": ota.NODE, "available": True, "attributes": attributes, "fabric": "private_fabric_secret"}


def update(version=ota.TARGET_VERSION):
    return {"vid": ota.VID, "pid": ota.PID, "software_version": version,
            "software_version_string": ota.TARGET_STRING, "min_applicable_software_version": ota.BASE_VERSION,
            "max_applicable_software_version": None, "update_source": "local", "firmware_information": "private_metadata"}


class FakeRPC:
    def __init__(self, device=None, candidate=None, read_overrides=None):
        self.device = device or node()
        self.candidate = candidate or update()
        self.read_overrides = read_overrides or {}
        self.calls = []
    def call(self, command, args=None, timeout=20):
        self.calls.append((command, args))
        if command == "get_node":
            return self.device, 1.0
        if command == "check_node_update":
            return self.candidate, 2.0
        if command == "read_attribute":
            path = args["attribute_path"]
            return {path: self.read_overrides.get(path, self.device["attributes"].get(path))}, 3.0
        if command == "initiate_ota_upload":
            return {"upload_id": "0123456789abcdef0123456789abcdef", "expires_in": 60, "max_size": 64 * 1024 * 1024}, 1.0
        if command == "update_node":
            return self.candidate, 4.0
        raise AssertionError("unexpected command")


class ImageTests(unittest.TestCase):
    def test_valid_image_is_bound_to_payload_esp_version_project_and_expected_digest(self):
        data = container()
        expected = hashlib.sha256(data).hexdigest()
        value = ota.validate_image_bytes(data, expected)
        self.assertEqual(value["version"], ota.TARGET_VERSION)
        self.assertEqual(value["min_applicable_version"], ota.BASE_VERSION)
        self.assertEqual(value["payload_sha256"], hashlib.sha256(esp_image()).hexdigest())
        with self.assertRaises(ota.GuardError):
            ota.validate_image_bytes(data, "0" * 64)

    def test_bad_container_payload_and_descriptor_are_rejected(self):
        bad_esp = bytearray(esp_image()); bad_esp[-1] ^= 1
        inputs = (b"", b"x" * (ota.MAX_IMAGE_BYTES + 1), container()[:-1],
                  container(vendor=0x1234), container(version=ota.TARGET_VERSION + 1), container(minimum=0),
                  container(payload=esp_image(version=ota.BASE_STRING)), container(payload=esp_image(project="other")),
                  container(payload=esp_image(chip=5)), container(digest=b"x" * 32), container(payload=bytes(bad_esp)))
        for data in inputs:
            with self.subTest(length=len(data)), self.assertRaises(ota.GuardError):
                ota.validate_image_bytes(data)

    def test_header_only_version_change_cannot_turn_base_into_release(self):
        # A valid container digest cannot disguise an embedded application from the base release.
        with self.assertRaisesRegex(ota.GuardError, "esp_descriptor_version_project"):
            ota.validate_image_bytes(container(payload=esp_image(version=ota.BASE_STRING)))

    def test_bad_file_is_rejected_without_network_and_matching_receipt_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ota.GuardError):
                ota.read_local_image(root / "missing.ota")
            image = root / "valid.ota"; image.write_bytes(container())
            _data, metadata = ota.read_local_image(image)
            with self.assertRaises(ota.GuardError):
                ota.matching_receipt(root, metadata)
            record = {"success": True, "operation": "import-image", "result": {"image": metadata, "imported": update()}}
            path = root / "private/ha-ota/receipts" / (metadata["ota_sha256"] + ".json")
            ota.private_json(path, record)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(ota.matching_receipt(root, metadata))
            record["result"]["image"] = dict(metadata, payload_sha256="0" * 64)
            ota.private_json(path, record)
            with self.assertRaises(ota.GuardError):
                ota.matching_receipt(root, metadata)


class ProtocolTests(unittest.TestCase):
    def restart_rpc(self, overrides=None):
        device = node(version=ota.TARGET_VERSION)
        device["attributes"]["0/40/10"] = ota.TARGET_STRING
        values = {"0/42/2": 1, "0/4294048768/0": 1,
                  "0/4294048768/13": True, "0/4294048768/15": False,
                  "0/37/0": [{"0": 1, "1": "Restart controller", "2": 0, "3": 1, "4": 1, "5": 0}],
                  "0/37/1": [{"0": 1, "1": "Controller", "2": 0, "3": [0]}]}
        values.update(overrides or {})
        class RestartRPC(FakeRPC):
            def call(self, command, args=None, timeout=20):
                if command == "device_command":
                    self.calls.append((command, args))
                    return {"private_response": "private_details"}, 1
                return super().call(command, args, timeout)
        return RestartRPC(device=device, read_overrides=values)

    def test_restart_requires_live_target_discovery_and_guards_then_invokes_one_exact_action(self):
        rpc = self.restart_rpc()
        result = ota.run_remote("restart-controller", b"", None, rpc)
        self.assertTrue(result["controller_restart_requested"])
        self.assertTrue(result["reboot_confirmation_required"])
        self.assertFalse(result["pairing_reset_requested"])
        self.assertFalse(result["automatic_retry"])
        invokes = [args for command, args in rpc.calls if command == "device_command"]
        self.assertEqual(invokes, [{"node_id": 7, "endpoint_id": 0, "cluster_id": 37,
                                   "command_name": "instantAction", "payload": {"actionID": 1}}])
        self.assertNotIn("private_", json.dumps(result))

    def test_restart_never_invokes_a_foreign_action_during_ota_or_with_missing_identity(self):
        invalid = ({"0/42/2": 4}, {"0/42/2": True}, {"0/4294048768/0": 3},
                   {"0/4294048768/13": False}, {"0/4294048768/15": True},
                   {"0/40/9": ota.BASE_VERSION}, {"0/40/4": ota.PID + 1},
                   {"0/37/0": [{"0": 1, "1": "Toggle fan", "2": 0, "3": 1, "4": 1, "5": 0}]},
                   {"0/37/0": [{"0": True, "1": "Restart controller", "2": 0, "3": 1, "4": 1, "5": 0}]},
                   {"0/37/1": [{"0": 1, "1": "Controller", "2": 0, "3": [False]}]},
                   {"0/37/1": [{"0": 1, "1": "Controller", "2": 0, "3": [2]}]})
        for values in invalid:
            rpc = self.restart_rpc(values)
            with self.subTest(paths=list(values)), self.assertRaises(ota.GuardError):
                ota.run_remote("restart-controller", b"", None, rpc)
            self.assertFalse(any(command == "device_command" for command, _args in rpc.calls))

    def test_restart_rpc_timeout_is_uncertain_and_never_repeats_the_invoke(self):
        class Client:
            def __init__(self):
                self.sent = []
            def send_json(self, value):
                self.sent.append(value)
            def recv_json(self, **_kwargs):
                raise TimeoutError("private_timeout_details")
        clock = [0]
        def monotonic():
            clock[0] += 5
            return clock[0]
        client = Client()
        with patch.object(ota.time, "monotonic", side_effect=monotonic):
            rpc = ota.MatterRPC(client, "restart-controller")
            with self.assertRaises(TimeoutError):
                rpc.call("device_command", {"node_id": 7, "endpoint_id": 0, "cluster_id": 37,
                                           "command_name": "instantAction", "payload": {"actionID": 1}})
        self.assertEqual(len(client.sent), 1)
        self.assertTrue(rpc.mutation_started)

    def test_restart_rpc_rejects_other_endpoint_payloads_cluster_actions_and_bool_ids(self):
        class Client:
            def send_json(self, _value):
                raise AssertionError("invalid restart reached the network")
        exact = {"node_id": 7, "endpoint_id": 0, "cluster_id": 37,
                 "command_name": "instantAction", "payload": {"actionID": 1}}
        for args in (dict(exact, endpoint_id=1), dict(exact, endpoint_id=False),
                     dict(exact, cluster_id=6), dict(exact, payload={"actionID": True}),
                     dict(exact, payload={"actionID": 2}), dict(exact, node_id=8),
                     dict(exact, payload={"actionID": 1, "invokeID": 2})):
            with self.subTest(args=args), self.assertRaises(ota.GuardError):
                ota.MatterRPC(Client(), "restart-controller").call("device_command", args)
        for operation in ("monitor-health", "status", "diagnostics", "watch-update"):
            with self.subTest(operation=operation), self.assertRaises(ota.GuardError):
                ota.MatterRPC(Client(), operation).call("device_command", exact)

    def test_server_140_candidate_minimum_zero_requires_exact_image_and_strict_receipt(self):
        data = container()
        metadata = ota.validate_image_bytes(data)
        candidate = dict(update(), min_applicable_software_version=0,
                         max_applicable_software_version=None)
        with self.assertRaisesRegex(ota.GuardError, "matching_import_receipt_required"):
            ota.require_candidate(candidate, data, metadata)
        metadata["import_receipt_verified"] = True
        self.assertEqual(ota.require_candidate(candidate, data, metadata)["min_applicable_software_version"], 0)
        with self.assertRaises(ota.GuardError):
            ota.require_update(candidate)  # The upload receipt remains strict.
        for wrong in (dict(candidate, update_source="main-net-dcl"),
                      dict(candidate, software_version=ota.TARGET_VERSION + 1),
                      dict(candidate, min_applicable_software_version=39999)):
            with self.assertRaises(ota.GuardError):
                ota.require_candidate(wrong, data, metadata)
        with self.assertRaises(ota.GuardError):
            ota.require_candidate(candidate, data, dict(metadata, ota_sha256="0" * 64))
        with self.assertRaises(ota.GuardError):
            ota.require_candidate(candidate, data, dict(metadata, payload_sha256="0" * 64))
        rpc = FakeRPC(candidate=candidate)
        checked = ota.run_remote("check-update", data, metadata, rpc)
        self.assertTrue(checked["expected_update"])
        self.assertEqual([command for command, _args in rpc.calls], ["get_node", "check_node_update"])
        result = ota.run_remote("install-update", data, metadata, FakeRPC(candidate=candidate))
        self.assertEqual(result["update_response"]["min_applicable_software_version"], 0)

    def test_minimum_zero_receipt_or_wrong_digest_cannot_authorize_check(self):
        data = container(); metadata = ota.validate_image_bytes(data)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "private/ha-ota/receipts" / (metadata["ota_sha256"] + ".json")
            record = {"success": True, "operation": "import-image",
                      "result": {"image": metadata, "imported": dict(update(), min_applicable_software_version=0)}}
            ota.private_json(path, record)
            with self.assertRaises(ota.GuardError):
                ota.matching_receipt(root, metadata)
        rpc = FakeRPC(candidate=dict(update(), min_applicable_software_version=0))
        with self.assertRaises(ota.GuardError):
            ota.run_remote("check-update", data, metadata, rpc)
        self.assertEqual([command for command, _args in rpc.calls], ["get_node"])

    def test_watch_filters_other_nodes_then_confirms_new_version_by_live_reads(self):
        old, new = node(), node(version=ota.TARGET_VERSION)
        new["attributes"]["0/40/10"] = ota.TARGET_STRING
        class Client:
            def __init__(self):
                self.messages = [
                    {"event": "attribute_updated", "data": [8, "0/42/2", "private_other_node"]},
                    {"event": "attribute_updated", "data": [7, "0/42/0", "private_provider_location"]},
                    {"event": "attribute_updated", "data": [7, "0/42/2", 2]},
                    {"event": "attribute_updated", "data": [7, "0/40/9", ota.TARGET_VERSION]},
                ]
            def recv_json(self, **_kwargs):
                return self.messages.pop(0)
        class WatchRPC(FakeRPC):
            def __init__(self):
                super().__init__(new)
                self.client = Client()
            def call(self, command, args=None, timeout=20):
                if command == "start_listening":
                    self.calls.append((command, args))
                    return [dict(old, node_id=8, fabric="private_fabric_secret"), old], 1
                return super().call(command, args, timeout)
        rpc = WatchRPC()
        output = io.StringIO()
        refresh_calls = []
        def refresh():
            refresh_calls.append(True)
            return {"success": False, "reason": "ha_update_state_not_confirmed", "installation_requested": False}
        with contextlib.redirect_stdout(output):
            result = ota.watch_update(rpc, 30, refresh=refresh)
        self.assertTrue(result["live_target_version_confirmed"])
        self.assertEqual(refresh_calls, [True])
        self.assertFalse(result["ha_firmware_refresh"]["success"])
        self.assertFalse(result["ha_firmware_refresh"]["installation_requested"])
        self.assertIn("ota_watch_ready", output.getvalue())
        self.assertIn("ota_live_confirmed", output.getvalue())
        self.assertNotIn("private_", output.getvalue())
        self.assertEqual([command for command, _args in rpc.calls], ["start_listening"] + ["read_attribute"] * 4 + ["get_node"])

    def test_ssh_timeout_and_missing_report_are_structured_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            envelope = {"operation": "install-update", "image": {}, "import_receipt_verified": True}
            with patch.object(ota, "ssh_command", return_value=["ssh"]), \
                    patch.object(ota.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 420)) as run:
                report = ota.invoke_ssh(root, envelope, b"", root)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(report["reason"], "ssh_timeout")
            self.assertTrue(report["outcome_may_be_uncertain"])
            self.assertFalse(report["automatic_retry"])
            with patch.object(ota, "ssh_command", return_value=["ssh"]), \
                    patch.object(ota.subprocess, "run", return_value=SimpleNamespace(stdout=b"invalid_private_raw", returncode=1)):
                report = ota.invoke_ssh(root, envelope, b"", root)
            self.assertEqual(report["reason"], "remote_report_missing")
            self.assertNotIn("invalid_private_raw", json.dumps(report))

    def test_ticket_validation_and_no_ticket_in_public_report(self):
        for value in ({}, {"upload_id": "../secret", "expires_in": 60, "max_size": 10000},
                      {"upload_id": "x" * 32, "expires_in": 61, "max_size": 10000},
                      {"upload_id": "x" * 32, "expires_in": 60, "max_size": 2}):
            with self.assertRaises(ota.GuardError):
                ota.validate_ticket(value, 500)
        data = container(); metadata = ota.validate_image_bytes(data)
        calls = []
        def http(host, port, method, path, **kwargs):
            calls.append((host, port, method, path))
            self.assertEqual(kwargs["body"], data)
            return update()
        rpc = FakeRPC()
        result = ota.run_remote("import-image", data, metadata, rpc, http)
        self.assertEqual([item[0] for item in rpc.calls], ["get_node"] + ["read_attribute"] * 4 + ["get_node", "initiate_ota_upload"])
        self.assertEqual(len(calls), 1)
        serialized = json.dumps(result)
        self.assertNotIn("0123456789abcdef0123456789abcdef", serialized)
        self.assertNotIn("private_metadata", serialized)
        self.assertFalse(result["installed"])

    def test_request_allowlist_rejects_actions_other_nodes_and_wildcard_reads(self):
        class NeverClient:
            def send_json(self, _value):
                raise AssertionError("invalid command reached the network")
        for operation, command, args in (("preflight", "update_node", {"node_id": 7, "software_version": ota.TARGET_VERSION}),
                                         ("check-update", "initiate_ota_upload", {}),
                                         ("install-update", "device_command", {"node_id": 7}),
                                         ("install-update", "read_attribute", {"node_id": 8, "attribute_path": "0/40/9"}),
                                         ("install-update", "read_attribute", {"node_id": 7, "attribute_path": "*"}),
                                         ("install-update", "update_node", {"node_id": 8, "software_version": ota.TARGET_VERSION}),
                                         ("install-update", "update_node", {"node_id": 7, "software_version": ota.TARGET_VERSION + 1})):
            with self.subTest(command=command), self.assertRaises(ota.GuardError):
                ota.MatterRPC(NeverClient(), operation).call(command, args)

    def test_development_version_zero_and_missing_ota_block_check_and_install(self):
        for operation in ("check-update", "install-update"):
            for device in (node(version=0), node(ota_root=False)):
                rpc = FakeRPC(device)
                with self.subTest(operation=operation), self.assertRaises(ota.GuardError):
                    ota.run_remote(operation, b"", {"import_receipt_verified": True}, rpc)
                self.assertEqual([call[0] for call in rpc.calls], ["get_node"])

    def test_install_reads_four_actual_attributes_then_checks_exact_release(self):
        metadata = ota.validate_image_bytes(container()); metadata["import_receipt_verified"] = True
        rpc = FakeRPC()
        result = ota.run_remote("install-update", container(), metadata, rpc)
        self.assertEqual([command for command, _args in rpc.calls],
                         ["get_node"] + ["read_attribute"] * 4 + ["get_node", "check_node_update", "update_node"])
        self.assertEqual([args["attribute_path"] for command, args in rpc.calls if command == "read_attribute"], list(ota.BASIC_PATHS))
        self.assertEqual(rpc.calls[-1], ("update_node", {"node_id": 7, "software_version": ota.TARGET_VERSION}))
        self.assertNotIn("private_metadata", json.dumps(result))
        self.assertNotIn("private_fabric_secret", json.dumps(result))
        for rpc in (FakeRPC(read_overrides={"0/40/9": 0}), FakeRPC(candidate=update(version=ota.TARGET_VERSION + 1))):
            with self.assertRaises(ota.GuardError):
                ota.run_remote("install-update", container(), metadata, rpc)
            self.assertFalse(any(command == "update_node" for command, _args in rpc.calls))

    def test_preflight_is_cache_only_and_redacts_supervisor_token_options(self):
        def http(_host, _port, _method, _path, **kwargs):
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer private_fake_token")
            return {"data": {"state": "started", "version": "1.4.0", "options": {"enable_test_net_dcl": True,
                     "token": "private_fake_token"}, "secret": "private_fake_token"}}
        with patch.dict(ota.os.environ, {"SUPERVISOR_TOKEN": "private_fake_token"}):
            rpc = FakeRPC()
            result = ota.run_remote("preflight", b"", None, rpc, http)
        self.assertEqual([command for command, _args in rpc.calls], ["get_node"])
        self.assertNotIn("private_fake_token", json.dumps(result))
        self.assertNotIn("private_fabric_secret", json.dumps(result))

    def test_import_base_reads_and_candidate_source_are_guarded(self):
        data = container(); metadata = ota.validate_image_bytes(data)
        for device in (node(version=0), node(ota_root=False)):
            rpc = FakeRPC(device)
            with self.assertRaises(ota.GuardError):
                ota.run_remote("import-image", data, metadata, rpc)
            self.assertFalse(any(command == "initiate_ota_upload" for command, _args in rpc.calls))
        for candidate in (dict(update(), update_source="main-net-dcl"),
                          dict(update(), max_applicable_software_version=ota.TARGET_VERSION)):
            with self.assertRaises(ota.GuardError):
                ota.require_update(candidate)

    def test_status_reads_actual_version_and_reports_only_public_identity(self):
        rpc = FakeRPC()
        result = ota.run_remote("status", b"", None, rpc)
        self.assertEqual(result["live_basic_information"]["0/40/9"], ota.BASE_VERSION)
        self.assertEqual([command for command, _args in rpc.calls], ["read_attribute"] * 4 + ["get_node"])
        self.assertNotIn("private_fabric_secret", json.dumps(result))

    def test_diagnostics_reads_only_bounded_health_fields_without_mutations_or_secrets(self):
        values = {"0/51/1": 17, "0/51/2": 21600, "0/51/3": 6,
                  "0/51/4": 5, "0/53/0": 15, "0/53/1": 5}
        rpc = FakeRPC(read_overrides=values)
        result = ota.run_remote("diagnostics", b"", None, rpc)
        self.assertTrue(result["diagnostics_complete"])
        self.assertFalse(result["device_changes"])
        self.assertFalse(result["continuous_monitoring"])
        self.assertEqual(result["live_diagnostics"]["uptime_seconds"]["value"], 21600)
        self.assertEqual(result["live_diagnostics"]["boot_reason"]["label"], "software_update_completed")
        self.assertEqual(result["live_diagnostics"]["thread_routing_role"]["label"], "router")
        self.assertEqual([args["attribute_path"] for command, args in rpc.calls if command == "read_attribute"],
                         list(ota.BASIC_PATHS) + list(ota.DIAGNOSTIC_FIELDS))
        self.assertTrue(all(command in ("read_attribute", "get_node") for command, _args in rpc.calls))
        self.assertNotIn("private_fabric_secret", json.dumps(result))

    def test_diagnostics_missing_rejected_and_timeout_fields_are_explicit_not_cached(self):
        class PartialRPC(FakeRPC):
            def call(self, command, args=None, timeout=20):
                if command == "read_attribute" and args["attribute_path"] == "0/51/2":
                    raise TimeoutError()
                if command == "read_attribute" and args["attribute_path"] == "0/51/4":
                    raise ota.GuardError("server_rejected")
                return super().call(command, args, timeout)
        rpc = PartialRPC(read_overrides={"0/51/1": 17, "0/51/3": None, "0/53/0": 15, "0/53/1": 5})
        rpc.device["attributes"]["0/51/2"] = 99999  # Never substitute a cache value for a failed read.
        result = ota.run_remote("diagnostics", b"", None, rpc)
        self.assertFalse(result["diagnostics_complete"])
        fields = result["live_diagnostics"]
        self.assertEqual(fields["uptime_seconds"]["reason"], "timeout")
        self.assertEqual(fields["boot_reason"]["reason"], "server_rejected")
        self.assertEqual(fields["total_operational_hours"]["reason"], "missing_or_null")
        self.assertNotIn("value", fields["uptime_seconds"])
        self.assertTrue(fields["thread_routing_role"]["available"])

    def test_diagnostics_rejects_invalid_types_and_out_of_range_without_logging_values(self):
        for path, bad in (("0/51/1", True), ("0/51/1", 65536), ("0/51/2", -1),
                          ("0/51/2", 1 << 64), ("0/51/3", "private_invalid"),
                          ("0/51/4", {"secret": "private_invalid"}), ("0/53/1", 256)):
            with self.subTest(path=path, value_type=type(bad).__name__), \
                    self.assertRaisesRegex(ota.GuardError, "live_diagnostic_type_range"):
                ota.live_diagnostics(FakeRPC(read_overrides={path: bad}))

    def test_diagnostics_rpc_allowlist_rejects_actions_topology_and_cross_operation_reads(self):
        class NeverClient:
            def send_json(self, _value):
                raise AssertionError("invalid diagnostic command reached the network")
        for operation, command, args in (
                ("diagnostics", "update_node", {"node_id": 7, "software_version": ota.TARGET_VERSION}),
                ("diagnostics", "initiate_ota_upload", {}),
                ("diagnostics", "read_attribute", {"node_id": 7, "attribute_path": "0/53/7"}),
                ("diagnostics", "read_attribute", {"node_id": 7, "attribute_path": "0/62/0"}),
                ("diagnostics", "read_attribute", {"node_id": 8, "attribute_path": "0/51/2"}),
                ("diagnostics", "read_attribute", {"node_id": 7, "attribute_path": "*"}),
                ("install-update", "read_attribute", {"node_id": 7, "attribute_path": "0/51/2"})):
            with self.subTest(operation=operation, command=command), self.assertRaises(ota.GuardError):
                ota.MatterRPC(NeverClient(), operation).call(command, args)

    def test_uncertain_command_is_not_resent_and_server_details_are_not_logged(self):
        class Client:
            def __init__(self):
                self.sent = []
            def send_json(self, value):
                self.sent.append(value)
            def recv_json(self, **_kwargs):
                return {"message_id": self.sent[-1]["message_id"], "error_code": 11, "details": "private_server_details"}
        client = Client()
        with self.assertRaisesRegex(ota.GuardError, "server_rejected") as error:
            ota.MatterRPC(client, "install-update").call("update_node", {"node_id": 7, "software_version": ota.TARGET_VERSION})
        self.assertEqual(len(client.sent), 1)
        self.assertNotIn("private_server_details", str(error.exception))

    def test_rpc_receive_timeout_never_resends_mutation(self):
        class Client:
            def __init__(self):
                self.sent = []
            def send_json(self, value):
                self.sent.append(value)
            def recv_json(self, **_kwargs):
                raise TimeoutError()
        clock = [0]
        def monotonic():
            clock[0] += 5
            return clock[0]
        client = Client()
        with patch.object(ota.time, "monotonic", side_effect=monotonic):
            rpc = ota.MatterRPC(client, "install-update")
            with self.assertRaises(TimeoutError):
                rpc.call("update_node", {"node_id": 7, "software_version": ota.TARGET_VERSION})
        self.assertEqual(len(client.sent), 1)
        self.assertTrue(rpc.mutation_started)


if __name__ == "__main__":
    unittest.main()
