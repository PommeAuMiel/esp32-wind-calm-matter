#!/usr/bin/env python3
"""Compile-only dedicated Matter/Thread SDK. No device or upload actions."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local/dedicated"
IDF = LOCAL / "esp-idf"
PROJECT = ROOT / "firmware/WindCalmThreadSDK"
BUILD = ROOT / "build/WindCalmThreadSDK"
PYTHON_ENV = LOCAL / "idf-tools/python_env/idf5.5_py3.9_env"
IDF_COMMIT = "b774170ff46c393eeb5e495ea37936038d3f4f4f"


def validate_config(source):
    values = {}
    for line in source.splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    required = ("ENABLE_MATTER_OVER_THREAD", "THREAD_NETWORK_COMMISSIONING_DRIVER",
                "OPENTHREAD_ENABLED", "OPENTHREAD_FTD", "ENABLE_CHIPOBLE", "BT_NIMBLE_ENABLED",
                "BT_NIMBLE_ROLE_PERIPHERAL", "BT_NIMBLE_ROLE_BROADCASTER", "BT_NIMBLE_GATT_SERVER",
                "BT_NIMBLE_SM_SC", "BT_NIMBLE_SECURITY_ENABLE", "BT_NIMBLE_LL_CFG_FEAT_LE_ENCRYPTION",
                "ESP_COEX_SW_COEXIST_ENABLE", "LWIP_IPV6", "LWIP_HOOK_IP6_SELECT_SRC_ADDR_DEFAULT",
                "MBEDTLS_CMAC_C",
                "MBEDTLS_AES_C", "MBEDTLS_CCM_C", "MBEDTLS_ECP_C", "MBEDTLS_ECDH_C",
                "MBEDTLS_ECDSA_C", "MBEDTLS_ECP_DP_SECP256R1_ENABLED", "MBEDTLS_HKDF_C",
                "MBEDTLS_SSL_PROTO_DTLS", "MBEDTLS_KEY_EXCHANGE_ECJPAKE",
                "FREERTOS_CHECK_STACKOVERFLOW_CANARY", "COMPILER_STACK_CHECK_MODE_NORM",
                "SUPPORT_ACCESS_CONTROL_CLUSTER", "SUPPORT_OPERATIONAL_CREDENTIALS_CLUSTER",
                "SUPPORT_ADMINISTRATOR_COMMISSIONING_CLUSTER", "SUPPORT_GENERAL_COMMISSIONING_CLUSTER",
                "SUPPORT_NETWORK_COMMISSIONING_CLUSTER", "SUPPORT_BASIC_INFORMATION_CLUSTER",
                "SUPPORT_GENERAL_DIAGNOSTICS_CLUSTER", "SUPPORT_GROUP_KEY_MANAGEMENT_CLUSTER",
                "SUPPORT_DESCRIPTOR_CLUSTER", "SUPPORT_THREAD_NETWORK_DIAGNOSTICS_CLUSTER",
                "SUPPORT_ON_OFF_CLUSTER", "SUPPORT_FAN_CONTROL_CLUSTER", "SUPPORT_MODE_SELECT_CLUSTER",
                "SUPPORT_GROUPS_CLUSTER", "SUPPORT_IDENTIFY_CLUSTER", "SUPPORT_SCENES_CLUSTER",
                "SUPPORT_BINDING_CLUSTER", "SUPPORT_LEVEL_CONTROL_CLUSTER",
                "SUPPORT_COLOR_CONTROL_CLUSTER")
    disabled = ("ESP_WIFI_ENABLED", "ENABLE_WIFI_STATION", "WIFI_NETWORK_COMMISSIONING_DRIVER",
                "COMPILER_CXX_EXCEPTIONS", "COMPILER_CXX_RTTI", "BT_NIMBLE_ROLE_CENTRAL",
                "BT_NIMBLE_ROLE_OBSERVER", "BT_NIMBLE_GATT_CLIENT", "SECURITY_TEST_MODE",
                "TEST_EVENT_TRIGGER_ENABLED", "CHIP_ENABLE_PAIRING_AUTOSTART", "ENABLE_OTA_REQUESTOR",
                "USE_BLE_ONLY_FOR_COMMISSIONING", "USE_MINIMAL_MDNS")
    for key in required:
        if values.get("CONFIG_" + key) != "y":
            raise ValueError("Required protection/function missing: CONFIG_" + key)
    for key in disabled:
        if values.get("CONFIG_" + key, "n") != "n":
            raise ValueError("Unexpected enabled component: CONFIG_" + key)
    for key, expected in {"THREAD_NETWORK_ENDPOINT_ID": "0", "FREERTOS_HZ": "1000",
                          "ARDUINO_VARIANT": '"XIAO_ESP32C6"',
                          "PARTITION_TABLE_CUSTOM_FILENAME": '"partitions.csv"',
                          "ESP_MAIN_TASK_STACK_SIZE": "4096", "ESP_TIMER_TASK_STACK_SIZE": "8192",
                          "ESP_IPC_TASK_STACK_SIZE": "2048", "FREERTOS_IDLE_TASK_STACKSIZE": "2304",
                          "FREERTOS_ISR_STACKSIZE": "2096", "LWIP_TCPIP_TASK_STACK_SIZE": "4096",
                          "BT_LE_CONTROLLER_TASK_STACK_SIZE": "5120"}.items():
        if values.get("CONFIG_" + key) != expected:
            raise ValueError("Configuration mismatch: CONFIG_" + key)


def compiler_bin():
    dedicated = LOCAL / "toolchains/esp-rv32/2601/riscv32-esp-elf/bin"
    legacy = ROOT / ".local/data/packages/esp32/tools/esp-rv32/2601/bin"
    return legacy if legacy.is_dir() else dedicated


def environment():
    global PYTHON_ENV
    preferred = LOCAL / f"idf-tools/python_env/idf5.5_py{sys.version_info.major}.{sys.version_info.minor}_env"
    candidates = sorted((LOCAL / "idf-tools/python_env").glob("idf5.5_py*_env"))
    if preferred.is_dir():
        PYTHON_ENV = preferred
    elif len(candidates) == 1:
        PYTHON_ENV = candidates[0]
    else:
        raise SystemExit("Missing/ambiguous IDF Python environment; run tools/bootstrap_sdk.py")
    cmake = LOCAL / "idf-tools/tools/cmake/3.30.2"
    cmake_bin = cmake / "CMake.app/Contents/bin" if (cmake / "CMake.app").is_dir() else cmake / "bin"
    paths = [PYTHON_ENV / "bin", cmake_bin,
             LOCAL / "idf-tools/tools/ninja/1.12.1", compiler_bin()]
    for path in paths:
        if not path.is_dir():
            raise SystemExit("Missing dedicated dependency; run tools/bootstrap_sdk.py: " + str(path))
    actual = subprocess.check_output(["git", "-C", str(IDF), "rev-parse", "HEAD"], text=True).strip()
    if actual != IDF_COMMIT:
        raise SystemExit("ESP-IDF revision mismatch: refusing build")
    compiler_version = subprocess.check_output([str(paths[-1] / "riscv32-esp-elf-gcc"), "--version"], text=True)
    if "14.2.0_20260121" not in compiler_version:
        raise SystemExit("RISC-V compiler version mismatch: refusing build")
    env = os.environ.copy()
    env.update(IDF_PATH=str(IDF), IDF_TOOLS_PATH=str(LOCAL / "idf-tools"),
               IDF_PYTHON_ENV_PATH=str(PYTHON_ENV), IDF_TARGET="esp32c6",
               IDF_COMPONENT_CACHE_PATH=str(LOCAL / "component-cache"),
               IDF_PY_BUILD_JOBS="4", IDF_CCACHE_ENABLE="0",
               COLUMNS="220", NO_COLOR="1",
               ESP_ROM_ELF_DIR=str(LOCAL / "idf-tools/tools/esp-rom-elfs/20241011"))
    env["PATH"] = os.pathsep.join(map(str, paths)) + os.pathsep + env.get("PATH", "")
    return env


def run_logged(command, log, env, cwd, live=False):
    """Keep the complete log; optionally stream compilation without flash advice."""
    with log.open("w") as stream:
        if not live:
            return subprocess.run(command, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT)
        with subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, bufsize=1) as process:
            printing = True
            for line in process.stdout:
                stream.write(line)
                if line.startswith("Project build complete. To flash"):
                    printing = False
                if printing and not line.startswith(("-- Components:", "-- Component paths:")):
                    print(line.rstrip(), flush=True)
            return subprocess.CompletedProcess(command, process.wait())


def patch_defaults():
    # These hidden defaults ignore =n in sdkconfig.defaults. Only isolated
    # project copies are changed; do not modify the installed Arduino SDK.
    edits = [
             # C6 rev>0 Thread-only BLE shutdown reaches the legitimate 1->0
             # transition. Keep imbalance protection, before decrementing.
             (IDF / "components/hal/esp32c6/modem_clock_hal.c",
              '        } else {\n            if (--ref == 0) {\n'
              '                modem_lpcon_ll_enable_wifipwr_clock(hal->lpcon_dev, enable);\n'
              '            }\n        }\n        HAL_ASSERT(ref > 0);',
              '        } else {\n            HAL_ASSERT(ref > 0);\n            if (--ref == 0) {\n'
              '                modem_lpcon_ll_enable_wifipwr_clock(hal->lpcon_dev, enable);\n'
              '            }\n        }\n        HAL_ASSERT(ref >= 0);'),
             (LOCAL / "arduino/Kconfig.projbuild",
              '    select MEMMAP_SMP\n    default "y"',
              '    select MEMMAP_SMP\n    default "n"'),
             (IDF / "components/esp_wifi/Kconfig",
              '    config ESP_WIFI_ENABLED\n        bool\n        default y if SOC_WIFI_SUPPORTED',
              '    config ESP_WIFI_ENABLED\n        bool\n        default n'),
             # IDF5.5.5's disabled-WiFi stub branch still compiles an AP helper
             # whose native types are unavailable without the driver on C6.
             (IDF / "components/esp_wifi/CMakeLists.txt",
              '            "src/wifi_netif.c"\n            "src/wifi_default_ap.c")',
              '            "src/wifi_netif.c")'),
             # Metadata-only fix for an empty parent Git repository: Insights
             # calls this helper even when its telemetry feature is disabled.
             (IDF / "tools/cmake/third_party/GetGitRevisionDescription.cmake",
              '    get_filename_component(GIT_DIR "${GIT_DIR}" ABSOLUTE BASE_DIR "${_repo_dir}")\n',
              '    execute_process(COMMAND "${GIT_EXECUTABLE}" rev-parse --verify HEAD\n'
              '        WORKING_DIRECTORY "${_repo_dir}" RESULT_VARIABLE has_head\n'
              '        OUTPUT_QUIET ERROR_QUIET)\n'
              '    if(NOT has_head EQUAL 0)\n        return()\n    endif()\n'
              '    get_filename_component(GIT_DIR "${GIT_DIR}" ABSOLUTE BASE_DIR "${_repo_dir}")\n')]
    for path, before, after in edits:
        source = path.read_text()
        if after in source:
            continue
        if source.count(before) != 1:
            raise SystemExit("Unexpected Kconfig: refusing patch " + str(path))
        path.write_text(source.replace(before, after))


def run(action, live=False):
    env = environment()
    patch_defaults()
    BUILD.mkdir(parents=True, exist_ok=True)
    defaults_hash = hashlib.sha256((PROJECT / "sdkconfig.defaults").read_bytes()).hexdigest()
    marker = BUILD / "defaults.sha256"
    if not marker.exists() or marker.read_text().strip() != defaults_hash:
        # Generated user values otherwise override changes to sdkconfig.defaults.
        config = BUILD / "sdkconfig"
        if config.exists():
            config.unlink()
    command = [str(PYTHON_ENV / "bin/python"), str(IDF / "tools/idf.py"),
               "-C", str(PROJECT), "-B", str(BUILD),
               "-D", "SDKCONFIG=" + str(BUILD / "sdkconfig"), action]
    log = BUILD / (action + ".log")
    print("Dedicated SDK " + action + "; log: " + str(log), flush=True)
    result = run_logged(command, log, env, ROOT, live)
    lines = [line for line in log.read_text().splitlines() if not line.startswith(("-- Components:", "-- Component paths:"))]
    print("\n".join(line[:600] for line in lines[-25:]))
    if (BUILD / "sdkconfig").exists():
        validate_config((BUILD / "sdkconfig").read_text())
        marker.write_text(defaults_hash + "\n")
    result.check_returncode()
    if action == "build":
        record = {"idf_commit": IDF_COMMIT, "arduino": "3.3.12", "matter": "1.5.1",
                  "profile": "thread-sdk", "sources": {}, "sdk_inputs": {}, "binaries": {}}
        for folder in (PROJECT, ROOT / "firmware/WindCalmController"):
            for path in folder.rglob("*"):
                if path.is_file() and "managed_components" not in path.parts:
                    record["sources"][str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
        record["sources"][str(Path(__file__).relative_to(ROOT))] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        for path in (BUILD / "sdkconfig", IDF / "components/esp_wifi/Kconfig",
                     IDF / "components/esp_wifi/CMakeLists.txt",
                     IDF / "tools/cmake/third_party/GetGitRevisionDescription.cmake",
                     IDF / "components/hal/esp32c6/modem_clock_hal.c"):
            record["sdk_inputs"][str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
        # Include every copied Arduino source/header/config, not only a version label.
        for path in sorted((LOCAL / "arduino").rglob("*")):
            if path.is_file():
                record["sdk_inputs"][str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
        for name in ("windcalm-thread.bin", "bootloader/bootloader.bin", "partition_table/partition-table.bin"):
            path = BUILD / name
            record["binaries"][name] = {"bytes": path.stat().st_size,
                                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        (BUILD / "manifest.json").write_text(json.dumps(record, indent=2) + "\n")
        print("Application bytes:", record["binaries"]["windcalm-thread.bin"]["bytes"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("reconfigure", "build", "size-components"))
    parser.add_argument("--live", action="store_true", help="Stream build progress while retaining the complete log")
    try:
        args = parser.parse_args()
        run(args.action, args.live)
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode)
