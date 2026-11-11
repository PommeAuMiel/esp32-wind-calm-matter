"""Validate the private target and SSH access; importing never reads credentials."""
import json
from pathlib import Path
import re


class AccessError(ValueError):
    """Invalid access configuration, reported without private contents."""


def load_access(root, require_light=False):
    path = Path(root) / "private/access/ha_ssh.json"
    try:
        if not path.is_file() or path.stat().st_size > 8192:
            raise AccessError("ssh_configuration_missing_or_size")
        config = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise AccessError("ssh_configuration_missing_or_format") from error
    if not isinstance(config, dict):
        raise AccessError("ssh_configuration_format")
    host, user, port = config.get("host"), config.get("user"), config.get("port")
    node = config.get("node_id")
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,253}", host) or host.startswith("-") or \
            not isinstance(user, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", user) or user.startswith("-") or \
            type(port) is not int or not 1 <= port < 65536 or \
            type(node) is not int or not 1 <= node <= 0x1FFFFFFFFFFFFF:
        raise AccessError("ssh_configuration_target")
    for key in ("identity_file", "known_hosts_file"):
        if not isinstance(config.get(key), str) or not Path(config[key]).is_file():
            raise AccessError("ssh_access_file")
    entity = config.get("light_entity")
    if require_light and (not isinstance(entity, str) or not re.fullmatch(r"light\.[a-z0-9_]{1,128}", entity)):
        raise AccessError("light_entity_configuration")
    # Unknown fields never leave this function. Secrets are not accepted as
    # command arguments, displayed, or passed to the remote Python program.
    result = {key: config[key] for key in ("host", "user", "port", "node_id", "identity_file", "known_hosts_file")}
    if require_light:
        result["light_entity"] = entity
    return result


def ssh_prefix(config):
    return ["ssh", "-T", "-i", config["identity_file"], "-p", str(config["port"]), "-o", "BatchMode=yes", "-o",
            "IdentitiesOnly=yes", "-o", "ForwardAgent=no", "-o", "StrictHostKeyChecking=yes", "-o",
            "UserKnownHostsFile=" + config["known_hosts_file"], "-o", "ConnectTimeout=8", "-o",
            "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2", config["user"] + "@" + config["host"]]
