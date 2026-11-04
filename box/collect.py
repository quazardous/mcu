#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Every minute, sample the machine into round-robin databases (rrdtool),
Munin style: fixed-size files, consolidated as they age (1 min for 2 days,
5 min for 2 weeks, 1 h for a year). Run by mcu-collect.timer as the
mcu-web user; reads only /proc, /sys, nvidia-smi and usage.json.

    machine.rrd   load, memory, swap, root disk free, disk I/O, CPU temperature
                  and mean frequency
    net.rrd       bytes received and sent by the physical network interfaces
    cpu.rrd       busy share of each thread
    gpu.rrd       NVIDIA GPU utilization, memory, temperature, power
    power.rrd     watts: the platform (RAPL psys: CPU, memory, chipset), the
                  CPU package, its cores, the memory, the GPU, and the total
                  (platform + GPU), each a mean over the minute; the RAPL
                  counters are made readable by mcu-web (mcu-admin init)
    wear.rrd      the SSD's wear (% used, TB written), a hard disk's reallocated
                  sectors, from smartctl through usage.json
    disk-<project>.rrd   the project's disk use and quota

The dashboard's API serves them as JSON (rrdtool xport); jobs are not here,
they are irregular and stay in events.jsonl.

It also keeps resources.json, what the box can do right now (the GPU, the
maintenance flag), and logs a `box` event when that changes, for the
dashboard and for `mcu-job box`.

    collect.py gpu <why>   only the GPU's state, now: run by udev (through
                           systemd-run, as mcu-web) when the eGPU's driver
                           binds or unbinds, and by an eject or re-attach, so
                           the box knows at once; the minute's run only
                           corrects a missed event
