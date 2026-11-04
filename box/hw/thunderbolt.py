# SPDX-License-Identifier: MIT
"""An eGPU behind Thunderbolt/USB4: it can be unplugged, so the box handles
its eject and re-attach (udev rule, shutdown helper)."""

from ._sys import path


def controllers(root="/"):
    d = path(root, "/sys/bus/thunderbolt/devices")
    return sorted(x.name for x in d.glob("domain*")) if d.exists() else []


def detect(root="/"):
    if not controllers(root):
        return "absent", "no Thunderbolt/USB4 controller: no eGPU handling", ""
    return "present", "Thunderbolt/USB4: eject and re-attach handled", ""
