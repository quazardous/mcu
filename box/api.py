#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""The mcu box's API over HTTPS, and the static files of its web UI
(dashboard/ in this repository, built with Vite). Runs as the mcu-web
user; reads the scheduler's files under /var/lib/mcu and the box config,
never a project's home.

Everything but the UI's static files and the login needs a session
(the admin password, mcu-admin passwd); agents use their SSH key instead.

    GET  /api/session                    whether logged in, and the CSRF token
    POST /api/login   {"password"}       open a session (cookie)
    POST /api/logout
    GET  /api/state                      threads, queue, running, machine, projects, volumes
    GET  /api/topology                   the declared resources and the hardware behind them
    GET  /api/health                     queue (with the expected wait), machine and GPU state
    GET  /api/events                     Server-Sent Events: every scheduler event as
                                         it happens, `state` when the queue changes, and
                                         `samples` (machine CPU, memory, disk and network
                                         every 2 s, the last 15 min on connect);
                                         Last-Event-ID resumes
    GET  /api/projects/<p>               settings, quota, jobs with their last runs
    GET  /api/projects/<p>/jobs/<job>    the job's runs, with cost
    GET  /api/runs/<p>/<id>              one run: event, cost sheet and curve
    GET  /api/series/<db>?window=&cf=    machine time series (rrdtool)
    POST /api/admin/invite {"project"}   -> {"key"}, shown once
    POST /api/admin/queue {"mode", "reason"}  open, paused (accepts, starts none) or
                                         closed (refuses, starts none); a reason unless open
    POST /api/admin/pool {"action", "name", "path"}  add, default or retire a storage pool
    POST /api/admin/task {"kind", "project", ...}  queue a long admin operation (or cancel one);
                                         {"kind": "gpu", "action": "eject"|"attach"} for the eGPU
    GET  /api/info                       the machine's hardware and its state (the Machine page's Overview)
    GET  /api/config                     the configuration in force, one row per setting: value,
                                         source (default, computed, box.toml and its line), the
                                         default, who may change it, when a change takes effect
    GET  /api/hardware                   the optional hardware (box/hw): each feature present, absent or
                                         unsupported, with the fix; the dashboard hides what is absent
    GET  /api/admin/tasks[?state=&kind=&project=&offset=&limit=&log=<id>]
                                         a page of the admin tasks (20 by default), the
                                         ones not ended, and one task's log
    POST /api/admin/envelope {"key", "value"|"reset"}  a limit of the envelope, written into box.toml
    POST /api/admin/cancel {"project", "id"}  cancel a queued or running job
    POST /api/admin/power {"profile": "fast"|"balanced"|"quiet"}  the CPU's power profile
    POST /api/admin/shutdown {"action": "reboot"|"poweroff"|"cancel"|"now"}  a graceful restart or power-off (now: skip its countdown)
    POST /api/admin/timing {"cooldown", "target", "cap_pct"}  the timing jobs' profile
    POST /api/admin/revoke {"project"}

    api.py [--port 8080]
