"""Private target configuration and capture filtering; no real SSH calls."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import ha_access
import ha_light_capture as capture


class AccessTests(unittest.TestCase):
    def test_target_and_ssh_files_are_required_unknown_secrets_are_not_propagated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); folder = root / "private/access"; folder.mkdir(parents=True)
            key = root / "identity"; key.touch(); known = root / "known_hosts"; known.touch()
            config = {"host": "192.0.2.1", "user": "maintenance", "port": 22, "node_id": 7,
                      "light_entity": "light.wind_calm_lumiere", "identity_file": str(key),
                      "known_hosts_file": str(known), "token": "private_token"}
            path = folder / "ha_ssh.json"; path.write_text(json.dumps(config))
            result = ha_access.load_access(root, require_light=True)
            self.assertNotIn("private_token", json.dumps(result))
            self.assertEqual(result["node_id"], 7)
            self.assertIn("StrictHostKeyChecking=yes", ha_access.ssh_prefix(result))
            for changed in (dict(config, host="$(private_command)"), dict(config, user="-oProxyCommand"),
                            dict(config, node_id=True), dict(config, node_id=0), dict(config, port=True),
                            dict(config, node_id=1 << 64), dict(config, light_entity="light.a;private_command"),
                            dict(config, identity_file=str(root / "missing_private_key"))):
                path.write_text(json.dumps(changed))
                with self.assertRaises(ha_access.AccessError) as error:
                    ha_access.load_access(root, require_light=True)
                self.assertNotIn("private_", str(error.exception))

    def test_capture_uses_configured_synthetic_target_and_filters_foreign_events(self):
        old_node, old_entity = capture.NODE, capture.ENTITY
        try:
            capture.NODE, capture.ENTITY = 7, "light.wind_calm_lumiere"
            self.assertIsNone(capture.filter_ha_state({"entity_id": "light.other", "state": "private_state"}))
            state = capture.filter_ha_state({"entity_id": capture.ENTITY, "state": "on", "token": "private_token"})
            self.assertNotIn("private_", json.dumps(state))
            self.assertIsNone(capture.filter_matter_node({"node_id": 8, "attributes": {"1/6/0": True}}))
            node = capture.filter_matter_node({"node_id": 7, "attributes": {"1/6/0": True, "0/62/0": "private_fabric"}})
            self.assertTrue(node["attributes"]["1/6/0"])
            self.assertNotIn("private_", json.dumps(node))
        finally:
            capture.NODE, capture.ENTITY = old_node, old_entity


if __name__ == "__main__":
    unittest.main()
