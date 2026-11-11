#!/usr/bin/env python3
"""Targeted HA presentation and update-check maintenance through SSH/API."""
import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import time


class HAClient:
    def __init__(self):
        from ha_ws_remote import WebSocket
        import os
        self.socket = WebSocket.connect("supervisor", 80, "/core/websocket", timeout=10)
        self.sequence = 0
        try:
            self.socket.recv_json(timeout=10)
            self.socket.send_json({"type": "auth", "access_token": os.environ["SUPERVISOR_TOKEN"]})
            response = self.socket.recv_json(timeout=10)
            if not isinstance(response, dict) or response.get("type") != "auth_ok":
                raise ValueError("ha_auth_failed")
        except Exception:
            self.close()
            raise

    def call(self, command, **args):
        self.sequence += 1
        self.socket.send_json(dict(id=self.sequence, type=command, **args))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = self.socket.recv_json(timeout=max(0.01, deadline - time.monotonic()))
            if isinstance(result, dict) and result.get("id") == self.sequence:
                if result.get("success") is not True:
                    raise ValueError("ha_request_failed")
                return result.get("result")
        raise TimeoutError("ha_request_timeout")

    def close(self):
        self.socket.close()


def node_entities(client, node):
    if type(node) is not int or not 0 < node <= 0xffffffffffffffff:
        raise ValueError("ha_target_node")
    marker = "-" + format(node, "016X") + "-"
    registry = client.call("config/entity_registry/list")
    if not isinstance(registry, list):
        raise ValueError("ha_registry_format")
    return [e for e in registry if isinstance(e, dict) and e.get("platform") == "matter"
            and isinstance(e.get("unique_id"), str) and isinstance(e.get("entity_id"), str)
            and marker in e.get("unique_id", "")]


def tidy(node, client):
    entities = node_entities(client, node)
    selected = []
    for endpoint in (1, 3):
        suffix = "-{}-MatterStartUpOnOff-6-16387".format(endpoint)
        matches = [e for e in entities if e.get("unique_id", "").endswith(suffix)]
        if len(matches) != 1 or not re.fullmatch(r"select\.[a-z0-9_]+", matches[0].get("entity_id", "")):
            raise ValueError("ha_startup_entity_ambiguous")
        selected.append(matches[0])
    # Validate both targets before any mutation. Preserve IDs, names and options.
    changes = []
    for e in selected:
        if e.get("disabled_by") is None:
            client.call("config/entity_registry/update", entity_id=e["entity_id"], disabled_by="user")
            changes.append(e["entity_id"])
    current = node_entities(client, node)
    checked = {e["entity_id"]: e.get("disabled_by") for e in current if e["entity_id"] in {x["entity_id"] for x in selected}}
    if len(checked) != 2 or any(value is None for value in checked.values()):
        raise ValueError("ha_tidy_not_confirmed")
    return {"changed_entities": changes, "disabled_entities": checked, "entity_ids_preserved": True}


def refresh_firmware(node, expected_version, client=None, sleep=time.sleep):
    owned = client is None
    try:
        client = client or HAClient()
        entities = [e for e in node_entities(client, node) if e.get("unique_id", "").endswith("-0-MatterUpdate-40-9")]
        if len(entities) != 1 or not re.fullmatch(r"update\.[a-z0-9_]+", entities[0].get("entity_id", "")):
            raise ValueError("ha_update_entity_ambiguous")
        entity = entities[0]["entity_id"]
        if entities[0].get("disabled_by") is not None:
            return {"success": False, "reason": "ha_update_entity_disabled", "installation_requested": False}
        client.call("call_service", domain="homeassistant", service="update_entity", service_data={"entity_id": entity})
        for attempt in range(4):
            states = client.call("get_states")
            if not isinstance(states, list):
                raise ValueError("ha_states_format")
            selected = [s for s in states if isinstance(s, dict) and s.get("entity_id") == entity]
            if len(selected) == 1:
                state = selected[0]
                attrs = state.get("attributes", {})
                if not isinstance(attrs, dict):
                    raise ValueError("ha_attributes_format")
                latest = attrs.get("latest_version")
                if (attrs.get("installed_version") == expected_version and isinstance(latest, str)
                        and len(latest) <= 128 and state.get("state") in ("on", "off")):
                    return {"success": True, "entity_id": entity, "state": state["state"],
                            "installed_version": expected_version, "latest_version": latest, "installation_requested": False}
            if attempt < 3:
                sleep(1)
        return {"success": False, "reason": "ha_update_state_not_confirmed", "installation_requested": False}
    except (ValueError, TimeoutError, OSError, ConnectionError):
        # OTA completion is independent: never retry an install to repair a label.
        return {"success": False, "reason": "ha_update_refresh_failed", "installation_requested": False}
    finally:
        if owned and client is not None:
            try:
                client.close()
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("tidy", "refresh-firmware"))
    parser.add_argument("--expected-version")
    args = parser.parse_args()
    if args.operation == "refresh-firmware" and not args.expected_version:
        parser.error("--expected-version is required")
    from ha_access import load_access, ssh_prefix
    root = Path(__file__).resolve().parents[1]
    config = load_access(root)
    ws = (root / "tools/ha_ws_remote.py").read_text()
    source = Path(__file__).read_text()
    payload = "import sys,types\nm=types.ModuleType('ha_ws_remote')\nexec({!r},m.__dict__)\nsys.modules['ha_ws_remote']=m\n".format(ws)
    payload += "p=types.ModuleType('ha_presentation')\nexec({!r},p.__dict__)\n".format(source)
    if args.operation == "tidy":
        payload += "c=p.HAClient()\ntry:\n print(p.json.dumps(p.tidy({!r},c)))\nfinally:\n c.close()\n".format(config["node_id"])
    else:
        payload += "print(p.json.dumps(p.refresh_firmware({!r},{!r})))\n".format(config["node_id"], args.expected_version)
    result = subprocess.run(ssh_prefix(config) + ["python3 -c " + shlex.quote(payload)],
                            capture_output=True, text=True, timeout=130)
    if result.returncode:
        raise SystemExit("HA presentation operation failed; no automatic retry.")
    record = json.loads(result.stdout)
    print(json.dumps(record, indent=2))
    if record.get("success") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