"""

import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcuconf  # noqa: E402  the box's configuration: defaults, then box.toml
from hw import nvidia  # noqa: E402  the NVIDIA GPU, through nvidia-smi

ETC = Path("/etc/mcu")
STATE = Path("/var/lib/mcu")
UI = Path(__file__).resolve().parent / "ui"
ADMIN = ["sudo", "-n", "/usr/local/lib/mcu/mcu-admin"]
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,20}\Z")
JOB_RE = re.compile(r"^[a-z][a-z0-9 -]{0,60}\Z")
ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}\Z")
SERIES_RE = re.compile(r"^(machine|cpu|gpu|net|power|wear|disk-[a-z][a-z0-9-]{1,20})\Z")
WINDOWS = {"1h": "-1h", "6h": "-6h", "1d": "-1d", "1w": "-1week", "1m": "-1month", "1y": "-1year"}
SESSION_SECONDS = 8 * 3600          # idle time after which a session ends
SESSION_MAX = 7 * 86400             # and its age, whatever its use
# sessions survive a restart of the dashboard (each deploy restarts it); the
# file holds token hashes, never a token, in mcu-web's own directory
SESSIONS_FILE = Path("/var/lib/mcu-web/sessions.json")

SAMPLE_SECONDS = 2
SAMPLE_KEEP = 450        # 15 minutes of samples

sessions = {}            # sha256(token) -> [expiry, csrf, created]
samples = []             # (time, cpu %, memory %, disk read, disk write, net rx, net tx
                         # in bytes/s, gpu %, vram used, vram total, cpu °C, gpu °C, cpu W):
                         # the live end of the machine curves; the gpu fields are None
                         # without a GPU, the watts without RAPL
failures = {}            # client ip -> (count, last time)
recent_failures = []     # the times of failed logins from anywhere, the last 5 minutes
lock = threading.Lock()


def read_json(path, default):
    try:
        return json.loads(path.read_text()) if path.stat().st_size else default
    except (OSError, ValueError):
        return default


def parse_cpus(spec):
    cpus = set()
    for part in str(spec).split(","):
        if "-" in part:
            a, b = part.split("-")
            cpus.update(range(int(a), int(b) + 1))
        elif part.strip():
            cpus.add(int(part))
    return cpus


def parse_size(v):
    """'2g', '512m', '1.5t' -> bytes"""
    v = str(v).strip().lower()
    return int(float(v[:-1]) * {"k": 2**10, "m": 2**20, "g": 2**30, "t": 2**40}[v[-1]]) if v[-1:] in "kmgt" else int(v)


def box():
    """the effective configuration (mcuconf: the defaults, then box.toml);
    `envelope_set` lists the envelope keys box.toml sets, `envelope_defaults`
    what each would be without its line"""
    conf, origins = mcuconf.load(with_origins=True)
    conf["origins"] = origins
    conf["envelope_set"] = sorted(k[len("envelope."):] for k, o in origins.items()
                                  if k.startswith("envelope.") and o == "box.toml")
    conf["envelope_defaults"] = {k[len("envelope."):]: v for k, (v, _) in
                                 mcuconf.defaults(conf.get("resources")).items() if k.startswith("envelope.")}
    # the memory all jobs share: the machine's minus the system's reserve,
    # unless box.toml sets it
    try:
        with open("/proc/meminfo") as f:
            total = int(next(line for line in f if line.startswith("MemTotal:")).split()[1]) * 1024
        rest = f"{(total - parse_size(conf.get('reserved_memory', '2g'))) / 2**30:.1f}g"
        conf["envelope"].setdefault("jobs_memory", rest)
        conf["envelope_defaults"]["jobs_memory"] = rest
    except (OSError, StopIteration, ValueError):
        pass
    return conf


def events_from(offset=0, limit=None):
    """(line number, event) pairs from events.jsonl, after line `offset`"""
    out = []
    try:
        with open(STATE / "events.jsonl") as f:
            for n, line in enumerate(f, 1):
                if n <= offset:
                    continue
                try:
                    out.append((n, json.loads(line)))
                except ValueError:
                    pass
    except OSError:
        pass
    return out[-limit:] if limit else out


def projects():
    usage = read_json(STATE / "usage.json", {})
    result = {}
    for path in sorted((STATE / "projects").glob("*.json")):
        p = path.stem
        conf = read_json(path, {})
        key = STATE / "keys" / f"{p}.fingerprint"
        result[p] = {
            "repo": conf.get("repo", ""),
            "benchfile": conf.get("benchfile"),
            "benchfiles": conf.get("benchfiles", {}),
            # each job's about: the default Benchfile's, then the branch ones'
            "abouts": {**{j: a for jobs in conf.get("label_jobs", {}).values() for j, a in jobs.items()},
                       **conf.get("jobs", {})},
            "disk": conf.get("disk", ""),
            "admin_disk": conf.get("admin_disk"),
            "disk_hdd": conf.get("disk_hdd", ""),
            "admin_disk_hdd": conf.get("admin_disk_hdd"),
            # where the quota in force comes from, per pool type: the admin
            # (sticky), a Benchfile, the project's config disk, or the default
            "quota_source": {m: ("admin" if conf.get(f"admin_disk{k}") else
                                 conf.get("disk_by_benchfile", {}).get(m) or
                                 ("config disk" if conf.get(f"disk{k}") else "default"))
                             for m, k in (("ssd", ""), ("hdd", "_hdd"))},
            "media": usage.get(p, {}).get("media"),
            "used": usage.get(p, {}).get("used"),
            "quota": usage.get(p, {}).get("quota"),
            "parts": usage.get(p, {}).get("parts"),
            "tree": usage.get(p, {}).get("tree"),
            "pool": usage.get(p, {}).get("pool"),
            "units": usage.get(p, {}).get("units"),
            "key": key.exists(),
        }
    return result


def volumes():
    result = {}
    for path in sorted((STATE / "volumes").glob("*.json")):
        v = read_json(path, {})
        result[path.stem] = {"owner": v.get("owner"), "readers": v.get("readers", [])}
    for _, ev in events_from():
        if ev.get("kind") == "provisioned" and ev.get("volume") in result:
            result[ev["volume"]].update(version=ev["version"], bytes=ev["bytes"], at=ev["t"])
    return result


def resources():
    """what the box can do now: the collector's view, the maintenance flag
    read directly (it changes outside the collector's minute), and a
    one-word status"""
    res = read_json(STATE / "resources.json", {})
    q = read_json(STATE / "queue.json", {})
    res["queue"] = q.get("mode") if q.get("mode") in ("paused", "closed") else "open"
    shut = res["queue"] == "open"
    res["maintenance"] = q.get("reason") if res["queue"] == "closed" else None
    res["hold"] = None if shut else q.get("reason")
    res["until"] = None if shut else q.get("until")
    res["queue_by"] = None if shut else q.get("by")
    problems = []
    if time.time() - res.get("updated", 0) > 180:
        problems.append("box monitor stale")
    if res.get("gpu") is False:
        problems.append(f"gpu {res.get('gpu_state') or 'absent'}" + (" by the admin" if res.get("gpu_state") in ("ejecting", "ejected", "attaching")
                                                                       else f" ({res['gpu_note']})" if res.get("gpu_note") else ""))
    res["problems"] = problems
    res["status"] = "maintenance" if res["maintenance"] else ("degraded" if problems else "ok")
    return res


_topology = {"t": 0, "data": None}


def topology():
    """mcu.py's view of the declared resources (as `box topology`),
    cached a minute: it asks lspci and nvidia-smi"""
    if time.time() - _topology["t"] > 60 or _topology["data"] is None:
        r = subprocess.run(["/usr/bin/python3", str(Path(__file__).with_name("mcu.py")), "topology"],
                           capture_output=True, text=True, timeout=30)
        try:
            _topology["data"] = json.loads(r.stdout)
        except ValueError:
            _topology["data"] = {"error": (r.stderr or "no output").strip()[-500:]}
        _topology["t"] = time.time()
    return _topology["data"]


_info = {"t": 0, "data": None}


def _read(path, default=None):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def _run(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def power_info():
    """the CPU's power profile: the chosen one, the watts of each (box.toml
    [power], else from the CPU's own limit as first found), the limit now"""
    lim = Path("/sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw")
    if not lim.exists():
        return None
    st = read_json(STATE / "power.json", {})
    base = st.get("firmware_w") or round(int(_read(lim, "0") or 0) / 1e6)
    try:
        conf = tomllib.loads((ETC / "box.toml").read_text()).get("power", {})
    except OSError:
        conf = {}
    profiles = {"fast": float(conf.get("fast", base)), "balanced": float(conf.get("balanced", round(base * 2 / 3))),
                "quiet": float(conf.get("quiet", round(base / 2)))}
    return {"profile": st.get("profile", "fast"), "profiles": profiles,
            "limit_w": int(_read(lim, "0") or 0) / 1e6}


def last_power():
    """the power graph's last minute, in watts: {total, platform, package,
    core, dram, gpu}, the absent ones None; {} without power.rrd"""
    out = subprocess.run(["rrdtool", "lastupdate", str(STATE / "rrd" / "power.rrd")],
                         capture_output=True, text=True).stdout.splitlines()
    if len(out) < 3:
        return {}
    num = lambda v: None if v == "U" else float(v)
    return dict(zip(out[0].split(), map(num, out[-1].split(":", 1)[1].split())))


def info():
    """the unit's hardware and its state, like a system information page:
    the system, CPU, memory, disks, graphics (with the eGPU), Thunderbolt,
    network and sensors; each device with a status (ok, warn, off) and a
    note. Read-only, from /proc, /sys, lspci, lsblk, ip and nvidia-smi;
    cached 5 s"""
    # cached 5 s, but never across a change of the box's state (the eGPU's)
    stamp = (STATE / "resources.json").stat().st_mtime if (STATE / "resources.json").exists() else 0
    if time.time() - _info["t"] < 5 and _info["data"] is not None and _info.get("stamp") == stamp:
        return _info["data"]
    dmi = Path("/sys/class/dmi/id")
    osr = dict(l.split("=", 1) for l in (_read("/etc/os-release", "") or "").splitlines() if "=" in l)
    uptime = float((_read("/proc/uptime", "0") or "0").split()[0])
    system = {"host": os.uname().nodename, "product": " ".join(filter(None, [_read(dmi / "sys_vendor"), _read(dmi / "product_name")])),
              "board": _read(dmi / "board_name"), "bios": f"{_read(dmi / 'bios_version', '?')} ({_read(dmi / 'bios_date', '?')})",
              "os": osr.get("PRETTY_NAME", "").strip('"'), "kernel": os.uname().release, "uptime": uptime}

    # CPU: model, threads, governor, the timing cap, temperature, throttling
    cpuinfo = _read("/proc/cpuinfo", "") or ""
    model = next((l.split(":", 1)[1].strip() for l in cpuinfo.splitlines() if l.startswith("model name")), "?")
    cpus = sorted(Path("/sys/devices/system/cpu").glob("cpu[0-9]*"))
    mhz = [int(_read(c / "cpufreq/scaling_cur_freq", "0") or 0) / 1000 for c in cpus]
    throttle = sum(int(_read(c / "thermal_throttle/package_throttle_count", "0") or 0) for c in cpus[:1])
    temps = []
    for hw in Path("/sys/class/hwmon").glob("hwmon*"):
        name = _read(hw / "name", "?")
        for t in sorted(hw.glob("temp*_input")):
            v = _read(t)
            # below -50 °C: a zone the firmware leaves unset (the NUC's ACPI
            # thermal zone without trip points reads -263 °C)
            if v and v.lstrip("-").isdigit() and int(v) > -50000:
                temps.append({"sensor": name, "label": _read(str(t).replace("_input", "_label"), t.name.split("_")[0]),
                              "c": int(v) / 1000})
    core = [t["c"] for t in temps if t["sensor"] in ("coretemp", "k10temp")]
    hot = max(core) if core else None
    cpu = {"model": model, "threads": len(cpus), "governor": _read(cpus[0] / "cpufreq/scaling_governor") if cpus else None,
           "driver": _read(cpus[0] / "cpufreq/scaling_driver") if cpus else None,
           "max_perf_pct": _read("/sys/devices/system/cpu/intel_pstate/max_perf_pct"),
           "no_turbo": _read("/sys/devices/system/cpu/intel_pstate/no_turbo"),
           "mhz": round(sum(mhz) / len(mhz)) if mhz else None, "temp": hot, "throttle_events": throttle,
           "status": "warn" if hot and hot >= 95 else "ok",
           "note": f"{hot:.0f} °C, at its limit" if hot and hot >= 95 else None,
           "power": dict(last_power(), limit=power_info())}

    mem = {}
    for line in (_read("/proc/meminfo", "") or "").splitlines():
        k, v = line.split(":", 1)
        mem[k] = int(v.split()[0]) * 1024
    memory = {"total": mem.get("MemTotal"), "available": mem.get("MemAvailable"),
              "swap_total": mem.get("SwapTotal"), "swap_free": mem.get("SwapFree")}
    try:
        memory["jobs"] = min(parse_size(box()["envelope"]["jobs_memory"]), mem["MemTotal"] - 2**30)
    except (KeyError, TypeError, ValueError):
        pass

    disks = []
    try:
        blk = json.loads(_run(["lsblk", "-J", "-b", "-o", "NAME,TYPE,SIZE,MODEL,FSTYPE,MOUNTPOINTS"]) or "{}")
        for d in blk.get("blockdevices", []):
            if d.get("type") != "disk" or d["name"].startswith(("zram", "loop")):
                continue
            mounts = []

            def walk(n):
                mounts.extend(m for m in (n.get("mountpoints") or []) if m)
                for c in n.get("children") or []:
                    walk(c)
            walk(d)
            # health from the root timer's smartctl (usage.json): wear, sectors
            h = next((x for x in read_json(STATE / "usage.json", {}).get("_disks", {}).get("smart", [])
                      if x.get("device") == f"/dev/{d['name']}"), {})
            bad = h.get("ok") is False or (h.get("reallocated") or 0) > 0 or (h.get("pending") or 0) > 0 \
                or (h.get("wear_pct") or 0) >= 80
            disks.append({"name": d["name"], "model": (d.get("model") or "").strip(), "size": d.get("size"),
                          "mounts": mounts, "media": h.get("media"), "wear_pct": h.get("wear_pct"),
                          "written": h.get("written"), "reallocated": h.get("reallocated"), "hours": h.get("hours"),
                          "temp": h.get("temp"), "status": "warn" if bad else "ok"})
    except ValueError:
        pass

    # graphics: every display controller on the PCI bus, its driver and link;
    # the NVIDIA eGPU's state from resources.json (what jobs see)
    res = read_json(STATE / "resources.json", {})
    graphics = []
    for line in _run(["lspci", "-D", "-nn"]).splitlines():
        if not re.search(r"\[03[0-9a-f]{2}\]", line):
            continue
        addr = line.split()[0]
        dev = Path("/sys/bus/pci/devices") / addr
        drv = Path(os.path.realpath(dev / "driver")).name if (dev / "driver").exists() else None
        speed, width = _read(dev / "current_link_speed"), _read(dev / "current_link_width")
        g = {"address": addr, "name": line.split(": ", 1)[1].rsplit(" (rev", 1)[0] if ": " in line else line,
             "driver": drv, "link": f"{speed} x{width}" if speed and width not in (None, "0") and "Unknown" not in speed else None,
             "egpu": "[10de:" in line, "status": "ok" if drv else "warn", "note": None if drv else "no driver bound"}
        graphics.append(g)
    nv = _run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.used,temperature.gpu,power.draw,pstate,"
               "pcie.link.gen.current,pcie.link.width.current", "--format=csv,noheader,nounits"])
    egpu = next((g for g in graphics if g["egpu"]), None)
    state = res.get("gpu_state") or ("present" if res.get("gpu") else "absent")
    if egpu is None and state != "present":
        egpu = {"address": None, "name": res.get("gpu_name") or "NVIDIA eGPU", "driver": None, "link": None,
                "egpu": True}
        graphics.append(egpu)
    if egpu is not None:
        f = [x.strip() for x in nv.splitlines()[0].split(",")] if nv.strip() else []
        if len(f) == 9:
            egpu.update(driver_version=f[1], vram_total=float(f[2]) * 2**20, vram_used=float(f[3]) * 2**20,
                        temp=float(f[4]), power=None if f[5] in ("[N/A]", "N/A") else float(f[5]), pstate=f[6],
                        link=f"Gen{f[7]} x{f[8]}")
        # its Thunderbolt enclosure still connected (a device other than the host's own)
        enclosure = any(re.fullmatch(r"\d+-[1-9]\d*", d.name) for d in Path("/sys/bus/thunderbolt/devices").glob("*-*"))
        egpu.update(state=state, since=res.get("gpu_since"), enclosure=enclosure,
                    status={"present": "ok", "ejecting": "warn", "attaching": "warn", "lost": "warn"}.get(state, "off"),
                    note={"present": "GPU jobs run", "absent": "unplugged: GPU jobs get exit 75; plugging it back brings it up"
                          if not enclosure else "not responding: GPU jobs get exit 75",
                          "ejecting": "being ejected: new GPU jobs get exit 75, running ones finish",
                          "attaching": "being re-attached: the bus is rescanned, the driver loads",
                          "ejected": "ejected by the admin and unplugged: plugging the enclosure back brings it up by itself"
                                     if not enclosure else "ejected by the admin: safe to unplug, or re-attach to use it again",
                          "lost": f"{res.get('gpu_note') or 'unplugged without an eject'}; plugging it back "
                                  "should bring it up, or the box says a reboot is needed"}.get(state))

    thunderbolt = []
    for d in sorted(Path("/sys/bus/thunderbolt/devices").glob("*-*")):
        if not re.fullmatch(r"\d+-\d+", d.name):
            continue
        auth = _read(d / "authorized")
        host = d.name.endswith("-0")
        speed = _read(d / "rx_speed")
        thunderbolt.append({"name": f"{_read(d / 'vendor_name', '')} {_read(d / 'device_name', '')}".strip(),
                            "id": d.name, "host": host, "authorized": auth == "1",
                            "speed": f"{speed} x{_read(d / 'rx_lanes', '?')}" if speed else None,
                            "status": "ok" if auth == "1" else "off",
                            "note": None if auth == "1" else "not authorized"})

    network = []
    try:
        for n in json.loads(_run(["ip", "-j", "addr"]) or "[]"):
            if n["ifname"] == "lo" or n["ifname"].startswith(("veth", "podman", "cni", "virbr")):
                continue
            up = n.get("operstate") == "UP"
            network.append({"name": n["ifname"], "state": n.get("operstate"),
                            "speed": _read(f"/sys/class/net/{n['ifname']}/speed") if up else None,
                            "addresses": [a["local"] for a in n.get("addr_info", []) if a.get("family") == "inet"],
                            "status": "ok" if up else "off"})
    except ValueError:
        pass

    _info["data"] = {"system": system, "cpu": cpu, "memory": memory, "disks": disks, "graphics": graphics,
                     "thunderbolt": thunderbolt, "network": network, "sensors": temps, "t": time.time()}
    _info["t"], _info["stamp"] = time.time(), stamp
    return _info["data"]


