# SPDX-License-Identifier: MIT
"""An NVIDIA GPU, given to jobs through CDI (nvidia-container-toolkit)."""

import shutil
import subprocess

from ._sys import path


def present(root="/"):
    """a PCI device of NVIDIA's vendor id with a display class"""
    for dev in path(root, "/sys/bus/pci/devices").glob("*"):
        try:
            if (dev / "vendor").read_text().strip() == "0x10de" and (dev / "class").read_text().startswith("0x03"):
                return True
        except OSError:
            continue
    return False


def detect(root="/"):
    if not present(root):
        return "absent", "no NVIDIA GPU: no `gpu` resource, GPU jobs refused", ""
    if not path(root, "/etc/cdi/nvidia.yaml").exists():
        return "unsupported", "NVIDIA GPU found, no CDI spec", "dnf install nvidia-container-toolkit; mcu-admin init"
    if root == "/" and not shutil.which("nvidia-smi"):
        return "unsupported", "NVIDIA GPU found, no driver tools", "install the NVIDIA driver (akmod-nvidia)"
    return "present", "through CDI (nvidia.com/gpu=all)", ""


def query(fields, timeout=10, units=False):
    """the first GPU's fields from nvidia-smi --query-gpu, as stripped
    strings (units=True drops the units, for numbers); None when there is no
    working nvidia-smi, no GPU, or it does not answer in time (an eGPU
    being pulled)"""
    fmt = "csv,noheader,nounits" if units else "csv,noheader"
    try:
        r = subprocess.run(["nvidia-smi", f"--query-gpu={','.join(fields)}", f"--format={fmt}"],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = r.stdout.strip().splitlines() if r.returncode == 0 else []
    row = [x.strip() for x in lines[0].split(",")] if lines else []
    return row if len(row) == len(fields) else None


def listed(timeout=15):
    """a GPU answers now (nvidia-smi -L): the eGPU can go at any time"""
    try:
        r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and "GPU" in r.stdout


def num(x):
    """a number from nvidia-smi, None where it says N/A"""
    return None if x in (None, "", "[N/A]", "N/A") else float(x)

