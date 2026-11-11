#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Install the pinned build-only SDK under .local/dedicated (Linux/macOS)."""
import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

import thread_sdk as sdk

CORE_URL = "https://github.com/espressif/arduino-esp32/releases/download/3.3.12/esp32-core-3.3.12.zip"
CORE_SHA = "87401004f279a206781e368b66098a8383b7cf87cce7811a6afd0163503fa78f"
COMPILER_URL = "https://github.com/espressif/crosstool-NG/releases/download/esp-14.2.0_20260121/"
COMPILERS = {
    ("Linux", "x86_64"): ("x86_64-linux-gnu", "1b79617f43cf0e25b92646359d1623d8bef135050aca44586df59b6a64496d82"),
    ("Linux", "aarch64"): ("aarch64-linux-gnu", "94f36eba9191f8cb8503cea29fd9a89ddd320527fba5ef3960c8bc693548d471"),
    ("Darwin", "x86_64"): ("x86_64-apple-darwin", "bf649fd1c986baeb08865a4663637676b9f0159c034c54d0c61961f5c2847ba6"),
    ("Darwin", "arm64"): ("aarch64-apple-darwin", "cb32336452a4c8996b38814761a945fe7856fada1f4d57950cbabe9b0ecbfc52"),
}
CORE_PARTS = {"CMakeLists.txt", "Kconfig.projbuild", "boards.txt", "cores", "libraries", "variants", "platform.txt", "package.json"}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url, expected, destination):
    """Only promote a complete, hash-verified download to the cache."""
    if destination.exists():
        if sha256(destination) != expected:
            raise ValueError("Cached archive checksum mismatch: " + destination.name)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".partial")
    try:
        print("Downloading " + destination.name, flush=True)
        with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as stream:
            shutil.copyfileobj(response, stream, length=1024 * 1024)
        if sha256(partial) != expected:
            raise ValueError("Downloaded archive checksum mismatch: " + destination.name)
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)


def safe_path(root, name):
    parts = PurePosixPath(name)
    if parts.is_absolute() or ".." in parts.parts or "\\" in name or ":" in name:
        raise ValueError("Unsafe archive member: " + name)
    path = root.joinpath(*parts.parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Archive member escapes extraction root")
    return path


def extract_compiler(archive, destination):
    """Extract into a fresh directory; allow only internal, relative links."""
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        paths = set()
        for member in members:
            path = safe_path(destination, member.name)
            if path in paths:
                raise ValueError("Duplicate archive member")
            paths.add(path)
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError("Unsupported archive entry")
            if member.issym() or member.islnk():
                base = path.parent if member.issym() else destination
                if Path(member.linkname).is_absolute() or "\\" in member.linkname:
                    raise ValueError("Unsafe archive link")
                target = (base / member.linkname).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise ValueError("Archive link escapes extraction root")
        # Create links last so ordinary members cannot write through them.
        for member in members:
            path = safe_path(destination, member.name)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                path.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(member) as stream, path.open("xb") as output:
                    shutil.copyfileobj(stream, output)
                path.chmod(member.mode & 0o777)
        for member in members:
            path = safe_path(destination, member.name)
            if member.issym():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(member.linkname)
            elif member.islnk():
                path.parent.mkdir(parents=True, exist_ok=True)
                os.link(safe_path(destination, member.linkname), path)


def extract_core(archive, destination):
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            # Validate every path, including files outside the copied component.
            safe_path(destination, member.filename)
            parts = PurePosixPath(member.filename).parts
            if not parts or parts[0] != "esp32-core-3.3.12":
                raise ValueError("Unexpected Arduino archive root")
            if len(parts) < 2 or parts[1] not in CORE_PARTS:
                continue
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ValueError("Unsupported Arduino archive entry")
            path = safe_path(destination, "/".join(parts[1:]))
            if member.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as stream, path.open("xb") as output:
                    shutil.copyfileobj(stream, output)
                path.chmod((mode & 0o777) or 0o644)
    (destination / "idf_component.yml").write_text('dependencies:\n  idf: ">=5.5.5,<5.6"\n')


def run(command, env=None):
    subprocess.run(command, check=True, env=env)


def install():
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 or newer is required")
    host = (platform.system(), platform.machine())
    if host not in COMPILERS:
        raise SystemExit("Supported hosts: Linux x86_64/aarch64, macOS x86_64/arm64")
    sdk.LOCAL.mkdir(parents=True, exist_ok=True)
    cache = sdk.LOCAL / "downloads"
    if not sdk.IDF.exists():
        # Exact commit, including pinned submodules. Never modify a global IDF.
        run(["git", "init", str(sdk.IDF)])
        run(["git", "-C", str(sdk.IDF), "remote", "add", "origin", "https://github.com/espressif/esp-idf.git"])
        run(["git", "-C", str(sdk.IDF), "fetch", "--depth", "1", "origin", sdk.IDF_COMMIT])
        run(["git", "-C", str(sdk.IDF), "checkout", "--detach", "FETCH_HEAD"])
    revision = subprocess.check_output(["git", "-C", str(sdk.IDF), "rev-parse", "HEAD"], text=True).strip()
    if revision != sdk.IDF_COMMIT:
        raise SystemExit("Existing ESP-IDF revision differs; refusing replacement")
    run(["git", "-C", str(sdk.IDF), "submodule", "update", "--init", "--recursive", "--depth", "1"])
    arduino = sdk.LOCAL / "arduino"
    if not arduino.exists():
        archive = cache / "esp32-core-3.3.12.zip"
        download(CORE_URL, CORE_SHA, archive)
        with tempfile.TemporaryDirectory(dir=sdk.LOCAL) as temporary:
            staged = Path(temporary) / "arduino"
            extract_core(archive, staged)
            staged.replace(arduino)
    if 'version=3.3.12' not in (arduino / "platform.txt").read_text():
        raise SystemExit("Existing Arduino version differs; refusing replacement")
    compiler_bin = sdk.compiler_bin()
    if not compiler_bin.exists():
        suffix, checksum = COMPILERS[host]
        filename = "riscv32-esp-elf-14.2.0_20260121-" + suffix + ".tar.gz"
        archive = cache / filename
        download(COMPILER_URL + filename, checksum, archive)
        with tempfile.TemporaryDirectory(dir=sdk.LOCAL) as temporary:
            staged = Path(temporary) / "compiler"
            extract_compiler(archive, staged)
            compiler_bin.parent.parent.mkdir(parents=True, exist_ok=True)
            (staged / "riscv32-esp-elf").replace(compiler_bin.parent)
    env = os.environ.copy()
    env.update(IDF_PATH=str(sdk.IDF), IDF_TOOLS_PATH=str(sdk.LOCAL / "idf-tools"))
    # The SDK helper discovers this per-Python environment on subsequent builds.
    python_env = sdk.LOCAL / f"idf-tools/python_env/idf5.5_py{sys.version_info.major}.{sys.version_info.minor}_env"
    env["IDF_PYTHON_ENV_PATH"] = str(python_env)
    installer = [sys.executable, str(sdk.IDF / "tools/idf_tools.py")]
    run(installer + ["install", "--targets", "esp32c6", "cmake@3.30.2", "ninja@1.12.1", "esp-rom-elfs@20241011"], env)
    run(installer + ["install-python-env"], env)
    sdk.environment()
    sdk.patch_defaults()
    print("Pinned SDK ready. Next: python3 tools/thread_ota.py build", flush=True)


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        install()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error))