_health = {"t": 0, "data": None}


def health():
    """mcu.py's health view without a project's disk, cached 10 s"""
    if time.time() - _health["t"] > 10 or _health["data"] is None:
        r = subprocess.run(["/usr/bin/python3", str(Path(__file__).with_name("mcu.py")), "health"],
                           capture_output=True, text=True, timeout=30)
        try:
            _health["data"] = json.loads(r.stdout)
        except ValueError:
            _health["data"] = {"error": (r.stderr or "no output").strip()[-500:]}
        _health["t"] = time.time()
    return _health["data"]


def state():
    conf = box()
    sched = read_json(STATE / "state.json", {})
    meminfo = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            meminfo[k] = int(v.split()[0]) * 1024
    disk = shutil.disk_usage("/")
    try:
        governor = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read_text().strip()
    except OSError:
        governor = "?"
    return {
        "now": time.time(),
        "version": mcuconf.version(),
        "envelope_bounds": mcuconf.bounds(),
        "host": os.uname().nodename,
        "threads": os.cpu_count(),
        "reserved": sorted(parse_cpus(conf.get("reserved", "0"))),
        "exclusive": ["machine", "gpu", "disk"],
        # the box's hardware resources, for the lanes: name -> share, about
        "declared": {n: {"share": r.get("share"), "about": r.get("about"), "present": r.get("present"),
                         # the hot-plug NVIDIA GPU: its lane greys out while it is away
                         "hotplug": ((conf.get("resources") or {}).get(n) or {"probe": "nvidia" if n == "gpu" else None})
                         .get("probe") == "nvidia"}
                     for n, r in (topology().get("resources") or {}).items()},
        "running": sched.get("running", []),
        "queue": sched.get("queue", []),
        "resources": resources(),
        "envelope": conf.get("envelope", {}),
        "power": power_info(),
        # a restart or power-off in progress (mcu-admin shutdown), or None
        "shutdown": read_json(STATE / "shutdown.json", None),
        "envelope_defaults": conf.get("envelope_defaults", {}),
        "envelope_set": conf.get("envelope_set", []),
        # the data filesystem holding the projects' trees: size, free, quotas promised
        # the storage pools: path, device, size, free, quotas promised, projects
        "pools": read_json(STATE / "usage.json", {}).get("_pools", {}),
        # what the machine really has (filesystems, LVM free space, blank disks)
        "disks": read_json(STATE / "usage.json", {}).get("_disks", {}),
        # the timing jobs' profile (mcu-admin timing), and the measured idle
        # temperature beside it
        "timing": dict(read_json(STATE / "timing.json", {}),
                       idle_temp=read_json(STATE / "idle.json", {}).get("idle_temp")),
        "projects": projects(),
        # the recent runs, for the timeline's history (the SSE stream adds to it)
        "recent": [e for _, e in events_from(limit=1500) if e.get("kind") in ("queued", "started", "done", "refused")],
        "machine": {
            "load": os.getloadavg(),
            "mem_total": meminfo.get("MemTotal", 0),
            "mem_available": meminfo.get("MemAvailable", 0),
            "reserved_memory": conf.get("reserved_memory", "2g"),
            "disk_total": disk.total,
            "disk_free": disk.free,
            "governor": governor,
        },
    }


