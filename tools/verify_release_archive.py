#!/usr/bin/env python3
"""Verify frozen release inputs and binaries without the current SDK or sources."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re


SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def checked_file(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("Invalid archived path")
    parts = PurePosixPath(relative)
    if parts.is_absolute() or any(part in ("", ".", "..") for part in relative.split("/")):
        raise ValueError("Invalid archived path")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("Missing or escaping archived file")
    return path


def check_digest(path, expected):
    if not isinstance(expected, str) or not SHA256.fullmatch(expected):
        raise ValueError("Invalid archived SHA-256")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        raise ValueError("Archived digest mismatch")


def verify_archive(directory):
    root = Path(directory).resolve()
    manifest = json.loads(checked_file(root, "manifest.json").read_text())
    if not isinstance(manifest, dict) or manifest.get("profile") != "thread-ota":
        raise ValueError("Invalid archived profile")
    version = manifest.get("ota", {}).get("version")
    if type(version) is not int or not 1 <= version <= 0xFFFFFFFF:
        raise ValueError("Invalid archived version")
    counts = {}
    for category, folder in (("sources", "sources"), ("sdk_inputs", "sdk-inputs")):
        entries = manifest.get(category)
        if not isinstance(entries, dict) or not entries:
            raise ValueError("Missing archived inputs")
        for relative, expected in entries.items():
            check_digest(checked_file(root, folder + "/" + relative), expected)
        counts[category] = len(entries)
    entries = manifest.get("binaries")
    if not isinstance(entries, dict) or not entries:
        raise ValueError("Missing archived binaries")
    for relative, expected in entries.items():
        if not isinstance(expected, dict) or type(expected.get("bytes")) is not int or expected["bytes"] <= 0:
            raise ValueError("Invalid archived binary metadata")
        path = checked_file(root, relative)
        if path.stat().st_size != expected["bytes"]:
            raise ValueError("Archived binary size mismatch")
        check_digest(path, expected.get("sha256"))
    counts["binaries"] = len(entries)
    return dict(version=version, verified=True, **counts)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(verify_archive(args.directory), indent=2))
    except (ValueError, OSError, TypeError, AttributeError):
        parser.exit(1, "Frozen release archive verification failed.\n")
