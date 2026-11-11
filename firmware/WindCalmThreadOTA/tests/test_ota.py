#!/usr/bin/env python3
import hashlib
import importlib.util
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[3]
import sys
sys.path.insert(0,str(ROOT/"tools"))
import thread_ota

class OtaTests(unittest.TestCase):
    def test_actual_policy_sanitized(self):
        compiler=shutil.which("clang++") or shutil.which("c++")
        self.assertTrue(compiler)
        with tempfile.TemporaryDirectory() as directory:
            exe=Path(directory)/"policy"
            subprocess.run([compiler,"-std=c++17","-Wall","-Wextra","-Werror","-fsanitize=address,undefined",
                            str(Path(__file__).with_name("policy_test.cpp")),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

    def test_actual_wrapper_sdk_boundaries_sanitized(self):
        compiler=shutil.which("clang++") or shutil.which("c++")
        with tempfile.TemporaryDirectory() as directory:
            exe=Path(directory)/"wrapper"
            folder=Path(__file__).parent
            subprocess.run([compiler,"-std=c++17","-Wall","-Wextra","-Werror","-fsanitize=address,undefined",
                            "-I"+str(folder/"fakes"),str(folder/"wrapper_test.cpp"),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

    def test_matter_header_roundtrip_and_bounds(self):
        # Independent fixed test vector in the standard Matter TLV wire format.
        payload=b"firmware"
        digest=hashlib.sha256(payload).digest()
        tlv=bytes.fromhex("152500f1ff250100802602419c00002c0304312e302d27040800000000000000240801300920")+digest+b"\x18"
        file=struct.pack("<IQI",0x1beef11e,16+len(tlv)+len(payload),len(tlv))+tlv+payload
        fields,decoded=thread_ota.read_tlv_header(file)
        self.assertEqual(decoded,payload);self.assertEqual(fields[0],0xfff1);self.assertEqual(fields[1],0x8000)
        self.assertEqual(fields[2],40001);self.assertEqual(fields[9],digest)
        for data in (b"",file[:15],file[:-1],file+b"x",b"\x00"+file[1:]):
            with self.assertRaises(ValueError):thread_ota.read_tlv_header(data)
        for offset in (16,17,18):
            bad=bytearray(file);bad[offset]=0xff
            with self.assertRaises(ValueError):thread_ota.read_tlv_header(bytes(bad))

    def test_effective_config_fails_closed(self):
        path=thread_ota.BUILD/"sdkconfig"
        if not path.exists():self.skipTest("OTA configuration not generated")
        source=path.read_text();thread_ota.validate_config(source)
        for key in ("ENABLE_OTA_REQUESTOR","CHIP_OTA_IMAGE_BUILD","BOOTLOADER_APP_ROLLBACK_ENABLE",
                    "SUPPORT_OTA_SOFTWARE_UPDATE_REQUESTOR_CLUSTER","LWIP_IPV6","LWIP_HOOK_IP6_SELECT_SRC_ADDR_DEFAULT",
                    "BT_NIMBLE_SM_SC","MBEDTLS_AES_C","FREERTOS_CHECK_STACKOVERFLOW_CANARY"):
            with self.subTest(key=key),self.assertRaises(ValueError):
                thread_ota.validate_config(source.replace("CONFIG_"+key+"=y","# CONFIG_"+key+" is not set"))
        for key in ("ESP_WIFI_ENABLED","ENABLE_DELTA_OTA","ENABLE_ENCRYPTED_OTA","BOOTLOADER_APP_ANTI_ROLLBACK"):
            with self.subTest(key=key),self.assertRaises(ValueError):thread_ota.validate_config(source+"\nCONFIG_"+key+"=y\n")

    def test_partition_table_md5_and_padding(self):
        path=thread_ota.BUILD/"partition_table/partition-table.bin"
        if not path.exists():self.skipTest("OTA partition table not built")
        table=path.read_bytes();thread_ota.read_partition_table(table)
        for offset in (0,32,160,176,len(table)-1):
            bad=bytearray(table);bad[offset]^=1
            with self.subTest(offset=offset),self.assertRaises(ValueError):thread_ota.read_partition_table(bytes(bad))
        with self.assertRaises(ValueError):thread_ota.read_partition_table(table[:-1])

    def test_built_payload_integrity_and_tamper(self):
        file=thread_ota.BUILD/"windcalm-thread-ota-ota.bin"
        if not file.exists():self.skipTest("OTA binary not built")
        artifact=file.read_bytes();fields,payload=thread_ota.read_tlv_header(artifact)
        self.assertEqual(hashlib.sha256(payload).digest(),fields[9]);self.assertEqual(fields[4],len(payload))
        for offset in (0,len(payload)//2,len(payload)-1):
            modified=bytearray(payload);modified[offset]^=1
            self.assertNotEqual(hashlib.sha256(modified).digest(),fields[9])
        thread_ota.verify_artifacts()

if __name__=="__main__":unittest.main()