def runs(project, job=None, limit=200):
    """finished runs (done or refused events), newest last, with cost"""
    result = []
    for _, e in events_from():
        if e.get("project") != project or e.get("kind") not in ("done", "refused"):
            continue
        if job is not None and e.get("job") != job:
            continue
        result.append(e)
    return result[-limit:]


def project_detail(p):
    info = projects().get(p)
    if info is None:
        return None
    learned = {k.split("/", 1)[1]: v for k, v in read_json(STATE / "memory.json", {}).items()
               if k.startswith(p + "/") and "#" not in k}
    jobs = {j: [] for j in info["abouts"]}   # the declared jobs, run or not
    for e in runs(p, limit=2000):
        jobs.setdefault(e["job"], []).append(e)
    return {
        "project": p, **info,
        "volumes": {n: v for n, v in volumes().items() if v["owner"] == p or p in v["readers"]},
        "jobs": {j: {"runs": len(r), "ok": sum(1 for e in r if e.get("status") == 0),
                     "last": r[-20:], "memory_learned": learned.get(j), "about": info["abouts"].get(j)}
                 for j, r in sorted(jobs.items())},
    }


def run_detail(p, run_id):
    event = next((e for e in reversed(runs(p, limit=5000)) if e.get("id") == run_id), None)
    cost = read_json(STATE / "runs" / p / f"{run_id}.json", {})
    if not event and not cost:
        return None
    return {"event": event, "cost": cost}


