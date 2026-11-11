#!/usr/bin/env python3
"""Offline guards for release selection and preservation of the reviewed base."""
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/"tools"))
import thread_ota
VERSION, VERSION_STRING = thread_ota.project_version()
OUTPUT_NAME = "WindCalmThreadOTA-" + str(VERSION)


class ReleaseTests(unittest.TestCase):
    def test_release_paths_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root/"build").mkdir()
            with mock.patch.object(thread_ota,"ROOT",root):
                expected = root/"build"/OUTPUT_NAME
                self.assertEqual(thread_ota.resolve_build_dir(expected),expected)
                self.assertEqual(thread_ota.resolve_build_dir("build/"+OUTPUT_NAME),expected)
                for value in (root,root/"build",root/"elsewhere"/OUTPUT_NAME,
                              "build/WindCalmThreadOTA", "build/WindCalmThreadOTA/base-40000-reviewed",
                              "build/WindCalmThreadOTA-40000/new-child", "build/WindCalmThreadSDK",
                              "build/"+OUTPUT_NAME+"/base-40000-reviewed",
                              "build/"+OUTPUT_NAME+"/sources"):
                    with self.subTest(value=value),self.assertRaises(ValueError):
                        thread_ota.resolve_build_dir(value)
                outside = root/"outside"; outside.mkdir()
                expected.symlink_to(outside,target_is_directory=True)
                with self.assertRaises(ValueError):thread_ota.resolve_build_dir(expected)

    def test_conflicting_metadata_rejected_before_any_build_mutation(self):
        for metadata,file in (({"profile":"thread-ota","ota":{"version":VERSION-1}},"manifest.json"),
                              ({"profile":"thread-sdk","ota":{"version":VERSION}},"manifest.json"),
                              ({"project_name":"windcalm-thread-ota","project_version":"4.0-thread-ota"},"project_description.json"),
                              ({"project_name":"another-project","project_version":VERSION_STRING},"project_description.json")):
            with self.subTest(metadata=metadata),tempfile.TemporaryDirectory() as directory:
                root=Path(directory); output=root/"build"/OUTPUT_NAME; output.mkdir(parents=True)
                marker=output/file; marker.write_text(json.dumps(metadata))
                before=marker.read_bytes()
                with mock.patch.object(thread_ota,"ROOT",root),mock.patch.object(thread_ota.sdk,"environment") as environment:
                    with self.assertRaises(ValueError):thread_ota.run("build",output)
                    environment.assert_not_called()
                self.assertEqual(marker.read_bytes(),before)
                self.assertEqual(list(output.iterdir()),[marker])

    def test_required_minimum_is_independent_of_image_metadata(self):
        key="CONFIG_CHIP_OTA_IMAGE_EXTRA_ARGS"
        self.assertEqual(thread_ota.applicable_range({key:'"-mi 40000"'},40001,required_minimum=40000),(40000,None))
        self.assertEqual(thread_ota.applicable_range({key:'"--min-version 0x9c40 -ma 40000"'},40001,required_minimum=40000),(40000,40000))
        for arguments in ("", "-mi 39999", "-mi 40001", "-mi -1", "-mi 4294967296",
                          "-mi 40000 --min-version 40000", "-mi", "-unknown 40000",
                          "-mi 40000 -ma 39999"):
            with self.subTest(arguments=arguments),self.assertRaises(ValueError):
                thread_ota.applicable_range({key:json.dumps(arguments)},40001,required_minimum=40000)
        with self.assertRaises(ValueError):thread_ota.applicable_range({},40001,required_minimum=40000)

    def test_archived_base_config_still_supported_by_general_validator(self):
        path=ROOT/"build/WindCalmThreadOTA/base-40000-reviewed/sdkconfig"
        if not path.exists():self.skipTest("Reviewed base not present")
        source=path.read_text()
        thread_ota.validate_config(source)
        with self.assertRaisesRegex(ValueError,"requires minimum applicable version "+str(VERSION-1)):
            thread_ota.validate_release_config(source)
        current=(ROOT/"firmware/WindCalmThreadOTA/sdkconfig.defaults").read_text()
        self.assertEqual(thread_ota.validate_release_config(current),(VERSION-1,None))

    def test_maintenance_watchdog_and_action_gates_are_required(self):
        if VERSION < 40002: self.skipTest("Maintenance release not selected")
        defaults=(ROOT/"firmware/WindCalmThreadOTA/sdkconfig.defaults").read_text()
        for name in ("ESP_TASK_WDT_EN", "ESP_TASK_WDT_INIT", "ESP_TASK_WDT_PANIC",
                     "ESP_TASK_WDT_CHECK_IDLE_TASK_CPU0", "SUPPORT_ACTIONS_CLUSTER"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError,name):
                thread_ota.validate_release_config(defaults.replace("CONFIG_"+name+"=y", "CONFIG_"+name+"=n"))
        with self.assertRaisesRegex(ValueError,"ESP_TASK_WDT_TIMEOUT_S"):
            thread_ota.validate_release_config(defaults.replace("CONFIG_ESP_TASK_WDT_TIMEOUT_S=15", "CONFIG_ESP_TASK_WDT_TIMEOUT_S=5"))

    def test_cli_rejects_reviewed_base_without_touching_it(self):
        base=ROOT/"build/WindCalmThreadOTA/base-40000-reviewed"
        before={str(p.relative_to(base)):hashlib.sha256(p.read_bytes()).digest()
                for p in base.rglob("*") if p.is_file()}
        result=subprocess.run([sys.executable,str(ROOT/"tools/thread_ota.py"),"build","--build-dir",str(base)],
                              capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertIn("protected",result.stderr)
        after={str(p.relative_to(base)):hashlib.sha256(p.read_bytes()).digest()
               for p in base.rglob("*") if p.is_file()}
        self.assertEqual(before,after)

    def test_header_applicable_version_tamper_rejected_with_valid_payload(self):
        output=thread_ota.BUILD
        artifact=output/"windcalm-thread-ota-ota.bin"
        if not artifact.exists():self.skipTest("Selected release not built")
        data=bytearray(artifact.read_bytes())
        header_bytes=struct.unpack_from("<I",data,12)[0]
        patterns=[bytes([control,5])+int(VERSION-1).to_bytes(width,"little")
                  for control,width in ((0x25,2),(0x26,4),(0x27,8))]
        matches=[(data.find(pattern,16,16+header_bytes),pattern) for pattern in patterns]
        matches=[(offset,pattern) for offset,pattern in matches if offset>=0]
        self.assertEqual(len(matches),1)
        offset,_=matches[0];data[offset+2]^=1
        fields,payload=thread_ota.read_tlv_header(data)
        self.assertEqual(fields[5],(VERSION-1)^1)
        self.assertEqual(hashlib.sha256(payload).digest(),fields[9])
        with tempfile.TemporaryDirectory() as directory:
            temporary=Path(directory)
            for name in ("sdkconfig","project_description.json","windcalm-thread-ota.bin"):
                shutil.copyfile(output/name,temporary/name)
            (temporary/"windcalm-thread-ota-ota.bin").write_bytes(data)
            with mock.patch.object(thread_ota,"BUILD",temporary),self.assertRaisesRegex(ValueError,"applicable versions differ"):
                thread_ota.verify_artifacts()


if __name__=="__main__":unittest.main()
