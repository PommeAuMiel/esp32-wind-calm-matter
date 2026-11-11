#!/usr/bin/env python3
"""Build/verify the separate Matter OTA profile. Never reads or writes a device."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import struct
import subprocess
import thread_sdk as sdk

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "firmware/WindCalmThreadOTA"
SLOT_BYTES = 0x1f0000
_sdk_validate = sdk.validate_config


def project_version():
    source = (PROJECT/"version.cmake").read_text()
    number = re.search(r'set\(PROJECT_VER_NUMBER ([0-9]+)\)', source)
    text = re.search(r'set\(PROJECT_VER "([^"\n]+)"\)', source)
    if not number or not text or not 0 < int(number.group(1)) <= 0xffffffff:
        raise ValueError("Missing/invalid OTA project version")
    return int(number.group(1)), text.group(1)


BUILD = ROOT / "build" / ("WindCalmThreadOTA-" + str(project_version()[0]))


def resolve_build_dir(value):
    """Keep generated files in a release directory, away from reviewed images."""
    requested = Path(value)
    path = (requested if requested.is_absolute() else ROOT/requested).resolve()
    root = (ROOT/"build").resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ValueError("OTA build directory must be inside the project build directory") from error
    version, version_string = project_version()
    if (not relative.parts or relative.parts[0] != "WindCalmThreadOTA-" + str(version) or
            any(part.startswith("base-") or part == "sources" for part in relative.parts)):
        raise ValueError("Use build/WindCalmThreadOTA-" + str(version) + "; reviewed/legacy builds are protected")
    manifest = path/"manifest.json"
    if manifest.exists():
        record = json.loads(manifest.read_text())
        if record.get("profile") != "thread-ota" or record.get("ota",{}).get("version") != version:
            raise ValueError("Refusing to overwrite a build for another profile/version")
    description = path/"project_description.json"
    if description.exists():
        record = json.loads(description.read_text())
        if record.get("project_name") != "windcalm-thread-ota" or record.get("project_version") != version_string:
            raise ValueError("Refusing to reuse a build for another project/version")
    return path


def applicable_range(config, version, required_minimum=None):
    """Read the selected SDK image arguments independently of its OTA parser."""
    raw = json.loads(config.get("CONFIG_CHIP_OTA_IMAGE_EXTRA_ARGS", '""'))
    if not isinstance(raw,str):
        raise ValueError("Invalid CONFIG_CHIP_OTA_IMAGE_EXTRA_ARGS")
    arguments = shlex.split(raw)
    flags = {"-mi":"minimum", "--min-version":"minimum", "-ma":"maximum", "--max-version":"maximum"}
    values = {}
    if len(arguments) % 2:
        raise ValueError("Incomplete OTA applicable-version arguments")
    for index in range(0,len(arguments),2):
        field = flags.get(arguments[index])
        if not field or field in values:
            raise ValueError("Unknown/duplicate OTA applicable-version argument")
        value = int(arguments[index+1],0)
        if not 0 <= value < version:
            raise ValueError("Applicable version must be lower than the target version")
        values[field] = value
    minimum,maximum = values.get("minimum"),values.get("maximum")
    if required_minimum is not None and minimum != required_minimum:
        raise ValueError("OTA release " + str(version) + " requires minimum applicable version " + str(required_minimum))
    if minimum is not None and maximum is not None and maximum < minimum:
        raise ValueError("Invalid OTA applicable-version range")
    return minimum,maximum


def validate_config(source):
    values = dict(line.split("=",1) for line in source.splitlines() if line.startswith("CONFIG_") and "=" in line)
    for name in ("ENABLE_OTA_REQUESTOR", "CHIP_OTA_IMAGE_BUILD", "BOOTLOADER_APP_ROLLBACK_ENABLE",
                 "SUPPORT_OTA_SOFTWARE_UPDATE_PROVIDER_CLUSTER", "SUPPORT_OTA_SOFTWARE_UPDATE_REQUESTOR_CLUSTER"):
        if values.get("CONFIG_" + name) != "y":
            raise ValueError("Required OTA function missing: CONFIG_" + name)
    for name in ("BOOTLOADER_APP_ANTI_ROLLBACK", "ESP_MATTER_OTA_PROVIDER_ENABLED", "ENABLE_DELTA_OTA",
                 "ENABLE_ENCRYPTED_OTA", "APP_PROJECT_VER_FROM_CONFIG"):
        if values.get("CONFIG_" + name,"n") != "n":
            raise ValueError("Unexpected OTA function enabled: CONFIG_" + name)
    # Reuse the complete stable profile's security/network/board validation,
    # changing only its deliberate OTA prohibition for this distinct profile.
    _sdk_validate(source.replace("CONFIG_ENABLE_OTA_REQUESTOR=y","# CONFIG_ENABLE_OTA_REQUESTOR is not set"))


def validate_release_config(source):
    """Release contract; generic validate_config also supports the archived base."""
    validate_config(source)
    values = dict(line.split("=",1) for line in source.splitlines() if line.startswith("CONFIG_") and "=" in line)
    version,_ = project_version()
    selected_range = applicable_range(values,version,required_minimum=version-1 if version >= 40001 else None)
    if version >= 40002:
        for name in ("ESP_TASK_WDT_EN", "ESP_TASK_WDT_INIT", "ESP_TASK_WDT_PANIC",
                     "ESP_TASK_WDT_CHECK_IDLE_TASK_CPU0", "SUPPORT_ACTIONS_CLUSTER"):
            if values.get("CONFIG_" + name) != "y":
                raise ValueError("Required maintenance protection missing: CONFIG_" + name)
        if values.get("CONFIG_ESP_TASK_WDT_TIMEOUT_S") != "15":
            raise ValueError("Required maintenance watchdog timeout: CONFIG_ESP_TASK_WDT_TIMEOUT_S=15")
    return selected_range


def read_tlv_header(data):
    # Matter OTA tool emits a flat TLV structure of context-tagged values. Parse
    # independently from its Python implementation, reject unknown encodings.
    if len(data) < 16:
        raise ValueError("Truncated Matter header")
    magic,total,header_bytes = struct.unpack_from("<IQI",data)
    if magic != 0x1beef11e or total != len(data) or not 0 < header_bytes <= 1024:
        raise ValueError("Invalid Matter header length/signature")
    tlv = memoryview(data)[16:16+header_bytes]
    if len(tlv) != header_bytes or tlv[0] != 0x15:
        raise ValueError("Invalid Matter TLV structure")
    fields = {}; offset = 1
    while offset < len(tlv) and tlv[offset] != 0x18:
        control = tlv[offset]; offset += 1
        if control >> 5 != 1 or offset >= len(tlv):
            raise ValueError("Unexpected Matter tag")
        tag = tlv[offset]; offset += 1; kind = control & 31
        if tag in fields:
            raise ValueError("Duplicate Matter tag")
        if kind in (4,5,6,7):
            count = 1 << (kind-4)
            if offset+count > len(tlv): raise ValueError("Truncated integer")
            value = int.from_bytes(tlv[offset:offset+count],"little"); offset += count
        elif kind in (12,13,14,15,16,17,18,19):
            width = 1 << ((kind-12) % 4)
            if offset+width > len(tlv): raise ValueError("Truncated span length")
            count = int.from_bytes(tlv[offset:offset+width],"little"); offset += width
            if offset+count > len(tlv): raise ValueError("Truncated span")
            value = bytes(tlv[offset:offset+count]); offset += count
            if kind < 16: value = value.decode("utf-8")
        else:
            raise ValueError("Unsupported Matter TLV type")
        fields[tag] = value
    if offset != len(tlv)-1 or tlv[offset] != 0x18:
        raise ValueError("Trailing Matter TLV bytes")
    if not {0,1,2,3,4,8,9}.issubset(fields) or set(fields)-set(range(10)):
        raise ValueError("Missing/unknown Matter header tags")
    return fields, bytes(data[16+header_bytes:])


def read_partition_table(table):
    if len(table) != 0xc00:
        raise ValueError("Unexpected partition table length")
    entries = {}
    for offset in range(0,len(table),32):
        magic,kind,subtype,address,size,label,flags = struct.unpack_from("<HBBII16sI",table,offset)
        if magic != 0x50aa: break
        name = label.split(b"\0")[0].decode()
        if name in entries: raise ValueError("Duplicate partition name")
        entries[name] = [kind,subtype,address,size,flags]
    if table[offset:offset+16] != b"\xeb\xeb"+b"\xff"*14 or table[offset+16:offset+32] != hashlib.md5(table[:offset]).digest():
        raise ValueError("Partition table MD5 footer is invalid")
    if table[offset+32:] != b"\xff"*(len(table)-offset-32):
        raise ValueError("Unexpected trailing partition table data")
    expected = {"nvs":[1,2,0x9000,0x5000,0], "otadata":[1,0,0xe000,0x2000,0],
                "app0":[0,16,0x10000,SLOT_BYTES,0], "app1":[0,17,0x200000,SLOT_BYTES,0],
                "coredump":[1,3,0x3f0000,0x10000,0]}
    if entries != expected: raise ValueError("Unexpected OTA partition layout")
    return entries


def verify_artifacts():
    minimum,maximum = validate_release_config((BUILD/"sdkconfig").read_text())
    app = BUILD/"windcalm-thread-ota.bin"; ota = BUILD/"windcalm-thread-ota-ota.bin"
    if not 0 < app.stat().st_size <= SLOT_BYTES:
        raise ValueError("Application exceeds OTA slot")
    fields,payload = read_tlv_header(ota.read_bytes())
    if payload != app.read_bytes() or fields.get(4) != len(payload) or fields.get(8) != 1 or fields.get(9) != hashlib.sha256(payload).digest():
        raise ValueError("Matter payload length/SHA-256 does not match application")
    config = dict(line.split("=",1) for line in (BUILD/"sdkconfig").read_text().splitlines() if line.startswith("CONFIG_") and "=" in line)
    # Read actual generated CHIP values, not Zigbee identifiers or guessed flags.
    description = json.loads((BUILD/"project_description.json").read_text())
    version, version_string = project_version()
    if description["project_version"] != version_string:
        raise ValueError("Generated project version differs from selected release")
    if fields.get(0) != int(config["CONFIG_DEVICE_VENDOR_ID"],0) or fields.get(1) != int(config["CONFIG_DEVICE_PRODUCT_ID"],0) or fields.get(2) != version or fields.get(3) != version_string:
        raise ValueError("Matter header differs from compiled identity/version")
    if fields.get(5) != minimum or fields.get(6) != maximum:
        raise ValueError("Matter applicable versions differ from selected SDK image arguments")
    commands = json.loads((BUILD/"compile_commands.json").read_text())
    command = next(entry["command"] for entry in commands if entry["file"].endswith("/ESP32/ConfigurationManagerImpl.cpp"))
    compiled_version = re.search(r"-DCHIP_CONFIG_SOFTWARE_VERSION_NUMBER=([0-9]+)(?: |$)",command)
    if not compiled_version or int(compiled_version.group(1)) != version:
        raise ValueError("Matter numeric version differs from compiled ConfigurationManager")
    if len(payload) < 288 or payload[0] != 0xe9 or struct.unpack_from("<H",payload,12)[0] != 13:
        raise ValueError("Application is not an ESP32-C6 image")
    if struct.unpack_from("<II",payload,32) != (0xabcd5432,0):
        raise ValueError("Unexpected ESP app descriptor/secure_version")
    app_version = payload[48:80].split(b"\0",1)[0].decode()
    app_project = payload[80:112].split(b"\0",1)[0].decode()
    if app_version != version_string or app_project != "windcalm-thread-ota":
        raise ValueError("ESP app descriptor differs from Matter version/project")
    table = (BUILD/"partition_table/partition-table.bin").read_bytes()
    entries = read_partition_table(table)
    return {"version":version,"version_string":version_string,"vendor_id":fields[0],"product_id":fields[1],
            "min_applicable_version":minimum,"max_applicable_version":maximum,
            "slot_bytes":SLOT_BYTES,"application_bytes":len(payload),"slot_margin_bytes":SLOT_BYTES-len(payload),
            "ota_bytes":ota.stat().st_size,"payload_sha256":hashlib.sha256(payload).hexdigest(),
            "partitions":entries,"hardware_tested":False,"flashed":False}


def run(action, build_dir=None, live=False):
    global BUILD
    BUILD = resolve_build_dir(BUILD if build_dir is None else build_dir)
    if action != "verify":
        validate_release_config((PROJECT/"sdkconfig.defaults").read_text())
        sdk.PROJECT = PROJECT; sdk.BUILD = BUILD; sdk.validate_config = validate_release_config
        env = sdk.environment(); sdk.patch_defaults(); BUILD.mkdir(parents=True,exist_ok=True)
        defaults_hash = hashlib.sha256((PROJECT/"sdkconfig.defaults").read_bytes()).hexdigest()
        marker = BUILD/"defaults.sha256"
        if not marker.exists() or marker.read_text().strip() != defaults_hash:
            (BUILD/"sdkconfig").unlink(missing_ok=True)
        command = [str(sdk.PYTHON_ENV/"bin/python"),str(sdk.IDF/"tools/idf.py"),
                   "-C",str(PROJECT),"-B",str(BUILD),"-D","SDKCONFIG="+str(BUILD/"sdkconfig"),action]
        log = BUILD/(action+".log")
        print("OTA profile "+action+"; log: "+str(log),flush=True)
        result = sdk.run_logged(command, log, env, ROOT, live)
        lines = [line for line in log.read_text().splitlines() if not line.startswith(("-- Components:","-- Component paths:"))]
        for index,line in enumerate(lines):
            if line.startswith("Project build complete. To flash"):
                lines = lines[:index]; break
        # IDF's generic flash instructions would write ota_data_initial/NVS
        # selectors. They are not this unit's reviewed migration procedure.
        print("\n".join(line[:600] for line in lines[-25:]))
        if (BUILD/"sdkconfig").exists():
            validate_release_config((BUILD/"sdkconfig").read_text()); marker.write_text(defaults_hash+"\n")
        result.check_returncode()
    if action in ("build","verify"):
        verification = verify_artifacts()
        if action == "build":
            import shutil
            ota_filename = "windcalm-"+str(verification["version"])+".ota"
            shutil.copyfile(BUILD/"windcalm-thread-ota-ota.bin",BUILD/ota_filename)
            verification["ota_filename"] = ota_filename
            record = {"idf_commit":sdk.IDF_COMMIT,"arduino":"3.3.12","matter":"1.5.1",
                      "profile":"thread-ota","sources":{},"sdk_inputs":{},"binaries":{},"ota":verification}
            for folder in (PROJECT,ROOT/"firmware/WindCalmController"):
                for path in folder.rglob("*"):
                    if path.is_file() and "managed_components" not in path.parts:
                        record["sources"][str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (Path(__file__),ROOT/"tools/thread_sdk.py",ROOT/"tools/bootstrap_sdk.py"):
                record["sources"][str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
            inputs = [BUILD/"sdkconfig",sdk.IDF/"components/esp_wifi/Kconfig",sdk.IDF/"components/esp_wifi/CMakeLists.txt",
                      sdk.IDF/"tools/cmake/third_party/GetGitRevisionDescription.cmake",
                      sdk.IDF/"components/hal/esp32c6/modem_clock_hal.c"]
            inputs += sorted(path for path in (sdk.LOCAL/"arduino").rglob("*") if path.is_file())
            for path in inputs:
                record["sdk_inputs"][str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
            for name in ("windcalm-thread-ota.bin","windcalm-thread-ota-ota.bin",ota_filename,"bootloader/bootloader.bin","partition_table/partition-table.bin"):
                path=BUILD/name
                record["binaries"][name] = {"bytes":path.stat().st_size,"sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
            (BUILD/"manifest.json").write_text(json.dumps(record,indent=2)+"\n")
        if action == "verify":
            record = json.loads((BUILD/"manifest.json").read_text())
            for category in ("sources","sdk_inputs"):
                for relative,expected in record[category].items():
                    if hashlib.sha256((ROOT/relative).read_bytes()).hexdigest() != expected:
                        raise ValueError("Changed manifested input: "+relative)
            for relative,expected in record["binaries"].items():
                path=BUILD/relative
                if path.stat().st_size != expected["bytes"] or hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]:
                    raise ValueError("Changed manifested binary: "+relative)
            verification["ota_filename"] = record["ota"]["ota_filename"]
        (BUILD/"verification.json").write_text(json.dumps(verification,indent=2)+"\n")
        print(json.dumps(verification,indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("reconfigure","build","size-components","verify"))
    parser.add_argument("--live", action="store_true", help="Stream build progress without generic flash instructions")
    parser.add_argument("--build-dir",default=BUILD,
                        help="Release output inside build/WindCalmThreadOTA-<release> (default: %(default)s)")
    args = parser.parse_args()
    try:
        run(args.action,args.build_dir,args.live)
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode)
    except (ValueError,OSError) as error:
        parser.error(str(error))
