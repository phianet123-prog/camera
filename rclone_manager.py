"""
Locates a usable rclone binary, downloading one automatically into this
project's bin/ folder if it isn't already installed system-wide. This
is what lets "Connect OneDrive" be a single button instead of a manual
"go install this CLI tool first" step.
"""
import os
import io
import stat
import shutil
import platform
import zipfile

import requests

import config as cfg

BIN_DIR = os.path.join(cfg.BASE_DIR, "bin")


def _local_binary_name():
    return "rclone.exe" if platform.system() == "Windows" else "rclone"


def _local_binary_path():
    return os.path.join(BIN_DIR, _local_binary_name())


def find_rclone():
    """Returns a path to a working rclone binary, or None if there
    isn't one installed system-wide and we haven't downloaded one yet."""
    system_rclone = shutil.which("rclone")
    if system_rclone:
        return system_rclone
    local_path = _local_binary_path()
    if os.path.exists(local_path):
        return local_path
    return None


def _download_url():
    system = platform.system()
    machine = platform.machine().lower()

    if system == "Windows":
        os_tag = "windows"
    elif system == "Darwin":
        os_tag = "osx"
    elif system == "Linux":
        os_tag = "linux"
    else:
        raise RuntimeError(f"Don't know how to fetch rclone for platform '{system}'.")

    if machine in ("arm64", "aarch64"):
        arch_tag = "arm64"
    elif machine in ("x86_64", "amd64"):
        arch_tag = "amd64"
    elif machine in ("i386", "i686", "x86"):
        arch_tag = "386"
    else:
        arch_tag = "amd64"  # best guess fallback

    return f"https://downloads.rclone.org/rclone-current-{os_tag}-{arch_tag}.zip"


def download_rclone():
    """Downloads the current rclone release and extracts just the
    executable into this project's bin/ folder. Returns the path to it."""
    os.makedirs(BIN_DIR, exist_ok=True)
    url = _download_url()
    print(f"[RCLONE] Not found locally — downloading from {url} ...")

    response = requests.get(url, timeout=120)
    response.raise_for_status()

    target_name = _local_binary_name()
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        member = next((m for m in zf.namelist() if m.endswith("/" + target_name)), None)
        if member is None:
            raise RuntimeError(f"Couldn't find {target_name} inside the downloaded rclone archive.")
        data = zf.read(member)

    local_path = _local_binary_path()
    with open(local_path, "wb") as f:
        f.write(data)

    if platform.system() != "Windows":
        st = os.stat(local_path)
        os.chmod(local_path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    print(f"[RCLONE] Installed to {local_path}")
    return local_path


def ensure_rclone():
    """Returns a path to a usable rclone binary, downloading one if needed."""
    existing = find_rclone()
    if existing:
        return existing
    return download_rclone()
