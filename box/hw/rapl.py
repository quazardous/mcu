# SPDX-License-Identifier: MIT
"""RAPL: the CPU package's energy counter (the power graph), its long-term
limit (the power profiles) and its short-term one (box.toml [power] burst). Intel, and recent AMD through the same powercap
interface."""

from ._sys import path


def zone(root="/"):
    for name in ("intel-rapl:0", "amd-rapl:0"):
        z = path(root, f"/sys/class/powercap/{name}")
        if (z / "energy_uj").exists():
            return z
    return None


def detect(root="/"):
    z = zone(root)
    if z is None:
        return "absent", "no powercap energy counter: no power graph, no power profiles", ""
    limit = (z / "constraint_0_power_limit_uw").exists()
    return "present", z.name + ("" if limit else " (energy only: no power profiles)"), ""


def limit_w(root="/"):
    """the long-term power limit (PL1) in watts, or None without RAPL"""
    z = zone(root)
    f = z / "constraint_0_power_limit_uw" if z else None
    return int(f.read_text()) / 1e6 if f and f.exists() else None


def set_limit_w(watts, root="/"):
    """PL1 in watts. constraint_0_max_power_uw is the CPU's nominal TDP (15 W
    on a U-series whose firmware sets 30 W), not a ceiling: no check against
    it, only a sane range"""
    if not 3 <= watts <= 200:
        raise ValueError(f"{watts:g} W is not a sensible CPU power limit")
    z = zone(root)
    if z is None or not (z / "constraint_0_power_limit_uw").exists():
        raise OSError("this CPU has no RAPL power limit to set")
    (z / "constraint_0_power_limit_uw").write_text(str(int(watts * 1e6)))


def burst_w(root="/"):
    """the short-term power limit (PL2) in watts, or None without one"""
    z = zone(root)
    f = z / "constraint_1_power_limit_uw" if z else None
    return int(f.read_text()) / 1e6 if f and f.exists() else None


def set_burst_w(watts, root="/"):
    """PL2 in watts: how far a burst may go above PL1 for a few seconds. On
    a small case, the firmware's PL2 (64 W on a 15 W U-series) reaches the
    CPU's maximum temperature within seconds at each job's start"""
    if not 3 <= watts <= 200:
        raise ValueError(f"{watts:g} W is not a sensible CPU power limit")
    z = zone(root)
    name = z / "constraint_1_name" if z else None
    if name is None or not name.exists() or name.read_text().strip() != "short_term":
        raise OSError("this CPU has no RAPL short-term power limit to set")
    (z / "constraint_1_power_limit_uw").write_text(str(int(watts * 1e6)))

