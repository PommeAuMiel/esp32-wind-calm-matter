#!/usr/bin/env python3
"""Synthetic offline regressions plus optional checks of private historical archives.

No device, radio or external process is involved. Approved production hashes
are patched only in memory while exercising deliberately synthetic fixtures.
"""
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import ota_migration_plan as migration

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_SNAPSHOT = ROOT / "private/flash-backups/before-light-diagnostic-20261004T171124Z/flash-4MiB.bin"
HISTORICAL_FRESH = ROOT / "private/flash-backups/before-ota-20261004T223512Z/flash-4MiB.bin"
HISTORICAL_BASE = ROOT / "build/WindCalmThreadOTA/base-40000-reviewed"
HISTORICAL_CONFIG = ROOT / "build/WindCalmController/sdkconfig"


def synthetic_esp_image(payload_size, version=None, project=None, fill=0):
    payload = bytearray([fill] * payload_size)
    if version is not None:
        struct.pack_into("<II", payload, 0, 0xabcd5432, 0)
        for offset, value in ((16, version), (48, project)):
            payload[offset:offset + 32] = value.encode().ljust(32, b"\0")
    header = bytearray(24)
    header[0], header[1], header[23] = 0xe9, 1, 1
    struct.pack_into("<H", header, 12, 13)
    data = header + struct.pack("<II", 0x40800000, len(payload)) + payload
    checksum = 0xef
    for value in payload:
        checksum ^= value
    checksum_offset = ((len(data) + 16) // 16) * 16 - 1
    data += b"\0" * (checksum_offset - len(data)) + bytes([checksum])
    return bytes(data) + hashlib.sha256(data).digest()


def synthetic_table(entries):
    data = b"".join(struct.pack("<HBBII16sI", 0x50aa, *values[:4],
                               name.encode(), values[4]) for name, values in entries.items())
    data += b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(data).digest()
    return data.ljust(0xc00, b"\xff")


def synthetic_ota(app):
    fields = {0: 0xfff1, 1: 0x8000, 2: 40000, 3: "4.0-thread-ota", 4: len(app),
              8: 1, 9: hashlib.sha256(app).digest()}
    tlv = bytearray(b"\x15")
    for tag, value in fields.items():
        if type(value) is int:
            tlv += bytes([0x26, tag]) + struct.pack("<I", value)
        else:
            raw = value.encode() if isinstance(value, str) else value
            tlv += bytes([0x2c if isinstance(value, str) else 0x30, tag, len(raw)]) + raw
    tlv += b"\x18"
    return struct.pack("<IQI", 0x1beef11e, 16 + len(tlv) + len(app), len(tlv)) + tlv + app


class HistoricalArchiveTests(unittest.TestCase):
    def setUp(self):
        if not all(path.exists() for path in (HISTORICAL_SNAPSHOT, HISTORICAL_FRESH,
                                              HISTORICAL_BASE, HISTORICAL_CONFIG)):
            self.skipTest("Private historical migration archives are not present")

    def test_reviewed_historical_plan_preserves_exact_known_base_margin(self):
        plan, _ = migration.build_plan(HISTORICAL_SNAPSHOT, HISTORICAL_BASE, HISTORICAL_CONFIG)
        self.assertFalse(plan["current_app_matches_installed0052"])
        self.assertEqual(plan["base40000"]["slot_margin"], 486160)
        self.assertTrue(plan["historical_bootloader"]["rollback_enabled_proven"])

    def test_saved_fresh_snapshot_matches_original_reviewed_identity(self):
        self.assertEqual(hashlib.sha256(HISTORICAL_FRESH.read_bytes()).hexdigest(),
                         "f1cb499cdcad15d81c1a66bfba8f836e38a09ccda26dd690bb6a600698181e63")
        plan, _ = migration.build_plan(HISTORICAL_FRESH, HISTORICAL_BASE, HISTORICAL_CONFIG)
        self.assertTrue(plan["current_app_matches_installed0052"])
        self.assertEqual(plan["current_app0"]["sha256"], migration.INSTALLED_0052_SHA)


class MigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Synthetic images only. Patch the approved fingerprints for this fixture,
        # never the parsers, integrity checks or production constants on disk.
        global SNAPSHOT, BASE, CONFIG
        temporary = tempfile.TemporaryDirectory(prefix="windcalm-migration-fixture-")
        cls.addClassCleanup(temporary.cleanup)
        root = Path(temporary.name)
        BASE = root / "base"; BASE.mkdir()
        CONFIG = root / "historical-config"
        CONFIG.write_text("CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y\n")
        defaults = (ROOT / "firmware/WindCalmThreadOTA/sdkconfig.defaults").read_bytes()
        app = synthetic_esp_image(1024, "4.0-thread-ota", "windcalm-thread-ota")
        old_app = synthetic_esp_image(1024, "old-synthetic", "windcalm-thread", fill=7)
        installed = synthetic_esp_image(1024, "current-synthetic", "windcalm-thread", fill=9)
        cls.installed_fixture = root / "installed.bin"; cls.installed_fixture.write_bytes(installed)
        boot = synthetic_esp_image(20648, fill=3)
        assert len(boot) == 20720
        ota = synthetic_ota(app)
        entries = {"nvs": [1,2,0x9000,0x5000,0], "otadata": [1,0,0xe000,0x2000,0],
                   "app0": [0,16,0x10000,migration.SLOT_BYTES,0],
                   "app1": [0,17,0x200000,migration.SLOT_BYTES,0],
                   "coredump": [1,3,0x3f0000,0x10000,0]}
        table = synthetic_table(entries)
        record = {"profile": "thread-ota", "sources": {}, "binaries": {}}
        for name, data in (("windcalm-thread-ota.bin", app), ("windcalm-40000.ota", ota),
                           ("partition_table/partition-table.bin", table)):
            path = BASE / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
            record["binaries"][name] = {"bytes": len(data), "sha256": migration.sha256(data)}
        (BASE / "sdkconfig").write_bytes(defaults)
        (BASE / "sources").mkdir(); (BASE / "sources/fixture.txt").write_bytes(b"synthetic fixture")
        record["sources"]["fixture.txt"] = migration.sha256(b"synthetic fixture")
        (BASE / "manifest.json").write_text(json.dumps(record))
        flash = bytearray(b"\xff" * migration.FLASH_BYTES)
        flash[:len(boot)] = boot
        flash[0x8000:0x8c00] = synthetic_table(migration.OLD_PARTITIONS)
        flash[0x9000:0xe000] = b"\x5a" * 0x5000  # Synthetic NVS sentinel.
        struct.pack_into("<I", flash, 0xe000, 1)
        struct.pack_into("<II", flash, 0xe000 + 24, 2, migration.selector_crc(1))
        flash[0x10000:0x10000 + len(old_app)] = old_app
        flash[0x3f0000:] = b"\x37" * 0x10000  # Synthetic coredump sentinel.
        SNAPSHOT = root / "snapshot.bin"; SNAPSHOT.write_bytes(flash)
        flash[0x10000:0x10000 + len(installed)] = installed
        cls.fresh_snapshot = root / "fresh.bin"; cls.fresh_snapshot.write_bytes(flash)
        fingerprints = dict(HISTORICAL_BOOTLOADER_SHA=migration.sha256(boot),
                            HISTORICAL_CONFIG_SHA=migration.sha256(CONFIG.read_bytes()),
                            INSTALLED_0052_SHA=migration.sha256(installed),
                            BASE_APP_SHA=migration.sha256(app), BASE_OTA_SHA=migration.sha256(ota),
                            BASE_TABLE_SHA=migration.sha256(table), BASE_CONFIG_SHA=migration.sha256(defaults))
        pins = patch.multiple(migration, **fingerprints); pins.start(); cls.addClassCleanup(pins.stop)

    def test_fixed_sdk_crc_vectors_and_selector(self):
        self.assertEqual([migration.selector_crc(n) for n in (0, 1, 2)], [0xffffffff, 0x4743989a, 0x55f63774])
        sector = migration.new_selector()
        self.assertEqual(len(sector), 4096)
        self.assertEqual(sector[:32], bytes.fromhex("02000000") + b"\xff" * 20 + bytes.fromhex("000000007437f655"))
        self.assertEqual(sector[32:], b"\xff" * (4096 - 32))
        entry = migration.parse_selector(sector)
        self.assertTrue(entry["bootloader_valid"])
        for state in (3, 4):
            bad = bytearray(sector);struct.pack_into("<I", bad, 24, state)
            self.assertFalse(migration.parse_selector(bytes(bad))["bootloader_valid"])
        bad = bytearray(sector);bad[0] ^= 1
        self.assertFalse(migration.parse_selector(bytes(bad))["bootloader_valid"])

    def test_synthetic_plan_and_sector_boundaries(self):
        plan, outputs = migration.build_plan(SNAPSHOT, BASE, CONFIG)
        self.assertEqual(plan["status"], "NONFLASHED")
        self.assertFalse(plan["device_accessed"])
        self.assertFalse(plan["current_app_matches_installed0052"])
        self.assertTrue(plan["historical_bootloader"]["rollback_enabled_proven"])
        self.assertEqual([op["offset"] for op in plan["ordered_operations"]], [0x200000, 0x8000, 0xf000])
        self.assertEqual(plan["base40000"]["slot_margin"], migration.SLOT_BYTES - (BASE / "windcalm-thread-ota.bin").stat().st_size)
        self.assertEqual(plan["new_selector"]["crc"], 0x55f63774)
        self.assertEqual(outputs["partition-table-sector.bin"][0xc00:], b"\xff" * 0x400)
        flash = bytearray(SNAPSHOT.read_bytes())
        app = (BASE / "windcalm-thread-ota.bin").read_bytes()
        staged = dict(outputs, application=app)
        for op, name in zip(plan["ordered_operations"], ["application", "partition-table-sector.bin", "otadata-inactive-new-sector.bin"]):
            off = op["offset"];flash[off:off + op["erase_bytes"]] = b"\xff" * op["erase_bytes"]
            flash[off:off + op["bytes"]] = staged[name]
            for protected in plan["preserved_ranges"]:
                start = protected["offset"]
                self.assertEqual(hashlib.sha256(flash[start:start + protected["bytes"]]).hexdigest(), protected["sha256"])
        self.assertEqual(migration.parse_selector(bytes(flash[0xe000:0xf000]))["sequence"], 1)
        self.assertEqual(migration.parse_selector(bytes(flash[0xf000:0x10000]))["sequence"], 2)

    def test_image_integrity_and_capacity_fail_closed(self):
        app = (BASE / "windcalm-thread-ota.bin").read_bytes()
        migration.parse_esp_image(app, migration.SLOT_BYTES, application=True, exact=True)
        for position in (12, 32, len(app) // 2, len(app) - 1):
            bad = bytearray(app);bad[position] ^= 1
            with self.subTest(position=position), self.assertRaises(ValueError):
                migration.parse_esp_image(bytes(bad), migration.SLOT_BYTES, application=True, exact=True)
        for data, capacity in ((app[:-1], migration.SLOT_BYTES), (app, len(app) - 1), (app + b"x", migration.SLOT_BYTES)):
            with self.assertRaises(ValueError):migration.parse_esp_image(data, capacity, exact=True)

    def test_partition_md5_and_bounds(self):
        table = (BASE / "partition_table/partition-table.bin").read_bytes()
        migration.parse_table(table)
        for position in (0, 32, 160, 176, len(table) - 1):
            bad = bytearray(table);bad[position] ^= 1
            with self.subTest(position=position), self.assertRaises(ValueError):migration.parse_table(bytes(bad))
        with self.assertRaises(ValueError):migration.parse_table(table[:-1])

    def test_snapshot_identity_selector_and_size_refusal(self):
        original = SNAPSHOT.read_bytes()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "snapshot.bin"
            for position in (0, 0x8000, 0xe000, 0x10000 + 32):
                bad = bytearray(original);bad[position] ^= 1;path.write_bytes(bad)
                with self.subTest(position=position), self.assertRaises(ValueError):migration.build_plan(path, BASE, CONFIG)
            path.write_bytes(original[:-1])
            with self.assertRaises(ValueError):migration.build_plan(path, BASE, CONFIG)
            bad = bytearray(original);bad[0xf000:0x10000] = migration.new_selector();path.write_bytes(bad)
            with self.assertRaises(ValueError):migration.build_plan(path, BASE, CONFIG)
            # Simulate a *saved* fresh dump, keeping all other historical bytes.
            current = self.installed_fixture.read_bytes()
            bad = bytearray(original);bad[0x10000:0x10000 + len(current)] = current;path.write_bytes(bad)
            plan, _ = migration.build_plan(path, BASE, CONFIG)
            self.assertTrue(plan["current_app_matches_installed0052"])
            self.assertFalse(plan["snapshot_freshness_verified"])

    def test_unreviewed_base_and_output_location_refused(self):
        with patch.object(migration, "BASE_APP_SHA", "0" * 64):
            with self.assertRaises(ValueError):migration.verified_base(BASE)
        plan, outputs = migration.build_plan(SNAPSHOT, BASE, CONFIG)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):migration.write_outputs(Path(folder) / "bad", plan, outputs)

    def test_fresh_saved_snapshot_identifies0052(self):
        path = self.fresh_snapshot
        plan, _ = migration.build_plan(path, BASE, CONFIG)
        self.assertTrue(plan["current_app_matches_installed0052"])
        self.assertEqual(plan["current_app0"]["sha256"], migration.INSTALLED_0052_SHA)


if __name__ == "__main__":
    unittest.main()
