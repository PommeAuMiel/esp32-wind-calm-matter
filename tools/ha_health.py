"""Bounded, read-only health sampling for one privately configured Matter node.

This module has no network or filesystem side effects on import. The CLI is
``ha_ota.py monitor-health``. It never changes a device or installs HA jobs.
"""
import math
import json
import os
from pathlib import Path
import tempfile
import time

import ha_ota as ota


MAX_SECONDS = 86400
SAMPLE_SECONDS = 20
HEALTH_CLUSTER = 0xFFF1FC00
_VENDOR_NAMES = (
    "health_schema", "tuya_ready", "known_mask", "uart_checksum_errors", "uart_rejected",
    "tuya_recoveries", "report_revision", "command_timeouts", "light_publication_errors",
    "loop_progress", "matter_progress", "supervisor_state", "last_recovery_reason",
    "restart_allowed", "recovery_budget_count", "restart_pending",
    "free_heap_bytes", "minimum_free_heap_bytes", "largest_free_block_bytes",
    "light_known", "light_confirmed", "light_pending", "light_attribute_known",
    "light_attribute", "report_seen", "report_age_ms", "light_publication_attempts",
)
VENDOR_FIELDS = {"0/{}/{}".format(HEALTH_CLUSTER, index):
                 (name, bool if index in (1, 13, 15, 19, 20, 21, 22, 23, 24) else 0xFFFFFFFF)
                 for index, name in enumerate(_VENDOR_NAMES)}
SUPERVISOR_STATES = {0: "starting", 1: "healthy", 2: "updating", 3: "suspect", 4: "holdoff", 5: "recovering"}
RECOVERY_REASONS = {0: "none", 1: "startup_stalled", 2: "loop_stalled", 3: "matter_stalled",
                    4: "supervisor_stalled", 5: "manual_restart"}
READ_PATHS = ota.BASIC_PATHS + tuple(ota.DIAGNOSTIC_FIELDS) + tuple(VENDOR_FIELDS)
_COUNTERS = ("uart_checksum_errors", "uart_rejected", "tuya_recoveries", "command_timeouts",
             "light_publication_errors", "recovery_budget_count")
_PROGRESS = ("uptime_seconds", "loop_progress", "matter_progress")


def validate_options(seconds, interval, stale_after):
    if type(seconds) is not int or not (seconds == 0 or 30 <= seconds <= MAX_SECONDS):
        raise ota.GuardError("health_duration")
    if type(interval) is not int or not 30 <= interval <= 3600:
        raise ota.GuardError("health_interval")
    if type(stale_after) is not int or not 2 * interval <= stale_after <= 21600:
        raise ota.GuardError("health_stale_threshold")
    if seconds and seconds < interval:
        raise ota.GuardError("health_duration_interval")


def empty_snapshot(reason):
    return {"node_id": ota.NODE, "utc": ota.utc_now(), "cache_available": None,
            "device_reachable": False, "ota_requestor_root": None, "version": None,
            "version_string": None, "fields": {}, "read_ms": {}, "reason": reason,
            "device_changes": False}


def health_snapshot(rpc, deadline, clock=time.monotonic):
    """Read scalar fields in small batches; failed live reads never use a cache."""
    snapshot = empty_snapshot(None)
    def timeout():
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError()
        return min(5, remaining)
    try:
        value, _elapsed = rpc.call("get_node", {"node_id": ota.NODE}, timeout=timeout())
        node = ota.filter_node(value)
        snapshot["cache_available"] = node["available"]
        snapshot["ota_requestor_root"] = node["ota_requestor_root"]
    except (TimeoutError, ConnectionError, OSError, ota.GuardError):
        # A cached availability flag is optional; the live identity read is
        # still attempted within the same sample budget.
        snapshot["reason"] = "cache_unavailable"
    actual, failures = {}, {}
    for start in range(0, len(READ_PATHS), 9):
        paths = list(READ_PATHS[start:start + 9])
        try:
            values, elapsed = rpc.call("read_attribute", {"node_id": ota.NODE, "attribute_path": paths}, timeout=timeout())
            if not isinstance(values, dict):
                raise ota.GuardError("health_read_format")
        except (TimeoutError, ConnectionError, OSError):
            failures.update((path, "read_unavailable") for path in paths)
            # A client timeout does not cancel the server's Matter transaction.
            # Stop this sample rather than leave more reads in flight on a peer
            # that has stopped responding; retain earlier successful live data.
            failures.update((path, "not_attempted_after_transport_error")
                            for path in READ_PATHS[start + len(paths):])
            break
        except ota.GuardError:
            failures.update((path, "read_rejected") for path in paths)
            continue
        for path in paths:
            actual[path] = values.get(path)
            snapshot["read_ms"][path] = elapsed
    native = dict(ota.DIAGNOSTIC_FIELDS, **VENDOR_FIELDS)
    for path, (name, maximum) in native.items():
        item = {"available": False}
        value = actual.get(path)
        if path in failures:
            item["reason"] = failures[path]
        elif value is None:
            item["reason"] = "missing_or_null"
        elif (maximum is bool and type(value) is bool) or \
                (maximum is not bool and type(value) is int and 0 <= value <= maximum):
            item.update(available=True, value=value)
            labels = (SUPERVISOR_STATES if name == "supervisor_state" else
                      RECOVERY_REASONS if name == "last_recovery_reason" else
                      ota.BOOT_REASONS if name == "boot_reason" else
                      ota.THREAD_ROLES if name == "thread_routing_role" else None)
            if labels is not None:
                item["label"] = labels.get(value, "unknown")
        else:
            item["reason"] = "invalid_type_or_range"
        snapshot["fields"][name] = item
    version, version_string, vendor, product = (actual.get(path) for path in ota.BASIC_PATHS)
    valid_identity = (type(version) is int and 0 <= version <= 0xFFFFFFFF and
                      type(vendor) is int and vendor == ota.VID and
                      type(product) is int and product == ota.PID and
                      isinstance(version_string, str) and len(version_string) <= 128)
    snapshot["device_reachable"] = valid_identity
    if valid_identity:
        snapshot["version"] = version
        # Only strings from the reviewed release contract enter health output.
        expected = {ota.BASE_VERSION: ota.BASE_STRING, ota.TARGET_VERSION: ota.TARGET_STRING}
        snapshot["version_string"] = version_string if expected.get(version) == version_string else "unrecognized"
        snapshot["reason"] = None
    else:
        snapshot["reason"] = "live_identity_unavailable_or_mismatched"
    return snapshot