"""

import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hw  # noqa: E402  the optional hardware, one module per feature

STATE = Path("/var/lib/mcu")
RRD = STATE / "rrd"
STEP = 60
RRAS = [f"RRA:{cf}:0.5:{steps}:{rows}" for cf in ("AVERAGE", "MAX")
        for steps, rows in ((1, 2880), (5, 4032), (60, 8760))]


def ensure(name, sources):
    """create the database the first time; sources: (name, type, max)"""
    path = RRD / f"{name}.rrd"
    if not path.exists():
        ds = [f"DS:{n}:{t}:{STEP * 2}:0:{m}" for n, t, m in sources]
        subprocess.run(["rrdtool", "create", str(path), "--step", str(STEP)] + ds + RRAS, check=True)
    return path


def update(name, sources, values):
    path = ensure(name, sources)
    vals = ":".join("U" if v is None else str(v) for v in values)
    subprocess.run(["rrdtool", "update", str(path), f"N:{vals}"], check=False)


def read_meminfo():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0]) * 1024
    return info


def disk_sectors():
    """bytes read and written by the physical disks (no partitions, zram,
    loop or device mapper, which would count the same I/O twice), cumulative"""
    rd = wr = 0
    found = False
    with open("/proc/diskstats") as f:
        for line in f:
            p = line.split()
            if Path(f"/sys/block/{p[2]}/device").exists():
                rd, wr, found = rd + int(p[5]) * 512, wr + int(p[9]) * 512, True
    return (rd, wr) if found else (None, None)


def net_bytes():
    """bytes received and sent by the physical interfaces (not lo, nor the
    containers' virtual ones), cumulative"""
    rx = tx = 0
    found = False
    with open("/proc/net/dev") as f:
        for line in f.readlines()[2:]:
            name, data = line.split(":", 1)
            if Path(f"/sys/class/net/{name.strip()}/device").exists():
                v = data.split()
                rx, tx, found = rx + int(v[0]), tx + int(v[8]), True
    return (rx, tx) if found else (None, None)


def cpu_temp():
    for mon in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            if (mon / "name").read_text().strip() in ("coretemp", "k10temp", "x86_pkg_temp"):
                temps = [int(t.read_text()) / 1000 for t in mon.glob("temp*_input")]
                return max(temps) if temps else None
        except OSError:
            pass
    return None


def mean_freq():
    freqs = []
    for f in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_cur_freq"):
        try:
            freqs.append(int(f.read_text()) / 1000)
        except OSError:
            pass
    return round(sum(freqs) / len(freqs)) if freqs else None


def cpu_jiffies():
    """per thread: busy jiffies, cumulative"""
    busy = {}
    with open("/proc/stat") as f:
        for line in f:
            p = line.split()
            if p[0].startswith("cpu") and p[0] != "cpu":
                v = list(map(int, p[1:]))
                busy[int(p[0][3:])] = sum(v) - v[3] - v[4]  # minus idle and iowait
    return busy


def gpu():
    row = hw.nvidia.query(["utilization.gpu", "memory.used", "temperature.gpu", "power.draw"], units=True)
    if not row:
        return None
    try:
        u, m, t, w = (hw.nvidia.num(x) for x in row)
    except ValueError:
        return None
    return u, m * 2**20 if m is not None else None, t, w


RAPL = Path("/sys/class/powercap")
RAPL_ZONES = {"psys": "platform", "package-0": "package", "core": "core", "dram": "dram"}


def rapl_watts():
    """each RAPL zone's mean watts since the last run, from its energy
    counter (microjoules, wrapping at max_energy_range_uj); the previous
    readings are kept in power.last.json; {} until there are two"""
    now, cur = time.time(), {}
    for z in RAPL.glob("intel-rapl:*"):
        try:
            name = RAPL_ZONES.get((z / "name").read_text().strip())
            if name:
                cur[name] = (int((z / "energy_uj").read_text()), int((z / "max_energy_range_uj").read_text()))
        except (OSError, ValueError):
            pass                # not readable: mcu-admin init grants it
    last_path = RRD / "power.last.json"
    last = read_json(last_path)
    tmp = RRD / f"power.last.json.{os.getpid()}"
    tmp.write_text(json.dumps({"t": now, "zones": cur}))
    os.replace(tmp, last_path)
    dt = now - last.get("t", now)
    if not 10 < dt < STEP * 3:
        return {}
    watts = {}
    for name, (e, wrap) in cur.items():
        prev = (last.get("zones") or {}).get(name)
        if prev:
            d = e - prev[0]
            watts[name] = round((d + wrap if d < 0 else d) / dt / 1e6, 2)
    return watts


def gpu_name():
    row = hw.nvidia.query(["name"])
    return row[0] if row else None


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def stale_deploy_hold():
    """a queue pause left by a box upgrade whose script is gone (killed
    before its own reopen): reopened"""
    q = read_json(STATE / "queue.json")
    by = str(q.get("by") or "")
    if q.get("mode") not in ("paused", "closed") or not by.startswith("deploy:"):
        return
    try:
        os.kill(int(by.split(":", 1)[1]), 0)
        return
    except PermissionError:
        return          # alive, root's
    except (ProcessLookupError, ValueError):
        pass
    # through the queue's one writer (the dashboard's sudo rule covers it),
    # only if it is still that deploy's
    subprocess.run(["sudo", "-n", "/usr/local/lib/mcu/mcu-admin", "queue", "open", "--if-by", by], capture_output=True)


def resources(gpu_sample, why=None):
    """resources.json, and a box event when the GPU or maintenance changes.

    The eGPU's state is a small state machine, so that what the dashboard
    shows never flickers:
      ejecting, attaching   the admin's operation in progress (mcu-helper
                            writes gpu.json before it starts and clears it in
                            the same step as the final state): shown as is,
                            whatever the card answers meanwhile
      ejected, lost         the admin's eject, or a pull without one: until
                            the driver takes the card back (mcu-helper)
      present, absent       what the card answers; an event (udev, the
                            helper: why is set) is believed at once, the
                            minute's own probe only after two misses in a
                            row (one slow nvidia-smi is not an unplug)
    gpu is whether GPU jobs can run: present only. Writers (the minute's run,
    udev's, the helper's) take turns, so an older reading never lands last"""
    with open(STATE / "resources.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = read_json(STATE / "resources.json")
        now = time.time()
        answers = gpu_sample is not None
        flag = read_json(STATE / "gpu.json")      # written by mcu-helper, for this boot
        held = flag.get("state") if flag.get("boot") in (None, boot_id()) else None
        misses = 0 if answers else old.get("gpu_misses", 0) + 1
        if held in ("ejecting", "attaching", "ejected"):
            state = held
        elif answers:
            state = "present"
        elif held == "lost":
            state = "lost"
        elif why is None and old.get("gpu_state") == "present" and misses < 2:
            state = "present"                     # one miss of the minute's probe: wait for the next
        else:
            state = "absent"
        present = state == "present"
        q = read_json(STATE / "queue.json")
        reason = q.get("reason") if q.get("mode") == "closed" else None
        new = {"updated": now, "gpu": present, "gpu_state": state, "gpu_misses": misses,
               "gpu_name": gpu_name() if answers else old.get("gpu_name"),
               "gpu_since": old.get("gpu_since") if old.get("gpu_state", "present" if old.get("gpu") else "absent") == state
               else now,
               "gpu_note": flag.get("note") if state == "lost" else None,
               "maintenance": reason}
        tmp = STATE / f"resources.json.{os.getpid()}"
        tmp.write_text(json.dumps(new, indent=1) + "\n")
        os.replace(tmp, STATE / "resources.json")
        changes = {}
        if "gpu" in old and old["gpu"] != present:
            changes["gpu"] = present
        if old.get("gpu_state") != state and "updated" in old:
            changes["gpu_state"] = state
            if why:
                changes["why"] = why
        if old.get("maintenance") != reason and "updated" in old:
            changes["maintenance"] = reason
        if changes:
            with open(STATE / "events.jsonl", "a") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.write(json.dumps({"kind": "box", **changes, "resources": new, "t": now}) + "\n")


def gpu_now(why):
    """the GPU's state now; after a bind the driver may need a moment"""
    g = gpu()
    for _ in range(5 if g is None and why in ("bind", "add", "attach") else 0):
        time.sleep(2)
        g = gpu()
        if g is not None:
            break
    resources(g, why)


def hardware():
    """hardware.json: what this machine has of the optional hardware (box/hw),
    for the dashboard and doctor, so they show what exists and hide the rest"""
    rows = hw.report()
    path = STATE / "hardware.json"
    try:
        if json.loads(path.read_text()).get("features") == rows:
            return
    except (OSError, ValueError):
        pass
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"updated": int(time.time()), "features": rows}, indent=1) + "\n")
    os.replace(tmp, path)


