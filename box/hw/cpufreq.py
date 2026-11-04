# SPDX-License-Identifier: MIT
"""The timing profile's frequency cap. intel_pstate's max_perf_pct when that
driver is active; otherwise each CPU's cpufreq scaling_max_freq, set to the
share of cpuinfo_max_freq (amd-pstate, acpi-cpufreq); otherwise no cap, said
plainly in the job's conditions."""

from ._sys import path, read

PSTATE = "/sys/devices/system/cpu/intel_pstate/max_perf_pct"


def _cpus(root):
    base = path(root, "/sys/devices/system/cpu")
    return sorted(base.glob("cpu[0-9]*/cpufreq")) if base.exists() else []


def method(root="/"):
    """intel_pstate | cpufreq | None"""
    if read(root, PSTATE) is not None and read(root, "/sys/devices/system/cpu/intel_pstate/status", "active") != "off":
        return "intel_pstate"
    if any((c / "scaling_max_freq").exists() and (c / "cpuinfo_max_freq").exists() for c in _cpus(root)):
        return "cpufreq"
    return None


def detect(root="/"):
    m = method(root)
    if m == "intel_pstate":
        return "present", "intel_pstate max_perf_pct", ""
    if m == "cpufreq":
        driver = read(root, "/sys/devices/system/cpu/cpu0/cpufreq/scaling_driver", "cpufreq")
        return "present", f"{driver} scaling_max_freq", ""
    return "absent", "no frequency control: timing jobs run uncapped (cool-down only)", ""


def set_cap(pct, root="/"):
    """the cap in percent (100: none); False when this box has no way to cap"""
    m = method(root)
    if m == "intel_pstate":
        path(root, PSTATE).write_text(str(int(pct)))
        return True
    if m == "cpufreq":
        for c in _cpus(root):
            top = int((c / "cpuinfo_max_freq").read_text())
            (c / "scaling_max_freq").write_text(str(top * int(pct) // 100))
        return True
    return False


def capped(root="/"):
    """a cap is set (a timing job's, or one a crash left)"""
    m = method(root)
    if m == "intel_pstate":
        return read(root, PSTATE) != "100"
    if m == "cpufreq":
        return any(int((c / "scaling_max_freq").read_text()) < int((c / "cpuinfo_max_freq").read_text())
                   for c in _cpus(root))
    return False


def reset(root="/"):
    """full speed again (after a timing job, or at boot after a crash)"""
    return set_cap(100, root)
