# SPDX-License-Identifier: MIT
"""Whether the box is a virtual machine: it works there, but its timings
are not comparable (the host's load, turbo and throttling are invisible)."""

import shutil
import subprocess

from ._sys import read


def kind(root="/"):
    """the hypervisor's name, or None on bare metal"""
    if root == "/" and shutil.which("systemd-detect-virt"):
        r = subprocess.run(["systemd-detect-virt", "--vm"], capture_output=True, text=True)
        v = r.stdout.strip()
        return None if r.returncode or v in ("", "none") else v
    # without systemd-detect-virt (or in tests): the CPU's hypervisor flag
    flags = read(root, "/proc/cpuinfo", "") or ""
    return "vm" if " hypervisor" in flags else None


def detect(root="/"):
    k = kind(root)
    if k is None:
        return "absent", "bare metal", ""
    return "present", f"{k}: timings not comparable, see docs/VM.md", ""