def _finite_epoch(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 4102444800


def validate_tracker_state(state):
    """Reject foreign targets and private/unbounded values before persisting."""
    if state is None:
        return None
    if not isinstance(state, dict) or set(state) != {"node_id", "version", "values", "advanced_at", "last_sample_at"} or \
            type(state.get("node_id")) is not int or state["node_id"] != ota.NODE or \
            not _finite_epoch(state.get("last_sample_at")) or \
            state.get("version") is not None and (type(state["version"]) is not int or not 0 <= state["version"] <= 0xFFFFFFFF):
        raise ota.GuardError("health_tracker_state")
    values, advanced = state.get("values"), state.get("advanced_at")
    allowed = {name: maximum for name, maximum in list(ota.DIAGNOSTIC_FIELDS.values()) + list(VENDOR_FIELDS.values())}
    if not isinstance(values, dict) or set(values) - set(allowed) or not isinstance(advanced, dict) or set(advanced) - set(_PROGRESS):
        raise ota.GuardError("health_tracker_fields")
    for name, value in values.items():
        maximum = allowed[name]
        if not ((maximum is bool and type(value) is bool) or
                (maximum is not bool and type(value) is int and 0 <= value <= maximum)):
            raise ota.GuardError("health_tracker_value")
    if not all(_finite_epoch(value) for value in advanced.values()):
        raise ota.GuardError("health_tracker_time")
    return {"node_id": ota.NODE, "version": state["version"], "values": dict(values),
            "advanced_at": dict(advanced), "last_sample_at": state["last_sample_at"]}


def acquire_monitor_lock(root):
    """Avoid concurrent collectors replacing each other's previous sample."""
    import fcntl
    folder = Path(root) / "private/ha-health"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(folder / "monitor.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as error:
        os.close(descriptor)
        raise ota.GuardError("health_monitor_already_running") from error
    return descriptor


def save_tracker_state(root, state):
    state = validate_tracker_state(state)
    if state is None:
        raise ota.GuardError("health_tracker_state_missing")
    folder = Path(root) / "private/ha-health"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", prefix="state-", suffix=".tmp", dir=folder, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump(state, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, folder / "state.json")
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class HealthTracker:
    def __init__(self, stale_after, state=None):
        self.stale_after = stale_after
        self.state = validate_tracker_state(state) or {
            "node_id": ota.NODE, "version": None, "values": {}, "advanced_at": {}, "last_sample_at": 0}

    def update(self, sample, now):
        if not _finite_epoch(now):
            raise ota.GuardError("health_clock")
        previous = self.state["values"]
        advanced = self.state["advanced_at"]
        fields = sample["fields"]
        values = {name: item["value"] for name, item in fields.items() if item.get("available") is True}
        alerts = []
        if self.state["last_sample_at"] > now:
            advanced.clear()
            alerts.append("clock_changed")
        uptime, old_uptime = values.get("uptime_seconds"), previous.get("uptime_seconds")
        reboot, old_reboot = values.get("reboot_count"), previous.get("reboot_count")
        reboot_detected = (uptime is not None and old_uptime is not None and uptime < old_uptime) or \
                          (reboot is not None and old_reboot is not None and reboot != old_reboot)
        if reboot_detected:
            alerts.append("reboot_detected")
            advanced.clear()
        old_version = self.state["version"]
        if sample["version"] is not None and old_version is not None and sample["version"] != old_version:
            alerts.append("version_changed")
        for name in _PROGRESS:
            if name in values and (name not in previous or values[name] != previous[name] or reboot_detected):
                advanced[name] = now
        if not sample["device_reachable"]:
            alerts.append("live_device_unavailable")
        if sample["cache_available"] is False:
            alerts.append("matter_cache_unavailable")
        if sample["device_reachable"] and sample["version_string"] == "unrecognized":
            alerts.append("unrecognized_version_string")
        if uptime is None:
            alerts.append("uptime_unavailable")
        role = values.get("thread_routing_role")
        if role is None:
            alerts.append("thread_role_unavailable")
        elif role not in (2, 3, 4, 5, 6):
            alerts.append("thread_not_attached")
        native_available = values.get("health_schema") in (1, 2)
        if sample["version"] == ota.TARGET_VERSION and not native_available:
            alerts.append("native_health_unavailable")
        if native_available:
            for name in ("tuya_ready", "known_mask", "loop_progress", "matter_progress", "supervisor_state"):
                if name not in values:
                    alerts.append(name + "_unavailable")
            supervisor = values.get("supervisor_state")
            if supervisor in (0, 3, 4, 5):
                alerts.append("runtime_" + SUPERVISOR_STATES[supervisor])
            elif supervisor is not None and supervisor not in (1, 2):
                alerts.append("runtime_state_unknown")
            if values.get("tuya_ready") is False:
                alerts.append("controller_not_ready")
            if values.get("known_mask") is not None and values["known_mask"] != 0x3F:
                alerts.append("controller_state_incomplete")
            if values.get("restart_pending") is True:
                alerts.append("restart_pending")
            for name in _COUNTERS:
                if not reboot_detected and name in values and name in previous and values[name] != previous[name]:
                    alerts.append(name + "_changed")
        ages = {name: round(max(0, now - timestamp), 1) for name, timestamp in advanced.items()}
        check_progress = native_available and values.get("supervisor_state") != 2
        stale = any(age >= self.stale_after for name, age in ages.items()
                    if name == "uptime_seconds" or check_progress)
        for name, age in ages.items():
            if age >= self.stale_after and (name == "uptime_seconds" or check_progress):
                alerts.append(name + "_stale")
        state = "unavailable" if not sample["device_reachable"] else "stale" if stale else "warning" if alerts else "ok"
        previous.update(values)
        if sample["version"] is not None:
            self.state["version"] = sample["version"]
        self.state["last_sample_at"] = now
        return dict(sample, health_state=state, alerts=alerts, reboot_detected=bool(reboot_detected),
                    stale=stale, progress_age_seconds=ages, native_health_available=native_available,
                    automatic_recovery=False)


def monitor_health(rpc_factory, seconds, interval, stale_after, previous=None,
                   emit=None, clock=time.monotonic, epoch=time.time, sleep=time.sleep):
    """Poll within a fixed duration; only read-only connections may be recreated."""
    validate_options(seconds, interval, stale_after)
    tracker = HealthTracker(stale_after, previous)
    started = clock()
    deadline = started + seconds
    next_sample = started
    counts = {"ok": 0, "warning": 0, "stale": 0, "unavailable": 0}
    samples, latest, rpc = 0, None, None
    try:
        while True:
            now = clock()
            if samples and (seconds == 0 or now >= deadline):
                break
            if now < next_sample:
                sleep(min(next_sample - now, max(0, deadline - now)))
                continue
            sample_deadline = min(clock() + SAMPLE_SECONDS, deadline) if seconds else clock() + SAMPLE_SECONDS
            try:
                if rpc is None:
                    rpc = rpc_factory()
                sample = health_snapshot(rpc, sample_deadline, clock)
            except (TimeoutError, ConnectionError, OSError, ota.GuardError):
                sample = empty_snapshot("connection_unavailable")
            latest = tracker.update(sample, epoch())
            samples += 1
            counts[latest["health_state"]] += 1
            if emit is not None:
                emit(dict(kind="health_sample", **latest))
            if not sample["device_reachable"] and rpc is not None:
                try:
                    rpc.client.close()
                except OSError:
                    pass
                rpc = None
            # Start-to-start interval; a late sample never causes a catch-up burst.
            next_sample = max(next_sample + interval, clock() + 0.01)
    finally:
        if rpc is not None:
            try:
                rpc.client.close()
            except OSError:
                pass
    return {"node_id": ota.NODE, "latest": latest, "samples": samples, "state_counts": counts,
            "elapsed_seconds": round(clock() - started, 1), "seconds_limit": seconds,
            "interval_seconds": interval, "stale_after_seconds": stale_after,
            "tracker_state": tracker.state, "automatic_recovery": False,
            "device_changes": False, "ha_automation_installed": False}
