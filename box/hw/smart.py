# SPDX-License-Identifier: MIT
"""Disk health from smartctl (root): an SSD's wear and bytes written, a hard
disk's reallocated and pending sectors, power-on hours, temperature, the
verdict. Without smartctl, nothing (the dashboard says "not available")."""

import json
import shutil
import subprocess


def detect(root="/"):
    if root == "/" and not shutil.which("smartctl"):
        return "absent", "disk health not available", "dnf install smartmontools"
    return "present", "smartctl", ""


def parse(dev, d):
    """one disk's health from lsblk's row and smartctl's JSON"""
    nv = d.get("nvme_smart_health_information_log") or {}
    attrs = {a.get("name"): a.get("raw", {}).get("value") for a in (d.get("ata_smart_attributes") or {}).get("table", [])}
    written = nv.get("data_units_written")
    return {"device": dev["path"], "model": d.get("model_name") or dev.get("model"), "size": dev.get("size"),
            "media": "hdd" if str(dev.get("rota")) in ("1", "True", "true") else "ssd",
            "ok": (d.get("smart_status") or {}).get("passed"),
            "wear_pct": nv.get("percentage_used"),
            "written": written * 512000 if written is not None else
                       (attrs.get("Total_LBAs_Written") or 0) * 512 or None,
            "reallocated": attrs.get("Reallocated_Sector_Ct"), "pending": attrs.get("Current_Pending_Sector"),
            "hours": nv.get("power_on_hours") if nv else attrs.get("Power_On_Hours"),
            "temp": (d.get("temperature") or {}).get("current")}


def read_all():
    """every physical disk's health; [] without smartctl"""
    if not shutil.which("smartctl"):
        return []
    try:
        devs = json.loads(subprocess.run(["lsblk", "-J", "-d", "-o", "NAME,PATH,TYPE,ROTA,MODEL,SIZE", "-b"],
                                         capture_output=True, text=True).stdout)["blockdevices"]
    except (ValueError, KeyError, OSError):
        return []
    out = []
    for dev in devs:
        if dev.get("type") != "disk" or dev["name"].startswith(("zram", "loop")):
            continue
        r = subprocess.run(["smartctl", "-j", "-a", dev["path"]], capture_output=True, text=True)
        try:
            out.append(parse(dev, json.loads(r.stdout)))
        except ValueError:
            continue
    return out