def series(name, window, cf="AVERAGE"):
    if not SERIES_RE.match(name) or window not in WINDOWS or cf not in ("AVERAGE", "MAX"):
        return None
    path = STATE / "rrd" / f"{name}.rrd"
    if not path.exists():
        return None
    info = subprocess.run(["rrdtool", "info", str(path)], capture_output=True, text=True).stdout
    names = sorted(set(re.findall(r"^ds\[([a-z0-9_]+)\]\.type", info, re.M)),
                   key=lambda n: info.index(f"ds[{n}]"))
    cmd = ["rrdtool", "xport", "--json", "--start", WINDOWS[window], "--end", "now", "--maxrows", "600"]
    for i, n in enumerate(names):
        cmd += [f"DEF:v{i}={path}:{n}:{cf}", f"XPORT:v{i}:{n}"]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    out = re.sub(r"([{,]\s*)([a-z_]+)(\s*:)", r'\1"\2"\3', out)
    try:
        data = json.loads(out)
    except ValueError:
        return None
    meta = data.get("meta", {})
    t, step = meta.get("start", 0), meta.get("step", 60)
    return {"name": name, "window": window, "cf": cf, "step": step, "names": names,
            "rows": [[t + i * step] + row for i, row in enumerate(data.get("data", []))]}


def io_bytes():
    """cumulative bytes read and written by the physical disks, received and
    sent by the physical interfaces (as collect.py counts them)"""
    rd = wr = rx = tx = 0
    with open("/proc/diskstats") as f:
        for line in f:
            p = line.split()
            if os.path.exists(f"/sys/block/{p[2]}/device"):
                rd, wr = rd + int(p[5]) * 512, wr + int(p[9]) * 512
    with open("/proc/net/dev") as f:
        for line in f.readlines()[2:]:
            name, data = line.split(":", 1)
            if os.path.exists(f"/sys/class/net/{name.strip()}/device"):
                v = data.split()
                rx, tx = rx + int(v[0]), tx + int(v[8])
    return rd, wr, rx, tx


def gpu_sample():
    """NVIDIA GPU utilization %, VRAM used and total in bytes, temperature, or Nones"""
    row = nvidia.query(["utilization.gpu", "memory.used", "memory.total", "temperature.gpu"], timeout=3, units=True)
    try:
        u, used, total, temp = (float(x) for x in row)
    except (TypeError, ValueError):
        return None, None, None, None
    return u, int(used * 2**20), int(total * 2**20), temp


def cpu_temp():
    """the hottest CPU sensor, °C"""
    for hw in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            if (hw / "name").read_text().strip() in ("coretemp", "k10temp", "x86_pkg_temp"):
                temps = [int(t.read_text()) / 1000 for t in hw.glob("temp*_input")]
                return max(temps) if temps else None
        except (OSError, ValueError):
            pass
    return None


def sampler():
    """the machine's CPU, memory, disk and network every 2 s: the rrd series
    have one point a minute, too coarse for the minutes next to "now" """
    def busy():
        with open("/proc/stat") as f:
            v = list(map(int, f.readline().split()[1:]))
        return sum(v) - v[3] - v[4], sum(v)  # minus idle and iowait
    def energy():
        try:
            z = Path("/sys/class/powercap/intel-rapl:0")
            return int((z / "energy_uj").read_text()), int((z / "max_energy_range_uj").read_text())
        except (OSError, ValueError):
            return None
    last, last_io, last_t = busy(), io_bytes(), time.time()
    last_e = energy()
    gpu, n = gpu_sample(), 0
    while True:
        time.sleep(SAMPLE_SECONDS)
        try:
            cur, cur_io, t = busy(), io_bytes(), time.time()
            # nvidia-smi costs more than /proc: every 10 s, and not at all
            # while the GPU is absent (the collector notices when it returns)
            n += 1
            if n % 5 == 0:
                present = read_json(STATE / "resources.json", {}).get("gpu") is not False
                gpu = gpu_sample() if present else (None, None, None, None)
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    info[k] = int(v.split()[0])
            cpu = 100 * (cur[0] - last[0]) / max(cur[1] - last[1], 1)
            mem = 100 * (info["MemTotal"] - info["MemAvailable"]) / info["MemTotal"]
            rates = [max(round((c - l) / max(t - last_t, 0.1)), 0) for c, l in zip(cur_io, last_io)]
            # the CPU package's watts over the 2 s (the counter wraps)
            e, watts = energy(), None
            if e and last_e:
                d = e[0] - last_e[0]
                watts = round((d + e[1] if d < 0 else d) / max(t - last_t, 0.1) / 1e6, 1)
            last, last_io, last_t, last_e = cur, cur_io, t, e
            with lock:
                samples.append((round(t, 1), round(cpu, 1), round(mem, 1), *rates, *gpu[:3], cpu_temp(), gpu[3], watts))
                del samples[:-SAMPLE_KEEP]
        except (OSError, ValueError, KeyError):
            pass


