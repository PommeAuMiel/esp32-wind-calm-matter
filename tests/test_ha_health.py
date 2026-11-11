"""Offline health failure/recovery and redaction tests; no HA or USB access."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import ha_ota as ota
ota.NODE = 7
import ha_health as health


class FakeClock:
    def __init__(self):
        self.seconds = 1000.0
    def now(self):
        return self.seconds
    def sleep(self, value):
        self.seconds += value


class FakeRPC:
    def __init__(self, clock, overrides=None, cache_available=True, read_error=None, read_error_on_batch=1):
        self.clock, self.calls, self.closed = clock, [], False
        self.client = self
        self.overrides, self.cache_available, self.read_error = overrides or {}, cache_available, read_error
        self.read_error_on_batch = read_error_on_batch
    def close(self):
        self.closed = True
    def call(self, command, args=None, timeout=20):
        self.calls.append((command, args, timeout))
        if command == "get_node":
            return {"node_id": ota.NODE, "available": self.cache_available,
                    "attributes": {"0/42/2": 1, "0/62/0": "private_fabric_secret"}}, 1
        if command != "read_attribute":
            raise AssertionError("A health monitor attempted a mutation")
        if self.read_error and sum(command == "read_attribute" for command, _args, _timeout in self.calls) == self.read_error_on_batch:
            self.clock.seconds += timeout
            raise self.read_error("private_error_details")
        values = {"0/40/9": ota.BASE_VERSION, "0/40/10": ota.BASE_STRING,
                  "0/40/2": ota.VID, "0/40/4": ota.PID, "0/51/1": 4,
                  "0/51/2": int(self.clock.seconds), "0/51/3": 1, "0/51/4": 5,
                  "0/53/0": 15, "0/53/1": 5}
        values.update(self.overrides)
        # Extra fields represent an untrusted remote response and never appear
        # in a filtered snapshot, even when individual requested values fail.
        return dict({path: values.get(path) for path in args["attribute_path"]}, secret="private_fabric_secret"), 2


def sample(uptime=100, reboot=4, reachable=True, role=5, **vendor):
    fields = {"uptime_seconds": {"available": uptime is not None},
              "reboot_count": {"available": reboot is not None},
              "thread_routing_role": {"available": role is not None}}
    for name, value in (("uptime_seconds", uptime), ("reboot_count", reboot), ("thread_routing_role", role)):
        if value is not None:
            fields[name]["value"] = value
    fields.update((name, {"available": True, "value": value}) for name, value in vendor.items())
    return dict(health.empty_snapshot(None), device_reachable=reachable, cache_available=reachable,
                version=ota.BASE_VERSION, version_string=ota.BASE_STRING, fields=fields)


class SnapshotTests(unittest.TestCase):
    def test_schema_two_light_and_memory_are_typed_and_reads_stay_readonly(self):
        values = {"0/4294048768/0": 2, "0/4294048768/16": 42000,
                  "0/4294048768/17": 38000, "0/4294048768/18": 32000,
                  "0/4294048768/19": True, "0/4294048768/20": False,
                  "0/4294048768/21": False, "0/4294048768/22": True,
                  "0/4294048768/23": False, "0/4294048768/24": False,
                  "0/4294048768/25": (1 << 32) - 1, "0/4294048768/26": 20}
        clock = FakeClock(); rpc = FakeRPC(clock, values)
        result = health.health_snapshot(rpc, 1020, clock.now)
        for path, value in values.items():
            name, _kind = health.VENDOR_FIELDS[path]
            self.assertTrue(result["fields"][name]["available"])
            self.assertEqual(result["fields"][name]["value"], value)
        # An unknown confirmed value remains distinguishable from OFF.
        self.assertFalse(result["fields"]["report_seen"]["value"])
        self.assertEqual(result["fields"]["report_age_ms"]["value"], (1 << 32) - 1)
        invalid = dict(values, **{"0/4294048768/20": 0, "0/4294048768/16": True})
        rejected = health.health_snapshot(FakeRPC(clock, invalid), 1020, clock.now)
        self.assertFalse(rejected["fields"]["light_confirmed"]["available"])
        self.assertFalse(rejected["fields"]["free_heap_bytes"]["available"])
        self.assertNotIn("private_", json.dumps(result))
        self.assertTrue(all(command in ("get_node", "read_attribute") for command, _, _ in rpc.calls))

    def test_scalar_live_batches_are_bounded_readonly_and_exclude_unrequested_private_values(self):
        clock = FakeClock(); rpc = FakeRPC(clock)
        result = health.health_snapshot(rpc, clock.now() + 20, clock.now)
        self.assertTrue(result["device_reachable"])
        self.assertEqual(result["fields"]["uptime_seconds"]["value"], 1000)
        self.assertNotIn("private_", json.dumps(result))
        paths = [path for command, args, _timeout in rpc.calls if command == "read_attribute" for path in args["attribute_path"]]
        self.assertEqual(paths, list(health.READ_PATHS))
        self.assertTrue(all(len(args["attribute_path"]) <= 9 for command, args, _timeout in rpc.calls if command == "read_attribute"))
        self.assertTrue(all(command in ("get_node", "read_attribute") for command, _args, _timeout in rpc.calls))

    def test_failed_live_reads_never_replace_uptime_with_a_cache_value(self):
        clock = FakeClock(); rpc = FakeRPC(clock, read_error=TimeoutError)
        result = health.health_snapshot(rpc, clock.now() + 20, clock.now)
        self.assertFalse(result["device_reachable"])
        self.assertNotIn("value", result["fields"]["uptime_seconds"])
        self.assertEqual(result["fields"]["uptime_seconds"]["reason"], "read_unavailable")
        self.assertEqual(len(rpc.calls), 2)
        self.assertEqual(result["fields"]["tuya_ready"]["reason"], "not_attempted_after_transport_error")
        self.assertLessEqual(clock.now(), 1020)
        self.assertNotIn("private_", json.dumps(result))

    def test_transport_failure_stops_later_batches_and_preserves_successful_live_fields(self):
        for error in (TimeoutError, ConnectionError, OSError):
            with self.subTest(error=error):
                clock = FakeClock()
                rpc = FakeRPC(clock, read_error=error, read_error_on_batch=2)
                result = health.health_snapshot(rpc, clock.now() + 20, clock.now)
                reads = [args["attribute_path"] for command, args, _timeout in rpc.calls if command == "read_attribute"]
                self.assertEqual(reads, [list(health.READ_PATHS[:9]), list(health.READ_PATHS[9:18])])
                self.assertTrue(result["device_reachable"])
                self.assertEqual(result["fields"]["uptime_seconds"]["value"], 1000)
                self.assertEqual(result["fields"]["tuya_ready"]["reason"], "read_unavailable")
                self.assertEqual(result["fields"]["loop_progress"]["reason"], "not_attempted_after_transport_error")
                self.assertNotIn("value", result["fields"]["loop_progress"])
                self.assertEqual(set(result["read_ms"]), set(health.READ_PATHS[:9]))
                self.assertNotIn("private_", json.dumps(result))

    def test_rejected_batch_does_not_stop_later_live_batches(self):
        clock = FakeClock()
        rpc = FakeRPC(clock, read_error=ota.GuardError, read_error_on_batch=2,
                      overrides={"0/4294048768/9": 12, "0/4294048768/10": 3})
        result = health.health_snapshot(rpc, clock.now() + 20, clock.now)
        reads = [args["attribute_path"] for command, args, _timeout in rpc.calls if command == "read_attribute"]
        self.assertEqual(reads, [list(health.READ_PATHS[start:start + 9])
                                 for start in range(0, len(health.READ_PATHS), 9)])
        self.assertTrue(result["device_reachable"])
        self.assertEqual(result["fields"]["tuya_ready"]["reason"], "read_rejected")
        self.assertEqual(result["fields"]["loop_progress"]["value"], 12)
        self.assertEqual(result["fields"]["matter_progress"]["value"], 3)
        self.assertNotIn("private_", json.dumps(result))

    def test_foreign_identity_unknown_version_and_invalid_values_cannot_claim_healthy_data(self):
        for overrides, reachable in (({"0/40/4": ota.PID + 1}, False),
                                     ({"0/40/9": True}, False),
                                     ({"0/40/10": "private_version_value"}, True)):
            clock = FakeClock()
            result = health.health_snapshot(FakeRPC(clock, overrides), 1020, clock.now)
            self.assertEqual(result["device_reachable"], reachable)
            self.assertNotIn("private_", json.dumps(result))
        clock = FakeClock()
        result = health.health_snapshot(FakeRPC(clock, {"0/51/2": -1, "0/51/1": True,
                   "0/4294048768/1": "private_bool", "0/4294048768/3": 1 << 32}), 1020, clock.now)
        for name in ("uptime_seconds", "reboot_count", "tuya_ready", "uart_checksum_errors"):
            self.assertFalse(result["fields"][name]["available"])
            self.assertNotIn("value", result["fields"][name])
        self.assertNotIn("private_", json.dumps(result))


class TrackerTests(unittest.TestCase):
    def test_successful_reads_with_frozen_uptime_eventually_become_stale(self):
        tracker = health.HealthTracker(180)
        self.assertEqual(tracker.update(sample(), 1000)["health_state"], "ok")
        self.assertEqual(tracker.update(sample(), 1120)["health_state"], "ok")
        result = tracker.update(sample(), 1180)
        self.assertEqual(result["health_state"], "stale")
        self.assertIn("uptime_seconds_stale", result["alerts"])
        self.assertFalse(result["automatic_recovery"])
        result = tracker.update(sample(uptime=101), 1181)
        self.assertEqual(result["health_state"], "ok")
        self.assertFalse(result["stale"])

    def test_unavailability_and_return_do_not_hide_a_reboot_between_samples(self):
        tracker = health.HealthTracker(180)
        tracker.update(sample(uptime=500), 1000)
        self.assertEqual(tracker.update(sample(uptime=None, reboot=None, reachable=False, role=None), 1060)["health_state"], "unavailable")
        result = tracker.update(sample(uptime=20, reboot=5), 1120)
        self.assertTrue(result["reboot_detected"])
        self.assertIn("reboot_detected", result["alerts"])
        self.assertEqual(result["health_state"], "warning")
        self.assertFalse(tracker.update(sample(uptime=80, reboot=5), 1180)["reboot_detected"])

    def test_counter_wrap_is_one_reboot_signal_and_not_a_repeated_fault(self):
        tracker = health.HealthTracker(180)
        tracker.update(sample(uptime=1000, reboot=65535), 1000)
        result = tracker.update(sample(uptime=5, reboot=0), 1060)
        self.assertTrue(result["reboot_detected"])
        self.assertEqual(result["alerts"].count("reboot_detected"), 1)
        self.assertFalse(tracker.update(sample(uptime=65, reboot=0), 1120)["reboot_detected"])

    def test_native_progress_stalls_even_if_matter_uptime_advances(self):
        tracker = health.HealthTracker(180)
        vendor = dict(health_schema=1, tuya_ready=True, known_mask=63,
                      loop_progress=90, matter_progress=40, supervisor_state=1, uart_checksum_errors=2)
        tracker.update(sample(**vendor), 1000)
        result = tracker.update(sample(uptime=280, **dict(vendor, matter_progress=41)), 1180)
        self.assertEqual(result["health_state"], "stale")
        self.assertIn("loop_progress_stale", result["alerts"])
        self.assertNotIn("matter_progress_stale", result["alerts"])
        self.assertNotIn("uart_checksum_errors_changed", result["alerts"])
        result = tracker.update(sample(uptime=340, **dict(vendor, loop_progress=91, matter_progress=42,
                                                  uart_checksum_errors=3)), 1240)
        self.assertIn("uart_checksum_errors_changed", result["alerts"])
        self.assertEqual(result["health_state"], "warning")

    def test_ota_suppresses_loop_progress_alarm_but_holdoff_and_recovery_remain_visible(self):
        tracker = health.HealthTracker(180)
        vendor = dict(health_schema=1, tuya_ready=True, known_mask=63,
                      loop_progress=90, matter_progress=40, supervisor_state=1)
        tracker.update(sample(**vendor), 1000)
        result = tracker.update(sample(uptime=280, **dict(vendor, supervisor_state=2)), 1180)
        self.assertEqual(result["health_state"], "ok")
        self.assertFalse(result["stale"])
        result = tracker.update(sample(uptime=340, **dict(vendor, supervisor_state=4)), 1240)
        self.assertIn("runtime_holdoff", result["alerts"])
        self.assertIn("loop_progress_stale", result["alerts"])
        result = tracker.update(sample(uptime=400, **dict(vendor, supervisor_state=5)), 1300)
        self.assertIn("runtime_recovering", result["alerts"])

    def test_detached_thread_incomplete_controller_and_clock_changes_are_explicit(self):
        tracker = health.HealthTracker(180)
        result = tracker.update(sample(role=1, health_schema=1, known_mask=1, tuya_ready=False), 1000)
        self.assertIn("thread_not_attached", result["alerts"])
        self.assertIn("controller_not_ready", result["alerts"])
        self.assertIn("controller_state_incomplete", result["alerts"])
        result = tracker.update(sample(), 999)
        self.assertIn("clock_changed", result["alerts"])
        self.assertFalse(result["stale"])

    def test_saved_state_detects_a_reboot_across_one_shot_processes_and_rejects_private_fields(self):
        first = health.HealthTracker(180)
        first.update(sample(uptime=200), 1000)
        second = health.HealthTracker(180, json.loads(json.dumps(first.state)))
        self.assertTrue(second.update(sample(uptime=10, reboot=5), 1060)["reboot_detected"])
        for changed in (dict(first.state, node_id=8), dict(first.state, secret="private_secret"),
                        dict(first.state, values={"fabric_secret": "private_secret"}),
                        dict(first.state, advanced_at={"uptime_seconds": float("nan")})):
            with self.assertRaises(ota.GuardError) as error:
                health.HealthTracker(180, changed)
            self.assertNotIn("private_", str(error.exception))


class MonitorTests(unittest.TestCase):
    def test_state_is_atomic_private_and_parallel_collectors_cannot_overwrite_a_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            descriptor = health.acquire_monitor_lock(root)
            try:
                with self.assertRaisesRegex(ota.GuardError, "health_monitor_already_running"):
                    health.acquire_monitor_lock(root)
                tracker = health.HealthTracker(180)
                tracker.update(sample(), 1000)
                health.save_tracker_state(root, tracker.state)
                path = root / "private/ha-health/state.json"
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(path.read_text())["values"]["uptime_seconds"], 100)
                before = path.read_bytes()
                with self.assertRaises(ota.GuardError):
                    health.save_tracker_state(root, dict(tracker.state, private="private_secret"))
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(list(path.parent.glob("*.tmp")), [])
            finally:
                os.close(descriptor)
            os.close(health.acquire_monitor_lock(root))

    def test_script_launch_shares_target_with_health_module_and_emits_one_json_without_network(self):
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "tools").mkdir(); (root / "private/access").mkdir(parents=True)
            for name in ("ha_ota.py", "ha_health.py", "ha_access.py", "ha_presentation.py"):
                shutil.copy(source_root / "tools" / name, root / "tools" / name)
            (root / "tools/thread_ota.py").write_text("def read_tlv_header(data):\n    return {},b''\n")
            (root / "tools/ha_ws_remote.py").write_text('''class WebSocket:
    @classmethod
    def connect(cls,*args,**kwargs):
        return cls()
    def send_json(self,request):
        self.request=request
    def recv_json(self,**kwargs):
        request=self.request
        if request['command']=='get_node':
            value={'node_id':9,'available':True,'attributes':{'0/42/2':1}}
        else:
            fields={'0/40/9':40004,'0/40/10':'4.0.4-thread-ota','0/40/2':65521,'0/40/4':32768,
                    '0/51/1':4,'0/51/2':1000,'0/53/0':15,'0/53/1':5}
            value={path:fields.get(path) for path in request['args']['attribute_path']}
        return {'message_id':request['message_id'],'result':value}
    def close(self):
        pass
''')
            for name in ("identity", "known_hosts"):
                (root / name).touch()
            config = {"host": "192.0.2.1", "user": "maintenance", "port": 22, "node_id": 9,
                      "identity_file": str(root / "identity"), "known_hosts_file": str(root / "known_hosts")}
            (root / "private/access/ha_ssh.json").write_text(json.dumps(config))
            binary = root / "bin"; binary.mkdir()
            fake_ssh = binary / "ssh"
            fake_ssh.write_text("#!" + sys.executable + "\nimport shlex,subprocess,sys\nsys.exit(subprocess.call(shlex.split(sys.argv[-1])))\n")
            fake_ssh.chmod(0o700)
            result = subprocess.run([sys.executable, str(root / "tools/ha_ota.py"), "monitor-health", "--seconds", "0", "--json"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
                                    env=dict(os.environ, PATH=str(binary) + os.pathsep + os.environ.get("PATH", "")))
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            report = json.loads(result.stdout)
            self.assertTrue(report["success"])
            self.assertEqual(report["result"]["latest"]["node_id"], 9)
            self.assertEqual(report["result"]["latest"]["health_state"], "ok")
            state = json.loads((root / "private/ha-health/state.json").read_text())
            self.assertEqual(state["node_id"], 9)

    def test_one_shot_and_fixed_duration_do_not_finish_early_on_a_healthy_version(self):
        for seconds, expected_samples in ((0, 1), (120, 2)):
            clock = FakeClock(); emitted = []; rpc = FakeRPC(clock)
            result = health.monitor_health(lambda: rpc, seconds, 60, 180, emit=emitted.append,
                                           clock=clock.now, epoch=clock.now, sleep=clock.sleep)
            self.assertEqual(result["samples"], expected_samples)
            self.assertEqual(result["elapsed_seconds"], seconds)
            self.assertEqual(len(emitted), expected_samples)
            self.assertTrue(rpc.closed)
            self.assertFalse(result["device_changes"])
            self.assertFalse(result["ha_automation_installed"])
            self.assertNotIn("private_", json.dumps(result))

    def test_failed_connections_are_only_retried_at_the_poll_interval_and_stop_on_time(self):
        clock = FakeClock(); attempts = []
        def factory():
            attempts.append(clock.now())
            raise ConnectionError("private_connect_details")
        result = health.monitor_health(factory, 180, 60, 180, clock=clock.now, epoch=clock.now, sleep=clock.sleep)
        self.assertEqual(attempts, [1000, 1060, 1120])
        self.assertEqual(result["state_counts"]["unavailable"], 3)
        self.assertEqual(result["elapsed_seconds"], 180)
        self.assertNotIn("private_", json.dumps(result))

    def test_invalid_options_and_wildcard_or_foreign_batch_never_reach_the_network(self):
        for args in ((True, 60, 180), (-1, 60, 180), (86401, 60, 180), (60, 29, 180),
                     (60, 60, 119), (60, 120, 240), (0, 3601, 8000)):
            with self.subTest(args=args), self.assertRaises(ota.GuardError):
                health.validate_options(*args)
        class Client:
            def send_json(self, _value):
                raise AssertionError("disallowed read reached the network")
        for paths, node in ((["*"], 7), (["0/62/0"], 7), (["0/51/2"], 8),
                            (["0/51/2"] * 2, 7), (["0/51/2"] * 10, 7), ("0/51/2", 7)):
            with self.subTest(paths=paths), self.assertRaises(ota.GuardError):
                ota.MatterRPC(Client(), "monitor-health").call("read_attribute", {"node_id": node, "attribute_path": paths})
        for command, args in (("check_node_update", {"node_id": 7}), ("initiate_ota_upload", {}),
                              ("update_node", {"node_id": 7, "software_version": ota.TARGET_VERSION}),
                              ("device_command", {"node_id": 7, "endpoint_id": 0, "cluster_id": 37,
                                                  "command_name": "instantAction", "payload": {"actionID": 1}})):
            with self.subTest(command=command), self.assertRaises(ota.GuardError):
                ota.MatterRPC(Client(), "monitor-health").call(command, args)


if __name__ == "__main__":
    unittest.main()
