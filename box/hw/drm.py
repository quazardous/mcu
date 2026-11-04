# SPDX-License-Identifier: MIT
"""Render nodes (/dev/dri/renderD*) and their drivers: the shared `render`
resource for compositors and GL/Vulkan jobs."""

import os

from ._sys import path


def nodes(root="/"):
    """{renderD128: driver}"""
    out = {}
    for d in sorted(path(root, "/sys/class/drm").glob("renderD*")):
        drv = d / "device" / "driver"
        out[d.name] = os.path.basename(os.path.realpath(drv)) if drv.exists() else "?"
    return out


def detect(root="/"):
    n = nodes(root)
    if not n:
        return "absent", "no render node: GUI jobs render on the CPU (pixman)", ""
    return "present", ", ".join(f"{k} ({v})" for k, v in n.items()), ""
