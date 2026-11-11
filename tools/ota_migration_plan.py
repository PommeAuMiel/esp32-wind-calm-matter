#!/usr/bin/env python3
"""Plan the first USB-to-OTA migration from saved files only; never access USB.

Outputs are explicitly NONFLASHED. This module has no flash execution mode.
The historical dump can document the procedure, but cannot authorize a current
migration unless its app0 matches the installed diagnostic image (0052).
"""
import argparse
import binascii
from datetime import datetime, timezone
import functools
import hashlib
import json
import operator
from pathlib import Path
import struct

from thread_ota import read_partition_table, read_tlv_header, validate_config

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / "private/output"
FLASH_BYTES = 0x400000
SECTOR_BYTES = 0x1000
SLOT_BYTES = 0x1f0000
HISTORICAL_BOOTLOADER_SHA = "a2ebc2b16ef67609d6bbbe542195b07f449c708943b5cf5307d4bef05e56d200"
HISTORICAL_CONFIG_SHA = "b4a92e801f0f00e1f9ccba67a888d1fe674ad471eacd48c42ccf90541c6982bc"
INSTALLED_0052_SHA = "052da126fef915e286524e03cc3cd18c0a0d4eac8025aaee9db6faafc004ad24"
BASE_APP_SHA = "8e474005b6a491133db037b03bfe0402720b50d4414db1665c72af69910b2c45"
BASE_OTA_SHA = "3d4e36d32443432e1493f4554f70e7cb28901b2b199da7826344a17e1a840e47"
BASE_TABLE_SHA = "6d574ef034d3df9cc43f75e69b0ab9d6a6e6904c8393a1a8cb1a9e789e24adef"
BASE_CONFIG_SHA = "bbc315fe576f277ac2557b04586c4b94f9fe25e8a3d7a2f61a5d8ee5f767db5e"
OLD_PARTITIONS = {
    "nvs": [1, 2, 0x9000, 0x5000, 0],
    "otadata": [1, 0, 0xe000, 0x2000, 0],
    "app0": [0, 16, 0x10000, 0x300000, 0],
    "spiffs": [1, 130, 0x310000, 0xe0000, 0],
    "coredump": [1, 3, 0x3f0000, 0x10000, 0],
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def parse_table(table):
    if len(table) != 0xc00:
        raise ValueError("Partition table must be exactly 0xc00 bytes")
    entries = {}
    footer = None
    for offset in range(0, len(table), 32):
        magic, kind, subtype, address, size, label, flags = struct.unpack_from("<HBBII16sI", table, offset)
        if magic != 0x50aa:
            footer = offset
            break
        name = label.split(b"\0", 1)[0].decode("ascii")
        if not name or name in entries or size == 0 or address + size > FLASH_BYTES:
            raise ValueError("Invalid/duplicate partition entry")
        if address % SECTOR_BYTES or size % SECTOR_BYTES or kind == 0 and address % 0x10000:
            raise ValueError("Unaligned partition entry")
        if any(address < prior[2] + prior[3] and prior[2] < address + size for prior in entries.values()):
            raise ValueError("Overlapping partition entries")
        entries[name] = [kind, subtype, address, size, flags]
    if footer is None or table[footer:footer + 16] != b"\xeb\xeb" + b"\xff" * 14:
        raise ValueError("Missing partition MD5 footer")
    if table[footer + 16:footer + 32] != hashlib.md5(table[:footer]).digest():
        raise ValueError("Partition MD5 mismatch")
    if table[footer + 32:] != b"\xff" * (len(table) - footer - 32):
        raise ValueError("Unexpected partition padding")
    return entries


def parse_esp_image(data, capacity, application=False, exact=False):
    """Validate unencrypted ESP32-C6 segment bounds, checksum and appended SHA."""
    if len(data) < 24 or data[0] != 0xe9 or not 1 <= data[1] <= 16:
        raise ValueError("Invalid ESP image header")
    if struct.unpack_from("<H", data, 12)[0] != 13 or data[23] != 1:
        raise ValueError("Expected ESP32-C6 image with appended SHA256")
    limit = min(len(data), capacity)
    position, checksum = 24, 0xef
    for _ in range(data[1]):
        if position + 8 > limit:
            raise ValueError("Truncated segment header")
        _, size = struct.unpack_from("<II", data, position)
        position += 8
        if position + size > limit:
            raise ValueError("Segment exceeds image/slot capacity")
        checksum = functools.reduce(operator.xor, data[position:position + size], checksum)
        position += size
    checksum_offset = ((position + 16) // 16) * 16 - 1
    digest_offset = checksum_offset + 1
    end = digest_offset + 32
    if end > limit or data[checksum_offset] != checksum:
        raise ValueError("Image checksum mismatch/truncation")
    if hashlib.sha256(data[:digest_offset]).digest() != data[digest_offset:end]:
        raise ValueError("ESP image SHA256 mismatch")
    if exact and end != len(data):
        raise ValueError("Unexpected trailing bytes after ESP image")
    result = {"bytes": end, "sha256": sha256(data[:end]), "chip_id": 13, "sha256_appended_verified": True}
    if application:
        if len(data) < 288 or struct.unpack_from("<II", data, 32) != (0xabcd5432, 0):
            raise ValueError("Invalid app descriptor/secure_version")
        for key, start in (("version_string", 48), ("project_name", 80)):
            result[key] = data[start:start + 32].split(b"\0", 1)[0].decode("ascii")
    return result


def selector_crc(sequence):
    # SDK esp_rom_crc32_le(UINT32_MAX, &ota_seq, 4); includes sequence only.
    return binascii.crc32(struct.pack("<I", sequence), 0xffffffff) & 0xffffffff


def parse_selector(sector):
    if len(sector) != SECTOR_BYTES:
        raise ValueError("OTA selector sector must be 4KiB")
    sequence, state, crc = struct.unpack_from("<I20xII", sector)
    valid = sequence != 0xffffffff and state not in (3, 4) and crc == selector_crc(sequence)
    return {"sequence": sequence, "state": state, "crc": crc, "crc_valid": crc == selector_crc(sequence),
            "bootloader_valid": valid, "sector_sha256": sha256(sector)}


def new_selector():
    sector = bytearray(b"\xff" * SECTOR_BYTES)
    # pack's padding code x writes zeros; keep the unused seq_label erased.
    struct.pack_into("<I", sector, 0, 2)
    struct.pack_into("<II", sector, 24, 0, selector_crc(2))
    result = bytes(sector)
    parsed = parse_selector(result)
    if not parsed["bootloader_valid"] or (parsed["sequence"] - 1) % 2 != 1:
        raise ValueError("New selector does not select app1")
    return result


def verified_base(base):
    base = Path(base).resolve()
    record = json.loads((base / "manifest.json").read_text())
    if record.get("profile") != "thread-ota":
        raise ValueError("Wrong frozen build profile")
    for relative, expected in record["binaries"].items():
        path = (base / relative).resolve()
        if base not in path.parents:
            raise ValueError("Binary manifest path escapes base artifact")
        data = path.read_bytes()
        if len(data) != expected["bytes"] or sha256(data) != expected["sha256"]:
            raise ValueError("Frozen binary differs from manifest: " + relative)
    for relative, expected in record["sources"].items():
        source_root = (base / "sources").resolve()
        path = (source_root / relative).resolve()
        if source_root not in path.parents or sha256(path.read_bytes()) != expected:
            raise ValueError("Frozen source differs from manifest")
    config = (base / "sdkconfig").read_bytes()
    if sha256(config) != BASE_CONFIG_SHA:
        raise ValueError("OTA configuration differs from reviewed base40000")
    validate_config(config.decode())
    app = (base / "windcalm-thread-ota.bin").read_bytes()
    ota = (base / "windcalm-40000.ota").read_bytes()
    table = (base / "partition_table/partition-table.bin").read_bytes()
    if (sha256(app), sha256(ota), sha256(table)) != (BASE_APP_SHA, BASE_OTA_SHA, BASE_TABLE_SHA):
        raise ValueError("Artifacts differ from reviewed base40000")
    info = parse_esp_image(app, SLOT_BYTES, application=True, exact=True)
    fields, payload = read_tlv_header(ota)
    if payload != app or fields[4] != len(app) or fields[8] != 1 or fields[9] != hashlib.sha256(app).digest():
        raise ValueError("Matter OTA payload/digest mismatch")
    if (fields[0], fields[1], fields[2], fields[3]) != (0xfff1, 0x8000, 40000, "4.0-thread-ota"):
        raise ValueError("Matter OTA identity/version mismatch")
    if (info["version_string"], info["project_name"]) != ("4.0-thread-ota", "windcalm-thread-ota"):
        raise ValueError("OTA ESP descriptor identity mismatch")
    partitions = read_partition_table(table)
    if partitions != parse_table(table):
        raise ValueError("Independent partition parsing disagrees")
    return app, table, info, {"version": 40000, "vendor_id": 0xfff1, "product_id": 0x8000,
                             "ota_sha256": sha256(ota), "manifest_sha256": sha256((base / "manifest.json").read_bytes())}


def build_plan(snapshot, base, historical_config):
    snapshot, base, historical_config = Path(snapshot).resolve(), Path(base).resolve(), Path(historical_config).resolve()
    flash = snapshot.read_bytes()
    if len(flash) != FLASH_BYTES:
        raise ValueError("Migration requires a complete 4MiB snapshot")
    original = parse_table(flash[0x8000:0x8c00])
    if original != OLD_PARTITIONS or flash[0x8c00:0x9000] != b"\xff" * 0x400:
        raise ValueError("Snapshot partition table differs from original layout")
    boot = parse_esp_image(flash[:0x8000], 0x8000)
    if boot["bytes"] != 20720 or boot["sha256"] != HISTORICAL_BOOTLOADER_SHA:
        raise ValueError("Snapshot bootloader is not the reviewed historical bootloader")
    config_bytes = historical_config.read_bytes()
    if sha256(config_bytes) != HISTORICAL_CONFIG_SHA or "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y" not in config_bytes.decode().splitlines():
        raise ValueError("Historical bootloader rollback configuration not proven")
    app0 = parse_esp_image(flash[0x10000:0x200000], SLOT_BYTES, application=True)
    if app0["project_name"] != "windcalm-thread":
        raise ValueError("Current app0 is not the WindCalm Thread profile")
    first, inactive = parse_selector(flash[0xe000:0xf000]), parse_selector(flash[0xf000:0x10000])
    if not first["bootloader_valid"] or first["sequence"] != 1 or first["state"] not in (2, 0xffffffff):
        raise ValueError("Expected app0 selector seq1 VALID or UNDEFINED at0xe000")
    if inactive["bootloader_valid"] and inactive["sequence"] >= 1:
        raise ValueError("Sector0xf000 is not inactive under the original selector state")
    app, table, base_info, identity = verified_base(base)
    selector = new_selector()
    table_sector = table + b"\xff" * (SECTOR_BYTES - len(table))
    app_erase_bytes = ((len(app) + SECTOR_BYTES - 1) // SECTOR_BYTES) * SECTOR_BYTES
    current_matches = app0["sha256"] == INSTALLED_0052_SHA
    preserved = []
    for name, start, end in (("bootloader_and_padding", 0, 0x8000), ("nvs", 0x9000, 0xe000),
                             ("active_app0_selector", 0xe000, 0xf000), ("app0_new_slot", 0x10000, 0x200000),
                             ("coredump", 0x3f0000, 0x400000)):
        preserved.append({"name": name, "offset": start, "bytes": end - start, "sha256": sha256(flash[start:end])})
    operations = [
        {"order": 1, "purpose": "Stage reviewed base40000 at future app1; preserve existing app0",
         "offset": 0x200000, "bytes": len(app), "erase_bytes": app_erase_bytes,
         "source": str(base / "windcalm-thread-ota.bin"), "sha256": sha256(app),
         "required_readback": "Byte-identical application and all protected ranges before step2"},
        {"order": 2, "purpose": "Replace partition table sector after app1 readback succeeds",
         "offset": 0x8000, "bytes": SECTOR_BYTES, "erase_bytes": SECTOR_BYTES,
         "source_generated": "partition-table-sector.bin", "sha256": sha256(table_sector),
         "required_readback": "Byte-identical table; app0 and all protected ranges unchanged before step3"},
        {"order": 3, "purpose": "Activate app1 with inactive OTA sector seq2 NEW; preserve0xe000",
         "offset": 0xf000, "bytes": SECTOR_BYTES, "erase_bytes": SECTOR_BYTES,
         "source_generated": "otadata-inactive-new-sector.bin", "sha256": sha256(selector),
         "required_readback": "Byte-identical selector; table/app1 valid; all protected ranges unchanged before reset"},
    ]
    for op in operations:
        for protected in preserved:
            if op["offset"] < protected["offset"] + protected["bytes"] and protected["offset"] < op["offset"] + op["erase_bytes"]:
                raise ValueError("Planned erase intersects protected range")
    plan = {
        "status": "NONFLASHED", "flashed": False, "device_accessed": False,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "current_app_matches_installed0052": current_matches,
        "snapshot_freshness_verified": False,
        "blocks_before_execution": ["A fresh complete snapshot and read-only device identity/security checks are required", "USB-only wiring must be confirmed by the human"]
            + ([] if current_matches else ["Supplied snapshot predates installed0052; generate the final plan from a fresh snapshot"]),
        "snapshot": {"path": str(snapshot), "bytes": len(flash), "sha256": sha256(flash)},
        "historical_bootloader": dict(boot, rollback_enabled_proven=True, written=False,
                                      config_path=str(historical_config), config_sha256=sha256(config_bytes)),
        "current_app0": app0, "expected_installed0052_sha256": INSTALLED_0052_SHA,
        "original_partitions": original, "new_partitions": parse_table(table),
        "base40000": dict(base_info, **identity, slot_capacity=SLOT_BYTES, slot_margin=SLOT_BYTES - len(app)),
        "original_selectors": {"0xe000": first, "0xf000": inactive},
        "new_selector": dict(parse_selector(selector), offset=0xf000, state_name="NEW", selected_slot="app1"),
        "preserved_ranges": preserved, "ordered_operations": operations,
        "power_failure_behavior": {
            "during_app1_staging": "Old table and seq1 still boot original app0; former SPIFFS contents may already be overwritten",
            "during_partition_table_write": "The single table sector is not atomic/redundant; interruption can require ROM USB rescue",
            "after_table_before_selector": "Valid seq1 selects original app0 under the new two-slot layout",
            "during_selector_write": "Preserved seq1 permits app0 fallback; a complete valid seq2 may select app1",
            "first_app1_boot": "Historical rollback bootloader changes NEW to PENDING_VERIFY; unconfirmed reboot marks it ABORTED and returns to seq1 app0",
        },
        "limitations": [
            "The old SPIFFS partition is removed; app1 staging overlaps some former SPIFFS bytes. The full snapshot retains them privately",
            "Rollback preserves the app0 image but runtime NVS changes made by the OTA app are not automatically rolled back",
            "Do not use generic full flash/erase, replace the bootloader, write NVS, or overwrite both otadata sectors",
            "No Matter OTA provider publication or wireless update is performed by this tool",
        ],
    }
    return plan, {"partition-table-sector.bin": table_sector, "otadata-inactive-new-sector.bin": selector}


def write_outputs(output, plan, binaries):
    output = Path(output).resolve()
    if OUTPUT_ROOT.resolve() not in output.parents:
        raise ValueError("Output must be a new directory below private/output")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, data in binaries.items():
        path = output / name
        with path.open("xb") as stream:
            stream.write(data)
        path.chmod(0o600)
    path = output / "plan.json"
    with path.open("x") as stream:
        json.dump(plan, stream, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--base-artifact", type=Path, default=ROOT / "build/WindCalmThreadOTA/base-40000-reviewed")
    parser.add_argument("--historical-sdkconfig", type=Path, default=ROOT / "build/WindCalmController/sdkconfig")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        plan, binaries = build_plan(args.snapshot, args.base_artifact, args.historical_sdkconfig)
        write_outputs(args.output_dir, plan, binaries)
    except (ValueError, OSError, KeyError, UnicodeError) as error:
        parser.exit(1, "Offline migration planning refused: " + str(error) + "\n")
    print(json.dumps({"status": "NONFLASHED", "plan": str(args.output_dir.resolve() / "plan.json"),
                      "current_app_matches_installed0052": plan["current_app_matches_installed0052"],
                      "selector_sequence": 2, "selector_state": "NEW", "selector_crc": "0x55f63774"}))


if __name__ == "__main__":
    main()
