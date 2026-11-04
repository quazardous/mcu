# SPDX-License-Identifier: MIT
"""An NVIDIA eGPU's operations on the bus: find its PCI functions, take them
off the bus, de-authorize and re-authorize its Thunderbolt device, wake the
ports, rescan, load and unload the driver within deadlines. mcu-helper runs
them (eject, attach, udev events, power-off) and keeps the state; detection
is thunderbolt.py and nvidia.py. root is "/" on the box, a fake tree in the
tests; the driver and process checks only make sense on the box."""

import re
import subprocess
import time

from ._sys import path

FELL_OFF = ("the eGPU fell off the bus (Xid 79): power the enclosure off, restart the box, "
            "then power the enclosure on once the box is up (booting with it on is what drops it)")

MODULES = ["nvidia_uvm", "nvidia_drm", "nvidia_modeset", "nvidia"]


def bounded(cmd, secs):
    """run cmd for at most secs; a process stuck in the kernel (D state)
    ignores every signal, so past the deadline it is left behind, never
    waited for. The exit code, or None on timeout"""
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return p.wait(secs)
    except subprocess.TimeoutExpired:
        p.kill()
        return None


def functions(root="/"):
    """the NVIDIA GPU's PCI functions (the card, its audio...), the card's
    last so a removal takes its companions first; [] without one"""
    devices = path(root, "/sys/bus/pci/devices")
    cards = []
    for dev in sorted(devices.glob("*")) if devices.exists() else []:
        try:
            if (dev / "vendor").read_text().strip() == "0x10de" and (dev / "class").read_text().startswith("0x03"):
                cards.append(dev.name)
        except OSError:
            continue
    if not cards:
        return []
    slot = cards[0].rsplit(".", 1)[0]
    return sorted((d.name for d in devices.glob(slot + ".*")), reverse=True)


def bound(root="/", wait=0):
    """the nvidia driver holds a card (now, or within wait seconds)"""
    for i in range(wait + 1):
        if list(path(root, "/sys/bus/pci/drivers/nvidia").glob("0000:*")):
            return True
        if i < wait:
            time.sleep(1)
    return False


def fell_off_bus():
    """did the card fall off the bus during this boot (NVRM Xid 79)? Then
    the driver holds it in the kernel: an unload hangs, only a power cycle
    of the enclosure and a restart bring it back"""
    r = subprocess.run(["journalctl", "-k", "-b", "--no-pager", "-g", "has fallen off the bus"],
                       capture_output=True, text=True)
    return "fallen off the bus" in r.stdout


def stuck():
    """nvidia threads stuck in the kernel, or a card that fell off the bus:
    touching the driver then would only hang with it (an unload that never
    returns)"""
    out = [line for line in subprocess.run(["ps", "-eo", "stat=,pid=,comm="], capture_output=True, text=True).stdout.splitlines()
           if line.split()[0].startswith("D") and "nvidia" in line]
    return out + (["the card fell off the bus this boot (Xid 79)"] if fell_off_bus() else [])


def unload(log):
    """nvidia-persistenced stopped, then the modules out, each within a
    deadline; False at the first that won't go (in use or stuck)"""
    if subprocess.run(["systemctl", "is-active", "--quiet", "nvidia-persistenced"]).returncode == 0:
        bounded(["systemctl", "stop", "nvidia-persistenced"], 15)
    loaded = path("/", "/proc/modules").read_text()
    for m in MODULES:
        if re.search(rf"^{m} ", loaded, re.M):
            code = bounded(["modprobe", "-r", m], 10)
            if code != 0:
                log(f"{m} would not unload ({'timed out' if code is None else 'in use'})")
                return False
            log(f"{m} unloaded")
    return True


def load():
    """the driver back in (nvidia_drm and nvidia_uvm pull the rest)"""
    bounded(["modprobe", "nvidia_drm"], 30)
    bounded(["modprobe", "nvidia_uvm"], 30)


def remove(fns, root="/"):
    """take these PCI functions off the bus"""
    for fn in fns:
        path(root, f"/sys/bus/pci/devices/{fn}/remove").write_text("1")


def _tb_devices(root):
    d = path(root, "/sys/bus/thunderbolt/devices")
    # a device behind a host port (1-1, 0-3), never the host controller (0-0)
    return [x for x in sorted(d.glob("*-*")) if re.fullmatch(r"\d+-[1-9]\d*", x.name)] if d.exists() else []


def deauthorize(root="/"):
    """de-authorize the authorized Thunderbolt devices where their domain
    allows it; the names of those done ([] when the host does not allow it)"""
    done = []
    for dev in _tb_devices(root):
        if (dev / "authorized").read_text().strip() != "1":
            continue
        domain = path(root, f"/sys/bus/thunderbolt/devices/domain{dev.name.split('-')[0]}")
        if (domain / "deauthorization").exists() and (domain / "deauthorization").read_text().strip() == "1":
            (dev / "authorized").write_text("0")
            name = dev / "device_name"
            done.append(name.read_text().strip() if name.exists() else dev.name)
    return done


def reauthorize(root="/"):
    """authorize again the de-authorized Thunderbolt devices; their names"""
    done = []
    for dev in _tb_devices(root):
        if (dev / "authorized").read_text().strip() == "0":
            (dev / "authorized").write_text("1")
            done.append(dev.name)
    return done


def wake_ports(root="/"):
    """PCIe bridges left runtime-suspended (D3) after an eject hide what is
    behind them from a rescan (the enclosure's downstream port suspends
    within minutes): keep them on; the ones woken"""
    woken = []
    devices = path(root, "/sys/bus/pci/devices")
    for dev in sorted(devices.iterdir()) if devices.exists() else []:
        try:
            if (dev / "class").read_text().startswith("0x0604") \
                    and (dev / "power/runtime_status").read_text().strip() == "suspended":
                (dev / "power/control").write_text("on")
                woken.append(dev.name)
        except OSError:
            pass
    return woken


def rescan(root="/"):
    path(root, "/sys/bus/pci/rescan").write_text("1")