def token_key(token):
    return hashlib.sha256(token.encode()).hexdigest()


def load_sessions():
    try:
        data = json.loads(SESSIONS_FILE.read_text())
    except (OSError, ValueError):
        return
    now = time.time()
    sessions.update({k: v for k, v in data.items() if v[0] > now and now - v[2] < SESSION_MAX})


def save_sessions():
    """under `lock`"""
    try:
        tmp = SESSIONS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(sessions))
        os.chmod(tmp, 0o600)
        os.replace(tmp, SESSIONS_FILE)
    except OSError:
        pass  # sessions then live in memory only, as before


def check_password(password):
    try:
        conf = json.loads((ETC / "admin.json").read_text())
    except (OSError, ValueError):
        return False
    h = hashlib.scrypt(password.encode(), salt=bytes.fromhex(conf["salt"]), n=2**15, r=8, p=1,
                       maxmem=2**26)
    return hmac.compare_digest(h.hex(), conf["scrypt"])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self):
        # a browser closing a kept-alive connection (a tab, a reload) breaks
        # the read of the next request: nothing to report
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass

    def send(self, code, body, ctype="application/json", headers=()):
        data = body if isinstance(body, bytes) else (
            body.encode() if isinstance(body, str) else json.dumps(body).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        # the page loads only its own files and talks only to this API
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def session(self):
        m = re.search(r"mcu_session=([A-Za-z0-9_-]+)", self.headers.get("Cookie", ""))
        if not m:
            return None
        key, now = token_key(m.group(1)), time.time()
        with lock:
            entry = sessions.get(key)
            if not entry or entry[0] < now or now - entry[2] > SESSION_MAX:
                if sessions.pop(key, None):
                    save_sessions()
                return None
            # sliding: in use, it stays valid; written at most every 5 minutes
            if entry[0] - now < SESSION_SECONDS - 300:
                entry[0] = now + SESSION_SECONDS
                save_sessions()
        return key, entry[1]

    def body(self):
        length = min(int(self.headers.get("Content-Length", 0) or 0), 8192)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    # ------------------------------------------------------------ GET

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if not path.startswith("/api/"):
            return self.static(path)
        if path == "/api/session":
            s = self.session()
            return self.send(200, {"logged_in": bool(s), "csrf": s[1] if s else None})
        if not self.session():
            return self.send(401, {"error": "log in"})
        q = parse_qs(query)
        if path == "/api/state":
            return self.send(200, state())
        if path == "/api/admin/tasks":
            # the admin task queue, newest first: one page of it (filters
            # state, kind, project; offset, limit), every task not ended
            # yet whatever the filters, and the log of one task
            q = parse_qs(urlparse(self.path).query) if "?" in self.path else {}
            arg = lambda k: (q.get(k) or [""])[0]
            every = [read_json(f, {}) for f in sorted((STATE / "tasks").glob("*.json"), reverse=True)]
            every = [t for t in every if t.get("id")]
            groups = {"active": ("queued", "waiting", "running"), "done": ("done",),
                      "failed": ("failed", "interrupted"), "cancelled": ("cancelled",)}
            picked = [t for t in every
                      if (not arg("state") or t.get("state") in groups.get(arg("state"), (arg("state"),)))
                      and (not arg("kind") or t.get("kind") == arg("kind"))
                      and (not arg("project") or (t.get("args") or [None])[0] == arg("project"))]
            try:
                offset = max(int(arg("offset") or 0), 0)
                limit = min(max(int(arg("limit") or 20), 1), 200)
            except ValueError:
                return self.send(400, {"error": "offset and limit are numbers"})
            projects = sorted({t["args"][0] for t in every if t.get("args") and t.get("kind") != "prune"})
            log = None
            tid = (q.get("log") or [""])[0]
            if re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", tid):
                try:
                    log = (STATE / "tasks" / f"{tid}.log").read_text()[-20000:]
                except OSError:
                    log = ""
            return self.send(200, {"tasks": picked[offset:offset + limit], "total": len(picked),
                                   "offset": offset, "limit": limit, "projects": projects,
                                   "active": [t for t in every if t.get("state") in groups["active"]],
                                   "log": log})
        if path == "/api/info":
            return self.send(200, info())
        if path == "/api/config":
            return self.send(200, {"version": mcuconf.version(), "file": str(mcuconf.ETC / "box.toml"),
                                   "settings": mcuconf.effective()})
        if path == "/api/hardware":
            try:
                return self.send(200, json.loads((STATE / "hardware.json").read_text()))
            except (OSError, ValueError):
                return self.send(200, {"updated": None, "features": []})   # not measured yet: hide nothing
        if path == "/api/topology":
            return self.send(200, topology())
        if path == "/api/health":
            return self.send(200, health())
        if path == "/api/events":
            return self.stream()
        if path == "/api/volumes":
            return self.send(200, volumes())
        m = re.fullmatch(r"/api/projects/([a-z][a-z0-9-]{1,20})", path)
        if m:
            d = project_detail(m.group(1))
            return self.send(200, d) if d else self.send(404, {"error": "no such project"})
        m = re.fullmatch(r"/api/projects/([a-z][a-z0-9-]{1,20})/jobs/([a-z][a-z0-9-]{0,40})", path)
        if m:
            return self.send(200, {"project": m.group(1), "job": m.group(2), "runs": runs(*m.groups())})
        m = re.fullmatch(r"/api/runs/([a-z][a-z0-9-]{1,20})/([0-9]{8}-[0-9]{6}-[0-9a-f]{4})", path)
        if m:
            d = run_detail(*m.groups())
            return self.send(200, d) if d else self.send(404, {"error": "no such run"})
        m = re.fullmatch(r"/api/series/([a-z0-9-]+)", path)
        if m:
            d = series(m.group(1), q.get("window", ["1d"])[0], q.get("cf", ["AVERAGE"])[0])
            return self.send(200, d) if d else self.send(404, {"error": "no such series or window"})
        self.send(404, {"error": "not found"})

    def static(self, path):
        """the built UI; unknown paths get index.html (client-side routes)"""
        rel = path.lstrip("/") or "index.html"
        target = (UI / rel).resolve()
        if not str(target).startswith(str(UI.resolve())) or not target.is_file():
            target = UI / "index.html"
        if not target.is_file():
            return self.send(503, "the UI is not installed", "text/plain")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send(200, target.read_bytes(), ctype)

    def stream(self):
        """Server-Sent Events: the events log line by line (the line number is
        the event id, so Last-Event-ID resumes), plus `state` whenever the
        scheduler's state changes"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            last = int(self.headers.get("Last-Event-ID") or 0)
        except ValueError:
            last = 0
        if not last:
            # a fresh client starts from the end: it fetched /api/state already
            last = len(events_from())
        state_mtime = 0
        beat = time.time()
        # the machine samples: the last 15 minutes at once, then each new one
        with lock:
            backlog = list(samples)
        sent = backlog[-1][0] if backlog else 0
        try:
            self.wfile.write(f"event: samples\ndata: {json.dumps(backlog)}\n\n".encode())
            while True:
                with lock:
                    fresh = [x for x in samples if x[0] > sent]
                if fresh:
                    self.wfile.write(f"event: samples\ndata: {json.dumps(fresh)}\n\n".encode())
                    sent = fresh[-1][0]
                for n, ev in events_from(last):
                    self.wfile.write(f"id: {n}\nevent: {ev.get('kind', 'event')}\ndata: {json.dumps(ev)}\n\n".encode())
                    last = n
                try:
                    m = (STATE / "state.json").stat().st_mtime
                except OSError:
                    m = 0
                if m != state_mtime:
                    state_mtime = m
                    s = read_json(STATE / "state.json", {})
                    self.wfile.write(f"event: state\ndata: {json.dumps({'running': s.get('running', []), 'queue': s.get('queue', []), 'now': time.time()})}\n\n".encode())
                if time.time() - beat > 15:
                    self.wfile.write(b": keep-alive\n\n")
                    beat = time.time()
                self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    # ------------------------------------------------------------ POST

    def do_POST(self):
        path = self.path.partition("?")[0]
        data = self.body()
        if path == "/api/login":
            ip = self.client_address[0]
            count, last = failures.get(ip, (0, 0))
            if count >= 5 and time.time() - last < 300:
                return self.send(429, {"error": "too many failures; wait 5 minutes"})
            # and for everyone at once: guessing from many addresses is
            # slowed down the same way (the admin waits 5 minutes too)
            recent = [t for t in recent_failures if time.time() - t < 300]
            recent_failures[:] = recent
            if len(recent) >= 20:
                return self.send(429, {"error": "too many failed logins; wait 5 minutes"})
            if check_password(str(data.get("password", ""))):
                failures.pop(ip, None)
                token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
                now = time.time()
                with lock:
                    for k in [k for k, v in sessions.items() if v[0] < now or now - v[2] > SESSION_MAX]:
                        del sessions[k]
                    sessions[token_key(token)] = [now + SESSION_SECONDS, csrf, now]
                    save_sessions()
                return self.send(200, {"logged_in": True, "csrf": csrf}, headers=[(
                    "Set-Cookie", f"mcu_session={token}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={SESSION_MAX}")])
            failures[ip] = (count + 1, time.time())
            recent_failures.append(time.time())
            print(f"dashboard: failed login from {ip} ({count + 1} in a row)", file=sys.stderr, flush=True)
            time.sleep(1)
            return self.send(401, {"error": "wrong password"})
        s = self.session()
        if not s:
            return self.send(401, {"error": "log in"})
        if not hmac.compare_digest(self.headers.get("X-Mcu-CSRF", ""), s[1]):
            return self.send(403, {"error": "missing or stale CSRF token"})
        if path == "/api/logout":
            with lock:
                sessions.pop(s[0], None)
                save_sessions()
            return self.send(200, {"logged_in": False}, headers=[(
                "Set-Cookie", "mcu_session=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Strict")])
        if path in ("/api/admin/invite", "/api/admin/revoke"):
            project = str(data.get("project", ""))
            if not NAME_RE.match(project) or project == "web":
                return self.send(400, {"error": "bad project name"})
            action = path.rsplit("/", 1)[1]
            r = subprocess.run(ADMIN + [action, project], capture_output=True, text=True)
            if r.returncode != 0:
                return self.send(500, {"error": r.stderr.strip()})
            if action == "invite":
                return self.send(200, {"project": project, "key": r.stdout,
                                       "host": os.uname().nodename, "note": r.stderr.strip()})
            return self.send(200, {"project": project, "note": r.stdout.strip()})
        if path == "/api/admin/queue":
            reason = str(data.get("reason", "")).strip()
            mode = {"open": "open", "paused": "pause", "closed": "close"}.get(data.get("mode"))
            if not mode:
                return self.send(400, {"error": "mode: open, paused or closed"})
            if mode != "open" and not reason:
                return self.send(400, {"error": "give a reason, shown to the agents"})
            until = str(data.get("until", "")).strip()
            if until and not re.fullmatch(r"\+\d+[mh]|\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", until):
                return self.send(400, {"error": "until: 18:30, +30m, +2h or 2026-10-13T18:30"})
            args = ["queue", mode] + ([reason] if mode != "open" else []) + (["--until", until] if until and mode != "open" else []) + (["--by", "dashboard"] if mode != "open" else [])
            if len(reason) > 200:
                return self.send(400, {"error": "keep the reason under 200 characters"})
            r = subprocess.run(ADMIN + args, capture_output=True, text=True)
            return self.send(200 if r.returncode == 0 else 500, {"note": (r.stdout or r.stderr).strip()})
        if path == "/api/admin/cancel":
            # a project's job, cancelled as the project would (mcu-admin cancel)
            project, job = str(data.get("project", "")), str(data.get("id", ""))
            if not NAME_RE.match(project) or project == "web" or not ID_RE.match(job):
                return self.send(400, {"error": "project and job id"})
            r = subprocess.run(ADMIN + ["cancel", project, job], capture_output=True, text=True, timeout=60)
            return self.send(200 if r.returncode in (0, 130) else 400, {"note": (r.stdout or r.stderr).strip()})
        if path == "/api/admin/power":
            profile = str(data.get("profile", ""))
            if profile not in ("fast", "balanced", "quiet"):
                return self.send(400, {"error": "profile: fast, balanced or quiet"})
            r = subprocess.run(ADMIN + ["power", profile], capture_output=True, text=True)
            return self.send(200 if r.returncode == 0 else 400, {"note": (r.stdout or r.stderr).strip()})
        if path == "/api/admin/shutdown":
            action = str(data.get("action", ""))
            if action not in ("reboot", "poweroff", "cancel", "now"):
                return self.send(400, {"error": "action: reboot, poweroff, cancel or now"})
            r = subprocess.run(ADMIN + ["shutdown", action], capture_output=True, text=True)
            note = (r.stdout or r.stderr).strip()
            if r.returncode == 0 and action in ("reboot", "poweroff"):
                note = f"{'restart' if action == 'reboot' else 'power-off'} started (task {note})"
            return self.send(200 if r.returncode == 0 else 400, {"note": note})
        if path == "/api/admin/quota":
            project = str(data.get("project", ""))
            size = str(data.get("size", "")).strip().lower()
            media = str(data.get("media", "ssd"))
            if not NAME_RE.match(project) or project == "web" or not re.fullmatch(r"\d+g|none", size) \
                    or media not in ("ssd", "hdd"):
                return self.send(400, {"error": "project, media (ssd or hdd) and size (e.g. 40g, or none)"})
            r = subprocess.run(ADMIN + ["quota", project, media, size], capture_output=True, text=True)
            return self.send(200 if r.returncode == 0 else 400, {"note": (r.stdout or r.stderr).strip()})
        if path == "/api/admin/pool":
            action, name, where = str(data.get("action", "")), str(data.get("name", "")), str(data.get("path", ""))
            size = str(data.get("size", "")).strip().lower()
            if action == "budget":
                if not (where.startswith("/") or re.fullmatch(r"[a-z][a-z0-9-]{0,20}", name)) or not re.fullmatch(r"\d+g?", size):
                    return self.send(400, {"error": "budget: a mount or pool, and a size like 120g"})
                args = ["pool", "budget", where or name, size if size.endswith("g") else size + "g"]
            else:
                args = {"add": ["pool", "add", name, where], "default": ["pool", "default", name],
                        "retire": ["pool", "retire", name]}.get(action)
                if not args or not re.fullmatch(r"[a-z][a-z0-9-]{0,20}", name) or (action == "add" and not where.startswith("/")):
                    return self.send(400, {"error": "action budget|add|default|retire, a pool name, and a path for add"})
            r = subprocess.run(ADMIN + args, capture_output=True, text=True, timeout=60)
            return self.send(200 if r.returncode == 0 else 400, {"note": (r.stdout or r.stderr).strip()})
        if path == "/api/admin/task":
            kind = str(data.get("kind", ""))
            project = str(data.get("project", ""))
            if kind == "cancel":
                tid = str(data.get("id", ""))
                if not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", tid):
                    return self.send(400, {"error": "bad task id"})
                r = subprocess.run(ADMIN + ["task", "cancel", tid], capture_output=True, text=True)
                return self.send(200 if r.returncode == 0 else 400, {"note": (r.stdout or r.stderr).strip()})
            if kind == "prune":
                args = []
            elif kind == "gpu" and data.get("action") in ("eject", "attach"):
                args = [data["action"]]
            elif kind in ("migrate", "purge", "reclaim") and NAME_RE.match(project) and project != "web":
                args = [project]
                if kind == "migrate":
                    pool, unit = str(data.get("pool", "")), str(data.get("unit", ""))
                    if not re.fullmatch(r"[a-z][a-z0-9-]{0,20}", pool):
                        return self.send(400, {"error": "a pool"})
                    if unit:
                        if not re.fullmatch(r"storage/[A-Za-z0-9._-]{1,64}|images|results|volume/[a-z][a-z0-9-]{0,30}", unit):
                            return self.send(400, {"error": "bad unit"})
                        args.append(unit)
                    args.append(pool)
                if kind == "purge" and data.get("volumes"):
                    args.append("--volumes")
                reason = str(data.get("reason", "")).strip()
                if reason and kind != "migrate":
                    if len(reason) > 200 or not re.fullmatch(r"[A-Za-z0-9._/@:+ ,'()-]*", reason):
                        return self.send(400, {"error": "keep the reason short and plain"})
                    args.append(reason)
            else:
                return self.send(400, {"error": "kind migrate|purge|reclaim|prune|cancel and a project"})
            r = subprocess.run(ADMIN + ["task", "add", kind, *args], capture_output=True, text=True)
            if r.returncode != 0:
                return self.send(400, {"error": (r.stderr or r.stdout).strip()})
            return self.send(200, {"id": r.stdout.strip(), "note": f"{kind} queued as task {r.stdout.strip()}"})
        if path == "/api/admin/envelope":
            key = str(data.get("key", ""))
            if not re.fullmatch(r"[a-z_]{1,30}", key):
                return self.send(400, {"error": "bad key"})
            if data.get("reset"):
                args = ["envelope", "reset", key]
            else:
                v = data.get("value")
                v = ",".join(map(str, v)) if isinstance(v, list) else str(v)
                args = ["envelope", "set", key, v]
            r = subprocess.run(ADMIN + args, capture_output=True, text=True)
            return self.send(200 if r.returncode == 0 else 400, {"note": (r.stdout or r.stderr).strip()})
        if path == "/api/admin/timing":
            args = ["timing"]
            if "cooldown" in data:
                args += ["cooldown", "on" if data["cooldown"] else "off"]
            if "target" in data:
                args += ["target", "auto" if data["target"] in (None, "", "auto") else str(float(data["target"]))]
            if "cap_pct" in data:
                args += ["cap", "none" if not data["cap_pct"] else str(int(data["cap_pct"]))]
            r = subprocess.run(ADMIN + args, capture_output=True, text=True)
            return self.send(200 if r.returncode == 0 else 400, {"note": (r.stdout or r.stderr).strip()})
        self.send(404, {"error": "not found"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8080
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    # the admin's own certificate (box.toml tls_cert / tls_key), else the
    # self-signed one init made
    try:
        tls = tomllib.loads((ETC / "box.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        tls = {}
    ctx.load_cert_chain(tls.get("tls_cert") or ETC / "dashboard.pem", tls.get("tls_key") or ETC / "dashboard.key")
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    load_sessions()
    threading.Thread(target=sampler, daemon=True).start()
    server.serve_forever()
