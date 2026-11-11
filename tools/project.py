#!/usr/bin/env python3
"""Pinned, project-local Arduino SDK. Upload always needs explicit confirmations."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "3.3.12"
FQBN = "esp32:esp32:XIAO_ESP32C6:CDCOnBoot=cdc,PartitionScheme=huge_app,ZigbeeMode=default,DebugLevel=error,EraseFlash=none,FlashSize=4M"
BASE_FQBN = FQBN
PROFILE = "debug"
BUILD = ROOT / "build/WindCalmThreadTest"
SKETCH = ROOT / "firmware/WindCalmThreadTest"
LOCAL = ROOT / ".local"
CONFIG = LOCAL / "arduino-cli.yaml"
INDEX = "https://espressif.github.io/arduino-esp32/package_esp32_index.json"


def cli_path():
    candidates = [os.environ.get("ARDUINO_CLI"), shutil.which("arduino-cli"),
                  "/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/resources/arduino-cli"]
    for item in candidates:
        if item and Path(item).is_file():
            return item
    raise SystemExit("Arduino CLI missing: install from https://arduino.github.io/arduino-cli/")


def configure():
    for name in ("data", "downloads", "user"):
        (LOCAL / name).mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({"directories": {
        "data": str(LOCAL / "data"), "downloads": str(LOCAL / "downloads"),
        "user": str(LOCAL / "user")}, "board_manager": {"additional_urls": [INDEX]}}, indent=2) + "\n")


def run(args, log=None):
    command = [cli_path(), "--config-file", str(CONFIG), *args]
    execute(command, log)


def execute(command, log=None):
    print(shlex.join(command), flush=True)
    if log is None:
        subprocess.run(command, cwd=ROOT, check=True)
        return
    log.parent.mkdir(parents=True, exist_ok=True)
    # Arduino CLI may delete its build directory when the sketch changes.
    # Keep the log outside that directory until the compiler exits.
    with tempfile.TemporaryFile(mode="w+", dir=log.parent.parent) as stream:
        stream.write(shlex.join(command) + "\n")
        stream.flush()
        result = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
        stream.seek(0)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(stream.read())
    print(log.read_text(), end="")
    result.check_returncode()


def check_sdk():
    installed = LOCAL / f"data/packages/esp32/hardware/esp32/{VERSION}/platform.txt"
    if not installed.is_file():
        raise SystemExit("Run python3 tools/project.py setup first")
    cores = list(installed.parent.parent.iterdir())
    if len(cores) != 1:
        raise SystemExit("Only pinned SDK 3.3.12 may be installed in this project's local data")


def manifest():
    data = {"core": VERSION, "fqbn": FQBN, "profile": PROFILE, "sources": {}, "binaries": {}}
    for path in sorted(SKETCH.glob("*")):
        if path.is_file():
            data["sources"][str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted(BUILD.glob("*.bin")):
        data["binaries"][path.name] = {"bytes": path.stat().st_size,
                                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    (BUILD / "manifest.json").write_text(json.dumps(data, indent=2) + "\n")
    print(json.dumps(data, indent=2))


def verify_usb_identity(port, expected_id):
    result = subprocess.run([cli_path(), "--config-file", str(CONFIG), "board", "list", "--format", "json"],
                            capture_output=True, text=True, check=True)
    ports = json.loads(result.stdout).get("detected_ports", [])
    matches = [p for p in ports if p.get("port", {}).get("address") == port]
    if len(matches) != 1:
        raise SystemExit("USB device missing or ambiguous: stop before writing")
    properties = matches[0]["port"].get("properties", {})
    if (properties.get("serialNumber") != expected_id or
            properties.get("vid", "").lower() != "0x303a" or
            properties.get("pid", "").lower() != "0x1001"):
        raise SystemExit("USB device identity mismatch: stop before writing")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["setup", "build", "ports", "upload", "monitor"])
    parser.add_argument("--port")
    parser.add_argument("--confirm-xiao", action="store_true")
    parser.add_argument("--confirm-usb-only", action="store_true")
    parser.add_argument("--erase-initial-confirmed", action="store_true")
    parser.add_argument("--loader", choices=["arduino", "rom"], default="arduino")
    parser.add_argument("--expected-id", help="Required for every upload; exact USB serial identifier")
    parser.add_argument("--profile", choices=["debug", "optimized"], default="debug",
                        help="optimized removes SDK logs; sketch diagnostics remain available")
    parser.add_argument("--sketch", choices=["WindCalmThreadTest", "WindCalmSimulator", "WindCalmProtocolTest", "WindCalmController"],
                        help="LED, simulation, or compile-only offline protocol probe")
    args = parser.parse_args()
    if args.sketch == "WindCalmProtocolTest" and args.action in ("upload", "monitor"):
        parser.error("Milestone 3 is offline / compile-only; keep the deployed simulator")
    if args.sketch:
        global BUILD, SKETCH
        BUILD = ROOT / "build" / args.sketch
        SKETCH = ROOT / "firmware" / args.sketch
    global FQBN, PROFILE
    PROFILE = args.profile
    FQBN = BASE_FQBN.replace("DebugLevel=error", "DebugLevel=none") if PROFILE == "optimized" else BASE_FQBN
    if PROFILE == "optimized":
        BUILD = ROOT / "build" / (SKETCH.name + "-optimized")
    configure()
    if args.action == "setup":
        run(["core", "update-index"])
        run(["core", "install", f"esp32:esp32@{VERSION}"])
    elif args.action == "ports":
        run(["board", "list", "--format", "json"])
    else:
        check_sdk()
        if args.action == "build":
            run(["compile", "--fqbn", FQBN, "--build-path", str(BUILD), str(SKETCH)], BUILD / "compile.log")
            manifest()
        else:
            if not args.port:
                parser.error("--port is required; no automatic device selection")
            if args.action == "monitor":
                run(["monitor", "--port", args.port, "--config", "baudrate=115200"])
            else:
                if not (args.confirm_xiao and args.confirm_usb_only):
                    parser.error("Upload requires --confirm-xiao and --confirm-usb-only after human confirmation")
                if not args.expected_id:
                    parser.error("Every upload requires --expected-id to prevent flashing another device")
                stored = json.loads((BUILD / "manifest.json").read_text())
                if stored.get("core") != VERSION or stored.get("fqbn") != FQBN:
                    raise SystemExit("Build profile / SDK mismatch: rebuild before upload")
                if stored.get("profile", "debug") != PROFILE:
                    raise SystemExit("Build profile mismatch: rebuild before upload")
                required = {SKETCH.name + ".ino.bin", SKETCH.name + ".ino.bootloader.bin",
                            SKETCH.name + ".ino.partitions.bin", "boot_app0.bin"}
                if not required.issubset(stored["binaries"]):
                    raise SystemExit("Required binary missing from manifest")
                # Check every current source, including newly added files; hashes are integrity checks,
                # not signatures and not proof that an untrusted artifact is safe.
                current = {str(p.relative_to(ROOT)) for p in SKETCH.glob("*") if p.is_file()}
                if current != set(stored["sources"]):
                    raise SystemExit("Source inventory changed: rebuild before upload")
                for path, digest in stored["sources"].items():
                    if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != digest:
                        raise SystemExit("Sources changed: rebuild before upload")
                for name, meta in stored["binaries"].items():
                    if hashlib.sha256((BUILD / name).read_bytes()).hexdigest() != meta["sha256"]:
                        raise SystemExit("Binary changed: rebuild before upload")
                fqbn = FQBN.replace("EraseFlash=none", "EraseFlash=all") if args.erase_initial_confirmed else FQBN
                command = ["upload", "--fqbn", fqbn, "--port", args.port, "--input-dir", str(BUILD)]
                verify_usb_identity(args.port, args.expected_id)
                if args.loader == "rom":
                    esptool = LOCAL / "data/packages/esp32/tools/esptool_py/5.3.1/esptool"
                    python = LOCAL / "esptool-venv/bin/python"
                    prefix = [str(python), "-m", "esptool"] if python.is_file() else [str(esptool)]
                    command = [*prefix, "--chip", "esp32c6", "--port", args.port, "--baud", "115200",
                               "--before", "no-reset", "--after", "hard-reset", "--connect-attempts", "2",
                               "write-flash", "--no-progress", "-z", "--flash-mode", "keep",
                               "--flash-freq", "keep", "--flash-size", "keep"]
                    if args.erase_initial_confirmed:
                        command += ["--erase-all"]
                    for offset, name in [("0x0", SKETCH.name + ".ino.bootloader.bin"),
                                         ("0x8000", SKETCH.name + ".ino.partitions.bin"),
                                         ("0xe000", "boot_app0.bin"), ("0x10000", SKETCH.name + ".ino.bin")]:
                        if name not in stored["binaries"]:
                            raise SystemExit("Required binary missing from manifest")
                        command += [offset, str(BUILD / name)]
                    execute(command, BUILD / "upload.log")
                else:
                    run(command, BUILD / "upload.log")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
    except KeyboardInterrupt:
        sys.exit(130)