def main():
    if sys.argv[1:2] == ["gpu"]:
        return gpu_now(sys.argv[2] if len(sys.argv) > 2 else None)
    RRD.mkdir(exist_ok=True)
    mem = read_meminfo()
    du = shutil.disk_usage("/")
    rd, wr = disk_sectors()
    update("machine",
           [("load", "GAUGE", "U"), ("mem_used", "GAUGE", "U"), ("mem_available", "GAUGE", "U"),
            ("swap_used", "GAUGE", "U"), ("disk_free", "GAUGE", "U"), ("disk_read", "DERIVE", "U"),
            ("disk_write", "DERIVE", "U"), ("cpu_temp", "GAUGE", 150), ("cpu_mhz", "GAUGE", "U")],
           [os.getloadavg()[0], mem["MemTotal"] - mem["MemAvailable"], mem["MemAvailable"],
            mem.get("SwapTotal", 0) - mem.get("SwapFree", 0), du.free, rd, wr, cpu_temp(), mean_freq()])

    update("net", [("rx", "DERIVE", "U"), ("tx", "DERIVE", "U")], list(net_bytes()))

    # per thread busy share: DERIVE of busy jiffies (USER_HZ = 100 per second)
    busy = cpu_jiffies()
    threads = sorted(busy)
    update("cpu", [(f"t{n}", "DERIVE", "U") for n in threads], [busy[n] for n in threads])

    stale_deploy_hold()
    hardware()
    g = gpu()
    resources(g)
    if g:
        update("gpu", [("util", "GAUGE", 100), ("mem_used", "GAUGE", "U"), ("temp", "GAUGE", 150),
                       ("power", "GAUGE", "U")], list(g))

    w = rapl_watts()
    gw = g[3] if g else None
    unit = w.get("platform", w.get("package"))
    if w or gw is not None:
        update("power", [(n, "GAUGE", 2000) for n in ("total", "platform", "package", "core", "dram", "gpu")],
               [None if unit is None else round(unit + (gw or 0), 2), w.get("platform"), w.get("package"),
                w.get("core"), w.get("dram"), gw])

    try:
        usage = json.loads((STATE / "usage.json").read_text())
    except (OSError, ValueError):
        usage = {}
    # the disks' wear, from the root timer's smartctl: the SSD's
    # percentage used and TB written, the hard disk's reallocated sectors
    health = usage.get("_disks", {}).get("smart", [])
    ssd = next((h for h in health if h.get("media") == "ssd" and h.get("wear_pct") is not None), {})
    hdd = next((h for h in health if h.get("media") == "hdd"), {})
    if ssd or hdd:
        update("wear", [("ssd_wear", "GAUGE", 200), ("ssd_written", "GAUGE", "U"), ("hdd_realloc", "GAUGE", "U")],
               [ssd.get("wear_pct"), round(ssd["written"] / 1e12, 3) if ssd.get("written") else None,
                hdd.get("reallocated")])
    for project, u in usage.items():
        if project.startswith("_"):
            continue  # the pools' section
        update(f"disk-{project}", [("used", "GAUGE", "U"), ("quota", "GAUGE", "U")],
               [u.get("used"), u.get("quota") or None])


if __name__ == "__main__":
    main()
