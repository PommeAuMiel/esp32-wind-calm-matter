#!/usr/bin/env python3
"""Observe one HA light and its Matter cache through an existing SSH access.

The remote program runs in memory, authenticates with SUPERVISOR_TOKEN already
present on HA, and emits only target-specific records. A live attribute read is
issued only after an explicit local ``signal --action read`` command.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import select
import selectors
import shlex
import subprocess
import sys
import time


ENTITY = None
NODE = None  # Loaded from private access configuration before a capture starts.
LIGHT_PATH = "1/6/0"
CACHE_PATHS = (LIGHT_PATH, "0/40/9", "0/40/10", "0/51/1", "0/51/2", "0/51/4")
LABEL = re.compile(r"[A-Za-z0-9_-]{1,48}\Z")
STAMP = re.compile(r"[0-9]{8}T[0-9]{12}Z\Z")
ACTIONS = frozenset(("snapshot", "read", "mark", "stop"))
KINDS = frozenset(("ready", "heartbeat", "ha_snapshot", "ha_state_changed", "matter_cache",
                   "matter_attribute", "matter_available", "matter_node_event", "mark",
                   "read_started", "read_result", "command_error", "request_error", "error", "complete"))


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def validate_command(value):
    if not isinstance(value, dict) or set(value) - {"action", "label"}:
        raise ValueError("invalid command fields")
    action, label = value.get("action"), value.get("label")
    if action not in ACTIONS or (label is not None and not isinstance(label, str)):
        raise ValueError("invalid capture action")
    if label is not None and not LABEL.fullmatch(label):
        raise ValueError("invalid marker label")
    if action == "mark" and label is None:
        raise ValueError("marker requires a label")
    return {"action": action, "label": label}


def filter_ha_state(state):
    if not isinstance(state, dict) or state.get("entity_id") != ENTITY:
        return None
    result = {"entity_id": ENTITY}
    for key in ("state", "last_changed", "last_updated", "last_reported"):
        value = state.get(key)
        if value is None or isinstance(value, str) and len(value) <= 128:
            result[key] = value
    return result


def filter_ha_event(message):
    if not isinstance(message, dict) or message.get("type") != "event":
        return None
    event = message.get("event")
    if not isinstance(event, dict) or event.get("event_type") != "state_changed":
        return None
    data = event.get("data")
    if not isinstance(data, dict) or data.get("entity_id") != ENTITY:
        return None
    result = {"entity_id": ENTITY, "old_state": filter_ha_state(data.get("old_state")),
              "new_state": filter_ha_state(data.get("new_state"))}
    if isinstance(event.get("time_fired"), str) and len(event["time_fired"]) <= 128:
        result["time_fired"] = event["time_fired"]
    return result


def filter_matter_node(node):
    if not isinstance(node, dict) or type(node.get("node_id")) is not int or node["node_id"] != NODE:
        return None
    safe_keys = {"node_id", "available", "attributes", "date_commissioned", "last_interview",
                 "attribute_subscriptions", "interview_version", "is_bridge"}
    result = {"node_id": NODE, "available": node.get("available") if type(node.get("available")) is bool else None,
              "keys_available": sorted(k for k in node if k in safe_keys), "attributes": {}}
    attributes = node.get("attributes")
    if isinstance(attributes, dict):
        for path in CACHE_PATHS:
            value = attributes.get(path)
            if path == LIGHT_PATH:
                if type(value) is bool or value is None:
                    result["attributes"][path] = value
            elif path == "0/40/10":
                if value is None or isinstance(value, str) and len(value) <= 128:
                    result["attributes"][path] = value
            elif value is None or type(value) is int and 0 <= value < 2 ** 64:
                result["attributes"][path] = value
    return result


def filter_matter_event(message):
    if not isinstance(message, dict):
        return None
    event, data = message.get("event"), message.get("data")
    if event == "attribute_updated" and isinstance(data, list) and len(data) == 3:
        if type(data[0]) is int and data[0] == NODE and data[1] == LIGHT_PATH and type(data[2]) is bool:
            return {"kind": "matter_attribute", "node_id": NODE, "attribute_path": LIGHT_PATH, "value": data[2]}
    elif event in ("node_added", "node_updated"):
        node = filter_matter_node(data)
        if node is not None:
            return {"kind": "matter_available", "node_id": NODE, "available": node["available"]}
    elif event == "node_removed" and type(data) is int and data == NODE:
        return {"kind": "matter_available", "node_id": NODE, "available": False, "removed": True}
    elif event == "node_event" and isinstance(data, dict):
        # Only public startup/shutdown events: BasicInformation (40), GeneralDiagnostics (51).
        if type(data.get("node_id")) is int and data["node_id"] == NODE and data.get("endpoint_id") == 0 \
                and (data.get("cluster_id"), data.get("event_id")) in ((40, 0), (40, 1), (51, 3)):
            result = {"kind": "matter_node_event", "node_id": NODE}
            for key in ("endpoint_id", "cluster_id", "event_id", "event_number", "timestamp", "timestamp_type"):
                if type(data.get(key)) is int and 0 <= data[key] < 2 ** 64:
                    result[key] = data[key]
            event_data = data.get("data")
            if isinstance(event_data, dict):
                for key in ("softwareVersion", "bootReason", "reason"):
                    if type(event_data.get(key)) is int and 0 <= event_data[key] < 2 ** 64:
                        result[key] = event_data[key]
            return result
    return None


class RemoteCapture:
    def __init__(self, seconds, ws_factory, emit=None):
        self.seconds = seconds
        self.factory = ws_factory
        self.start = time.monotonic()
        self.emit_sink = emit or (lambda value: print(json.dumps(value, separators=(",", ":")), flush=True))
        self.ws = {}
        self.pending = {}
        self.counter = 0
        self.ready_parts = set()
        self.ready = False
        self.stop = False
        self.read_job = None
        self.stdin_buffer = bytearray()

    def emit(self, kind, **fields):
        self.emit_sink(dict(kind=kind, utc=utc_now(), elapsed_ms=round((time.monotonic() - self.start) * 1000, 1), **fields))

    def request(self, source, command, args, callback):
        allowed = {"ha": ("subscribe_events", "get_states"),
                   "matter": ("start_listening", "get_node", "read_attribute")}
        if source not in allowed or command not in allowed[source]:
            raise ValueError("request command is not allowed")
        if command in ("get_states", "start_listening") and args != {}:
            raise ValueError("unexpected arguments for passive request")
        if command == "subscribe_events" and args != {"event_type": "state_changed"}:
            raise ValueError("only state_changed subscription is allowed")
        if command == "get_node" and args != {"node_id": NODE}:
            raise ValueError("get_node must target the configured node")
        if command == "read_attribute" and args != {"node_id": NODE, "attribute_path": LIGHT_PATH}:
            raise ValueError("read_attribute must target only the light attribute")
        if command == "read_attribute" and self.read_job is None:
            raise ValueError("live read requires an explicit read command")
        self.counter += 1
        identifier = self.counter if source == "ha" else "capture-{}".format(self.counter)
        value = {"id": identifier, "type": command} if source == "ha" else {"message_id": identifier, "command": command}
        if args:
            value.update(args) if source == "ha" else value.update(args=args)
        self.pending[(source, identifier)] = (time.monotonic(), callback, command)
        self.ws[source].send_json(value)

    def bootstrap(self):
        self.ws["ha"] = self.factory.connect("supervisor", 80, "/core/websocket", timeout=10)
        if self.ws["ha"].recv_json().get("type") != "auth_required":
            raise RuntimeError("unexpected HA authentication greeting")
        self.ws["ha"].send_json({"type": "auth", "access_token": os.environ["SUPERVISOR_TOKEN"]})
        if self.ws["ha"].recv_json().get("type") != "auth_ok":
            raise RuntimeError("HA authentication failed")
        self.ws["matter"] = self.factory.connect("core-matter-server", 5580, "/ws", timeout=10)

        def subscribed(value, error, elapsed):
            if error or value is not None:
                if error:
                    raise RuntimeError("HA subscription failed")
            self.ready_parts.add("ha_subscription")

        def listening(value, error, elapsed):
            if error or not isinstance(value, list):
                raise RuntimeError("Matter subscription failed")
            target = next((filter_matter_node(node) for node in value if filter_matter_node(node) is not None), None)
            if target is None:
                raise RuntimeError("target Matter node missing")
            self.emit("matter_cache", label="subscription_initial", node=target, request_ms=round(elapsed, 1))
            self.ready_parts.add("matter_subscription")

        self.request("ha", "subscribe_events", {"event_type": "state_changed"}, subscribed)
        self.request("matter", "start_listening", {}, listening)
        self.snapshot("initial", initial=True)

    def snapshot(self, label, initial=False, complete=None):
        completed = set()
        values = {}

        def result(source, value, error, elapsed):
            if error:
                self.emit("request_error", source=source, operation="snapshot", label=label, reason=error)
                values[source] = None
            elif source == "ha":
                target = next((filter_ha_state(item) for item in value if filter_ha_state(item) is not None), None) \
                    if isinstance(value, list) else None
                values[source] = target
                self.emit("ha_snapshot", label=label, state=target, request_ms=round(elapsed, 1))
            else:
                target = filter_matter_node(value)
                values[source] = target
                self.emit("matter_cache", label=label, node=target, request_ms=round(elapsed, 1))
            completed.add(source)
            if initial:
                if values[source] is None:
                    raise RuntimeError("initial target state missing")
                self.ready_parts.add(source + "_initial")
            if len(completed) == 2 and complete:
                complete(values)

        self.request("ha", "get_states", {}, lambda v, e, t: result("ha", v, e, t))
        self.request("matter", "get_node", {"node_id": NODE}, lambda v, e, t: result("matter", v, e, t))

    def explicit_read(self, label):
        if self.read_job is not None:
            self.emit("command_error", action="read", reason="read_already_pending", label=label)
            return
        self.read_job = label

        def before(values):
            if any(value is None for value in values.values()):
                self.read_job = None
                self.emit("command_error", action="read", reason="before_snapshot_failed", label=label)
                return
            self.emit("read_started", label=label, node_id=NODE, attribute_path=LIGHT_PATH)

            def read_result(value, error, elapsed):
                light = value.get(LIGHT_PATH) if isinstance(value, dict) else None
                self.emit("read_result", label=label, node_id=NODE, attribute_path=LIGHT_PATH,
                          value=light if type(light) is bool else None, error=error, request_ms=round(elapsed, 1))
                self.snapshot(label + "_after", complete=lambda _values: setattr(self, "read_job", None))

            self.request("matter", "read_attribute", {"node_id": NODE, "attribute_path": LIGHT_PATH}, read_result)

        self.snapshot(label + "_before", complete=before)

    def command(self, value):
        try:
            command = validate_command(value)
        except ValueError:
            self.emit("command_error", reason="invalid_command")
            return
        action, label = command["action"], command["label"] or command["action"]
        if action == "stop":
            self.stop = True
        elif not self.ready:
            self.emit("command_error", action=action, reason="not_ready", label=label)
        elif action == "mark":
            self.emit("mark", label=label)
        elif action == "snapshot":
            self.snapshot(label)
        elif action == "read":
            self.explicit_read(label)

    def message(self, source, value):
        if not isinstance(value, dict):
            return
        identifier = value.get("id") if source == "ha" else value.get("message_id")
        pending = self.pending.pop((source, identifier), None) if isinstance(identifier, (str, int)) else None
        if pending:
            sent, callback, _command = pending
            success = value.get("success") is True if source == "ha" else "error_code" not in value and "result" in value
            callback(value.get("result") if success else None, None if success else "server_rejected", (time.monotonic() - sent) * 1000)
        elif source == "ha":
            event = filter_ha_event(value)
            if event:
                self.emit("ha_state_changed", **event)
        else:
            event = filter_matter_event(value)
            if event:
                self.emit(event.pop("kind"), **event)
        if not self.ready and self.ready_parts == {"ha_subscription", "matter_subscription", "ha_initial", "matter_initial"}:
            self.ready = True
            self.emit("ready", entity_id=ENTITY, node_id=NODE, duration_limit_seconds=self.seconds,
                      automatic_live_reads=False, actuation_commands=False)

    def run(self):
        self.bootstrap()
        stdin_fd = sys.stdin.fileno()
        next_heartbeat = time.monotonic() + 5
        while not self.stop and time.monotonic() - self.start < self.seconds:
            fds = {client.fileno(): source for source, client in self.ws.items()}
            buffered = [fd for fd, source in fds.items() if self.ws[source].has_buffered_data()]
            readable = select.select([stdin_fd] + list(fds), [], [], 0 if buffered else 0.25)[0]
            if stdin_fd in readable:
                chunk = os.read(stdin_fd, 4096)
                if not chunk:
                    self.stop = True
                self.stdin_buffer.extend(chunk)
                if len(self.stdin_buffer) > 32768:
                    raise ValueError("command input exceeds limit")
                while b"\n" in self.stdin_buffer:
                    line, _, rest = self.stdin_buffer.partition(b"\n")
                    self.stdin_buffer = bytearray(rest)
                    try:
                        self.command(json.loads(line))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        self.emit("command_error", reason="invalid_json")
            for fd in set(buffered + readable) - {stdin_fd}:
                source = fds[fd]
                try:
                    self.message(source, self.ws[source].recv_json(timeout=0.5))
                except TimeoutError:
                    pass
            now = time.monotonic()
            for key, pending in list(self.pending.items()):
                if now - pending[0] >= 15:
                    self.pending.pop(key)
                    pending[1](None, "timeout", (now - pending[0]) * 1000)
            if now >= next_heartbeat:
                self.emit("heartbeat", ready=self.ready)
                next_heartbeat = now + 5
        self.emit("complete", reason="stop" if self.stop else "duration_limit")


def remote_main(seconds):
    from ha_ws_remote import WebSocket
    capture = RemoteCapture(seconds, WebSocket)
    failed = False
    try:
        capture.run()
    except BaseException as error:
        # Exception messages may include remote content. Never emit them.
        capture.emit("error", reason=type(error).__name__)
        failed = True
    finally:
        for client in capture.ws.values():
            try:
                client.close()
            except OSError:
                failed = True
    if failed:
        raise SystemExit(1)


def private_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def ssh_command(root, seconds):
    from ha_access import load_access, ssh_prefix
    config = load_access(root, require_light=True)
    ws_source = (root / "tools/ha_ws_remote.py").read_text()
    capture_source = (root / "tools/ha_light_capture.py").read_text()
    source = (
        "import sys,types\nm=types.ModuleType('ha_ws_remote')\nexec({!r},m.__dict__)\n"
        "sys.modules['ha_ws_remote']=m\nscope={{'__name__':'ha_light_capture_remote'}}\n"
        "exec({!r},scope)\nscope['NODE']={!r}\nscope['ENTITY']={!r}\nscope['remote_main']({})\n"
    ).format(ws_source, capture_source, config["node_id"], config["light_entity"], seconds)
    return ssh_prefix(config) + ["python3 -u -c " + shlex.quote(source)]


def signal_capture(args, root):
    if not STAMP.fullmatch(args.capture):
        raise SystemExit("Invalid capture identifier")
    folder = root / "private/ha-diagnostics" / args.capture
    if folder.is_symlink() or not (folder / "state.json").is_file():
        raise SystemExit("Unknown capture")
    state = json.loads((folder / "state.json").read_text())
    if not state.get("running"):
        raise SystemExit("Capture has finished")
    command = validate_command({"action": args.action, "label": args.label})
    descriptor = os.open(folder / "commands.jsonl", os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(command) + "\n")
        stream.flush()
    print(json.dumps({"queued": command["action"], "capture": args.capture}), flush=True)


def start_capture(args, root):
    from ha_access import load_access
    global NODE, ENTITY
    config = load_access(root, require_light=True)
    NODE, ENTITY = config["node_id"], config["light_entity"]
    if not 30 <= args.seconds <= 900:
        raise SystemExit("Capture duration must be 30..900 seconds")
    os.umask(0o077)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = root / "private/ha-diagnostics" / stamp
    folder.mkdir(mode=0o700, parents=True)
    command_path = folder / "commands.jsonl"
    command_path.touch(mode=0o600)
    state = {"running": True, "ready": False, "was_ready": False, "capture": stamp, "entity_id": ENTITY, "node_id": NODE,
             "duration_limit_seconds": args.seconds, "started_utc": utc_now(), "elapsed_seconds": 0,
             "automatic_live_reads": False, "actuation_commands": False, "explicit_reads": 0,
             "last_ha": None, "last_matter": None, "error_records": 0}
    private_json(folder / "state.json", state)
    process = subprocess.Popen(ssh_command(root, args.seconds), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, bufsize=0)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    start = time.monotonic()
    buffer = bytearray()
    next_state = start
    print("CAPTURE_STARTED: " + stamp, flush=True)
    try:
        with (folder / "events.jsonl").open("x") as events, command_path.open() as commands:
            while selector.get_map() and time.monotonic() - start < args.seconds + 35:
                for line in commands.readlines():
                    command = validate_command(json.loads(line))
                    process.stdin.write((json.dumps(command) + "\n").encode("utf-8"))
                    process.stdin.flush()
                for key, _mask in selector.select(0.25):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "stderr":
                        # SSH stderr is never persisted: only an exit code is reported.
                        continue
                    buffer.extend(chunk)
                    if len(buffer) > 131072:
                        raise ValueError("remote output exceeds record limit")
                    while b"\n" in buffer:
                        line, _, rest = buffer.partition(b"\n")
                        buffer = bytearray(rest)
                        record = json.loads(line)
                        if not isinstance(record, dict) or record.get("kind") not in KINDS:
                            raise ValueError("unexpected remote output record")
                        record["host_utc"] = utc_now()
                        record["host_elapsed_ms"] = round((time.monotonic() - start) * 1000, 1)
                        events.write(json.dumps(record, separators=(",", ":")) + "\n")
                        events.flush()
                        kind = record["kind"]
                        if kind == "ready":
                            state["ready"] = True
                            state["was_ready"] = True
                            print("READY: " + stamp, flush=True)
                        elif kind in ("error", "complete"):
                            state["ready"] = False
                        if kind in ("ha_snapshot", "ha_state_changed"):
                            state["last_ha"] = record.get("state", record.get("new_state"))
                        if kind == "matter_cache":
                            state["last_matter"] = record.get("node")
                        if kind == "matter_attribute" and state["last_matter"]:
                            state["last_matter"]["attributes"][LIGHT_PATH] = record["value"]
                        if kind == "matter_available" and state["last_matter"]:
                            state["last_matter"]["available"] = record["available"]
                        state["explicit_reads"] += kind == "read_started"
                        state["error_records"] += kind in ("error", "request_error", "command_error")
                        if kind != "heartbeat":
                            print(json.dumps(record, separators=(",", ":")), flush=True)
                if time.monotonic() >= next_state:
                    state["elapsed_seconds"] = round(time.monotonic() - start, 1)
                    private_json(folder / "state.json", state)
                    next_state = time.monotonic() + 1
    finally:
        selector.close()
        if process.poll() is None:
            try:
                process.stdin.write(b'{"action":"stop","label":null}\n')
                process.stdin.flush()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        state["running"] = False
        state["ready"] = False
        state["ssh_exit_code"] = process.returncode
        state["elapsed_seconds"] = round(time.monotonic() - start, 1)
        private_json(folder / "state.json", state)
    print(json.dumps(state, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--seconds", type=int, default=600)
    signal = sub.add_parser("signal")
    signal.add_argument("--capture", required=True)
    signal.add_argument("--action", choices=sorted(ACTIONS), required=True)
    signal.add_argument("--label")
    sub.add_parser("self-test", help="Run offline filtering and command boundary checks")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.command == "self-test":
        self_test()
    elif args.command == "start":
        start_capture(args, root)
    else:
        signal_capture(args, root)


def self_test():
    import contextlib
    import io
    import types
    import unittest
    from unittest.mock import patch

    class BoundaryTests(unittest.TestCase):
        def test_remote_failure_exits_nonzero_without_raw_error(self):
            class FailingWS:
                @staticmethod
                def connect(*_args, **_kwargs):
                    raise RuntimeError("private_failure_details")
            output = io.StringIO()
            with patch.dict(sys.modules, {"ha_ws_remote": types.SimpleNamespace(WebSocket=FailingWS)}), \
                    contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit) as error:
                    remote_main(600)
            self.assertEqual(error.exception.code, 1)
            self.assertNotIn("private_failure_details", output.getvalue())
            record = json.loads(output.getvalue())
            self.assertEqual((record["kind"], record["reason"]), ("error", "RuntimeError"))

        def test_filters_exclude_other_entities_and_attributes(self):
            target = {"entity_id": ENTITY, "state": "on", "attributes": {"secret": "never_emit"},
                      "context": {"user_id": "never_emit"}, "last_changed": "time"}
            self.assertEqual(filter_ha_state(target), {"entity_id": ENTITY, "state": "on", "last_changed": "time",
                                                      "last_updated": None, "last_reported": None})
            self.assertIsNone(filter_ha_state(dict(target, entity_id="light.another")))
            event = {"type": "event", "event": {"event_type": "state_changed", "time_fired": "time",
                     "data": {"entity_id": ENTITY, "old_state": target, "new_state": target, "secret": "never_emit"}}}
            self.assertNotIn("never_emit", json.dumps(filter_ha_event(event)))
            event["event"]["data"]["entity_id"] = "light.another"
            self.assertIsNone(filter_ha_event(event))

        def test_matter_cache_and_events_are_strictly_scoped(self):
            node = {"node_id": NODE, "available": True, "fabric_id": "never_emit",
                    "attributes": {LIGHT_PATH: True, "0/40/9": 0, "0/40/10": "3.1-thread-sdk",
                                   "0/51/1": 614, "0/51/2": 0, "0/51/4": 2, "0/62/0": "never_emit"}}
            filtered = filter_matter_node(node)
            self.assertNotIn("never_emit", json.dumps(filtered))
            self.assertNotIn("fabric_id", filtered["keys_available"])
            self.assertEqual(set(filtered["attributes"]), set(CACHE_PATHS))
            self.assertIsNone(filter_matter_node(dict(node, node_id=30)))
            self.assertIsNotNone(filter_matter_event({"event": "attribute_updated", "data": [NODE, LIGHT_PATH, False]}))
            for data in ([30, LIGHT_PATH, False], [NODE, "2/6/0", False], [NODE, LIGHT_PATH, {"secret": "value"}]):
                self.assertIsNone(filter_matter_event({"event": "attribute_updated", "data": data}))
            self.assertEqual(filter_matter_event({"event": "node_updated", "data": node})["available"], True)
            self.assertIsNone(filter_matter_event({"event": "server_info_updated", "data": node}))

        def test_public_startup_event_excludes_nested_data(self):
            data = {"node_id": NODE, "endpoint_id": 0, "cluster_id": 51, "event_id": 3, "event_number": 32,
                    "timestamp": 12345, "data": {"bootReason": 2, "fabric": "never_emit"}, "secret": "never_emit"}
            filtered = filter_matter_event({"event": "node_event", "data": data})
            self.assertEqual(filtered["bootReason"], 2)
            self.assertNotIn("never_emit", json.dumps(filtered))
            self.assertIsNone(filter_matter_event({"event": "node_event", "data": dict(data, node_id=30)}))
            self.assertIsNone(filter_matter_event({"event": "node_event", "data": dict(data, event_id=0)}))

        def test_commands_accept_only_bounded_actions_and_labels(self):
            for action in ACTIONS:
                self.assertEqual(validate_command({"action": action, "label": "phase_1"})["action"], action)
            for value in ({"action": "write"}, {"action": "snapshot", "node_id": 30},
                          {"action": "mark", "label": "a\nsecret"}, {"action": "mark"},
                          {"action": "read", "label": "x" * 49}, {"action": "stop", "label": ["bad"]}):
                with self.assertRaises(ValueError):
                    validate_command(value)

        def test_requests_cannot_write_or_read_other_targets(self):
            capture = RemoteCapture(600, None, emit=lambda _value: None)
            for source, command, args in (("matter", "device_command", {}), ("ha", "call_service", {}),
                                         ("matter", "get_node", {"node_id": 30}),
                                         ("matter", "read_attribute", {"node_id": NODE, "attribute_path": "*"}),
                                         ("ha", "subscribe_events", {"event_type": "*"}),
                                         ("matter", "read_attribute", {"node_id": NODE, "attribute_path": LIGHT_PATH})):
                with self.assertRaises(ValueError):
                    capture.request(source, command, args, lambda *_args: None)

        def test_passive_bootstrap_then_explicit_read_before_after(self):
            class FakeWS:
                def __init__(self, greetings=()):
                    self.greetings = list(greetings)
                    self.sent = []
                def recv_json(self):
                    return self.greetings.pop(0)
                def send_json(self, value):
                    self.sent.append(value)
            ha, matter = FakeWS([{"type": "auth_required"}, {"type": "auth_ok"}]), FakeWS()
            class Factory:
                @staticmethod
                def connect(host, *_args, **_kwargs):
                    return ha if host == "supervisor" else matter
            records = []
            capture = RemoteCapture(600, Factory, records.append)
            with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "fake_offline_token"}):
                capture.bootstrap()
            def respond(source, command, result):
                pending = next((key for key, value in capture.pending.items() if key[0] == source and value[2] == command), None)
                self.assertIsNotNone(pending)
                capture.message(source, {("id" if source == "ha" else "message_id"): pending[1],
                                         "success": True, "result": result})
            node = {"node_id": NODE, "available": True, "attributes": {LIGHT_PATH: True}}
            other = {"node_id": 30, "attributes": {LIGHT_PATH: "never_emit"}, "fabric": "never_emit"}
            state = [{"entity_id": ENTITY, "state": "on"}, {"entity_id": "other", "state": "never_emit"}]
            respond("ha", "subscribe_events", None)
            respond("matter", "start_listening", [other, node])
            respond("ha", "get_states", state)
            respond("matter", "get_node", node)
            self.assertTrue(capture.ready)
            self.assertFalse(any(message.get("command") == "read_attribute" for message in matter.sent))
            self.assertNotIn("never_emit", json.dumps(records))
            self.assertNotIn("fake_offline_token", json.dumps(records))
            capture.command({"action": "read", "label": "blocked"})
            self.assertFalse(any(message.get("command") == "read_attribute" for message in matter.sent))
            respond("matter", "get_node", node)
            respond("ha", "get_states", state)
            self.assertEqual(sum(message.get("command") == "read_attribute" for message in matter.sent), 1)
            respond("matter", "read_attribute", {LIGHT_PATH: False, "other": "never_emit"})
            respond("ha", "get_states", state)
            respond("matter", "get_node", node)
            self.assertIsNone(capture.read_job)
            self.assertEqual([record["value"] for record in records if record["kind"] == "read_result"], [False])
            self.assertNotIn("never_emit", json.dumps(records))

    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(BoundaryTests))
    if not result.wasSuccessful():
        raise SystemExit(1)


if __name__ == "__main__":
    main()
