#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""The mcu box core (mcu: multiple compute unit): one file for the SSH entry point of the project
users, the detached job runner and the retention cleanup.

    mcu.py entry       the forced command of a mcu-<project> SSH key;
                         reads the request from SSH_ORIGINAL_COMMAND
    mcu.py run <id>    run one queued job (started by the entry point)
    mcu.py prune       remove job results past their retention (root timer)
    mcu.py topology    the declared resources and the hardware behind them (JSON)

Rights. A project's repository carries a manifest (tools/bench/bench.toml
by default) that *requests* per job: cores, a memory estimate, exclusive
resources, network, devices, mounts, a timeout. One envelope for every
project (box.toml [envelope]) bounds the requests: anything inside it is
granted with no admin step, anything above is refused, never silently
downgraded. Projects also set their own repo, disk quota and shared volumes
(`config`, `volume`). Memory is learned: the scheduler reserves the larger
of the declaration and the job's last measured peak plus 10 %.

Scheduling. The job threads (/etc/mcu/box.toml: all threads but the
reserved ones) are shared: a job takes the number it declares, pinned, and
jobs run side by side while they fit. Exclusive resources (gpu, disk,
machine) go to one job at a time; `machine` means no other job at all.
The queue is FIFO with a conservative backfill: a job may pass the jobs
ahead of it only if it takes nothing any of them is waiting for. The state
is one JSON file under a lock (state.json), so there is no daemon to keep
alive; a runner that died is found by its pid and its allocation freed.
"""

import errno
import fcntl
import hashlib
import json
import os
import pwd
import re
import secrets
import signal
import socket
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urljoin

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcuconf  # noqa: E402  the box's configuration: defaults, then box.toml
from hw import nvidia  # noqa: E402  the NVIDIA GPU, through nvidia-smi

ETC = Path("/etc/mcu")
STATE = Path("/var/lib/mcu")
VOLUMES = Path(mcuconf.load()["volumes_dir"])
NETWORK_LEVELS = ["none", "fetch", "full"]
# the box's own pseudo-resources; hardware ones (gpu, render...) are declared
# in box.toml [resources], or detected (mcuconf)
PSEUDO = ["machine", "disk"]
EXCLUSIVE = ["machine", "gpu", "disk"]   # the historical names, for the dashboard
# the old Benchfile keys, kept as aliases: devices = ["dri"] is the render
# resource, exclusive = ["gpu"] the gpu resource
ALIASES = {"dri": "render"}
# paths a checkout's `at` may not take: the box's own mounts and the system
RESERVED_PATHS = ("/work", "/out", "/run", "/proc", "/sys", "/dev", "/etc", "/usr", "/bin", "/sbin",
                  "/lib", "/lib64", "/var", "/tmp", "/root", "/home", "/boot", "/opt")
SELECT_KEYS = ("vendor_device", "driver", "model")
ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}\Z")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}\Z")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}\Z")
# a run parameter (run <job> key=value): substituted into the Benchfile's
# commands, so only characters that need no quoting in a shell
PARAM_KEY_RE = re.compile(r"^[a-z_][a-z0-9_]{0,30}\Z")
PARAM_VALUE_RE = re.compile(r"^[A-Za-z0-9._/@+-]{1,200}\Z")
FILE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}\Z")

# state.json and events.jsonl are shared by every project user through the
# mcu group
os.umask(0o002)


# EX_TEMPFAIL: the box cannot run this now (maintenance, GPU absent, box
# restarted under the job); retry later or run elsewhere, never a test failure
EX_TEMPFAIL = 75
# EX_CONFIG: the job asks for something this box's configuration cannot give
# (a resource without a render node for display, an undeclared capability);
# retrying cannot help
EX_CONFIG = 78
# EX_DEPENDENCY: a job submitted with after_ok= whose dependency did not
# succeed; it never ran
EX_DEPENDENCY = 79


class Unavailable(Exception):
    pass


class Cancelled(BaseException):
    """the job's runner got SIGTERM (`cancel`); a BaseException, so no
    `except Exception` on the way swallows it"""


EX_CANCELLED = 130
CURRENT_JOB = None      # the job this runner runs: its containers carry it as a label


def stop_containers(job_id, wait=10):
    """stop the job's containers: SIGTERM, SIGKILL after `wait` s (killing
    the podman client alone leaves the container running under conmon)"""
    ids = subprocess.run(["podman", "ps", "-q", "--filter", f"label=mcu.job={job_id}"],
                         capture_output=True, text=True).stdout.split()
    # its services are stopped by stop_services, which keeps their logs
    services = subprocess.run(["podman", "ps", "-q", "--filter", f"label=mcu.job={job_id}",
                               "--filter", "label=mcu.service"], capture_output=True, text=True).stdout.split()
    ids = [i for i in ids if i not in services]
    if ids:
        subprocess.run(["podman", "stop", "-t", str(wait), *ids], capture_output=True)
    return len(ids)


def stop_orphans(log):
    """the project's containers whose job already ended (a runner killed
    before it could stop them): stopped, at each new job of the project"""
    out = subprocess.run(["podman", "ps", "--filter", "label=mcu.job",
                          "--format", '{{.ID}} {{index .Labels "mcu.job"}}'],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        cid, _, jid = line.partition(" ")
        if ID_RE.match(jid) and (jobs_dir() / jid / "status").exists():
            subprocess.run(["podman", "stop", "-t", "5", cid], capture_output=True)
            log(f"stopped a container left running by job {jid}, which had ended")


class Refused(Exception):
    """a request the box will not run; with a topic, the message ends with
    the section of the docs that explains it (`docs <topic>`)"""

    def __init__(self, msg, topic=None):
        super().__init__(f"{msg}; see: docs {topic}" if topic else msg)


class Misconfigured(Refused):
    """refused for good: exit 78, never worth a retry"""


def project_name():
    user = os.environ.get("USER") or os.getlogin()
    # bench-<project>: the name before mcu, an alias of the same account
    # for a while after a box is migrated (mcu-admin migrate-name)
    for prefix in ("mcu-", "bench-"):
        if user.startswith(prefix) and user != f"{prefix}web":
            return user[len(prefix):]
    raise Refused(f"{user} is not an mcu project user")


def load_toml(path):
    with open(path, "rb") as f:
        return tomllib.load(f)


def parse_cpus(spec):
    cpus = set()
    for part in str(spec).split(","):
        if "-" in part:
            a, b = part.split("-")
            cpus.update(range(int(a), int(b) + 1))
        elif part.strip():
            cpus.add(int(part))
    return cpus


def parse_size(value):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kmgKMG]?)", str(value))
    if not m:
        raise Refused(f"bad memory size {value!r}")
    return int(float(m.group(1)) * {"": 1, "k": 2**10, "m": 2**20, "g": 2**30}[m.group(2).lower()])


def box_conf():
    """the box's configuration: the defaults, then box.toml (mcuconf, the one
    place that merges them)"""
    return mcuconf.load()


def job_threads():
    """the threads jobs may use: all of them but the reserved ones"""
    total = set(range(os.cpu_count()))
    return sorted(total - parse_cpus(box_conf().get("reserved", "0")))


def job_memory():
    """the memory jobs may share: the envelope's jobs_memory (the dashboard's
    slider), else the machine's minus the system's reserve; always 1 GB
    left to the system"""
    with open("/proc/meminfo") as f:
        total = int(next(line for line in f if line.startswith("MemTotal:")).split()[1]) * 1024
    conf = box_conf()
    if conf.get("envelope", {}).get("jobs_memory"):
        return min(parse_size(conf["envelope"]["jobs_memory"]), total - 2**30)
    return total - parse_size(conf.get("reserved_memory", "2g"))


MEMORY_RECENT = 5    # the peaks of a job's last runs its reservation is built on


def memory_limit(need, cap):
    """a container's hard memory limit for a reservation: a quarter more,
    2 GB more at least, so a large write's page cache has room above what
    the job needs (a job reserving the per-job cap still gets it: with no
    headroom, lld writing through mmap onto a slow disk died with SIGBUS);
    never above the cap plus that room, nor the memory the jobs share"""
    return min(max(int(need * 1.25), need + 2**31), parse_size(cap) + 2**31, job_memory())


def learned_memory(project, job):
    """the largest peak of the job's last runs, if any (memory.json, kept by
    the runners): one small run does not shrink the next reservation"""
    path = STATE / "memory.json"
    data = json.loads(path.read_text()) if path.exists() and path.stat().st_size else {}
    return data.get(f"{project}/{job}", 0)


def project_conf(project):
    """the project's own settings (repo, manifest, disk), which it sets
    itself through `config`"""
    path = STATE / "projects" / f"{project}.json"
    if not path.exists():
        raise Refused(f"no project {project} on this box")
    conf = json.loads(path.read_text())
    conf.setdefault("manifest", "tools/bench/bench.toml")
    return conf


def ceiling(project):
    """the project's settings plus the box's envelope, which applies to every
    project alike: a project asks for anything inside it, with no admin step"""
    conf = project_conf(project)
    if not conf.get("benchfile") and not conf.get("repo"):
        raise Refused(f"{project} has no Benchfile yet: mcu-job config benchfile < Benchfile")
    lim = conf["limits"] = dict(box_conf()["envelope"])   # every key filled in by mcuconf
    lim.setdefault("devices", [])
    return conf


def volume_conf(name):
    path = STATE / "volumes" / f"{name}.json"
    if not NAME_RE.match(name) or not path.exists():
        raise Refused(f"no shared volume {name}")
    conf = json.loads(path.read_text())
    conf.setdefault("readers", [])
    conf.setdefault("keep_versions", 2)
    return conf


def parse_duration(value):
    if isinstance(value, int):
        return value
    m = re.fullmatch(r"(\d+)([smhd]?)", str(value))
    if not m:
        raise Refused(f"bad duration {value!r}")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def event(**fields):
    fields["t"] = time.time()
    with open(STATE / "events.jsonl", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(fields) + "\n")


# ---------------------------------------------------------------- rights


def grant(project, conf, manifest, job_name):
    """the rights of one job: what the manifest requests, checked against
    the box's envelope; raises Refused on anything above it"""
    jobs = manifest.get("jobs", {})
    if job_name not in jobs:
        raise Refused(f"job {job_name!r} not in the manifest (jobs: {' '.join(sorted(jobs))})", topic="benchfile")
    job = jobs[job_name]
    lim = conf["limits"]
    if "command" not in job:
        raise Refused(f"job {job_name!r} has no command")

    cores = job.get("cores", 1)
    if not isinstance(cores, int) or cores < 1:
        raise Refused(f"job {job_name!r}: cores must be a positive integer")
    if cores > lim["max_cores"]:
        raise Refused(f"job {job_name!r} requests {cores} cores, above the box's envelope ({lim['max_cores']})", topic="config")
    if cores > len(job_threads()):
        raise Refused(f"job {job_name!r} requests {cores} cores; the box has {len(job_threads())} job threads")

    declared = box_resources()
    wanted = request_resources(job_name, job, declared)
    exclusive = []
    for res in job.get("exclusive", []):
        if res in declared:
            continue  # the old spelling of a hardware resource: handled above
        if res not in PSEUDO:
            raise Refused(f"job {job_name!r}: unknown exclusive resource {res!r} (known: {' '.join(PSEUDO + sorted(declared))})")
        if res not in lim["exclusive"]:
            raise Refused(f"job {job_name!r} requests exclusive {res}, not allowed by the box's envelope", topic="config")
        exclusive.append(res)
    allowed = lim.get("resources", sorted(declared))
    for name in wanted:
        if name not in allowed:
            raise Refused(f"job {job_name!r} requests resource {name}, not allowed by the box's envelope", topic="config")
        if declared[name].get("share", "shared") == "exclusive":
            exclusive.append(name)
    needs = job.get("needs", [])
    provided = {c for name in wanted for c in declared[name].get("provides", [])}
    missing = [c for c in needs if c not in provided]
    if missing:
        raise Misconfigured(f"job {job_name!r} needs {', '.join(missing)}, which none of its resources "
                            f"({', '.join(wanted) or 'none'}) declares on this box")
    display = job.get("display")
    if display is not None:
        if display not in wanted:
            raise Misconfigured(f"job {job_name!r}: display = {display!r} is not one of its resources")
        if not renders(declared[display]):
            raise Misconfigured(f"job {job_name!r}: resource {display} has no render node to display on")
        if "WLR_RENDER_DRM_DEVICE" in job.get("env", {}):
            raise Misconfigured(f"job {job_name!r}: display and env.WLR_RENDER_DRM_DEVICE both choose the "
                                "render node; keep one")
    elif "WLR_RENDER_DRM_DEVICE" not in job.get("env", {}):
        # no choice made: the compositor goes on the first resource that renders
        display = next((n for n in wanted if renders(declared[n]) and declared[n].get("share") != "exclusive"),
                       next((n for n in wanted if renders(declared[n])), None))

    # timing = true: a job whose result is its duration; it holds the whole
    # machine, and gets the box's timing profile (frequency cap, cool-down)
    timing = bool(job.get("timing", False))
    if timing and "machine" not in exclusive:
        if "machine" not in lim["exclusive"]:
            raise Refused(f"job {job_name!r}: timing needs the whole machine, not allowed by the box's envelope", topic="config")
        exclusive.append("machine")

    network = job.get("network", "none")
    if network not in ("none", "full"):
        raise Refused(f"job {job_name!r}: network must be none or full")
    if NETWORK_LEVELS.index(network) > NETWORK_LEVELS.index(lim["network"]):
        raise Refused(f"job {job_name!r} requests network={network}, above the box's envelope ({lim['network']})", topic="config")

    memory = parse_size(job.get("memory", lim["memory"]))
    if memory > parse_size(lim["memory"]):
        raise Refused(f"job {job_name!r} declares {job.get('memory')} of memory, above the box's envelope ({lim['memory']})", topic="config")

    timeout = parse_duration(job.get("timeout", lim["timeout_max"]))
    if timeout > parse_duration(lim["timeout_max"]):
        raise Refused(f"job {job_name!r}: timeout {timeout}s above the box's envelope ({lim['timeout_max']})", topic="config")

    # how long others may pass the job in the queue (backfill) before it is
    # no longer delayed: the job's own, else the box's
    max_wait = parse_duration(job.get("max_wait", box_conf().get("backfill_max_wait", "30m")))
    if not 300 <= max_wait <= 86400:
        raise Refused(f"job {job_name!r}: max_wait between 5m and 24h")

    mounts, life = parse_mounts(project, manifest, f"job {job_name!r}", job.get("mounts", {}))

    sandbox = job.get("sandbox", False)
    if not isinstance(sandbox, bool):
        raise Refused(f"job {job_name!r}: sandbox is true or false")

    # services: containers beside the job, in its pod, for the job's
    # duration only; their memory counts in the job's reservation
    services = []
    if not isinstance(job.get("services", []), list) or len(set(map(str, job.get("services", [])))) != len(job.get("services", [])):
        raise Refused(f"job {job_name!r}: services is a list of [services] names, each once")
    for sname in job.get("services", []):
        svc = service_spec(manifest, sname, f"job {job_name!r}")
        svc["mounts"], svc["life"] = parse_mounts(project, manifest, f"service {sname!r}", svc["mounts"])
        services.append(svc)
    services_memory = sum(s["memory"] for s in services)
    if services and memory + services_memory > parse_size(lim["memory"]):
        raise Refused(f"job {job_name!r}: {memory / 2**30:.1f} GB plus {services_memory / 2**30:.1f} GB for its "
                      f"services, above the box's envelope ({lim['memory']})")
    return {
        "cores": cores,
        "exclusive": exclusive,
        "network": network,
        "timing": timing,
        "resources": wanted,        # name -> select (or {})
        "display": display,
        "timeout": timeout,
        "max_wait": max_wait,
        "mounts": mounts,
        "life": life,             # mount -> its own stated end, over [storage]'s
        "command": job["command"],
        "sandbox": sandbox,
        "env": {str(k): str(v) for k, v in job.get("env", {}).items()},
        # what the scheduler reserves: the declared estimate, or the largest
        # recent peak plus 10 % when that is larger (the box learns); the
        # container's hard limit follows it (a quarter more, 1 GB at least),
        # so jobs that outgrow their reservation only hurt themselves
        "memory_need": (need := min(max(memory, int(learned_memory(project, job_name) * 1.1)), parse_size(lim["memory"]))),
        "memory": f"{memory_limit(need, lim['memory']) // 2**20}m",
        "max_jobs": lim["max_jobs"],
        "services": services,
        "services_memory": services_memory,
    }


def parse_mounts(project, manifest, who, spec):
    """mounts: storage of the project (rw) and shared volumes (read-only);
    returns [(kind, name, target)] and each mount's own stated end"""
    caches = storage_decls(manifest)
    mounts, life = [], {}
    if not isinstance(spec, dict):
        raise Refused(f"{who}: mounts is a table, name = \"/target\"")
    for name, target in spec.items():
        base = name.partition("/")[0]
        override = None
        if isinstance(target, dict):   # { path = "/target", ttl = "2d" }: this mount's own end
            override, target = target, target.get("path", "")
        if not NAME_RE.match(base) or not str(target).startswith("/"):
            raise Refused(f"{who}: bad mount {name} -> {target}")
        if base in caches:
            mounts.append(("cache", name, target))
            if override and life_of(override, where=f"{who} mount {name}") is not None:
                life[name] = {k: override[k] for k in LIFE_KEYS if k in override}
            continue
        if "/" in name:
            raise Refused(f"{who}: {base} is not a [storage] of this Benchfile", topic="storage")
        vol = volume_conf(name)
        if vol["owner"] != project and project not in vol["readers"]:
            raise Refused(f"{who}: {project} may not read volume {name}")
        mounts.append(("volume", name, target))
    return mounts, life


SERVICE_KEYS = {"about", "image", "command", "cwd", "env", "mounts", "tmpfs", "ready", "ready_timeout", "memory"}


def service_spec(manifest, name, who="the Benchfile"):
    """[services.<name>], checked: a container that runs beside the jobs
    listing it, in their pod (the same loopback), torn down when the job
    ends. Its rootfs is read-only: it writes only to its storage mounts,
    /work, /out and tmpfs (counted in its memory)"""
    spec = manifest.get("services", {}).get(name) if NAME_RE.match(str(name)) else None
    if not isinstance(spec, dict):
        raise Refused(f"{who}: no [services.{name}] in the Benchfile")
    where = f"[services.{name}]"
    unknown = sorted(set(spec) - SERVICE_KEYS)
    if unknown:
        raise Refused(f"{where}: unknown keys {', '.join(unknown)} (known: {', '.join(sorted(SERVICE_KEYS))})")
    about = spec.get("about")
    if not isinstance(about, str) or not about.strip() or "\n" in about or len(about) > ABOUT_MAX:
        raise Refused(f"{where} about: one line of at most {ABOUT_MAX} characters")
    image = spec.get("image")
    if image is not None and not (isinstance(image, dict) and "from" in image):
        raise Refused(f"{where} image: a table like [image] (from, run, env, copy); without it, the job's image")
    command = spec.get("command")
    if command is None and image is None:
        raise Refused(f"{where}: a service on the job's image needs a command")
    tmpfs = spec.get("tmpfs", [])
    if not isinstance(tmpfs, list) or not all(isinstance(t, str) and t.startswith("/") and ":" not in t
                                              and t not in ("/", "/work", "/out") for t in tmpfs):
        raise Refused(f"{where} tmpfs: a list of absolute paths (writable, in memory)")
    if not isinstance(spec.get("env", {}), dict):
        raise Refused(f"{where} env: a table")
    return {"name": name, "about": about, "image": image,
            "command": str(command) if command is not None else None,
            "cwd": str(spec["cwd"]) if spec.get("cwd") else None,
            "env": {str(k): str(v) for k, v in spec.get("env", {}).items()},
            "mounts": spec.get("mounts", {}), "tmpfs": tmpfs,
            "ready": str(spec["ready"]) if spec.get("ready") else None,
            "ready_timeout": parse_duration(spec.get("ready_timeout", "2m")),
            "memory": parse_size(str(spec.get("memory", "1g")))}


# ---------------------------------------------------------------- resources

def box_resources():
    """the hardware resources this box declares (box.toml [resources]), by
    name; projects ask for these names, never for devices. Without the
    table, what the machine has (mcuconf.detected_resources)"""
    return dict(box_conf()["resources"])


def renders(spec):
    """whether a resource comes with a DRM render node a compositor can use"""
    return "drm" in spec or spec.get("probe") == "nvidia"


def request_resources(job_name, job, declared):
    """the job's resources as {name: select}, from `resources` and the old
    `devices` / `exclusive` spellings"""
    wanted = {}
    items = list(job.get("resources", []))
    items += [ALIASES.get(d, d) for d in job.get("devices", [])]
    items += [r for r in job.get("exclusive", []) if r in declared]
    for item in items:
        if isinstance(item, str):
            name, select = item, {}
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            name, select = item["name"], dict(item.get("select", {}))
        else:
            raise Refused(f"job {job_name!r}: a resource is a name or {{ name = ..., select = {{...}} }}")
        if name not in declared:
            raise Refused(f"job {job_name!r}: unknown resource {name!r} (this box has: {' '.join(sorted(declared))})")
        bad = [k for k in select if k not in SELECT_KEYS]
        if bad:
            raise Refused(f"job {job_name!r}: resource {name} selects on {', '.join(bad)}; "
                          f"only {', '.join(SELECT_KEYS)} (stable attributes, never a bus address)")
        wanted[name] = {k: str(v).lower() for k, v in select.items()}
    return wanted


def read(path, default=None):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def pci_model(addr):
    """a PCI device's name, from lspci when installed"""
    try:
        out = subprocess.run(["lspci", "-mm", "-s", addr], capture_output=True, text=True, timeout=5).stdout
        f = re.findall(r'"([^"]*)"', out)
        return f"{f[1]} {f[2]}" if len(f) >= 3 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def drm_nodes():
    """every render node on the host: path, driver, PCI vendor:device and
    address, model, driver version"""
    nodes = []
    for n in sorted(Path("/sys/class/drm").glob("renderD*")):
        dev = n / "device"
        driver = os.path.basename(os.path.realpath(dev / "driver")) if (dev / "driver").exists() else None
        vendor, device = read(dev / "vendor", ""), read(dev / "device", "")
        addr = os.path.basename(os.path.realpath(dev))
        nodes.append({"node": f"/dev/dri/{n.name}", "driver": driver, "pci": addr,
                      "vendor_device": f"{vendor[2:]}:{device[2:]}".lower() if vendor and device else None,
                      "model": pci_model(addr),
                      "driver_version": read(f"/sys/module/{driver}/version") or os.uname().release if driver else None})
    return nodes


def matches(node, select):
    return all(str(node.get(k) or "").lower().find(v) >= 0 if k == "model" else str(node.get(k) or "").lower() == v
               for k, v in select.items())


def resolve(name, spec, select):
    """the host devices behind one resource, now: {devices: podman args,
    render: node or None, info}; Unavailable when the hardware is gone,
    Misconfigured when it is there but the select does not match"""
    nodes = drm_nodes()
    if spec.get("probe") == "nvidia" and not gpu_present():
        raise Unavailable(gpu_absent_reason())
    out = {"devices": [], "render": None, "info": {"name": name, "about": spec.get("about")}}
    if "drm" in spec:
        drivers = spec["drm"].get("driver", [])
        found = [n for n in nodes if n["driver"] in drivers]
        if not found:
            raise Unavailable(f"resource {name}: no render node with driver {'/'.join(drivers)} on this box now")
        pick = [n for n in found if matches(n, select)]
        if not pick:
            raise Misconfigured(f"resource {name}: nothing matches {select} (this box has: "
                                + "; ".join(f"{n['vendor_device']} {n['driver']} {n['model']}" for n in found) + ")")
        out["render"] = pick[0]["node"]
        out["devices"] += ["--device", pick[0]["node"]]
        out["info"].update(pick[0])
    if "cdi" in spec:
        out["devices"] += ["--device", spec["cdi"]]
        if spec.get("probe") == "nvidia":
            # the GPU's own render node, which the CDI spec maps as well
            nv = [n for n in nodes if n["driver"] == "nvidia"]
            pick = [n for n in nv if matches(n, select)]
            if select and not pick:
                raise Misconfigured(f"resource {name}: nothing matches {select} (this box has: "
                                    + "; ".join(f"{n['vendor_device']} {n['model']}" for n in nv) + ")")
            if pick:
                out["render"] = pick[0]["node"]
                out["info"].update(pick[0])
                out["info"]["model"] = gpu_name() or pick[0]["model"]
    for path in spec.get("device", []):
        out["devices"] += ["--device", str(path)]
        out["info"].setdefault("devices", []).append(str(path))
    out["info"]["render"] = out["render"]
    return out


def gpu_name():
    row = nvidia.query(["name"])
    return row[0] if row else None


def cpu_facts(threads):
    """the CPU state a timing result depends on"""
    model = next((l.split(":", 1)[1].strip() for l in (read("/proc/cpuinfo", "") or "").splitlines()
                  if l.startswith("model name")), None)
    cpu = Path("/sys/devices/system/cpu")
    no_turbo = read(cpu / "intel_pstate/no_turbo")
    boost = read(cpu / "cpufreq/boost")
    return {
        "model": model, "threads": threads,
        "siblings": {t: read(cpu / f"cpu{t}/topology/thread_siblings_list") for t in threads},
        "governor": read(cpu / f"cpu{threads[0]}/cpufreq/scaling_governor") if threads else None,
        "turbo": (no_turbo == "0") if no_turbo is not None else (boost == "1") if boost is not None else None,
    }


def topology():
    """the box as a project may see it: declared resources with their
    hardware now, the CPU, memory and filesystems"""
    declared = box_resources()
    res = {}
    for name, spec in declared.items():
        entry = {"about": spec.get("about"), "share": spec.get("share", "shared"),
                 "provides": spec.get("provides", []), "renders": renders(spec)}
        try:
            entry.update(present=True, **resolve(name, spec, {})["info"])
        except (Unavailable, Refused) as e:
            entry.update(present=False, reason=str(e))
        res[name] = entry
    cpu = Path("/sys/devices/system/cpu")
    threads = sorted(int(p.name[3:]) for p in cpu.glob("cpu[0-9]*"))
    reserved = sorted(set(threads) - set(job_threads()))
    fs = {}
    for label, path in (("jobs", Path.home() if os.environ.get("USER", "").startswith("mcu-") else Path("/home")),
                        ("volumes", VOLUMES)):
        try:
            u = shutil.disk_usage(path)
            fs[label] = {"path": str(path), "total": u.total, "free": u.free}
        except OSError:
            pass
    return {"resources": res, "pseudo": PSEUDO,
            "cpu": dict(cpu_facts(threads), reserved=reserved, job_threads=job_threads()),
            "memory": {"jobs": job_memory()}, "kernel": os.uname().release, "filesystems": fs}


# ---------------------------------------------------------------- health


def cpu_temp():
    """the hottest CPU sensor, in degrees C"""
    for hw in Path("/sys/class/hwmon").glob("hwmon*"):
        if read(hw / "name") in ("coretemp", "k10temp", "x86_pkg_temp"):
            temps = [int(t) / 1000 for t in (read(f) for f in hw.glob("temp*_input")) if t and t.lstrip("-").isdigit()]
            return max(temps) if temps else None
    return None


def throttle_count():
    """thermal throttling events so far: the cores' counters plus the
    package's (counted once)"""
    cpu = Path("/sys/devices/system/cpu")
    total, seen = 0, False
    for f in cpu.glob("cpu[0-9]*/thermal_throttle/core_throttle_count"):
        v = read(f)
        if v and v.isdigit():
            total, seen = total + int(v), True
    pkg = read(cpu / "cpu0/thermal_throttle/package_throttle_count")
    if pkg and pkg.isdigit():
        total, seen = total + int(pkg), True
    return total if seen else None


def throttle_time():
    """time spent throttled so far, in ms: the package's, and each CPU's
    (its core's counter); None where the kernel does not say"""
    cpu = Path("/sys/devices/system/cpu")
    pkg = read(cpu / "cpu0/thermal_throttle/package_throttle_total_time_ms")
    cores = {}
    for f in cpu.glob("cpu[0-9]*/thermal_throttle/core_throttle_total_time_ms"):
        v = read(f)
        if v and v.isdigit():
            cores[int(f.parent.parent.name[3:])] = int(v)
    return {"package": int(pkg) if pkg and pkg.isdigit() else None, "cores": cores}


# a run counts as throttled (not comparable) past this share of its time: the
# event counters rise by thousands of 1 ms throttles on a small box under
# load, which cost a run nothing measurable
THROTTLE_SHARE = 0.01


def throttled(start, end, seconds, threads):
    """(seconds throttled, share of the run) between two throttle_time()
    readings: the package's time or the most throttled of the job's CPUs (a
    core's throttle slows only the jobs on it), whichever is longer: both
    often come from the same heat at the same moment, so adding them would
    count it twice; (None, None) unknown"""
    if not start or not end:
        return None, None
    ms, known = 0, False
    if start.get("package") is not None and end.get("package") is not None:
        ms, known = max(end["package"] - start["package"], 0), True
    c0, c1 = start.get("cores") or {}, end.get("cores") or {}
    deltas = [max(c1[c] - c0[c], 0) for c in (threads or c1) if c in c0 and c in c1]
    if deltas:
        ms, known = max(ms, max(deltas)), True
    if not known:
        return None, None
    secs = round(ms / 1000, 1)
    return secs, (round(ms / 1000 / seconds, 4) if seconds else None)


def machine_now():
    """the machine's state, now"""
    cpu = Path("/sys/devices/system/cpu")
    freqs = [int(v) / 1000 for v in (read(f) for f in cpu.glob("cpu[0-9]*/cpufreq/scaling_cur_freq")) if v and v.isdigit()]
    mem = {}
    for line in (read("/proc/meminfo", "") or "").splitlines():
        k, _, v = line.partition(":")
        if v.split():
            mem[k] = int(v.split()[0]) * 1024
    facts = cpu_facts([])
    return {"t": round(time.time(), 1), "load": [round(x, 2) for x in os.getloadavg()],
            "cpu_temp": cpu_temp(), "cpu_mhz": round(sum(freqs) / len(freqs)) if freqs else None,
            "governor": read(cpu / "cpu0/cpufreq/scaling_governor"), "turbo": facts["turbo"],
            "throttle_count": throttle_count(), "throttle_time": throttle_time(),
            "mem_total": mem.get("MemTotal"), "mem_available": mem.get("MemAvailable")}


def gpu_now():
    res = read_state_json("resources.json")
    out = {"present": False, "since": res.get("gpu_since"), "name": res.get("gpu_name")}
    f = nvidia.query(["name", "temperature.gpu", "memory.used", "memory.total", "utilization.gpu", "power.draw"],
                     units=True) or []
    if len(f) == 6:
        num = lambda x: None if x in ("", "[N/A]", "N/A") else float(x)
        out.update(present=True, name=f[0], temp=num(f[1]),
                   vram_used=int(num(f[2]) * 2**20) if num(f[2]) is not None else None,
                   vram_total=int(num(f[3]) * 2**20) if num(f[3]) is not None else None,
                   util=num(f[4]), power=num(f[5]))
    return out


def recent_events(max_bytes=4 * 2**20):
    """the tail of events.jsonl, parsed"""
    try:
        with open(STATE / "events.jsonl", "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(size - max_bytes, 0))
            lines = f.read().splitlines()[1 if size > max_bytes else 0:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def expected_wait(running, events):
    """seconds until the running jobs are all done (what a job holding the
    machine waits for), from each job's median recent duration; None when a
    running job has no history"""
    durations = job_durations(events)
    left = 0
    for r in running:
        past = durations.get((r["project"], r["job"]), [])
        if not past:
            return None
        med = sorted(past)[len(past) // 2]
        left = max(left, med - (time.time() - r.get("started", time.time())))
    return round(max(left, 0))


def job_durations(events, last=5):
    """each job's recent run times, in seconds, oldest first: {(project, job): [...]}"""
    durations = {}
    for e in events:
        # a cancelled run says nothing of how long the job takes
        if e.get("kind") == "done" and e.get("seconds") and e.get("status") != EX_CANCELLED:
            durations.setdefault((e.get("project"), e.get("job")), []).append(e["seconds"])
    return {k: v[-last:] for k, v in durations.items()}


INF = float("inf")


class Estimates:
    """how long a job lasts, from its last runs: typical (the median of the
    last 5, for the queue's times) and safe (the p90 of the last 20, at most
    its timeout; its timeout when it has no history: the box stops it then),
    for a job that passes others"""

    def __init__(self, events):
        self.runs = job_durations(events, last=20)

    def typical(self, e):
        past = self.runs.get((e["project"], e["job"]), [])[-5:]
        return sorted(past)[len(past) // 2] if past else None

    def safe(self, e):
        past = sorted(self.runs.get((e["project"], e["job"]), []))
        p90 = past[min(len(past) - 1, int(len(past) * 0.9))] if past else None
        bounds = [x for x in (p90, e.get("timeout")) if x]
        return min(bounds) if bounds else INF


def run_slot(e, now, est, start=None):
    """a running job as the scheduler weighs it: what it holds, until when
    (its typical end, else its timeout; one that overran ends "soon")"""
    t0 = e.get("started", now) if start is None else start
    typ = est.typical(e)
    end = t0 + typ if typ is not None else t0 + e["timeout"] if e.get("timeout") else INF
    threads = e.get("threads")
    return {"id": e.get("id"), "project": e["project"], "job": e["job"],
            "threads": len(threads) if isinstance(threads, list) else e.get("cores", 1),
            "memory": e.get("memory", 0), "exclusive": set(e.get("exclusive", [])),
            "locks": set(e.get("locks", [])), "end": max(end, now + 60)}


def fits(e, live, nthreads, pool):
    """would a job fit beside these running ones"""
    excl = set(e.get("exclusive", []))
    if any("machine" in s["exclusive"] for s in live) or "machine" in excl and live:
        return False
    if any(s["exclusive"] & excl for s in live):
        return False
    return (nthreads - sum(s["threads"] for s in live) >= e.get("cores", 1)
            and pool - sum(s["memory"] for s in live) >= e.get("memory", 0))


def reserved_start(e, live, now, nthreads, pool):
    """the earliest time a job fits, as the running jobs end"""
    for t in [now] + sorted(s["end"] for s in live if now < s["end"] < INF):
        if fits(e, [s for s in live if s["end"] > t], nthreads, pool):
            return t
    return INF


PRIORITIES = ("high", "normal", "low")


def queue_order(queue):
    """the order the queue is served in. Within a project, the places its
    jobs hold are dealt again, high then normal then low (each in queue
    order), so another project's jobs never move; then each project's first
    high job goes ahead of the rest, in queue order between projects"""
    rank = lambda e: PRIORITIES.index(e.get("priority") or "normal")
    order = list(queue)
    for p in {e.get("project") for e in queue}:
        places = [i for i, e in enumerate(queue) if e.get("project") == p]
        for i, e in zip(places, sorted((queue[i] for i in places), key=rank)):
            order[i] = e
    first, seen = [], set()
    for e in order:
        if e.get("priority") == "high" and e.get("project") not in seen:
            seen.add(e.get("project"))
            first.append(e)
    return first + [e for e in order if not any(e is f for f in first)]


def may_start(me, ahead, live, now, est, nthreads, pool, max_wait):
    """None if a waiting job may start now, else why it waits. The jobs
    ahead of it keep their place: it passes one only without delaying the
    time that one could start (it fits now, and ends before, or leaves room
    then); one that waited longer than its max_wait is not delayed even if
    the estimates are wrong; nothing passes a job that needs the machine"""
    excl = set(me.get("exclusive", []))
    who = lambda s: f"{s['project']}/{s['job']}"
    holders = lambda what: " (held by " + ", ".join(what(s) for s in live) + ")" if live else ""
    if any("machine" in s["exclusive"] for s in live):
        return "the machine is held by " + next(who(s) for s in live if "machine" in s["exclusive"])
    if "machine" in excl and live:
        return f"needs the whole machine, {len(live)} job(s) running"
    held = excl & {x for s in live for x in s["exclusive"]}
    if held:
        return f"{', '.join(sorted(held))} held"
    # a locked cache (a build tree per branch): one job at a time, within the project
    locked = {x for s in live if s["project"] == me["project"] for x in s["locks"]} & set(me.get("locks", []))
    if locked:
        return f"{', '.join(sorted(locked))} in use"
    free = nthreads - sum(s["threads"] for s in live)
    if free < me.get("cores", 1):
        return (f"needs {me['cores']} threads, {free} free"
                + holders(lambda s: f"{who(s)} {s['threads']}"))
    mem_free = pool - sum(s["memory"] for s in live)
    if mem_free < me.get("memory", 0):
        return (f"needs {me['memory'] / 2**30:.1f} GB of memory, {max(mem_free, 0) / 2**30:.1f} GB free"
                + holders(lambda s: f"{who(s)} {s['memory'] / 2**30:.1f} GB"))
    mine = [s for s in live if s["project"] == me["project"]]
    if len(mine) >= me.get("max_jobs", 99):
        return f"{me['project']} already runs {len(mine)} job(s), its limit"
    for a in ahead:
        if "machine" in a.get("exclusive", []):
            return f"behind {who(a)}, which needs the machine"
        if a["project"] == me["project"] and set(a.get("locks", [])) & set(me.get("locks", [])):
            return f"behind {who(a)} (same cache)"
        waited = now - a.get("queued", now)
        starved = waited >= (a.get("max_wait") or max_wait)
        # past its max wait, count this job as never ending: pass only
        # what leaves that one's room for good
        end = INF if starved else now + est.safe(me)
        me_slot = {"project": me["project"], "job": me["job"], "threads": me.get("cores", 1),
                   "memory": me.get("memory", 0), "exclusive": excl, "locks": set(), "end": end}
        t = reserved_start(a, live, now, nthreads, pool)
        if reserved_start(a, live + [me_slot], now, nthreads, pool) > t:
            if starved:
                return f"behind {who(a)}, not overtaken: it waited {int(waited // 60)} min, its max wait"
            return f"behind {who(a)}" + (f", its turn from {clock(t, '%H:%M')}" if t < INF else "")
    return None


def queue_view(sched, events, project=None):
    """the queue as the scheduler will likely serve it: for each running job
    its expected end, for each waiting one its place, the jobs ahead of it
    and its expected start. The scheduler's own rule (may_start) played
    forward, each job lasting its median recent run; a job with no history
    makes the times after it unknown (None). The parameters of jobs ahead
    are shown to their own project only, like health's"""
    now = time.time()
    est = Estimates(events)
    nthreads, pool = len(job_threads()), job_memory()
    max_wait = parse_duration(box_conf().get("backfill_max_wait", "30m"))
    known = lambda t: round(t) if t < INF else None
    slots, ends = [], {}
    for r in sched.get("running", []):
        s = run_slot(r, now, est)
        typ = est.typical(r)
        ends[r["id"]] = max(r.get("started", now) + typ, now) if typ is not None else INF
        if typ is None:
            s["end"] = INF  # for the times: unknown, not its timeout
        slots.append(s)
    waiting, starts, t = queue_order(sched.get("queue", [])), {}, now
    if not (hold() or maintenance()):
        while waiting and t < INF:
            live = [s for s in slots if s["end"] > t]
            for i, q in enumerate(waiting):
                if may_start(q, waiting[:i], live, t, est, nthreads, pool, max_wait) is None:
                    starts[q["id"]] = t
                    typ = est.typical(q)
                    slots.append(dict(run_slot(dict(q, started=t), t, est), end=t + typ if typ is not None else INF))
                    waiting.pop(i)
                    break
            else:
                t = min((s["end"] for s in slots if s["end"] > t), default=INF)
    queue = queue_order(sched.get("queue", []))
    # a job waiting for its dependencies: no earlier than their expected
    # ends (then the queue, not counted here)
    entries = {e["id"]: e for e in queue + sched.get("deps", [])}
    blocked = {e["id"]: e for e in sched.get("deps", [])}
    exts = {(e["project"], e["ext"]): e["id"] for e in queue + sched.get("running", []) + sched.get("deps", [])
            if e.get("ext")}
    def dep_end(i, seen=()):
        if i in ends:
            return ends[i]
        typ = est.typical(entries[i]) if i in entries else None
        if i in starts:
            return starts[i] + typ if typ is not None else INF
        if i in blocked and i not in seen:
            start = deps_start(blocked[i], seen + (i,))
            return start + typ if typ is not None and start < INF else INF
        if i in entries:
            return INF
        # neither queued nor running: ended (met), or still before the queue
        # (its checkout, its image), which only its own project can see
        found = resolve_ref(i) if project and project == owner.get(i) else None
        return INF if found and not (jobs_dir() / found / "status").exists() else now
    owner = {}
    def deps_start(e, seen=()):
        refs = [exts.get((e["project"], ext_ref(i)), i) if ext_ref(i) else i
                for i in e.get("after", []) + e.get("after_ok", [])]
        owner.update((i, e["project"]) for i in refs)
        return max([dep_end(i, seen) for i in refs] + [now])
    return {"ends": {i: known(e) for i, e in ends.items()},
            "blocked": {i: {"eta_start": known(deps_start(e))} for i, e in blocked.items()},
            "waiting": {q["id"]: {"position": n + 1, "eta_start": known(starts.get(q["id"], INF)),
                                  "ahead": [{"project": a["project"], "job": a["job"], "threads": a.get("cores"),
                                             "memory": a.get("memory"),
                                             **({"params": a.get("branch")} if project and a["project"] == project else {})}
                                            for a in queue[:n]]}
                        for n, q in enumerate(queue)},
            "estimate": "the scheduler's rule played forward, each job lasting the median of its last 5 runs; "
                        "null when a job before it has no history or the queue is held"}


def queue_line(view, job_id):
    """where a waiting job stands, on one line: "position 2, behind
    myapp/lint, myapp/test; start ~10:42 CET (estimate)" """
    w = view["waiting"].get(job_id)
    if not w:
        return ""
    ahead = ", ".join(f"{a['project']}/{a['job']}" for a in w["ahead"][:4]) + (" …" if len(w["ahead"]) > 4 else "")
    eta = w["eta_start"]
    return (f"position {w['position']}" + (f", behind {ahead}" if ahead else "")
            + (f"; start ~{clock(eta, '%H:%M')} (estimate)" if eta else "; start time unknown"))


def queue_state(project, sched, events):
    """what runs and what waits, with the expected times, for health and queue"""
    # the memory each job reserved (and uses, while it runs); the ids of the
    # asking project's own jobs, for wait, status and cancel
    mine = lambda e: ({"id": e["id"], "params": e.get("branch"), **({"ext_job_id": e["ext"]} if e.get("ext") else {})}
                      if project and e.get("project") == project else {})
    abouts = {}
    def about(e):
        """the job's about, from its project's last upload"""
        if e["project"] not in abouts:
            conf = read_state_json(f"projects/{e['project']}.json")
            abouts[e["project"]] = {**{j: a for js in conf.get("label_jobs", {}).values() for j, a in js.items()},
                                    **conf.get("jobs", {})}
        return abouts[e["project"]].get(e["job"])
    running = [{"project": r["project"], "job": r["job"], "threads": r.get("threads"),
                "exclusive": r.get("exclusive", []), "started": r.get("started"),
                "memory": r.get("memory"), "mem_now": r.get("mem_now"), "mem_peak": r.get("mem_peak"),
                "about": about(r), **mine(r)}
               for r in sched.get("running", [])]
    view = queue_view(sched, events, project)
    for r, e in zip(running, sched.get("running", [])):
        r["eta_end"] = view["ends"].get(e["id"])
    waiting = [{"project": q["project"], "job": q["job"], "cores": q.get("cores"), "memory": q.get("memory"),
                "exclusive": q.get("exclusive", []), "queued": q.get("queued"), "waiting": q.get("waiting"),
                "priority": q.get("priority") or "normal",
                "about": about(q), **mine(q), **view["waiting"][q["id"]]}
               for q in queue_order(sched.get("queue", []))]
    blocked = [{"project": e["project"], "job": e["job"], "since": e.get("since"), "about": about(e), **mine(e),
                **({"after": e.get("after", []), "after_ok": e.get("after_ok", []), "waiting_for": e.get("waiting_for")}
                   if project and e.get("project") == project else {}),
                **view["blocked"][e["id"]]}
               for e in sched.get("deps", [])]
    return {"mode": queue_mode(), "reason": hold() or maintenance(), "running": running, "waiting": waiting,
            "blocked": blocked,
            "expected_wait": {"seconds": expected_wait(running, events),
                              "for": "the machine to be free (all running jobs done)",
                              "estimate": "median of each running job's last runs, minus its elapsed time"},
            "capacity": {"threads": len(job_threads()), "memory": job_memory()},
            "eta": view["estimate"]}


def health(project):
    """everything an agent may want to know before or after a run"""
    out = {
        "status": ("maintenance" if maintenance() else "degraded" if read_state_json("resources.json").get("gpu") is False else "ok"),
        "queue": queue_state(project, read_state_json("state.json"), recent_events()),
        "machine": machine_now(),
        "gpu": gpu_now(),
    }
    if project:
        out["disk"] = disk_breakdown(project)
        out["purged"] = purge_notice(project)
    return out


def queue_text(project, as_json=False):
    """queue: the capacity, what runs and what waits, in queue order, with
    the expected times; read-only, to plan before submitting"""
    q = queue_state(project, read_state_json("state.json"), recent_events())
    if as_json:
        print(json.dumps({k: q[k] for k in ("mode", "reason", "capacity", "running", "waiting", "blocked", "eta")}, indent=1))
        return 0
    cap, gb = q["capacity"], lambda b: f"{(b or 0) / 2**30:.1f} GB"
    hhmm = lambda t: clock(t, "%H:%M") if t else "?"
    used = sum(len(r.get("threads") or []) for r in q["running"])
    print(f"capacity  {cap['threads']} threads, {gb(cap['memory'])} for jobs; "
          f"{used} threads, {gb(sum(r.get('memory') or 0 for r in q['running']))} in use")
    print("rule      jobs run side by side when their threads and memory fit; a job passes those ahead\n"
          "          only when it delays none of them (none past its max_wait); "
          "exclusive = [\"machine\"] and timing jobs run alone")
    if q["mode"] != "open":
        print(f"queue     {q['mode']}: {q['reason']}")
    print(f"running   {len(q['running'])}" + ("" if q["running"] else "  (none)"))
    for r in q["running"]:
        print(f"  {r['project']}/{r['job']}" + (f" [{r['params']}]" if r.get("params") else "")
              + f"  {len(r.get('threads') or [])} threads, {gb(r.get('memory'))}"
              + (f", {', '.join(r['exclusive'])}" if r.get("exclusive") else "")
              + f"; since {hhmm(r.get('started'))}, end ~{hhmm(r.get('eta_end'))}"
              + (f"  ({r['id']}" + (f", x:{r['ext_job_id']}" if r.get("ext_job_id") else "") + ")" if r.get("id") else ""))
    print(f"waiting   {len(q['waiting'])}" + ("" if q["waiting"] else "  (none)"))
    for w in q["waiting"]:
        print(f"  {w['position']}. {w['project']}/{w['job']}" + (f" [{w['params']}]" if w.get("params") else "")
              + {"high": " HIGH", "low": " low"}.get(w.get("priority"), "")
              + f"  {w.get('cores')} threads, {gb(w.get('memory'))}"
              + (f", {', '.join(w['exclusive'])}" if w.get("exclusive") else "")
              + f"; start ~{hhmm(w.get('eta_start'))}"
              + (f"  ({w['id']}" + (f", x:{w['ext_job_id']}" if w.get("ext_job_id") else "") + ")" if w.get("id") else ""))
        if w.get("waiting"):
            print(f"       {w['waiting']}")
    if q["blocked"]:
        print(f"waiting for dependencies   {len(q['blocked'])}")
    for b in q["blocked"]:
        print(f"  {b['project']}/{b['job']}" + (f" [{b['params']}]" if b.get("params") else "")
              + (f"; for {b['waiting_for']}" if b.get("waiting_for") else "")
              + f"; then queued, not before ~{hhmm(b.get('eta_start'))}"
              + (f"  ({b['id']}" + (f", x:{b['ext_job_id']}" if b.get("ext_job_id") else "") + ")" if b.get("id") else ""))
    print("times     estimates: the rule played forward, each job lasting the median of its last 5 runs;\n"
          "          ? = no history or queue held")
    return 0


def timing_settings():
    """the admin's timing profile (mcu-admin timing): cool-down on or off,
    its target (auto: the box's idle temperature + 10 °C), the frequency cap
    while a timing job runs (max_perf_pct), the cool-down's longest wait"""
    t = {"cooldown": False, "target": "auto", "cap_pct": None, "cooldown_max": 600}
    t.update(read_state_json("timing.json"))
    return t


def cooldown_target(t):
    if t.get("target") not in (None, "auto"):
        return float(t["target"])
    idle = read_state_json("idle.json").get("idle_temp")
    return (idle or 50) + 10


def timing_start(rights, log):
    """before a timing job's container: wait for the machine to cool (if on),
    then apply the frequency cap; returns what was done, for the job's facts"""
    t = timing_settings()
    state = {"timing": True, "cooldown": t["cooldown"], "max_perf_pct": 100, "start_temp": cpu_temp(),
             "cap_applied": None}
    if t["cooldown"]:
        target, t0 = cooldown_target(t), time.time()
        state["target"] = target
        temp = cpu_temp()
        if temp is not None and temp > target:
            log(f"cooling down: {temp:.0f} °C -> {target:.0f} °C (at most {t['cooldown_max']} s)")
            while temp is not None and temp > target and time.time() - t0 < t["cooldown_max"]:
                time.sleep(5)
                temp = cpu_temp()
            state["cooled_s"] = round(time.time() - t0)
            state["started_hot"] = temp is not None and temp > target
        state["start_temp"] = temp
    # perf-cap also puts the CPU at its fast power profile, so a timing job
    # always goes through it, at 100 % when no cap is set
    pct = int(t.get("cap_pct") or 100)
    r = subprocess.run(HELPER + ["perf-cap", str(pct)], capture_output=True, text=True)
    state["cap_applied"] = r.returncode == 0
    if r.returncode == 0:
        state["max_perf_pct"] = pct
        if pct < 100:
            log(f"timing profile: frequency capped at {pct} %")
    elif "no frequency control" in (r.stderr or ""):
        state["cap"] = "none on this box"
        log("timing profile: no frequency cap on this box (no intel_pstate or cpufreq); cool-down only")
    else:
        log(f"timing profile NOT applied: {(r.stderr or r.stdout).strip()}")
    return state


def timing_end(state):
    if state and state.get("cap_applied"):
        subprocess.run(HELPER + ["perf-cap", "off"], capture_output=True)


class Conditions:
    """what the machine did while a job ran: sampled every 5 s, compared at
    both ends, and the other jobs that overlapped it; written next to the
    job's log as conditions.json, for timing harnesses to trust or discard
    a run"""

    def __init__(self, job_id, threads, exclusive):
        import threading
        self.job_id, self.threads, self.exclusive = job_id, threads, exclusive
        self.start = machine_now()
        self.temps, self.mhz = [], []
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.sample, daemon=True)
        self.thread.start()

    def sample(self):
        while not self.done.wait(5):
            t = cpu_temp()
            if t is not None:
                self.temps.append(t)
            freqs = [int(v) / 1000 for v in (read(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq")
                                             for c in self.threads) if v and v.isdigit()]
            if freqs:
                self.mhz.append(sum(freqs) / len(freqs))

    def stop(self, d, log):
        self.done.set()
        end = machine_now()
        t0, t1 = self.start["t"], end["t"]
        temps = [x for x in [self.start["cpu_temp"], *self.temps, end["cpu_temp"]] if x is not None]
        runs = {}
        for e in recent_events():
            i = e.get("id")
            if e.get("kind") == "started" and i != self.job_id:
                runs[i] = {"project": e.get("project"), "job": e.get("job"), "threads": e.get("threads"),
                           "exclusive": e.get("exclusive", []), "start": e.get("t"), "end": None}
            elif e.get("kind") in ("done", "refused") and i in runs:
                runs[i]["end"] = e.get("t")
        overlaps = [dict(r, start=round(r["start"] or 0, 1), end=round(r["end"], 1) if r["end"] else None)
                    for r in runs.values() if (r["start"] or 0) < t1 and (r["end"] is None or r["end"] > t0)]
        delta = (end["throttle_count"] - self.start["throttle_count"]
                 if end["throttle_count"] is not None and self.start["throttle_count"] is not None else None)
        machine = "machine" in self.exclusive
        # JSON keys are strings: the snapshot may have gone through a file
        norm = lambda x: x and dict(x, cores={int(k): v for k, v in (x.get("cores") or {}).items()})
        thr_s, thr_share = throttled(norm(self.start.get("throttle_time")), norm(end.get("throttle_time")),
                                     t1 - t0, self.threads)
        flags = []
        if thr_share is not None and thr_share > THROTTLE_SHARE:
            flags.append(f"throttled {thr_s:g} s ({thr_share * 100:.1f} % of the run)")
        elif thr_share is None and delta:
            flags.append(f"throttled ({delta} events)")   # a kernel without the time counters
        if self.start["governor"] != end["governor"]:
            flags.append(f"governor changed ({self.start['governor']} -> {end['governor']})")
        if self.start["turbo"] != end["turbo"]:
            flags.append(f"turbo changed ({self.start['turbo']} -> {end['turbo']})")
        if overlaps and not machine:
            flags.append(f"{len(overlaps)} other job(s) ran meanwhile")
        ts = getattr(self, "timing", None)
        if ts:
            if ts.get("cap_applied") is False:
                flags.append("timing profile not applied")
            if ts.get("started_hot"):
                flags.append(f"started hot ({ts.get('start_temp')} °C)")
        cond = {
            "seconds": round(t1 - t0, 1), "machine_held": machine, "threads": self.threads,
            "cpu_temp": {"max": max(temps) if temps else None,
                         "mean": round(sum(temps) / len(temps), 1) if temps else None},
            "cpu_mhz_mean": round(sum(self.mhz) / len(self.mhz)) if self.mhz else None,
            "throttle_events": delta, "throttle_seconds": thr_s, "throttle_share": thr_share,
            "governor": [self.start["governor"], end["governor"]], "turbo": [self.start["turbo"], end["turbo"]],
            "load_start": self.start["load"], "overlaps": overlaps,
            "timing": getattr(self, "timing", None),
            "comparable": not flags, "flags": flags,
        }
        (d / "conditions.json").write_text(json.dumps(cond, indent=1) + "\n")
        log("conditions: " + ("comparable" if not flags else "NOT comparable: " + "; ".join(flags))
            + (f"; cpu {cond['cpu_temp']['max']} C max" if temps else "")
            + (f"; throttled {thr_s:g} s ({thr_share * 100:.1f} %)"
               if thr_s and thr_share is not None and thr_share <= THROTTLE_SHARE else ""))
        return cond


# ---------------------------------------------------------------- scheduler


def read_state_json(name):
    try:
        return json.loads((STATE / name).read_text())
    except (OSError, ValueError):
        return {}


def queue_flag():
    """queue.json (mcu-admin queue, its only writer) while the queue is paused
    or closed: {mode, reason, since, until, by}; {} when it is open"""
    q = read_state_json("queue.json")
    return q if q.get("mode") in ("paused", "closed") else {}


def maintenance():
    """the reason the queue is closed (mcu-admin queue close), or None"""
    q = queue_flag()
    return q.get("reason") if q.get("mode") == "closed" else None


def gpu_present():
    """asked now, not from resources.json: the eGPU can go at any time"""
    return nvidia.listed()


def clock(t, fmt="%Y-%m-%d %H:%M"):
    """a time for a message, in the box's local time with its zone
    ("12:08 CEST"): /etc/localtime, or TZ when set"""
    return time.strftime(f"{fmt} %Z", time.localtime(t))


def gpu_flag():
    """gpu.json (mcu-helper): the admin's eject (ejecting, ejected) or a
    loss (lost: gone without an eject), with its note; {} when none, or when
    it dates from an earlier boot"""
    g = read_state_json("gpu.json")
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        boot = None
    return g if g.get("state") and g.get("boot") in (None, boot) else {}


def gpu_ejected():
    """the admin's eject or re-attach in progress (mcu-admin gpu): ejecting, ejected, attaching, or None"""
    state = gpu_flag().get("state")
    return state if state in ("ejecting", "ejected", "attaching") else None


def gpu_absent_reason():
    since = read_state_json("resources.json").get("gpu_since")
    when = f" since {clock(since, '%H:%M')}" if since else ""
    ejected = gpu_ejected()
    if ejected == "ejecting":
        return f"gpu unavailable on this box: the admin is ejecting the eGPU{when}"
    if ejected == "attaching":
        return f"gpu unavailable on this box: the admin is re-attaching the eGPU{when}; retry in a minute"
    if ejected:
        return f"gpu unavailable on this box: the admin ejected the eGPU{when}"
    flag = gpu_flag()
    if flag.get("state") == "lost":
        return f"gpu unavailable on this box: the eGPU was {flag.get('note') or 'lost'}{when}"
    return f"gpu unavailable on this box (eGPU unplugged or not responding{when})"


def hold():
    """why nothing starts: the queue is paused or closed (mcu-admin queue);
    queued jobs wait, running ones finish. The reason, or None"""
    return queue_flag().get("reason")


def queue_mode():
    return queue_flag().get("mode", "open")


def needs_nvidia(exclusive):
    """whether one of these resources is the hot-plug NVIDIA GPU"""
    declared = box_resources()
    return any(declared.get(r, {}).get("probe") == "nvidia" for r in exclusive)


def until_text(info):
    """the expected end of a pause or a maintenance, for a message: " (expected
    back 18:30 CEST); retry-after: 1200", or overdue, or nothing"""
    end = info.get("until")
    if not end:
        return ""
    left = int(end - time.time())
    if left > 0:
        return f" (expected back {clock(end, '%H:%M')}); retry-after: {left}"
    return f" (expected back {clock(end, '%H:%M')}, overdue)"


def maintenance_text():
    """why the box refuses new jobs, on one line: the reason, the expected end,
    a retry-after in seconds when the end is known"""
    m = queue_flag()
    return f"the box is in maintenance: {m.get('reason')}{until_text(m)}"


def held_text():
    """why a queued job doesn't start: the queue is paused or closed"""
    h = queue_flag()
    return f"held: {h['reason']}{until_text(h)}" if h.get("reason") else None


def check_available(exclusive):
    reason = maintenance()
    if reason:
        raise Unavailable(maintenance_text())
    if needs_nvidia(exclusive) and (gpu_ejected() or not gpu_present()):
        raise Unavailable(gpu_absent_reason())


def gpu_lost(exclusive, code, log):
    """a GPU job that failed because the GPU went away is not a test failure"""
    if code != 0 and needs_nvidia(exclusive) and not gpu_present():
        log("gpu lost during the run")
        return EX_TEMPFAIL, {"reason": "gpu lost during the run"}
    return code, {}


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM  # alive, another user's process
    return True


def uses_unit(e, project, unit):
    """does a queue entry use a project's unit (as mcu-helper's)"""
    uses = e.get("units")
    if unit.startswith("volume/"):
        return unit in (uses or [])
    if e["project"] != project:
        return False
    if unit in ("images", "results"):
        return True
    return uses is None or unit in uses


class Scheduler:
    """the queue and the allocations, in state.json under sched.lock"""

    def __init__(self):
        self.path = STATE / "state.json"

    def _locked(self, fn):
        with open(STATE / "sched.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            text = self.path.read_text() if self.path.exists() else ""
            state = json.loads(text) if text.strip() else {}
            state.setdefault("queue", [])
            state.setdefault("running", [])
            state.setdefault("moving", [])
            state.setdefault("deps", [])
            # a runner (or a unit move) that died leaves its entry behind:
            # free it
            for key in ("queue", "running", "moving", "deps"):
                state[key] = [e for e in state[key] if pid_alive(e["pid"])]
            result = fn(state)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=1))
            os.replace(tmp, self.path)
            return result

    def enqueue(self, entry):
        self._locked(lambda s: s["queue"].append(entry))
        event(kind="queued", id=entry["id"], project=entry["project"], job=entry["job"],
              cores=entry["cores"], exclusive=entry["exclusive"], memory=entry["memory"])

    def try_start(self, job_id, est):
        """the allocated threads if the job may start now, else None and
        why it waits (may_start, with est: the jobs' learned durations)"""
        threads = job_threads()
        pool = job_memory()
        held_reason = hold()
        max_wait = parse_duration(box_conf().get("backfill_max_wait", "30m"))

        def decide(state):
            queue, running = state["queue"], state["running"]
            me = next(e for e in queue if e["id"] == job_id)
            if held_reason:
                return None, f"the queue is {queue_mode()}: {held_reason}"
            # a unit the admin is moving: its jobs wait for the move
            for m in state["moving"]:
                if uses_unit(me, m["project"], m["unit"]):
                    return None, f"{m['project']}'s {m['unit']} is being moved to {m['pool']}"
            now = time.time()
            live = [run_slot(r, now, est) for r in running]
            order = queue_order(queue)
            why = may_start(me, order[:order.index(me)], live, now, est, len(threads), pool, max_wait)
            used = {c for r in running for c in r["threads"]}
            free = [c for c in threads if c not in used]
            if why is None and len(free) < me["cores"]:
                why = f"needs {me['cores']} threads, {len(free)} free"
            if why:
                return None, why
            alloc = free[:me["cores"]]
            queue.remove(me)
            running.append(dict(me, threads=alloc, started=now))
            return alloc, None

        return self._locked(decide)

    def wait_reason(self, job_id, reason):
        project = []
        def note(state):
            for e in state["queue"]:
                if e["id"] == job_id:
                    e["waiting"] = reason
                    project.append((e["project"], e["job"]))
        self._locked(note)
        if project:
            event(kind="waiting", id=job_id, project=project[0][0], job=project[0][1], reason=reason)

    def release(self, job_id):
        def drop(state):
            for key in ("queue", "running", "deps"):
                state[key] = [e for e in state[key] if e["id"] != job_id]
        self._locked(drop)

    def prioritize(self, project, job_id, level="high"):
        """job_id (queued or waiting for dependencies) takes the priority
        level (high, normal or low); no other job changes. False when it is
        neither queued nor waiting"""
        prio = None if level == "normal" else level
        found = []
        def move(state):
            for e in state["queue"] + state["deps"]:
                if e.get("project") == project and e["id"] == job_id:
                    e["priority"] = prio
                    found.append(e)
        self._locked(move)
        if not found:
            return False
        # the request too: a job waiting for dependencies queues from its request
        path = jobs_dir() / job_id / "request"
        try:
            req = json.loads(path.read_text())
            req.pop("priority", None)
            path.write_text(json.dumps(dict(req, **({"priority": prio} if prio else {}))) + "\n")
        except (OSError, ValueError):
            pass
        event(kind="priority", id=job_id, project=project, level=level)
        return True

    def block(self, entry):
        """a job waiting for its dependencies: holds nothing, listed apart"""
        self._locked(lambda s: s["deps"].append(entry))
        event(kind="deps", id=entry["id"], project=entry["project"], job=entry["job"],
              after=entry["after"], after_ok=entry["after_ok"])

    def waits_for(self, job_id, dep):
        def note(state):
            for e in state["deps"]:
                if e["id"] == job_id:
                    e["waiting_for"] = dep
        self._locked(note)


def status_code(text):
    """the exit code in a job's status ("exit 130 (cancelled)"), or None"""
    try:
        return int(text.split()[1])
    except (AttributeError, IndexError, ValueError):
        return None


def wait_deps(job_id, project, req, log, finish, sched):
    """a job submitted with after= / after_ok=: wait until those jobs ended,
    before anything else (its checkout, its image), so it sees what they
    left; holding nothing meanwhile. A dependency that did not succeed ends
    an after_ok job with EX_DEPENDENCY"""
    deps = [("after", i) for i in req.get("after", [])] + [("after_ok", i) for i in req.get("after_ok", [])]
    if not deps:
        return
    params = req.get("params", {})
    sched.block({"id": job_id, "pid": os.getpid(), "project": project, "job": req["name"],
                 "branch": " ".join(f"{k}={v}" for k, v in params.items()), "since": time.time(),
                 "ext": req.get("ext_job_id"), "priority": req.get("priority"),
                 "after": req.get("after", []), "after_ok": req.get("after_ok", []), "waiting_for": None})
    log("waiting for " + ", ".join(f"{i} ({kind})" for kind, i in deps))
    said = None
    while True:
        pending = []
        for kind, dep in deps:
            # x:<ext_job_id> is looked up at each check: a job resubmitted
            # under that name takes the place of the one before
            found = resolve_ref(dep)
            if found is None:
                log(f"dependency {dep}: no such job any more")
                if kind == "after_ok":
                    finish(EX_DEPENDENCY, reason=f"dependency {dep} gone")
                continue
            status = read(jobs_dir() / found / "status")
            if status is None:
                pending.append((kind, dep))
            elif kind == "after_ok" and status_code(status) != 0:
                log(f"dependency {dep} failed ({status}): not run (exit {EX_DEPENDENCY})")
                finish(EX_DEPENDENCY, reason=f"dependency {dep} failed")
        if not pending:
            break
        if pending[0] != said:
            sched.waits_for(job_id, f"{pending[0][1]} ({pending[0][0]})")
            said = pending[0]
        time.sleep(2)
    sched.release(job_id)
    log("dependencies met")


def allocate(sched, entry, log):
    """queue the job and wait for its threads; returns them. A job the box
    cannot serve now (maintenance, GPU absent) is refused at once rather
    than left waiting for something that may not come back"""
    check_available(entry["exclusive"])
    sched.enqueue(entry)
    last, said, est, est_at = None, 0, None, 0
    while True:
        # cheap checks while queued: the flag file and the collector's view
        if needs_nvidia(entry["exclusive"]) and read_state_json("resources.json").get("gpu") is False:
            sched.release(entry["id"])
            raise Unavailable(gpu_absent_reason())
        if time.time() - est_at >= 60:
            est, est_at = Estimates(recent_events()), time.time()
        alloc, why = sched.try_start(entry["id"], est)
        if alloc is not None:
            return alloc
        # where it stands, at each change of reason and every minute, so a
        # client shows the wait without asking (the times are estimates)
        if why != last or time.time() - said >= 60:
            where = queue_line(queue_view(read_state_json("state.json"), recent_events(), entry["project"]),
                               entry["id"])
            log(f"waiting: {why}" + (f"; {where}" if where else ""))
            said = time.time()
        if why != last:
            sched.wait_reason(entry["id"], why)
            last = why
        time.sleep(2)


# ---------------------------------------------------------------- jobs


def jobs_dir():
    return Path.home() / "jobs"


# the client's own name for a job (ext_job_id=), usable wherever the box's
# id is when written x:<ext_job_id>, so the two never mix up
EXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def ext_ref(ref):
    """the ext_job_id in a reference x:<ext_job_id>, or None"""
    return ref[2:] if (ref or "").startswith("x:") and EXT_RE.match(ref[2:]) else None


def ext_lookup(ext):
    """the job that carries an ext_job_id now (the latest submitted with it), or None"""
    try:
        target = os.readlink(jobs_dir() / ".ext" / ext)
    except OSError:
        return None
    job_id = Path(target).name
    return job_id if ID_RE.match(job_id) and (jobs_dir() / job_id).is_dir() else None


def resolve_ref(ref):
    """a job id, or x:<ext_job_id>: the id of the job carrying it now; None if neither"""
    if ext_ref(ref):
        return ext_lookup(ext_ref(ref))
    if ID_RE.match(ref or ""):
        return ref if (jobs_dir() / ref).is_dir() else None
    return None


def job_dir(job_id):
    if ext_ref(job_id):
        found = ext_lookup(ext_ref(job_id))
        if not found:
            raise Refused(f"no job with ext_job_id {ext_ref(job_id)}")
        job_id = found
    if not ID_RE.match(job_id or ""):
        raise Refused("bad job id (a job id, or x:<ext_job_id>)")
    d = jobs_dir() / job_id
    if not d.is_dir():
        raise Refused(f"no job {job_id}")
    return d


def state_of(d):
    if (d / "status").exists():
        return (d / "status").read_text().strip()
    return "running" if (d / "started").exists() else "queued"


def follow(d):
    log = d / "log"
    log.touch()
    said = None
    with open(log) as f:
        while True:
            chunk = f.read()
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            elif (d / "status").exists():
                break
            else:
                # a queued job: say once when the queue holds it, once when it lifts
                # compared on the reason and the end, not the countdown
                h = queue_flag() if not (d / "started").exists() else {}
                key = (h.get("reason"), h.get("until")) if h.get("reason") else None
                if key != said and (key or said):
                    print(f"bench: {held_text()}" if key else "bench: hold lifted", flush=True)
                    said = key
                time.sleep(1)
    status = (d / "status").read_text().strip()
    print(f"mcu-job: {d.name} {status}", flush=True)
    return {"exit 0": 0, f"exit {EX_TEMPFAIL}": EX_TEMPFAIL, f"exit {EX_CONFIG}": EX_CONFIG,
            f"exit {EX_CANCELLED} (cancelled)": EX_CANCELLED,
            f"exit {EX_DEPENDENCY}": EX_DEPENDENCY}.get(status, 1)


def benchfile_dir():
    return Path.home() / "benchfiles"


# one Benchfile per branch: `config benchfile <label>` stores a
# Benchfile of its own beside the default one, so agents on different
# branches no longer overwrite each other's jobs; a job uses its branch's
LABEL_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]{0,79}\Z")


def label_dir(label):
    return benchfile_dir() / "labels" / label.replace("/", "~")


def benchfile_path(label, version):
    """a stored Benchfile: the default one (label None) or a branch's"""
    return (benchfile_dir() if label is None else label_dir(label)) / f"v{version}.toml"


def live_benchfiles(conf):
    """every Benchfile in use: {label or None: version}"""
    out = {None: conf["benchfile"]} if conf.get("benchfile") else {}
    out.update(conf.get("benchfiles", {}))
    return out


def pick_benchfile(conf, params):
    """which Benchfile a job uses: benchfile=<label> (taken out of the
    parameters), else the Benchfile named like its branch= when there is
    one, else the default; (label or None, version)"""
    labels = conf.get("benchfiles", {})
    want = params.pop("benchfile", None)
    if want not in (None, "", "default"):
        if want not in labels:
            raise Refused(f"no Benchfile {want} (`config show` lists them under benchfiles)")
        return want, labels[want]
    if want is None and params.get("branch") in labels:
        return params["branch"], labels[params["branch"]]
    if not conf.get("benchfile"):
        raise Refused("no default Benchfile, and none for this branch: mcu-job config benchfile < Benchfile")
    return None, conf["benchfile"]


def load_benchfile(conf, label=None):
    return load_toml(benchfile_path(label, conf["benchfile"] if label is None else conf["benchfiles"][label]))


def benchfile_name(label, version):
    return f"Benchfile {label} v{version}" if label else f"Benchfile v{version}"


def substitute(text, params):
    """${name} and ${name:-default} from the run's parameters; an unknown
    name without a default is refused rather than left empty"""
    def one(m):
        name, default = m.group(1), m.group(3)
        if name in params:
            return params[name]
        if default is not None:
            return default
        raise Refused(f"parameter {name} is not set (run <job> {name}=...)")
    return re.sub(r"\$\{([a-z_][a-z0-9_]*)(:-([A-Za-z0-9._/@+-]*))?\}", one, text)


def podman_build(args, log, project=None, limits=()):
    """podman build, its output in the job's log when it fails (quiet, it
    prints nothing else); `limits`: the threads and memory the scheduler gave"""
    # build scratch (layer tarballs) on the project's tree, under its quota,
    # not in /var/tmp on the system disk
    env = dict(os.environ)
    sconf = Path.home() / ".config/containers/storage.conf"
    m = re.search(r'graphroot\s*=\s*"([^"]+)"', sconf.read_text()) if sconf.exists() else None
    if m and (Path(m.group(1)).parent / "tmp").is_dir():   # next to the images, on their pool
        env["TMPDIR"] = str(Path(m.group(1)).parent / "tmp")
    r = subprocess.run(["podman", "build", "-q", *limits] + args, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        for line in (r.stdout + r.stderr).strip().splitlines()[-40:]:
            log(f"  {line}")
        if project and re.search(r"quota exceeded|no space left", r.stdout + r.stderr, re.I):
            log("the project's disk quota is full: " + (disk_warning(project) or "see `config usage`"))
        raise subprocess.CalledProcessError(r.returncode, ["podman", "build"] + args)


def executable(path):
    """a script or an ELF binary: copied into the image as 755, anything else
    as 644 (an upload arrives without its mode)"""
    with open(path, "rb") as f:
        head = f.read(4)
    return head[:2] == b"#!" or head == b"\x7fELF"


BUILDS = {}   # tag -> how to build it again (an image dropped while its job waited)


def build_once(project, tag, build, log):
    """build the image unless it exists, one build per tag: the jobs that
    find it missing at the same time wait for the first one instead of all
    building it (each build costs the image's size in temporary layers);
    then drop the project's older images"""
    BUILDS[tag] = build
    if subprocess.run(["podman", "image", "exists", tag], capture_output=True).returncode == 0:
        return
    locks = Path.home() / ".bench-locks"
    locks.mkdir(exist_ok=True)
    # images placed by the box before the first build
    sconf = Path.home() / ".config/containers/storage.conf"
    store = Path.home() / ".local/share/containers/storage"
    if not sconf.exists() and not (store.exists() and any(store.iterdir())):
        r = subprocess.run(HELPER + ["place-unit", "images"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            sconf.parent.mkdir(parents=True, exist_ok=True)
            sconf.write_text(f'[storage]\ndriver = "overlay"\ngraphroot = "{r.stdout.strip()}"\n')
    with open(locks / f"build-{tag.rsplit(':', 1)[1]}.lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(f"waiting for another job's build of {tag}")
            fcntl.flock(f, fcntl.LOCK_EX)
        if subprocess.run(["podman", "image", "exists", tag], capture_output=True).returncode == 0:
            log(f"image {tag} built by another job")
            return
        scheduled_build(project, tag, build, log)
    prune_images(project, keep={tag} | kept_tags(project), log=log)


def scheduled_build(project, tag, build, log):
    """an image build is a job of the box like any other: queued, given its
    own threads and a memory cap, never beside a job holding the machine,
    shown on the dashboard, in health and in other jobs' conditions"""
    env = box_conf().get("envelope", {})
    cores = max(1, min(int(env.get("build_cores", 4)), len(job_threads())))
    cap = parse_size(env.get("memory", "8g"))
    need = min(max(int(learned_memory(project, "image build") * 1.1), 2 * 2**30), cap)
    sched = Scheduler()
    bid = f"{time.strftime('%Y%m%d-%H%M%S')}-b{secrets.token_hex(2)[:3]}"
    entry = {"id": bid, "pid": os.getpid(), "project": project, "job": "image build",
             "branch": tag.rsplit(":", 1)[1], "cores": cores, "exclusive": [], "memory": need,
             "max_jobs": 99, "queued": time.time(), "build": True, "units": ["images"], "subs": [], "honors_moves": True}
    log(f"image build {tag} queued as {bid} ({cores} threads)")
    threads = allocate(sched, entry, log)
    t0 = time.time()
    event(kind="started", id=bid, project=project, job="image build", threads=threads, exclusive=[],
          build=True, tag=tag, memory=need)
    log(f"building image {tag} on threads {','.join(map(str, threads))}")
    code = 0
    try:
        # room first: the images no Benchfile in use needs, then the
        # pre-flight with what a build of this project is known to take
        # (evicting unused tmp storage, or exit 75 with the figures)
        prune_images(project, keep={tag} | kept_tags(project), log=log)
        preflight_disk(project, "image build", [], log, bid)
        used0 = {m: x["used"] for m, x in disk_media(project).items()}
        build(["--cpuset-cpus", ",".join(map(str, threads)), "--memory", str(env.get("memory", "8g")),
               "--jobs", "1"])
    except subprocess.CalledProcessError as e:
        code = e.returncode or 1
        raise
    except Unavailable:
        code = EX_TEMPFAIL
        raise
    finally:
        sched.release(bid)
        used1 = {m: x["used"] for m, x in disk_media(project).items()} if code == 0 else {}
        for m, u0 in (locals().get("used0") or {}).items():
            if m in used1:
                record_disk(project, "image build", used1[m] - u0, log, m)
        event(kind="done", id=bid, project=project, job="image build", status=code, build=True, tag=tag,
              seconds=round(time.time() - t0, 1))
        log(f"image build {'done' if code == 0 else 'failed'} in {time.time() - t0:.0f} s")


def prune_images(project, keep=None, log=print):
    """remove the project's images but those in `keep` (a set of tags); an image a running container
    uses is refused by podman and stays. Then the dangling layers"""
    out = subprocess.run(["podman", "images", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}"],
                         capture_output=True, text=True).stdout
    removed = 0
    for line in out.splitlines():
        name, _, iid = line.partition(" ")
        if name.startswith(f"localhost/mcu-{project}:") and name not in (keep or set()):
            if subprocess.run(["podman", "rmi", name], capture_output=True).returncode == 0:
                removed += 1
    subprocess.run(["podman", "image", "prune", "-f"], capture_output=True)
    if removed:
        log(f"removed {removed} older image(s) of {project}")
    return removed


def disk_use(project):
    """the project's quota and use, as the root timer last measured them
    (every 5 minutes): {used, quota} in bytes, or {}"""
    return read_state_json("usage.json").get(project, {})


# ---------------------------------------------------------------- disk room

def disk_now(project):
    """the project's use and quota right now, {used, quota}, or {}"""
    r = subprocess.run(HELPER + ["disk-now"], capture_output=True, text=True)
    try:
        return json.loads(r.stdout) if r.returncode == 0 else {}
    except ValueError:
        return {}


MEDIA = ("ssd", "hdd")


def disk_media(project):
    """the project's use per pool type right now: {media: {used, quota,
    mounts}}; {} when the helper cannot tell"""
    u = disk_now(project)
    if u.get("media"):
        return u["media"]
    return {"ssd": {"used": u["used"], "quota": u.get("quota", 0), "mounts": ["/"]}} if "used" in u else {}


def media_of(path, per):
    """the pool type a path lives on, from the mounts disk_media lists"""
    try:
        dev = os.stat(path).st_dev
        for media, x in per.items():
            if any(os.stat(m).st_dev == dev for m in x.get("mounts", [])):
                return media
    except OSError:
        pass
    return "ssd"


def disk_key(project, job, media):
    return f"{project}/{job}" + ("" if media == "ssd" else f"@{media}")


def learned_disk(project, job, media="ssd"):
    """the most a run of this job has grown the project's disk on a pool
    type (disk.json, kept by the runners like memory.json)"""
    path = STATE / "disk.json"
    data = json.loads(path.read_text()) if path.exists() and path.stat().st_size else {}
    return data.get(disk_key(project, job, media), 0)


def _disk_json(change):
    with open(STATE / "sched.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = STATE / "disk.json"
        data = json.loads(path.read_text()) if path.exists() and path.stat().st_size else {}
        if not change(data):
            return False
        tmp = path.with_suffix(f".{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=1))
        os.chmod(tmp, 0o664)
        os.replace(tmp, path)
        return True


def record_disk(project, job, growth, log, media="ssd"):
    key = disk_key(project, job, media)

    def change(data):
        if growth <= data.get(key, 0):
            return False
        data[key] = growth
        return True
    if _disk_json(change):
        log(f"disk need learned{'' if media == 'ssd' else ' on ' + media}: {growth / 2**30:.2f} GB")


def forget_disk(project, jobs):
    """the learned needs of these jobs go (their storage moved pool type)"""
    keys = {disk_key(project, j, m) for j in jobs for m in MEDIA}
    return _disk_json(lambda data: bool([data.pop(k) for k in list(data) if k in keys]))


class DiskWatch:
    """the project's disk use per pool type while a job runs, every 15 s:
    its peak minus its use at the start is what this job needs there,
    learned for its pre-flight"""

    def __init__(self, project):
        import threading
        self.project = project
        self.start = {m: x["used"] for m, x in disk_media(project).items()}
        self.peak = dict(self.start)
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.sample, daemon=True)
        self.thread.start()

    def take(self):
        for m, x in disk_media(self.project).items():
            if m in self.peak:
                self.peak[m] = max(self.peak[m], x["used"])

    def sample(self):
        while not self.done.wait(15):
            self.take()

    def stop(self, job, log):
        self.done.set()
        self.take()
        for m, start in self.start.items():
            record_disk(self.project, job, self.peak[m] - start, log, m)


def tmp_subs(project, protect=(), keep=()):
    """the project's evictable storage: the subs of its tmp storages (one per
    branch or parameter), least recently used first, as (mtime, name, path);
    never a sub a queued or running job of the project mounts (protect), nor
    one listed in keep, nor a storage without the tmp mark, nor a seed (what
    new subs start from: evicting it makes every new branch start cold)"""
    home = Path.home() / "cache"
    seeds = seed_subs(project)
    out = []
    for c in sorted(home.iterdir()) if home.exists() else []:
        if c.name == ".git" or not c.is_dir() or not (c / ".bench-tmp").exists() or not (c / ".bench-per").exists():
            continue
        for sub in c.iterdir():
            name = f"{c.name}/{sub.name}"
            if sub.is_dir() and not sub.is_symlink() and name not in protect and name not in keep \
                    and sub.name not in keep and name not in seeds:
                out.append((sub.stat().st_mtime, name, sub))
    return sorted(out)


def seed_subs(project):
    """the subs that the Benchfiles in use name as seeds: {"target/main"}"""
    out = set()
    try:
        conf = project_conf(project)
        for label, version in live_benchfiles(conf).items():
            for name, spec in storage_decls(load_toml(benchfile_path(label, version))).items():
                if spec.get("seed"):
                    out.add(f"{name}/{re.sub(r'[^A-Za-z0-9._-]', '_', str(spec['seed']))}")
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError):
        pass
    return out


def in_use_subs(project, but=None):
    """the storage subs the project's queued and running jobs mount"""
    sched = read_state_json("state.json")
    return {x for e in sched.get("running", []) + sched.get("queue", [])
            if e.get("project") == project and e.get("id") != but for x in e.get("subs", [])}


def evict(project, need, protect, log, job_id=None, media="ssd"):
    """free room for need bytes on a pool type by dropping the least
    recently used tmp subs that sit there; returns the room left after it"""
    per = disk_media(project)
    x = per.get(media, {})
    room = x.get("quota", 0) - x.get("used", 0)
    for mtime, name, path in tmp_subs(project, protect):
        if media_of(path, per) != media:
            continue
        if room >= need:
            break
        size = du(path)
        shutil.rmtree(path, ignore_errors=True)
        room += size
        when = clock(mtime)
        log(f"evicted storage {name} ({size / 2**30:.2f} GB, unused since {when}) to make room")
        event(kind="evicted", project=project, id=job_id, storage=name, bytes=size, unused_since=mtime)
    return room


def preflight_disk(project, job, own_subs, log, job_id):
    """before a job's container: is there room, on each pool type, for what
    this job is known to need there? If not, drop the images nothing needs,
    then evict tmp storage on that pool type; if that is not enough, exit 75
    with the figures rather than fail midway"""
    # what a seed copied for this run is the bulk of that growth, done
    # already (and counted in the quota): it comes off the need
    needs = {m: max(int(learned_disk(project, job, m) * 1.1) - SEEDED.get(m, 0), 0) for m in MEDIA}
    if not any(needs.values()):
        return
    per = disk_media(project)
    pruned = False
    for m in MEDIA:
        need, x = needs[m], per.get(m)
        if need <= 0 or not x or not x.get("quota"):
            continue
        room = x["quota"] - x["used"]
        if room >= need:
            continue
        # the images nothing needs now go first: they rebuild in minutes
        if not pruned:
            pruned = True
            if prune_images(project, keep=kept_tags(project), log=log):
                per = disk_media(project)
                x = per.get(m, x)
                room = x["quota"] - x["used"]
                if room >= need:
                    continue
        protect = in_use_subs(project, but=job_id) | set(own_subs)
        here = [path for _, _, path in tmp_subs(project, protect) if media_of(path, per) == m]
        # nothing goes when even all of it would not make room
        if room + sum(du(path) for path in here) >= need:
            log(f"needs about {need / 2**30:.1f} GB on {m}, {max(room, 0) / 2**30:.1f} GB left in the quota: "
                "evicting tmp storage")
            room = evict(project, need, protect, log, job_id, m)
        if room < need:
            raise Unavailable(f"needs about {need / 2**30:.1f} GB on {m}, {max(room, 0) / 2**30:.1f} GB left in your "
                              f"{x['quota'] / 2**30:.0f} GB {m} quota, and evicting your unused tmp storage there "
                              "cannot make the rest; `storage list` shows what holds it (`storage rm`, or ask the "
                              "admin for more)")


def enospc_note(project, d, log, rights=None):
    """a run that still ran out of room: where the quota went. "No space
    left on device", or a SIGBUS (a write into a mapped file, as lld writes
    its output, that finds no room): a full quota, or the container's
    memory limit filled by that write's page cache"""
    tail = b""
    # the job's log, and its services' (a database that filled the quota)
    for path in [d / "log"] + sorted((d / "out" / "services").glob("*.log")):
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                f.seek(max(f.tell() - 200_000, 0))
                tail += f.read()
        except OSError:
            continue
    enospc = b"No space left on device" in tail
    sigbus = b"signal 7 [Bus error]" in tail or b"SIGBUS" in tail or b"Bus error (core dumped)" in tail
    if not (enospc or sigbus):
        return
    per = disk_media(project)
    full = [(m, x) for m, x in per.items() if x.get("quota") and x["used"] >= 0.95 * x["quota"]]
    if sigbus and not enospc and not full:
        limit = parse_size(rights["memory"]) if rights else None
        log("a SIGBUS, with no quota full: the likely cause is the container's memory limit"
            + (f" ({limit / 2**30:.1f} GB)" if limit else "")
            + " filled by the page cache of a large write through a mapped file (lld's output); "
            "it usually passes on the next run, or declare more `memory`")
        return
    home = Path.home() / "cache"
    sizes = sorted(((du(c), c.name) for c in home.iterdir() if c.is_dir() and c.name != ".git"), reverse=True)[:5] \
        if home.exists() else []
    where = ", ".join(f"{m} {x['used'] / 2**30:.1f} of {x['quota'] / 2**30:.0f} GB" for m, x in (full or per.items()))
    log(f"out of disk ({where})" + (": a SIGBUS while writing a mapped file is a full quota" if sigbus and not enospc else "")
        + "; largest storage: " + ", ".join(f"{n} {b / 2**30:.1f} GB" for b, n in sizes)
        + " (`storage list`, `storage rm`, `storage prune`)")


def purge_notice(project):
    """what the admin freed since the project's last good job, or None"""
    pg = read_state_json(f"projects/{project}.json").get("purged")
    if not pg:
        return None
    when = clock(pg["at"])
    what = {"purge": "your key, settings and Benchfile remain; everything else is gone",
            "move": "your storage and results are kept; the next job rebuilds your image"}.get(
        pg["level"], "the next job rebuilds them, expect a longer first run")
    verb = {"purge": "purged", "move": "moved"}.get(pg["level"], "reclaimed")
    return (f"the admin {verb} your {pg['parts']} on {when}"
            + (f" ({pg['reason']})" if pg.get("reason") else "") + f": {what}")


def disk_warning(project):
    """a line for the agent when its quota is nearly full, else None"""
    u = disk_use(project)
    per = u.get("media") or {"ssd": {"used": u.get("used", 0), "quota": u.get("quota", 0)}}
    lines = []
    for media, x in per.items():
        if not x.get("quota"):
            continue
        share = x["used"] / x["quota"]
        if share < 0.8:
            continue
        what = "disk" if media == "ssd" and len(per) == 1 else f"{media} disk"
        lines.append(f"{what} {x['used'] / 2**30:.1f} of {x['quota'] / 2**30:.1f} GB used ({share:.0%})"
                     + (" -- full: jobs will fail to build or write" if share >= 0.97 else ""))
    if not lines:
        return None
    return "; ".join(lines) + ": see `config usage`, free with `config clean images|cache <name>|src`"


def image_spec(project, manifest):
    """a Benchfile's image: its Containerfile text, its hash, its tag"""
    spec = manifest.get("image", {})
    if "from" not in spec:
        raise Refused("the Benchfile's [image] has no `from`")
    lines = [f"FROM {spec['from']}"]
    for k, v in spec.get("env", {}).items():
        lines.append(f"ENV {k}={json.dumps(str(v))}")
    files = benchfile_dir() / "files"
    for name, dest in spec.get("copy", {}).items():
        if not FILE_RE.match(name) or not (files / name).exists():
            raise Refused(f"[image] copy: no uploaded file {name} (mcu-job config file {name} < it)")
        lines.append(f"COPY --chmod={755 if executable(files / name) else 644} {name} {dest}")
    for cmd in spec.get("run", []):
        lines.append(f"RUN {cmd}")
    text = "\n".join(lines) + "\n"
    h = hashlib.sha256(text.encode())
    for name in sorted(spec.get("copy", {})):
        h.update((files / name).read_bytes())
    return text, h, f"localhost/mcu-{project}:{h.hexdigest()[:12]}"


IMAGE_KEEP_SECONDS = 86400   # a branch Benchfile's image stays this long after its last use


def kept_tags(project, why=None):
    """the images a prune keeps: the one each queued or running job runs,
    the default Benchfile's, those of the
    Benchfiles the project's queued or running jobs use (and the builds in
    progress), and those of branch Benchfiles used within the last day; an
    older branch keeps its Benchfile, its image is rebuilt when it runs.
    `why` (a dict), when given, gets tag -> the reason it stays"""
    why = why if why is not None else {}
    try:
        conf = project_conf(project)
    except (OSError, ValueError):
        return set()
    labels = live_benchfiles(conf)
    sched = read_state_json("state.json")
    mine = [r for r in sched.get("running", []) + sched.get("queue", []) if r.get("project") == project]
    for r in mine:
        if r.get("build"):
            why.setdefault(f"localhost/mcu-{project}:{r['branch']}", "being built")
        elif r.get("image"):
            # the image a queued or running job runs, whichever Benchfile
            # version is live now (an upload may have replaced its own)
            why.setdefault(r["image"], "a job queued or running uses it")
            for t in r.get("images", []):
                why.setdefault(t, "a service of a job queued or running uses it")
    busy = {r.get("benchfile") for r in mine if not r.get("build")}
    for label, version in labels.items():
        if label is None:
            reason = "the default Benchfile"
        elif label in busy:
            reason = f"Benchfile {label}: a job queued or running"
        else:
            d = label_dir(label)
            stamps = [f.stat().st_mtime for f in (d / ".used", d / f"v{version}.toml") if f.exists()]
            if not stamps or time.time() - max(stamps) > IMAGE_KEEP_SECONDS:
                continue
            reason = f"Benchfile {label}: used within a day"
        try:
            manifest = load_toml(benchfile_path(label, version))
            why.setdefault(image_spec(project, manifest)[2], reason)
            for sname, svc in (manifest.get("services") or {}).items():
                if isinstance(svc, dict) and isinstance(svc.get("image"), dict):
                    why.setdefault(image_spec(project, {"image": svc["image"]})[2], f"{reason} (service {sname})")
        except (OSError, ValueError, KeyError, Refused, tomllib.TOMLDecodeError):
            pass
    return set(why)


def image_v5(project, manifest, log):
    """the project's image, generated from the Benchfile's [image]: FROM,
    ENV, COPY of uploaded files, RUN; rebuilt only when that changes"""
    spec = manifest.get("image", {})
    files = benchfile_dir() / "files"
    text, h, tag = image_spec(project, manifest)

    def build(limits):
        # a context per tag: two different builds never share a directory
        ctx = benchfile_dir() / f"build-{h.hexdigest()[:12]}"
        shutil.rmtree(ctx, ignore_errors=True)
        ctx.mkdir(parents=True)
        (ctx / "Containerfile").write_text(text)
        for name in spec.get("copy", {}):
            shutil.copy2(files / name, ctx / name)
        try:
            podman_build(["-t", tag, str(ctx)], log, project, limits)
        finally:
            shutil.rmtree(ctx, ignore_errors=True)
    build_once(project, tag, build, log)
    return tag


# git on the host in a tree jobs also write: no hooks, no fsmonitor, whatever
# the tree says
SAFE_GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]


def checkout(project, conf, branch, log):
    """update the project's checkout to the branch: the only step that
    always has network, and it runs on the host as the project user"""
    src = Path.home() / "src"
    if not (src / ".git").exists():
        subprocess.run([*SAFE_GIT, "clone", "--quiet", conf["repo"], str(src)], check=True)
    ref = f"refs/remotes/origin/{branch}"
    subprocess.run([*SAFE_GIT, "-C", str(src), "fetch", "--quiet", "origin",
                    f"+refs/heads/{branch}:{ref}"], check=True)
    subprocess.run([*SAFE_GIT, "-C", str(src), "checkout", "--quiet", "--force", "--detach", ref], check=True)
    subprocess.run([*SAFE_GIT, "-C", str(src), "submodule", "update", "--quiet", "--init", "--recursive"],
                   check=True)
    head = subprocess.run([*SAFE_GIT, "-C", str(src), "log", "--oneline", "-1"],
                          capture_output=True, text=True).stdout.strip()
    log(f"at {head}")
    return src


def image(project, src, manifest, log):
    """the project's image, built from its own Containerfile; rebuilt only
    when the Containerfile's directory changes"""
    cf = src / manifest.get("containerfile", "tools/bench/Containerfile")
    ctx = cf.parent
    h = hashlib.sha256()
    for p in sorted(ctx.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(ctx)).encode())
            h.update(p.read_bytes())
    tag = f"localhost/mcu-{project}:{h.hexdigest()[:12]}"
    build_once(project, tag, lambda limits: podman_build(["-t", tag, "-f", str(cf), str(ctx)], log, project, limits), log)
    return tag


def podman_args(rights, threads, src, out, workdir="/src", mount="/src", image=None, pod=None):
    # --pull=never: the image is the box's own build; a missing one is never
    # looked up on a registry (localhost/... would be tried over https)
    # sandbox = true: no --init, its host binary (bin_t) is not executable
    # under the SELinux type such a job gets (podman refuses /run/podman-init)
    args = ["podman", "run", "--rm", *([] if rights.get("sandbox") else ["--init"]), "--pull=never",
            *(["--label", f"mcu.job={CURRENT_JOB}"] if CURRENT_JOB else []),
            "--cpuset-cpus", ",".join(map(str, threads)), "--memory", rights["memory"],
            "--pids-limit", "8192", "-w", workdir,
            "-v", f"{src}:{mount}", "-v", f"{out}:/out"]
    if src == Path.home() / "src" and (src / ".git").is_dir():
        # repository mode: the host runs git in this checkout as the project
        # user, so the job must not write its .git (hooks, config)
        args += ["-v", f"{src}/.git:{mount}/.git:ro"]
    if pod:
        args += ["--pod", pod]     # the pod's network: none, or the job's
    elif rights["network"] == "none":
        args += ["--network=none"]
    # sandbox = true: the job runs sandboxes of its own (bwrap: GTK's glycin
    # image loaders, Flatpak-style tools), which mount a fresh /proc. The
    # SELinux type for containers that run containers (still confined, MCS
    # kept) and /proc unmasked allow it; the container stays rootless
    if rights.get("sandbox"):
        args += ["--security-opt", "label=type:container_engine_t", "--security-opt", "systempaths=unconfined"]
    # the job's resources, resolved now (a node number can change between two
    # jobs: the eGPU comes and goes): only their own devices reach the
    # container, never all of /dev/dri. A GPU through CDI needs the host's
    # SELinux boolean container_use_devices
    declared = box_resources()
    granted = {}
    for name, select in rights.get("resources", {}).items():
        r = resolve(name, declared[name], select)
        args += r["devices"]
        granted[name] = r
    display = rights.get("display")
    facts = {"resources": {}, "cpu": cpu_facts(threads), "memory_limit": rights["memory"],
             "kernel": os.uname().release, "image": image,
             "machine": "machine" in rights.get("exclusive", []),
             "timing": rights.get("timing_state")}
    for name, r in granted.items():
        facts["resources"][name] = dict(r["info"], display=name == display)
    if display and granted.get(display, {}).get("render"):
        args += ["-e", f"WLR_RENDER_DRM_DEVICE={granted[display]['render']}"]
    # what the job got, readable inside it ($MCU_RESOURCES; $BENCH_RESOURCES too, for one release), for the
    # results to record the hardware they were measured on
    facts_file = Path(out).parent / "resources.json"
    facts_file.write_text(json.dumps(facts, indent=1) + "\n")
    # /run/bench/resources.json: the older path, for one release
    args += ["-v", f"{facts_file}:/run/mcu/resources.json:ro", "-e", "MCU_RESOURCES=/run/mcu/resources.json",
             "-v", f"{facts_file}:/run/bench/resources.json:ro", "-e", "BENCH_RESOURCES=/run/mcu/resources.json"]
    args += mount_args(rights["mounts"])
    args += place_args(rights, src)
    # a login-session runtime dir, as Wayland compositors and clients want:
    # empty, private, on a tmpfs that goes with the container (the job's
    # own env.XDG_RUNTIME_DIR wins)
    if "XDG_RUNTIME_DIR" not in rights.get("env", {}):
        args += ["--tmpfs", "/run/mcu-xdg:rw,mode=0700,size=64m", "-e", "XDG_RUNTIME_DIR=/run/mcu-xdg"]
    # the job's own thread count, for tools that size themselves on it
    args += ["-e", f"MCU_CORES={len(threads)}", "-e", f"BENCH_CORES={len(threads)}"]
    for k, v in rights["env"].items():
        args += ["-e", f"{k}={v}"]
    return args


def mount_args(mounts):
    """podman's -v for storage (rw) and shared volumes (read-only)"""
    args = []
    if any(kind == "volume" for kind, _, _ in mounts):
        # rootless podman drops supplementary groups; readers reach a shared
        # volume through its vol-<name> group
        args += ["--group-add", "keep-groups"]
    for kind, name, target in mounts:
        if kind == "cache":
            path = Path.home() / "cache" / name
            path.mkdir(parents=True, exist_ok=True)
            args += ["-v", f"{path}:{target}"]
        else:
            current = VOLUMES / name / "current"
            if not current.exists():
                raise Refused(f"volume {name} has no version yet (mcu-job provision <branch> {name})")
            # read-only for everyone outside provisioning, the owner included;
            # the resolved version stays mounted even if `current` moves
            args += ["-v", f"{current.resolve()}:{target}:ro"]
    return args


def place_args(rights, src):
    """a checkout or pushed code also seen at its fixed path (`at`)"""
    return [a for cname, at in rights.get("places", []) if (Path(src) / cname).exists()
            for a in ("-v", f"{Path(src) / cname}:{at}")]


# ---------------------------------------------------------------- services

def pod_name(job_id):
    return f"mcu-{job_id}"


KILLED_PODS = set()   # pods whose processes this runner killed: their record waits for the reap


class ServiceFailed(Exception):
    pass


def start_services(job_id, rights, threads, work, out, log):
    """the job's pod and its services in it, before the job: each started
    and, when it says how, checked ready (`ready`, retried every second up
    to its ready_timeout). The pod's network is the job's (none: loopback
    only). Raises ServiceFailed; the caller tears the pod down whatever
    happens (stop_services)"""
    pod = pod_name(job_id)
    net = ["--network", "none"] if rights["network"] == "none" else []
    r = subprocess.run(["podman", "pod", "create", "--name", pod, "--label", f"mcu.job={job_id}", *net],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise ServiceFailed(f"pod: {r.stderr.strip()[-300:]}")
    for svc in rights["services"]:
        name = f"{pod}-{svc['name']}"
        # read-only rootfs: a service writes only to its storage (the
        # project's quota), /work and /out (the job's, same quota) and its
        # tmpfs (/run, /tmp, /var/tmp and its own, counted in its memory)
        args = ["podman", "run", "-d", "--name", name, "--pod", pod, "--label", f"mcu.job={job_id}",
                "--label", f"mcu.service={svc['name']}", "--init", "--pull=never", "--read-only",
                "--log-driver", "k8s-file", "--cpuset-cpus", ",".join(map(str, threads)),
                "--memory", str(svc["memory"]), "--pids-limit", "8192",
                "-v", f"{work}:/work", "-v", f"{out}:/out", "--tmpfs", "/run/mcu-xdg:rw,mode=0700,size=64m"]
        args += mount_args(svc["mounts"]) + place_args(rights, work)
        args += [a for t in svc["tmpfs"] for a in ("--tmpfs", f"{t}:rw")]
        args += [a for k, v in svc["env"].items() for a in ("-e", f"{k}={v}")]
        args += ["-e", f"MCU_CORES={len(threads)}", "-e", f"BENCH_CORES={len(threads)}"]
        if svc["cwd"]:
            args += ["-w", svc["cwd"]]
        args += [svc["tag"]] + (["sh", "-c", svc["command"]] if svc["command"] else [])
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0:
            raise ServiceFailed(f"service {svc['name']} did not start: {r.stderr.strip()[-300:]}")
        log(f"service {svc['name']} started ({svc['about']}; {svc['memory'] / 2**30:.1f} GB)")
    for svc in rights["services"]:
        name = f"{pod}-{svc['name']}"
        deadline = time.time() + svc["ready_timeout"]
        while True:
            state = subprocess.run(["podman", "inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", name],
                                   capture_output=True, text=True).stdout.split()
            if not state or state[0] != "true":
                raise ServiceFailed(f"service {svc['name']} exited (code {state[1] if len(state) > 1 else '?'}) "
                                    f"before the job started; its log is in /out/services/{svc['name']}.log")
            if not svc["ready"]:
                break
            if subprocess.run(["podman", "exec", name, "sh", "-c", svc["ready"]],
                              capture_output=True).returncode == 0:
                log(f"service {svc['name']} ready")
                break
            if time.time() > deadline:
                raise ServiceFailed(f"service {svc['name']} not ready after {svc['ready_timeout']} s "
                                    f"({svc['ready']}); its log is in /out/services/{svc['name']}.log")
            time.sleep(1)


def stop_services(job_id, out, log):
    """the job's pod gone, its services with it: stopped, their logs kept in
    /out/services/<name>.log (the job's results), and a service that died
    during the job said. Called whatever ended the job; idempotent. A pod
    that would not go is a loud error, and the next job of the project (or
    the daily housekeeping) removes it (reap_services)"""
    pod = pod_name(job_id)
    if pod in KILLED_PODS or subprocess.run(["podman", "pod", "exists", pod], capture_output=True).returncode != 0:
        return
    listing = subprocess.run(["podman", "ps", "-a", "--filter", f"pod={pod}", "--filter", "label=mcu.service",
                              "--format", '{{index .Labels "mcu.service"}} {{.ID}}'],
                             capture_output=True, text=True).stdout.split("\n")
    services = [line.partition(" ")[::2] for line in listing if " " in line]
    for svc, cid in services:
        state = subprocess.run(["podman", "inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", cid],
                               capture_output=True, text=True).stdout.split()
        if state and state[0] != "true":
            log(f"service {svc} had already exited (code {state[1] if len(state) > 1 else '?'})")
    # stopped first (SIGTERM, SIGKILL after 10 s), so their logs end with
    # their shutdown
    subprocess.run(["podman", "pod", "stop", "-t", "10", pod], capture_output=True)
    for svc, cid in services:
        try:
            (out / "services").mkdir(exist_ok=True)
            with open(out / "services" / f"{svc}.log", "wb") as f:
                subprocess.run(["podman", "logs", cid], stdout=f, stderr=subprocess.STDOUT, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"service {svc}: its log could not be kept ({e})")
    subprocess.run(["podman", "pod", "rm", "-f", "-t", "10", pod], capture_output=True, text=True)
    if subprocess.run(["podman", "pod", "exists", pod], capture_output=True).returncode == 0:
        # podman could not (a full quota: it writes its own state there):
        # every process of the pod killed through its cgroup, which needs
        # no disk; the pod's record goes at the next job or the housekeeping
        cg = subprocess.run(["podman", "pod", "inspect", "--format", "{{.CgroupPath}}", pod],
                            capture_output=True, text=True).stdout.strip()
        try:
            if not cg:
                raise OSError("no cgroup found")
            Path("/sys/fs/cgroup", cg.lstrip("/"), "cgroup.kill").write_text("1")
            KILLED_PODS.add(pod)
            log("the services' pod could not be removed by podman (disk full?): its processes were killed "
                "through its cgroup; the pod's record is removed by the next job of the project or the daily housekeeping")
            event(kind="services", id=job_id, project=project_name(), killed="cgroup")
        except OSError as e:
            log(f"ERROR: the services' pod {pod} could not be removed nor killed ({e}); the next job of the "
                "project or the daily housekeeping removes it")
            event(kind="services", id=job_id, project=project_name(), error="pod not removed")
    else:
        log(f"services stopped and removed: {', '.join(sorted(svc for svc, _ in services)) or 'none'}")


def reap_services(log):
    """the project's pods whose job already ended (a runner killed before
    its teardown): removed, at each new job of the project and daily"""
    out = subprocess.run(["podman", "pod", "ps", "--filter", "label=mcu.job",
                          "--format", '{{.Name}} {{index .Labels "mcu.job"}}'],
                         capture_output=True, text=True).stdout
    n = 0
    for line in out.splitlines():
        pod, _, jid = line.partition(" ")
        d = jobs_dir() / jid
        if (jobs_dir() / "debug" / jid).exists():
            continue      # a debug shell's: removed when the shell ends
        if ID_RE.match(jid) and ((d / "status").exists() or not d.exists()):
            subprocess.run(["podman", "pod", "rm", "-f", "-t", "5", pod], capture_output=True)
            log(f"removed the services' pod left by job {jid}, which had ended")
            n += 1
    return n


def latest_names(job, params):
    """the names a successful run takes in jobs/.latest: the job's, and the
    job's with its parameters (sorted, values quoted: a branch has slashes),
    what `fetch latest` resolves; the project's view shows them"""
    names = [job]
    if params:
        names.append(job + "".join(f",{k}={quote(str(v), safe='')}" for k, v in sorted(params.items())))
    return names


def link_latest(d, req):
    """jobs/.latest/<name> -> the run, replaced atomically"""
    index = d.parent / ".latest"
    try:
        index.mkdir(exist_ok=True)
        for name in latest_names(req["name"], req.get("params") or {}):
            tmp = index / f".{name}.{d.name}"
            tmp.symlink_to(f"../{d.name}")
            os.replace(tmp, index / name)
        for link in index.iterdir():          # runs the retention removed
            if link.is_symlink() and not link.exists():
                link.unlink(missing_ok=True)
    except OSError:
        pass


def view_line(project):
    """the mount line of the project's read-only view (sftp on its key)"""
    return f"sshfs -o ro,IdentityFile=<your key> mcu-{project}@{socket.gethostname()}: <dir>"


def run_job(job_id):
    project = project_name()
    d = job_dir(job_id)
    req = json.loads((d / "request").read_text())
    logf = open(d / "log", "a", buffering=1)
    sched = Scheduler()
    t0 = time.time()
    global CURRENT_JOB
    CURRENT_JOB = job_id
    (d / "pid").write_text(f"{os.getpid()}\n")
    # room kept for the end: a job (or its service) that fills the quota
    # must not leave the job without a status; freed when it is written
    reserve = d / ".bench-reserve"
    try:
        reserve.write_bytes(b"\0" * 65536)
    except OSError:
        pass
    finishing = []

    def on_term(signum, frame):
        if not finishing:
            raise Cancelled()
    signal.signal(signal.SIGTERM, on_term)

    def log(msg):
        logf.write(f"bench: {msg}\n")

    def finish(code, **extra):
        finishing.append(code)
        stop_containers(job_id)     # whatever path ended the job, nothing of it keeps running
        stop_services(job_id, d / "out", log)
        sched.release(job_id)
        if code == 0:
            # a good job after a reclaim or purge: the notice has served
            path = STATE / "projects" / f"{project}.json"
            try:
                pconf = json.loads(path.read_text())
                if pconf.pop("purged", None):
                    path.write_text(json.dumps(pconf, indent=1) + "\n")
            except (OSError, ValueError):
                pass
        drop_work(d)  # v5's disposable /work
        reserve.unlink(missing_ok=True)
        (d / "status").write_text(f"exit {code}" + (" (cancelled)" if code == EX_CANCELLED else "") + "\n")
        if code == 0:
            link_latest(d, req)
        event(kind="done", id=job_id, project=project, job=req["name"], branch=req.get("branch"),
              params=req.get("params", {}), status=code, seconds=round(time.time() - t0, 1), **extra)
        sys.exit(0)

    try:
        wait_deps(job_id, project, req, log, finish, sched)
        stop_orphans(log)
        reap_services(log)
        conf = ceiling(project)
        if conf.get("benchfile"):
            return run_v5(project, conf, req, d, logf, log, finish, sched, t0)
        src = checkout(project, conf, req["branch"], log)
        manifest = load_toml(src / conf["manifest"])
        if req["kind"] == "provision":
            return provision(project, conf, manifest, src, req, d, logf, log, finish, sched)
        rights = grant(project, conf, manifest, req["name"])
        log(f"{req['name']}: {rights['cores']} threads, {rights['memory_need'] / 2**30:.1f} GB, "
            f"exclusive {rights['exclusive'] or 'none'}, "
            f"network {rights['network']}, timeout {rights['timeout']}s")
        tag = image(project, src, manifest, log)
        (d / "out").mkdir(exist_ok=True)
        # repository mode: every job of the project shares one checkout and
        # one build tree, so they run one at a time (until per-job
        # worktrees)
        threads = allocate(sched, {"id": job_id, "pid": os.getpid(), "project": project,
                                   "job": req["name"], "branch": req["branch"],
                                   "cores": rights["cores"], "exclusive": rights["exclusive"],
                                   "memory": rights["memory_need"], "timeout": rights["timeout"],
                                   "max_wait": rights["max_wait"], "max_jobs": 1, "queued": time.time()}, log)
        (d / "started").write_text(time.strftime("%FT%T%z") + "\n")
        log(f"started on threads {','.join(map(str, threads))}")
        event(kind="started", id=job_id, project=project, job=req["name"], threads=threads,
              exclusive=rights["exclusive"], memory=rights["memory_need"],
              memory_limit=parse_size(rights["memory"]))
        prefetch = manifest.get("prefetch")
        if prefetch and rights["network"] == "none":
            if NETWORK_LEVELS.index(conf["limits"]["network"]) < NETWORK_LEVELS.index("fetch"):
                raise Refused("the manifest's prefetch needs network; the box's envelope is none", topic="config")
            log("prefetch, with network")
            cmd = podman_args(dict(rights, network="full"), threads, src, d / "out") + [tag, "bash", "-c", prefetch]
            if subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT,
                              timeout=rights["timeout"]).returncode != 0:
                log("prefetch failed")
                finish(3)
        # the container's peak memory, from its own cgroup, for the next estimate
        measured = with_trace(rights["command"])
        ts = timing_start(rights, log) if rights.get("timing") else None
        rights = dict(rights, timing_state=ts)
        cmd = podman_args(rights, threads, src, d / "out", image=tag) + [tag, "bash", "-c", measured]
        watch = Conditions(d.name, threads, rights["exclusive"])
        watch.timing = ts
        try:
            code = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                  timeout=rights["timeout"]).returncode
        except subprocess.TimeoutExpired:
            log(f"timeout after {rights['timeout']}s: stopping the container")
            stop_containers(job_id)
            code = 124
        finally:
            timing_end(ts)
        cond = watch.stop(d, log)
        grow = memory_hit(d, code, rights, log)
        cost = record_cost(d, project, req["name"], threads, log, cond, rights, grow)
        code, why = gpu_lost(rights["exclusive"], code, log)
        finish(code, cost=cost, **why)
    except Cancelled:
        how = (d / "cancel").read_text().strip() if (d / "cancel").exists() else "SIGTERM"
        log(f"cancelled ({how}): stopping the container")
        stop_containers(job_id)
        finish(EX_CANCELLED, reason="cancelled")
    except Unavailable as e:
        log(f"unavailable: {e}")
        event(kind="refused", id=job_id, project=project, job=req["name"], reason=str(e), unavailable=True)
        finish(EX_TEMPFAIL)
    except Misconfigured as e:
        log(f"refused for good (exit {EX_CONFIG}): {e}")
        event(kind="refused", id=job_id, project=project, job=req["name"], reason=str(e), config=True)
        finish(EX_CONFIG)
    except Refused as e:
        log(f"refused: {e}")
        event(kind="refused", id=job_id, project=project, job=req["name"], reason=str(e))
        finish(2)
    except subprocess.CalledProcessError as e:
        log(f"failed: {' '.join(map(str, e.cmd))}")
        finish(3)
    except OSError as e:
        if e.errno != errno.ENOSPC:
            import traceback
            logf.write(traceback.format_exc())
            log("internal error in the mcu runner")
            finish(4)
        # the project's quota is full (a service may have filled it)
        log(f"out of disk: {e}")
        enospc_note(project, d, log)
        finish(EX_TEMPFAIL)
    except SystemExit:
        raise
    except BaseException:
        # a runner must always leave a status, or the job stays queued forever
        import traceback
        logf.write(traceback.format_exc())
        log("internal error in the mcu runner")
        finish(4)


def prepare_v5(project, conf, kind, name, params, log):
    """what a Benchfile job or provisioning needs before it queues: its
    rights (caches resolved for these parameters), command, image"""
    label, version = pick_benchfile(conf, params)
    manifest = load_toml(benchfile_path(label, version))
    if label:
        (label_dir(label) / ".used").touch()      # the last use, for the expiry of unused branch Benchfiles
    log(f"{benchfile_name(label, version)}, parameters {params or 'none'}")
    if kind == "provision":
        spec = manifest.get("volumes", {}).get(name)
        if not spec or "provision" not in spec:
            raise Refused(f"the Benchfile does not say how to provision {name}")
        job = {"command": spec["provision"], "cores": spec.get("cores", 2),
               "mounts": spec.get("mounts", {}), "timeout": spec.get("timeout", "1h"),
               "memory": spec.get("memory", conf["limits"]["memory"]), "cwd": spec.get("cwd", "/work")}
        rights = grant(project, conf, {**manifest, "jobs": {name: job}}, name)
        vol = volume_conf(name)
        if vol["owner"] != project:
            raise Refused(f"{project} does not own volume {name}")
    else:
        rights = grant(project, conf, manifest, name)
        job = manifest["jobs"][name]
    rights = resolve_caches(rights, manifest, params, log)
    declared = manifest.get("checkout", {})
    for cname, spec in declared.items():
        if not NAME_RE.match(cname) or not isinstance(spec, dict) or "repo" not in spec:
            raise Refused(f"[checkout.{cname}]: needs a repo")
    # a job may list the checkouts it needs (job.checkouts): the others are
    # neither fetched nor mounted, and their ${param}s are not needed
    skipped = []
    wanted = list(declared)
    if isinstance(job.get("checkouts"), list):
        unknown = [c for c in job["checkouts"] if c not in declared]
        if unknown:
            raise Refused(f"job {name}: checkouts {', '.join(map(str, unknown))} not in [checkout]")
        skipped = sorted(set(declared) - set(job["checkouts"]))
        wanted = job["checkouts"]
        if skipped:
            log(f"checkouts skipped (not in the job's list): {', '.join(skipped)}")
    checkouts = {}
    for cname in wanted:
        spec = declared[cname]
        repo = substitute(str(spec["repo"]), params)
        if not URL_RE.match(repo):
            raise Refused(f"[checkout.{cname}]: the repo must be an https URL")
        checkouts[cname] = {"repo": repo, "ref": substitute(str(spec.get("ref", "HEAD")), params),
                            "submodules": bool(spec.get("submodules", False))}
    # `at`: a checkout or pushed code also seen at a fixed path, so a build
    # cache shared by both always sees the same path
    places = []
    for table in ("checkout", "push"):
        for cname, spec in manifest.get(table, {}).items():
            at = spec.get("at") if isinstance(spec, dict) else None
            if at is None:
                continue
            at = os.path.normpath(str(at))
            if not NAME_RE.match(cname) or not at.startswith("/") or at == "/" or any(
                    at == r or at.startswith(r + "/") for r in RESERVED_PATHS):
                raise Refused(f"[{table}.{cname}] at = {spec['at']!r}: an absolute path outside "
                              f"{', '.join(RESERVED_PATHS)}")
            if not (table == "checkout" and cname in skipped):
                places.append((cname, at))
    rights = dict(rights, places=places)
    fetch = [substitute(c, params) for c in manifest.get("fetch", {}).get("run", [])]
    if (fetch or checkouts) and NETWORK_LEVELS.index(conf["limits"]["network"]) < NETWORK_LEVELS.index("fetch"):
        raise Refused("the Benchfile's [checkout] and [fetch] need network; the box's envelope is none", topic="config")
    tag = image_v5(project, manifest, log)
    # a service runs its own image (built like [image]), or the job's
    rights = dict(rights, services=[dict(svc, tag=image_v5(project, {"image": svc["image"]}, log) if svc["image"] else tag,
                                         command=substitute(svc["command"], params) if svc["command"] else None)
                                    for svc in rights.get("services", [])])
    return {"manifest": manifest, "rights": rights, "job": job,
            "command": substitute(rights["command"], params),
            "cwd": substitute(job.get("cwd", "/work"), params),
            "checkouts": checkouts, "fetch": fetch, "benchfile": (label, version),
            "tag": tag}


def job_units(rights):
    """the units a job uses: its storages, shared volumes, images, results"""
    out = ["images", "results"]
    for kind, name, _ in all_mounts(rights):
        out.append(f"storage/{name.partition('/')[0]}" if kind == "cache" else f"volume/{name}")
    return list(dict.fromkeys(out))


def job_media(manifest, job):
    """the pool types a job's storage mounts sit on: {"ssd", "hdd"}"""
    decls = storage_decls(manifest)
    out = set()
    for name in (manifest.get("jobs", {}).get(job, {}).get("mounts") or {}):
        spec = decls.get(str(name).split("/", 1)[0])
        if spec is not None:
            out.add(str(spec.get("media") or "ssd").rstrip("!"))
    return out


def storage_decls(manifest):
    """the project's named storage: [storage] (v6) and the older [cache],
    which is storage tagged tmp. {name: {about, tmp, lock, per, default}}"""
    out = {}
    for table, tmp in (("cache", True), ("storage", True)):
        for name, spec in manifest.get(table, {}).items():
            spec = {"about": spec} if isinstance(spec, str) else dict(spec)
            spec.setdefault("tmp", tmp and not spec.get("keep"))   # a kept storage is never evicted
            out[name] = spec
    return out


# when a storage ends: every storage says it, in [storage] or per
# mount. drop: at the job's end; until = "<param>": while the branch that
# parameter names exists on the remote; ttl: after that long unused; keep +
# max: kept, trimmed down to its cap. They combine, the first reached wins
LIFE_KEYS = ("drop", "until", "ttl", "keep", "max", "trim")
ABOUT_MAX = 120             # a job's about: one line, shown beside its name
DECLARE_BY = "2026-11-03"   # from then on, a storage that states no end is refused


def life_of(spec, override=None, where="storage"):
    """a storage's (or one mount's) stated end, checked: {drop, until, ttl,
    keep, max, trim}; None when it states none"""
    src = {k: spec[k] for k in LIFE_KEYS if k in spec}
    src.update({k: override[k] for k in LIFE_KEYS if k in (override or {})})
    if not any(k in src for k in ("drop", "until", "ttl", "keep")):
        return None
    life = {"drop": bool(src.get("drop")), "until": None, "ttl": None, "keep": bool(src.get("keep")),
            "max": None, "trim": str(src["trim"]) if src.get("trim") else None}
    if src.get("until") is not None:
        if not PARAM_KEY_RE.match(str(src["until"])):
            raise Refused(f"{where}: until names the parameter that holds the branch (until = \"branch\")")
        life["until"] = str(src["until"])
    if src.get("ttl") is not None:
        life["ttl"] = parse_duration(src["ttl"])
    if src.get("max") is not None:
        life["max"] = parse_size(str(src["max"]).rstrip("Bb"))
    if life["keep"] and not life["max"]:
        raise Refused(f"{where}: keep needs a max (keep = true, max = \"40g\"): nothing is kept unbounded")
    return life


TEMPLATE_PARAM_RE = re.compile(r"\$\{([a-z_][a-z0-9_]*)")


def until_param(until, template):
    """the parameter an until rule follows on a mount: the one it names, or,
    when the sub is keyed on exactly one other parameter
    ("lib-target/${lib:-main}" with until = "branch"), that one:
    the branch to check is the one the directory holds"""
    keys = set(TEMPLATE_PARAM_RE.findall(template or ""))
    if until in keys or len(keys) != 1:
        return until
    return keys.pop()


def life_text(life, info=None):
    """a stated end in words, with what is left of it"""
    if not life:
        return "no end stated"
    info = info or {}
    parts = []
    if life.get("drop"):
        parts.append("dropped after the job")
    if life.get("until"):
        parts.append(f"until branch {info.get('value', '?')} goes" if info.get("value") else f"until its {life['until']} goes")
    if life.get("ttl"):
        left = life["ttl"] - (time.time() - info.get("used", time.time()))
        parts.append(f"{max(left, 0) / 86400:.1f} d left unused" if info.get("used") else f"{life['ttl'] / 86400:g} d unused")
    if life.get("max"):
        parts.append(f"capped at {life['max'] / 2**30:g} GB")
    return ", ".join(parts)


def life_file(path):
    """where a storage's (or sub's) life record lives: <home>/.mcu-life/
    <storage>[~<sub>].json, outside the storage, which a service's container
    may own (it chowns its data directory) so the runner could not write
    there any more"""
    p = Path(path)
    cache = p.parent if p.parent.name == "cache" else p.parent.parent
    return cache.parent / ".mcu-life" / (str(p.relative_to(cache)).replace("/", "~") + ".json")


def life_info(path):
    """what a mount wrote about a storage (or sub) directory: its rule, the
    parameter's value, the repositories, the last use (an older box kept it
    in the directory, as .bench-life)"""
    for f in (life_file(path), Path(path) / ".bench-life"):
        try:
            return json.loads(f.read_text())
        except (OSError, ValueError):
            continue
    return {}


def trim_to(path, cap, log):
    """a storage over its cap: the least recently modified files go first,
    down to 90 % of it; emptied only if that is not enough. Returns bytes freed"""
    size = du(path)
    if size <= cap:
        return 0
    files = []
    for root, dirs, names in os.walk(path):
        for n in names:
            f = Path(root) / n
            if n.startswith(".bench-"):
                continue
            try:
                st = f.lstat()
                files.append((st.st_mtime, st.st_blocks * 512, f))
            except OSError:
                pass
    goal, freed = size - int(cap * 0.9), 0
    for _, blocks, f in sorted(files):
        if freed >= goal:
            break
        try:
            f.unlink()
            freed += blocks
        except OSError:
            pass
    if du(path) > cap:   # trimming could not get there: empty it
        for x in Path(path).iterdir():
            if x.name.startswith(".bench-"):
                continue
            shutil.rmtree(x, ignore_errors=True) if x.is_dir() and not x.is_symlink() else x.unlink(missing_ok=True)
        freed = size - du(path)
    log(f"storage {Path(path).name}: over its {cap / 2**30:g} GB cap ({size / 2**30:.1f} GB), "
        f"trimmed {freed / 2**30:.1f} GB (oldest files first)")
    return freed


def storage_ends(project, ends, job_id, log, run_trim=None):
    """at a job's end, what its mounts said: a `drop` storage goes (unless
    another job of the project still mounts it), a capped one is trimmed
    down to its cap (its `trim` command first, in the job's image)"""
    home = Path.home() / "cache"
    busy = in_use_subs(project, but=job_id)
    for name, life in ends:
        path = home / name
        if not path.is_dir():
            continue
        if life["drop"]:
            if name in busy:
                log(f"storage {name}: not dropped, another job of yours still mounts it")
                continue
            size = du(path)
            if "/" in name:
                shutil.rmtree(path, ignore_errors=True)
            else:
                empty_storage_dir(path)
            log(f"storage {name}: dropped ({size / 2**30:.2f} GB), as its mount said")
            continue
        if life["max"] and du(path) > life["max"]:
            if life["trim"] and run_trim:
                log(f"storage {name}: over its cap, running its trim command")
                run_trim(life["trim"])
            trim_to(path, life["max"], log)


def empty_storage_dir(path):
    """a storage's (or sub's) content goes; its directory and markers stay"""
    for x in Path(path).iterdir():
        if x.name in (".bench-per", ".bench-tmp"):
            continue
        shutil.rmtree(x, ignore_errors=True) if x.is_dir() and not x.is_symlink() else x.unlink(missing_ok=True)


SEEDED = {}   # pool type -> bytes this run's seeds copied: already part of its learned need


def seed_sub(base, sub, seed, log):
    """a new sub-directory of a storage starts as a copy of its seed sub
    (storage `seed = "main"`): a reflink copy on XFS or btrfs, so it
    is instant and shares the disk until rewritten (the quota still counts
    it whole); skipped while a running job mounts the seed"""
    home = Path.home() / "cache"
    src = home / base / seed
    if seed == sub or not src.is_dir() or not any(src.iterdir()):
        return
    busy = [e["job"] for e in read_state_json("state.json").get("running", [])
            if e.get("project") == project_name() and f"{base}/{seed}" in e.get("subs", [])]
    if busy:
        log(f"storage {base}/{sub}: starts empty (its seed {seed} is in use by {', '.join(busy)})")
        return
    # room first: on XFS a reflinked copy shares its blocks but counts in
    # full against the project's quota, so a seed bigger than the room left
    # on its pool cannot be copied (the clone fails, as a plain copy would)
    per = disk_media(project_name())
    media = media_of(src, per)
    x = per.get(media) or {}
    size = du(src)
    room = x.get("quota", 0) - x.get("used", 0)
    if x.get("quota") and size > room:
        log(f"storage {base}/{sub}: not seeded: {base}/{seed} ({size / 2**30:.1f} GB) doesn't fit in your "
            f"{media} quota ({max(room, 0) / 2**30:.1f} GB left); it starts empty")
        return
    t0 = time.time()
    r = subprocess.run(["cp", "-a", "--reflink=auto", f"{src}/.", str(home / base / sub)], capture_output=True, text=True,
                       env={**os.environ, "LC_ALL": "C"})
    if r.returncode != 0:
        err = r.stderr.strip()
        why = ("your quota ran out during the copy" if re.search(r"quota exceeded|No space left", err, re.I)
               else err[:200])
        log(f"storage {base}/{sub}: seeding from {seed} failed ({why}); it starts empty")
        shutil.rmtree(home / base / sub, ignore_errors=True)
        (home / base / sub).mkdir(parents=True, exist_ok=True)
        return
    SEEDED[media] = SEEDED.get(media, 0) + size
    log(f"storage {base}/{sub}: seeded from {seed} in {time.time() - t0:.1f} s (a reflink copy where the disk allows)")


def resolve_caches(rights, manifest, params, log=lambda m: None):
    """a mount of storage `name` or `name/<sub>`, the sub templated with the
    run's parameters (`app-target/${branch:-main}`), so a job keys its
    storage on whatever it likes; `lock = true`: one job at a time in each
    resolved directory. The older `per = "<param>"` is `name/${param}`
    (with `default`). A `tmp` storage is marked for `reclaim`"""
    decls = storage_decls(manifest)
    home = Path.home() / "cache"
    mounts, locks, ends = [], [], []
    for kind, name, target in rights["mounts"]:
        if kind != "cache":
            mounts.append((kind, name, target))
            continue
        base, _, sub = name.partition("/")
        spec = decls.get(base, {})
        life = life_of(spec, rights.get("life", {}).get(name), where=f"storage {base}")
        if not (home / base).exists() and not (home / base).is_symlink():
            # a storage mounted for the first time: the box picks its pool
            media = [str(spec["media"])] if spec.get("media") else []
            r = subprocess.run(HELPER + ["place-unit", f"storage/{base}"] + media, capture_output=True, text=True)
            if r.returncode == 3:
                raise Misconfigured(f"storage {base}: media = {media[0]}, and this box has no such pool with room", topic="storage")
            if r.returncode == 0 and r.stdout.strip():
                home.mkdir(exist_ok=True)
                (home / base).symlink_to(r.stdout.strip())
        template = sub
        if sub:
            sub = substitute(sub, params)
        elif spec.get("per"):
            key = str(spec["per"])
            if not PARAM_KEY_RE.match(key):
                raise Refused(f"[storage] {base}: bad per {key!r}", topic="storage")
            sub = str(params.get(key, spec.get("default", "")))
            if not sub:
                raise Refused(f"storage {base} is per {key}: run <job> {key}=..., or give it a default "
                              f"(v6: mount \"{base}/${{{key}:-main}}\")")
        sub_value = sub   # before it is sanitised: the branch an until rule checks
        if sub:
            # a branch like feature/x becomes feature_x, as before v6, so the
            # existing per-branch trees keep their names
            sub = re.sub(r"[^A-Za-z0-9._-]", "_", sub)
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", sub) or sub in (".", ".."):
                raise Refused(f"storage {base}: bad sub-directory {sub!r}", topic="storage")
            new = not (home / base / sub).exists()
            (home / base / sub).mkdir(parents=True, exist_ok=True)
            if new and spec.get("seed"):
                seed_sub(base, sub, re.sub(r"[^A-Za-z0-9._-]", "_", str(spec["seed"])), log)
            # prune and reclaim walk the sub-directories, by last use
            (home / base / ".bench-per").touch()
            os.utime(home / base / sub)
            name = f"{base}/{sub}"
        else:
            (home / base).mkdir(parents=True, exist_ok=True)
        marker = home / base / ".bench-tmp"
        if spec.get("tmp", not (life and life["keep"])):
            marker.touch()
        else:
            marker.unlink(missing_ok=True)
        if life:
            # the rule goes with the directory, for the housekeeping and
            # `storage list`: the parameter's real value (sub names are
            # sanitised) and the repositories whose branch it is
            where_ = home / name
            info = {"life": life, "used": time.time()}
            if life["until"]:
                key = until_param(life["until"], template)
                if key != life["until"]:
                    log(f"storage {base}: until = \"{life['until']}\", but its sub is keyed on {key}: "
                        f"the rule follows {key} (write until = \"{key}\")")
                    info["param"] = key
                # the parameter's value, or the mount's default for it
                # ("target/${branch:-main}" run without branch=)
                info["value"] = str(params.get(key, "")) or \
                    (sub_value if "${" + key in (template or "") else "")
                refs = [(str(c.get("repo", "")), str(c.get("ref", ""))) for c in manifest.get("checkout", {}).values()
                        if isinstance(c, dict)]
                mine = [r for r, ref in refs if "${" + key in ref] or [r for r, _ in refs]
                info["repos"] = [substitute(r, params) for r in mine]
            record = life_file(where_)
            record.parent.mkdir(mode=0o755, exist_ok=True)
            record.write_text(json.dumps(info))
            try:   # an older box's record in the directory, when it can still go
                (where_ / ".bench-life").unlink(missing_ok=True)
            except OSError:
                pass
            ends.append((name, life))
        if spec.get("lock"):
            locks.append(f"storage {name}")
        mounts.append((kind, name, target))
    # the services' mounts: resolved the same way, their locks and ends the job's
    services = []
    for svc in rights.get("services", []):
        r = resolve_caches({"mounts": svc["mounts"], "life": svc["life"]}, manifest, params, log)
        services.append(dict(svc, mounts=r["mounts"]))
        locks += [x for x in r["locks"] if x not in locks]
        ends += [e for e in r["ends"] if e[0] not in {n for n, _ in ends}]
    return dict(rights, mounts=mounts, locks=locks, ends=ends, services=services)


def all_mounts(rights):
    """the job's mounts and its services'"""
    return list(rights.get("mounts", [])) + [m for svc in rights.get("services", []) for m in svc["mounts"]]


class GitFailed(Exception):
    pass


# git's messages (in the C locale) for a network failure, worth one retry
NET_ERROR_RE = re.compile(r"Failed to connect|Could not resolve|Couldn't connect|Connection (refused|reset|timed out)"
                          r"|Operation timed out|timed out|early EOF|RPC failed|SSL_|TLS", re.I)


# git's automatic maintenance after a fetch runs in the foreground: detached,
# it outlives the cache's lock and a job's clone copies the cache halfway
# through it (a lock file gone, a pack repacked away)
GIT_NO_DETACH = ["-c", "maintenance.autoDetach=false", "-c", "gc.autoDetach=false"]


def git(*args, cwd=None, check=True):
    r = subprocess.run(["git", *GIT_NO_DETACH, *(["-C", str(cwd)] if cwd else []), *args],
                       capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"})
    if check and r.returncode != 0:
        # --quiet hides a rejected ref's line: say which refs were refused
        err = r.stderr.strip().splitlines() or [l for l in r.stdout.splitlines() if l.lstrip().startswith("!")]
        raise GitFailed(f"git {' '.join(args[:2])}: {err[-1].strip() if err else r.returncode}")
    return r.stdout.strip()


def has_commit(repo, sha):
    return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
                          capture_output=True).returncode == 0


def git_cache(url, log, want=None):
    """the project's bare clone of a repository, fetched with what is new
    (or skipped when `want` is already there). It lives under ~/cache,
    labeled for containers: the per-job clones hardlink its objects, and a
    hardlink shares the label"""
    root = Path.home() / "cache" / ".git"
    root.mkdir(parents=True, exist_ok=True)
    key = re.sub(r"[^A-Za-z0-9._-]", "_", url.removeprefix("https://"))
    bare = root / f"{key}.git"
    lock = open(root / f"{key}.lock", "a")
    fcntl.flock(lock, fcntl.LOCK_EX)  # released when the job's clone is done
    if not bare.exists():
        log(f"caching {url} (first time)")
        git("clone", "--quiet", "--bare", url, str(bare))
        git("config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*", cwd=bare)
    elif not (want and has_commit(bare, want)):
        # --force: an upstream that moves a tag (a `nightly` tag, say) is
        # followed, not refused as "would clobber existing tag"
        git("fetch", "--quiet", "--prune", "--tags", "--force", "origin", cwd=bare)
    if want and not has_commit(bare, want):
        # a commit no branch or tag reaches (a submodule pinned off-branch)
        git("fetch", "--quiet", "origin", want, cwd=bare)
    return bare, lock


def checkout_v5(name, spec, work, log):
    """/work/<name>: its own clone of the cached repository at the ref, with
    its submodules from the cache too; returns the commit"""
    bare, lock = git_cache(spec["repo"], log)
    try:
        sha = git("rev-parse", "--verify", "--quiet", f"{spec['ref']}^{{commit}}", cwd=bare, check=False)
        if not sha:
            git("fetch", "--quiet", "origin", spec["ref"], cwd=bare)
            sha = git("rev-parse", "--verify", "FETCH_HEAD^{commit}", cwd=bare)
        dest = work / name
        # a local clone, not a worktree: a worktree's .git points to the
        # bare repository by a host path the container does not see, and
        # builds that run git (git describe for a version) fail on it
        git("clone", "--quiet", "--no-checkout", str(bare), str(dest))
    finally:
        lock.close()
    git("remote", "set-url", "origin", spec["repo"], cwd=dest)
    git("checkout", "--quiet", "--detach", sha, cwd=dest)
    if spec["submodules"]:
        submodules(dest, spec["repo"], log)
    log(f"checkout {name}: {spec['ref']} at {sha[:12]}")
    return sha


def submodules(dest, parent_url, log):
    if not (dest / ".gitmodules").exists():
        return
    paths = git("config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$", cwd=dest, check=False)
    for line in paths.splitlines():
        key, path = line.split(" ", 1)
        sub = key[len("submodule."):-len(".path")]
        url = git("config", "-f", ".gitmodules", f"submodule.{sub}.url", cwd=dest)
        if url.startswith("../") or url.startswith("./"):
            url = urljoin(parent_url.rstrip("/") + "/", url)
        if not URL_RE.match(url):
            raise Refused(f"submodule {path}: only https URLs ({url})")
        tree = git("ls-tree", "HEAD", path, cwd=dest).split()
        if len(tree) < 3 or tree[1] != "commit":
            continue
        bare, lock = git_cache(url, log, want=tree[2])
        try:
            git("config", f"submodule.{sub}.url", str(bare), cwd=dest)
            git("-c", "protocol.file.allow=always", "submodule", "update", "--init", "--quiet", "--", path, cwd=dest)
        finally:
            lock.close()
        git("config", f"submodule.{sub}.url", url, cwd=dest)
        git("remote", "set-url", "origin", url, cwd=dest / path)
        submodules(dest / path, url, log)


def stage_v5(plan, threads, work, out, logf, log):
    """after the allocation: the checkouts on the host, then [fetch] in the
    container with network; returns the commits found"""
    commits = {}
    for name, spec in plan["checkouts"].items():
        for attempt in (1, 2):
            try:
                commits[name] = checkout_v5(name, spec, work, log)
                break
            except GitFailed as e:
                # a network glitch gets one more try; a missing ref or
                # anything else fails at once
                if attempt == 1 and NET_ERROR_RE.search(str(e)):
                    log(f"checkout {name}: the network failed ({e}); trying again in 5 s")
                    shutil.rmtree(work / name, ignore_errors=True)
                    time.sleep(5)
                    continue
                log(f"checkout {name} failed: {e}")
                raise FetchFailed() from e
    if plan["fetch"]:
        log("fetch, with network")
        script = " && ".join(plan["fetch"]) + (
            "\nrc=$?; for g in $(find /work -maxdepth 3 -name .git 2> /dev/null); do"
            " r=$(dirname $g); echo \"${r#/work/} $(git -C $r rev-parse HEAD 2> /dev/null)\";"
            " done > /out/.bench-commits; exit $rc")
        rights = plan["rights"]
        cmd = podman_args(dict(rights, network="full"), threads, work, out, "/work", "/work") + [plan["tag"], "bash", "-c", script]
        if subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT, timeout=rights["timeout"]).returncode != 0:
            raise FetchFailed()
        try:
            for line in (out / ".bench-commits").read_text().splitlines():
                repo, _, sha = line.partition(" ")
                if sha:
                    commits.setdefault(repo, sha)
            (out / ".bench-commits").unlink()
        except OSError:
            pass
    if commits:
        log("commits: " + ", ".join(f"{r} {c[:9]}" for r, c in commits.items()))
    return commits


class FetchFailed(Exception):
    pass


def push_pushed(work):
    # local code pushed by the project (mcu-job push <name>) goes in first
    pushed = Path.home() / "pushed"
    for p in sorted(pushed.glob("*")) if pushed.exists() else []:
        # cp without the SELinux context: the copy takes /work's label
        # (container_file_t); copytree would keep the home's and the
        # container could not read it
        subprocess.run(["cp", "-r", "--preserve=mode,timestamps,links", str(p), str(work / p.name)],
                       check=True)


OUTPUT_KINDS = ("junit", "images", "json", "log", "file")
ERROR_LINE_RE = re.compile(r"error\[|^error:|\berror: |FAILED|panicked at|^\s*failures:", re.M)


def junit_summary(path):
    """counts and the first failing tests of a JUnit XML file"""
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    failed, skipped = [], 0
    for c in cases:
        bad = c.find("failure") if c.find("failure") is not None else c.find("error")
        if c.find("skipped") is not None:
            skipped += 1
        elif bad is not None:
            msg = (bad.get("message") or (bad.text or "")).strip().splitlines()
            failed.append({"test": ".".join(x for x in (c.get("classname"), c.get("name")) if x),
                           "message": (msg[0] if msg else "")[:300]})
    return {"tests": len(cases), "passed": len(cases) - len(failed) - skipped, "failed": len(failed),
            "skipped": skipped, "failures": failed[:10]}


def job_summary(d, job, code):
    """the job's results in brief, written to summary.json beside status:
    each declared output (a JUnit's counts and first failures, an image
    directory's files, a JSON's validity), and for a failed job without a
    JUnit, its log's first error lines"""
    out = {"exit": code, "outputs": {}}
    for name, spec in (job.get("outputs") or {}).items():
        path = d / "out" / str(spec.get("path", ""))
        kind = spec.get("kind", "file")
        item = {"kind": kind, "path": str(spec.get("path", "")), "present": path.exists()}
        try:
            if kind == "junit" and path.is_file():
                item.update(junit_summary(path))
            elif kind == "images" and path.is_dir():
                files = sorted(f.name for f in path.iterdir() if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"))
                item.update(count=len(files), files=files[:50])
            elif kind == "json" and path.is_file():
                json.loads(path.read_text())
                item.update(valid=True, bytes=path.stat().st_size)
            elif path.is_file():
                item.update(bytes=path.stat().st_size)
        except (ET.ParseError, ValueError, OSError) as e:
            item.update(valid=False, problem=str(e)[:200])
        out["outputs"][name] = item
    junits = [o for o in out["outputs"].values() if o["kind"] == "junit" and "tests" in o]
    if junits:
        out["tests"] = {k: sum(o[k] for o in junits) for k in ("tests", "passed", "failed", "skipped")}
        out["failures"] = [f for o in junits for f in o["failures"]][:10]
    elif code != 0:
        try:
            with open(d / "log", "rb") as f:
                f.seek(0, 2)
                f.seek(max(f.tell() - 400_000, 0))
                text = f.read().decode(errors="replace")
            out["errors"] = [l.strip()[:300] for l in text.splitlines() if ERROR_LINE_RE.search(l)][:10]
        except OSError:
            pass
    (d / "summary.json").write_text(json.dumps(out, indent=1) + "\n")
    return out


def summary_line(sm):
    """a few words for list and status: 684 passed, 3 failed / 2 errors"""
    if not sm:
        return ""
    if sm.get("tests"):
        t = sm["tests"]
        return f"{t['passed']} passed" + (f", {t['failed']} failed" if t["failed"] else "") \
            + (f", {t['skipped']} skipped" if t["skipped"] else "")
    if sm.get("errors"):
        return f"{len(sm['errors'])} error line(s)"
    return ""


def read_json_file(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def job_work(d, job, log):
    """the job's disposable /work: in its job directory, or with
    work_media = "hdd" (or "ssd"; "!" insists) on a pool of that disk type,
    reached by the job directory's `work` link"""
    media = job.get("work_media")
    if media:
        r = subprocess.run(HELPER + ["place-unit", "scratch", str(media)], capture_output=True, text=True)
        if r.returncode == 3:
            raise Misconfigured(f"work_media = {media}: {r.stderr.strip() or 'no such pool on this box'}")
        if r.returncode == 0 and r.stdout.strip():
            work = Path(r.stdout.strip()) / d.name
            work.mkdir(parents=True, exist_ok=True)
            (d / "work").symlink_to(work)
            log(f"/work on {media.rstrip('!')} ({work.parent})")
            return work
        log(f"work_media = {media}: no such pool on this box; /work stays with the job")
    work = d / "work"
    work.mkdir(exist_ok=True)
    return work


def drop_work(d):
    """the job's /work, wherever it is"""
    w = d / "work"
    if w.is_symlink():
        shutil.rmtree(w.resolve(), ignore_errors=True)
        w.unlink(missing_ok=True)
    else:
        shutil.rmtree(w, ignore_errors=True)


def run_v5(project, conf, req, d, logf, log, finish, sched, t0):
    """a Benchfile job: the checkouts and [fetch] with network into a
    disposable /work, then the job without network on it; or the
    provisioning of a volume"""
    params = req.get("params", {})
    plan = prepare_v5(project, conf, req["kind"], req["name"], params, log)
    rights = plan["rights"]
    work, out = job_work(d, plan["job"], log), d / "out"
    out.mkdir(exist_ok=True)
    push_pushed(work)
    entry = {"id": d.name, "pid": os.getpid(), "project": project,
             "job": req["name"], "branch": " ".join(f"{k}={v}" for k, v in params.items()),
             "ext": req.get("ext_job_id"), "priority": req.get("priority"),
             "cores": rights["cores"], "exclusive": rights["exclusive"],
             "timeout": rights["timeout"], "max_wait": rights["max_wait"],
             "locks": rights["locks"],
             "units": job_units(rights), "honors_moves": True, "benchfile": plan["benchfile"][0],
             "image": plan["tag"],
             "images": sorted({svc["tag"] for svc in rights["services"]} - {plan["tag"]}),
             "subs": sorted({n for k, n, _ in all_mounts(rights) if k == "cache" and "/" in n}),
             "memory": rights["memory_need"] + rights["services_memory"], "max_jobs": rights["max_jobs"],
             "queued": time.time()}
    threads = allocate(sched, entry, log)
    # the images moved (or were pruned) while the job waited: build again,
    # as a build job of its own, then queue again
    gone = [t for t in [plan["tag"]] + entry["images"] if t in BUILDS and subprocess.run(
        ["podman", "image", "exists", t], capture_output=True).returncode != 0]
    if gone:
        log("the image went away while the job waited (images moved): building it again")
        sched.release(d.name)
        for t in gone:
            build_once(project, t, BUILDS[t], log)
        threads = allocate(sched, dict(entry, queued=time.time()), log)
    (d / "started").write_text(time.strftime("%FT%T%z") + "\n")
    log(f"started on threads {','.join(map(str, threads))}")
    event(kind="started", id=d.name, project=project, job=req["name"], threads=threads,
          exclusive=rights["exclusive"], memory=rights["memory_need"],
          memory_limit=parse_size(rights["memory"]))
    preflight_disk(project, req["name"], entry["subs"], log, d.name)
    disk = DiskWatch(project)
    log(f"memory: {rights['memory_need'] / 2**30:.1f} GB reserved, the container's limit "
        f"{parse_size(rights['memory']) / 2**30:.1f} GB"
        + (f"; its services {rights['services_memory'] / 2**30:.1f} GB more" if rights["services"] else ""))
    try:
        commits = stage_v5(plan, threads, work, out, logf, log)
    except FetchFailed:
        log("fetch failed")
        finish(3)
    extra = []
    if req["kind"] == "provision":
        base = VOLUMES / req["name"]
        versions = sorted((int(p.name[1:]) for p in base.glob("v[0-9]*")), reverse=True)
        new = base / f"v{(versions[0] + 1) if versions else 1}"
        new.mkdir()
        extra = ["-v", f"{new}:/volume"]
        rights = dict(rights, network="full")
    measured = with_trace(plan["command"])
    pod = None
    if rights["services"]:
        pod = pod_name(d.name)
        try:
            start_services(d.name, rights, threads, work, out, log)
        except ServiceFailed as e:
            log(f"services: {e}")
            stop_services(d.name, out, log)
            finish(3)
    ts = timing_start(rights, log) if rights.get("timing") else None
    rights = dict(rights, timing_state=ts)
    cmd = podman_args(rights, threads, work, out, plan["cwd"], "/work", image=plan["tag"], pod=pod) + extra + [plan["tag"], "bash", "-c", measured]
    watch = Conditions(d.name, threads, rights["exclusive"])
    watch.timing = ts
    mem = MemWatch(d.name, out)
    try:
        code = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT,
                              timeout=rights["timeout"]).returncode
    except subprocess.TimeoutExpired:
        log(f"timeout after {rights['timeout']}s: stopping the container")
        stop_containers(d.name)
        code = 124
    finally:
        timing_end(ts)
        mem.stop()
        if pod:
            # the services go with the job, whatever ended it (finish()
            # tries again, should this one fail)
            stop_services(d.name, out, log)
    cond = watch.stop(d, log)
    disk.stop(req["name"], log)
    if code != 0:
        enospc_note(project, d, log, rights)
    grow = memory_hit(d, code, rights, log)
    cost = record_cost(d, project, req["name"], threads, log, cond, rights, grow)
    code, why = gpu_lost(rights["exclusive"], code, log)
    if rights.get("ends"):
        trim_cmd = lambda line: subprocess.run(
            podman_args(rights, threads, work, out, plan["cwd"], "/work", image=plan["tag"]) + [plan["tag"], "bash", "-c", line],
            stdout=logf, stderr=subprocess.STDOUT, timeout=900)
        try:
            storage_ends(project, rights["ends"], d.name, log, trim_cmd)
        except Exception as e:   # the storage's housekeeping never fails the job
            log(f"storage ends: {e}")
    if req["kind"] == "provision":
        if code != 0:
            shutil.rmtree(new, ignore_errors=True)
            log(f"provisioning failed (exit {code}); current version kept")
        else:
            switch_volume(req["name"], new, project, log)
    try:
        sm = job_summary(d, plan["job"], code)
        if summary_line(sm):
            log(f"summary: {summary_line(sm)}")
    except Exception as e:      # a summary must never fail the job
        sm = {"problem": str(e)[:200]}
    label, version = plan["benchfile"]
    finish(code, commits=commits, benchfile=version, **({"benchfile_label": label} if label else {}),
           cost=cost, summary={k: sm[k] for k in ("tests", "failures", "errors") if k in sm}, **why)


def debug_v5(project, conf, job_name, params):
    """debug <job> [key=value...]: the job's checkouts and [fetch], then a
    shell in its container (its rights, no network, its /work), through
    the queue like the job; everything is removed on exit"""
    log = lambda m: print(f"bench: {m}", flush=True)
    plan = prepare_v5(project, conf, "job", job_name, params, log)
    rights = plan["rights"]
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    d = jobs_dir() / "debug" / job_id
    work, out = d / "work", d / "out"
    work.mkdir(parents=True)
    out.mkdir()
    sched = Scheduler()
    try:
        push_pushed(work)
        threads = allocate(sched, {"id": job_id, "pid": os.getpid(), "project": project,
                                   "job": f"debug {job_name}", "branch": " ".join(f"{k}={v}" for k, v in params.items()),
                                   "cores": rights["cores"], "exclusive": rights["exclusive"],
                                   "locks": rights["locks"], "memory": rights["memory_need"] + rights["services_memory"],
                                   "timeout": rights["timeout"], "max_wait": rights["max_wait"],
                                   "max_jobs": rights["max_jobs"], "queued": time.time()}, log)
        try:
            stage_v5(plan, threads, work, out, sys.stdout, log)
        except FetchFailed:
            log("fetch failed")
            return 3
        pod = None
        if rights["services"]:
            pod = pod_name(job_id)
            try:
                start_services(job_id, rights, threads, work, out, log)
            except ServiceFailed as e:
                log(f"services: {e}")
                return 3
        log(f"shell in {plan['cwd']} on threads {','.join(map(str, threads))}; the job would run: {plan['command']}")
        cmd = podman_args(rights, threads, work, out, plan["cwd"], "/work", pod=pod)
        cmd[2:2] = ["-it"]
        return subprocess.run(cmd + [plan["tag"], "bash"], timeout=rights["timeout"]).returncode
    finally:
        stop_services(job_id, out, log)
        sched.release(job_id)
        shutil.rmtree(d, ignore_errors=True)


def switch_volume(name, new, project, log):
    base = VOLUMES / name
    link = base / "current.new"
    if link.is_symlink():
        link.unlink()
    link.symlink_to(new.name)
    os.replace(link, base / "current")
    keep = volume_conf(name)["keep_versions"]
    for old in sorted(base.glob("v[0-9]*"), key=lambda p: int(p.name[1:]))[:-keep]:
        shutil.rmtree(old, ignore_errors=True)
    size = sum(f.stat().st_size for f in new.rglob("*") if f.is_file())
    event(kind="provisioned", volume=name, version=new.name, bytes=size, project=project)
    log(f"{name}: current -> {new.name} ({size / 1e6:.0f} MB)")


def provision(project, conf, manifest, src, req, d, logf, log, finish, sched):
    """fill a new version of a shared volume this project owns, with
    network; switch `current` to it only on success"""
    name = req["name"]
    vol = volume_conf(name)
    if vol["owner"] != project:
        raise Refused(f"{project} does not own volume {name}")
    spec = manifest.get("volumes", {}).get(name)
    if not spec or "provision" not in spec:
        raise Refused(f"the manifest does not say how to provision {name}")
    if NETWORK_LEVELS.index(conf["limits"]["network"]) < NETWORK_LEVELS.index("fetch"):
        raise Refused("provisioning needs network; the box's envelope is none", topic="config")
    caches = storage_decls(manifest)
    mounts = []
    for cache, target in spec.get("mounts", {}).items():
        if cache not in caches or not str(target).startswith("/"):
            raise Refused(f"provisioning {name}: only the project's caches can be mounted")
        mounts.append(("cache", cache, target))
    cores = min(spec.get("cores", 2), conf["limits"]["max_cores"])
    rights = {"memory": conf["limits"]["memory"], "network": "full", "resources": {},
              "mounts": mounts, "env": {}}
    timeout = parse_duration(spec.get("timeout", conf["limits"]["timeout_max"]))
    tag = image(project, src, manifest, log)
    (d / "out").mkdir(exist_ok=True)
    threads = allocate(sched, {"id": d.name, "pid": os.getpid(), "project": project,
                               "job": f"provision {name}", "branch": req["branch"], "cores": cores,
                               "exclusive": [], "memory": parse_size(conf["limits"]["memory"]),
                               "timeout": timeout, "max_jobs": 1,
                               "queued": time.time()}, log)
    base = VOLUMES / name
    versions = sorted((int(p.name[1:]) for p in base.glob("v[0-9]*")), reverse=True)
    new = base / f"v{(versions[0] + 1) if versions else 1}"
    new.mkdir()
    (d / "started").write_text(time.strftime("%FT%T%z") + "\n")
    log(f"provisioning {name} into {new.name} on threads {','.join(map(str, threads))}")
    cmd = podman_args(rights, threads, src, d / "out") + ["-v", f"{new}:/volume", tag, "bash", "-c", spec["provision"]]
    try:
        code = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        code = 124
    if code != 0:
        shutil.rmtree(new, ignore_errors=True)
        log(f"provisioning failed (exit {code}); current version kept")
        finish(code)
    link = base / "current.new"
    if link.is_symlink():
        link.unlink()
    link.symlink_to(new.name)
    os.replace(link, base / "current")
    for old in sorted(base.glob("v[0-9]*"), key=lambda p: int(p.name[1:]))[:-vol["keep_versions"]]:
        shutil.rmtree(old, ignore_errors=True)
    size = sum(f.stat().st_size for f in new.rglob("*") if f.is_file())
    event(kind="provisioned", volume=name, version=new.name, bytes=size, project=project)
    log(f"{name}: current -> {new.name} ({size / 1e6:.0f} MB)")
    finish(0)


# the job's command, sampled from its own cgroup every 2 s (CPU time used,
# memory in use with the page cache, its anonymous part: what the job really
# needs) and summed up at the end (CPU, memory peak, swap, disk I/O); the
# subshell keeps the command's exit status and `exit` inside it
TRACE = """cg=/sys/fs/cgroup
( while :; do echo "$(date +%s) $(awk '/^usage_usec/ {print $2}' $cg/cpu.stat) $(cat $cg/memory.current) $(awk '/^anon / {print $2}' $cg/memory.stat)"; sleep 2; done ) > /out/.bench-trace 2> /dev/null &
tracer=$!
(
{command}
)
rc=$?
kill $tracer 2> /dev/null
cat $cg/memory.peak > /out/.bench-memory-peak 2> /dev/null
cat $cg/memory.events > /out/.bench-memory-events 2> /dev/null
cat $cg/memory.swap.peak > /out/.bench-swap-peak 2> /dev/null
awk '/^usage_usec/ {print $2}' $cg/cpu.stat > /out/.bench-cpu 2> /dev/null
awk '{for (i = 2; i <= NF; i++) {split($i, kv, "="); s[kv[1]] += kv[2]}} END {print s["rbytes"] + 0, s["wbytes"] + 0}' $cg/io.stat > /out/.bench-io 2> /dev/null
exit $rc
"""


def with_trace(command):
    return TRACE.replace("{command}", command)


class MemWatch:
    """a running job's memory, from its trace every 10 s, into its entry in
    state.json (mem_now, mem_peak): the dashboard shows it beside what the
    job reserved"""

    def __init__(self, job_id, out):
        import threading
        self.job_id, self.trace, self.peak = job_id, out / ".bench-trace", 0
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.sample, daemon=True)
        self.thread.start()

    def sample(self):
        while not self.done.wait(10):
            try:
                last = self.trace.read_text().splitlines()[-1].split()
                now = int(last[3] if len(last) > 3 else last[2])   # anonymous memory, the cache left out
            except (OSError, IndexError, ValueError):
                continue
            self.peak = max(self.peak, now)

            def put(state):
                for e in state["running"]:
                    if e["id"] == self.job_id:
                        e["mem_now"], e["mem_peak"] = now, self.peak
            try:
                Scheduler()._locked(put)
            except (OSError, ValueError):
                pass

    def stop(self):
        self.done.set()


def anon_peak(out):
    """the largest anonymous memory the trace saw: what the job needs, the
    page cache (a build writing gigabytes) left out; None without samples"""
    try:
        vals = [int(line.split()[3]) for line in (out / ".bench-trace").read_text().splitlines()
                if len(line.split()) == 4]
    except (OSError, ValueError):
        return None
    return max(vals) if vals else None


def memory_hit(d, code, rights, log):
    """did the run go past its container's memory limit? The sign is swap
    used (podman allows as much swap as memory) or a process killed; a peak
    at the limit alone is not: the page cache fills a cgroup up to its limit
    and is dropped at no cost. Then say so, and return what the next run
    should learn"""
    out = d / "out"
    try:
        events = dict(line.split() for line in (out / ".bench-memory-events").read_text().splitlines())
        (out / ".bench-memory-events").unlink()
    except (OSError, ValueError):
        events = {}
    try:
        swapped = int((out / ".bench-swap-peak").read_text().strip())
        (out / ".bench-swap-peak").unlink()
    except (OSError, ValueError):
        swapped = 0
    limit = parse_size(rights["memory"])
    near = (anon_peak(out) or 0) >= 0.8 * limit
    # the in-container counters may stay at 0 (a rootless container): a
    # SIGKILL with the anonymous memory near the limit tells it too
    killed = int(events.get("oom_kill", 0)) > 0 or (code == 137 and near)
    if not killed and swapped < 64 * 2**20:
        return None
    if not killed and not near:
        # the box's own memory pressure swaps a little of any job out, far
        # below its limit: nothing to learn from that
        log(f"{swapped / 2**30:.1f} GB went to swap under the box's memory pressure, not the job's limit")
        return None
    what = ("the kernel killed a process of the job" if killed
            else f"the job was slowed down ({swapped / 2**30:.1f} GB went to swap) instead of being killed")
    log(f"memory limit {limit / 2**30:.1f} GB reached (reserved {rights['memory_need'] / 2**30:.1f} GB): "
        f"{what}; the next run reserves more, or declare more `memory`")
    return int(limit * 1.5)


def record_cost(d, project, job, threads, log, conditions=None, rights=None, grow_to=None):
    """the run's cost from its cgroup: CPU time and mean use of its threads,
    memory peak, disk I/O, and a CPU/memory curve; kept in runs/<project>/
    for the dashboard, and returned for the done event"""
    out = d / "out"
    def take(name):
        try:
            text = (out / name).read_text().strip()
            (out / name).unlink()
            return text
        except OSError:
            return ""
    try:
        peak = int((out / ".bench-memory-peak").read_text().strip())
    except (OSError, ValueError):
        peak = None
    # what the job needs is its anonymous memory: record that, not the
    # cgroup's peak, which counts the page cache of what it wrote
    anon = anon_peak(out)
    if anon:
        (out / ".bench-memory-peak").write_text(f"{anon}\n")
    record_peak(d, project, job, log, grow_to)  # reads and removes .bench-memory-peak
    cpu_us = int(take(".bench-cpu") or 0)
    io = take(".bench-io").split()
    samples = []
    for line in take(".bench-trace").splitlines():
        try:
            t, u, m = (int(x) for x in line.split()[:3])
            samples.append((t, u, m))
        except ValueError:
            pass
    curve = []
    for (t0, u0, _), (t1, u1, m1) in zip(samples, samples[1:]):
        if t1 > t0:
            busy = (u1 - u0) / ((t1 - t0) * 1e6) / max(len(threads), 1) * 100
            curve.append([t1, round(min(busy, 100), 1), m1])
    # at most 120 points: average neighbours together
    while len(curve) > 120:
        curve = [[b[0], round((a[1] + b[1]) / 2, 1), max(a[2], b[2])]
                 for a, b in zip(curve[::2], curve[1::2])]
    seconds = (samples[-1][0] - samples[0][0]) if len(samples) > 1 else 0
    cost = {
        "threads": len(threads),
        "cpu_seconds": round(cpu_us / 1e6, 1),
        "cpu_use": round(cpu_us / 1e6 / seconds / max(len(threads), 1) * 100, 1) if seconds else None,
        "memory_peak": anon or peak or None,       # what it needed (anonymous memory)
        "memory_peak_total": peak or None,         # with the page cache
        **({"memory_reserved": rights["memory_need"], "memory_limit": parse_size(rights["memory"])} if rights else {}),
        "read_bytes": int(io[0]) if len(io) == 2 else None,
        "write_bytes": int(io[1]) if len(io) == 2 else None,
    }
    runs = STATE / "runs" / project
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{d.name}.json").write_text(json.dumps({"id": d.name, "job": job, **cost, "curve": curve,
                                                     "conditions": conditions}))
    log(f"cost: {cost['cpu_seconds']} s of CPU, {cost['cpu_use']} % of {cost['threads']} threads, "
        f"read {(cost['read_bytes'] or 0) / 1e6:.0f} MB, written {(cost['write_bytes'] or 0) / 1e6:.0f} MB")
    return cost


def record_peak(d, project, job, log, grow_to=None):
    """the run's memory peak into memory.json: the job's last peaks, the
    largest of them as what its next run reserves; grow_to (a run killed at
    its limit) is recorded instead, so the next run gets more room"""
    peak_file = d / "out" / ".bench-memory-peak"
    try:
        peak = int(peak_file.read_text().strip())
        peak_file.unlink()
    except (OSError, ValueError):
        peak = None
    seen = peak
    if grow_to:
        peak = max(peak or 0, grow_to)
    if not peak:
        return
    with open(STATE / "sched.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = STATE / "memory.json"
        data = json.loads(path.read_text()) if path.exists() and path.stat().st_size else {}
        recent = (data.get(f"{project}/{job}#recent") or [])[-(MEMORY_RECENT - 1):] + [peak]
        data[f"{project}/{job}#recent"] = recent
        data[f"{project}/{job}"] = max(recent)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, path)
    event(kind="memory", project=project, job=job, peak=peak)
    log(f"memory peak {(seen or peak) / 2**30:.2f} GB"
        + (f"; the next run is sized for {peak / 2**30:.2f} GB" if grow_to else ""))


STEP_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}\Z")


def take_deps(words):
    """after=<ref>[,<ref>...], after_ok=..., ext_job_id= and priority= out
    of a job's words: (the other words, {"after": [...], "after_ok": [...]},
    ext_job_id or None, the priority given (high, normal, low) or None). A ref is a job id, an
    ext_job_id, or in a chain an earlier step's name"""
    rest, deps, ext, prio = [], {"after": [], "after_ok": []}, None, None
    for w in words:
        key, sep, value = w.partition("=")
        if sep and key in deps:
            items = value.split(",")
            if not all(ID_RE.match(i) or ext_ref(i) or STEP_RE.match(i) for i in items):
                raise Refused(f"bad {key}={value}: job ids or x:<ext_job_id> (or a chain's step names), "
                              "separated by commas", topic="deps")
            deps[key] += [i for i in items if i not in deps[key]]
        elif sep and key == "ext_job_id":
            if not EXT_RE.match(value):
                raise Refused(f"bad ext_job_id={value}: letters, digits and . _ -, 64 at most")
            ext = value
        elif sep and key == "priority":
            if value not in PRIORITIES:
                raise Refused(f"bad priority={value}: high, normal or low", topic="priority")
            prio = value
        else:
            rest.append(w)
    return rest, deps, ext, prio


def check_deps(project, deps, steps=(), new=1):
    """a job's dependencies at submit: its own project's jobs only; one
    already ended counts as met, unless it failed and after_ok asks for
    success. Notes for the submitter, or Refused"""
    notes = []
    for key, ids in deps.items():
        for i in ids:
            if i in steps:
                continue
            found = resolve_ref(i)
            if not found:
                raise Refused(f"{key}: no job {i} of {project} (a job id or x:<ext_job_id>; "
                              "dependencies are within a project)")
            d = jobs_dir() / found
            status = read(d / "status")
            if status is not None:
                if key == "after_ok" and status_code(status) != 0:
                    raise Refused(f"after_ok: {i} already ended without success ({status})", topic="deps")
                notes.append(f"{i} already ended ({status}): met")
    if any(deps.values()):
        limit = int(box_conf().get("max_waiting_deps", 50))
        waiting = [e for e in read_state_json("state.json").get("deps", []) if e.get("project") == project]
        if len(waiting) + new > limit:
            raise Refused(f"{project} already has {len(waiting)} job(s) waiting for dependencies, "
                          f"the box's limit is {limit}", topic="deps")
    return notes


def create_job(kind, branch, name, params=None, deps=None, ext=None, prio=None):
    """a job's directory and its runner, in the background; the job's dir.
    With an ext_job_id, the new job takes it: from now on that name means
    this job (status, wait, fetch, cancel, and the jobs waiting on it); a
    job still queued or running under that name is replaced, so cancelled"""
    if branch is not None and (not BRANCH_RE.match(branch) or ".." in branch):
        raise Refused("bad branch name")
    if not NAME_RE.match(name):
        raise Refused("bad job or volume name")
    deps = deps or {}
    prio = None if prio == "normal" else prio
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    d = jobs_dir() / job_id
    d.mkdir(parents=True)
    (d / "request").write_text(json.dumps({"kind": kind, "branch": branch, "name": name,
                                           "params": params or {}, **{k: v for k, v in deps.items() if v},
                                           **({"ext_job_id": ext} if ext else {}),
                                           **({"priority": prio} if prio else {})}) + "\n")
    replaced = None
    if ext:
        index = jobs_dir() / ".ext"
        index.mkdir(exist_ok=True)
        for link in index.iterdir():          # names whose job the retention removed
            if link.is_symlink() and not link.exists():
                link.unlink(missing_ok=True)
        old = ext_lookup(ext)
        tmp = index / f".{ext}.{job_id}"
        tmp.symlink_to(f"../{job_id}")
        os.replace(tmp, index / ext)          # the name moves first: its waiters follow the new job
        if old and not (jobs_dir() / old / "status").exists():
            replaced = old
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "run", job_id],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    where = f"on {branch}" if branch else " ".join(f"{k}={v}" for k, v in (params or {}).items())
    after = ("".join(f" {k}={','.join(v)}" for k, v in deps.items() if v) + (f" ext_job_id={ext}" if ext else "")
             + (f" priority={prio}" if prio else ""))
    print(f"mcu-job: job {job_id} ({kind} {name} {where}{after}) queued", flush=True)
    if replaced:
        print(f"mcu-job: {job_id} takes ext_job_id {ext} from {replaced}, still queued or running: cancelling it",
              flush=True)
        cancel([replaced])
    return d


def submit_warnings():
    for warn in (purge_notice(project_name()), disk_warning(project_name())):
        if warn:
            print(f"mcu-job: warning: {warn}", file=sys.stderr, flush=True)


def submit(kind, branch, name, params=None, deps=None, ext=None, prio=None):
    deps = deps or {}
    for note in check_deps(project_name(), deps):
        print(f"mcu-job: {note}", flush=True)
    submit_warnings()
    return follow(create_job(kind, branch, name, params, deps, ext, prio))


def chain(lines, detach=False, prio=None):
    """several jobs in one call, each its own job (id, log, out/), linked by
    after= / after_ok= naming earlier steps: one "name = job [key=value...]"
    per line; prio, the chain's priority, for the steps without their own.
    All lines are checked before any job is created; then the ids, then
    each step followed in turn (exit: the first failing step's)"""
    project = project_name()
    steps = []
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_-]{1,32})\s*=\s*(\S.*)$", line)
        if not m:
            raise Refused(f"chain, line {n}: name = job [key=value...] [after=...] [after_ok=...]", topic="deps")
        name, words = m.group(1), m.group(2).split()
        if name in (s[0] for s in steps):
            raise Refused(f"chain, line {n}: step {name} named twice", topic="deps")
        rest, deps, ext, step_prio = take_deps(words[1:])
        if ext and ext in (s[4] for s in steps):
            raise Refused(f"chain, line {n}: ext_job_id {ext} given twice", topic="deps")
        if not NAME_RE.match(words[0]):
            raise Refused(f"chain, line {n}: bad job name {words[0]!r}", topic="deps")
        steps.append((name, words[0], parse_params(rest), deps, ext, step_prio or prio))
    if not steps:
        raise Refused("chain: no steps on stdin (name = job [key=value...], one per line)", topic="deps")
    if len(steps) > 20:
        raise Refused("chain: at most 20 steps", topic="deps")
    waiting = sum(1 for s in steps if any(s[3].values()))
    for n, (name, _, _, deps, _, _) in enumerate(steps):
        # an earlier step's name, else a job id or an ext_job_id already on the box
        for note in check_deps(project, deps, steps=[s[0] for s in steps[:n]], new=waiting):
            print(f"mcu-job: {note}", flush=True)
    submit_warnings()
    ids, dirs = {}, []
    for name, job, params, deps, ext, step_prio in steps:
        # a step named by its ext_job_id when it has one: a later resubmission
        # of that step takes its place for the steps waiting on it
        resolved = {k: [ids.get(i, i) for i in v] for k, v in deps.items()}
        d = create_job("job", None, job, params, resolved, ext, step_prio)
        ids[name] = f"x:{ext}" if ext else d.name
        dirs.append(d)
    print("mcu-job: chain: " + " ".join(f"{s[0]}={d.name}" for s, d in zip(steps, dirs)), flush=True)
    if detach:
        return 0
    first = 0
    for d in dirs:
        code = follow(d)
        first = first or code
    return first


def debug(branch, job_name):
    """an interactive shell in a disposable container with the job's rights,
    through the queue like the job"""
    if not sys.stdin.isatty():
        raise Refused("debug needs a terminal: ssh -t")
    project = project_name()
    conf = ceiling(project)
    log = lambda m: print(f"bench: {m}", flush=True)
    src = checkout(project, conf, branch, log)
    manifest = load_toml(src / conf["manifest"])
    rights = grant(project, conf, manifest, job_name)
    tag = image(project, src, manifest, log)
    out = Path.home() / "jobs" / "debug"
    out.mkdir(parents=True, exist_ok=True)
    sched = Scheduler()
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    threads = allocate(sched, {"id": job_id, "pid": os.getpid(), "project": project,
                               "job": f"debug {job_name}", "branch": branch, "cores": rights["cores"],
                               "exclusive": rights["exclusive"], "memory": rights["memory_need"],
                               "timeout": rights["timeout"], "max_wait": rights["max_wait"],
                               "max_jobs": rights["max_jobs"], "queued": time.time()}, log)
    try:
        cmd = podman_args(rights, threads, src, out)
        cmd[2:2] = ["-it"]
        return subprocess.run(cmd + [tag, "bash"], timeout=rights["timeout"]).returncode
    finally:
        sched.release(job_id)


def entry():
    if os.environ.get("SSH_ORIGINAL_COMMAND", "").strip() in ("/usr/libexec/openssh/sftp-server", "internal-sftp"):
        # sftp (sshfs) on the project's key: its read-only view, built by
        # the root helper for this session (docs view)
        os.execv("/usr/bin/sudo", ["sudo", "-n", "/usr/local/lib/mcu/mcu-helper", "sftp"])
    words = os.environ.get("SSH_ORIGINAL_COMMAND", "help").split()
    cmd, args = (words[0], words[1:]) if words else ("help", [])
    try:
        if cmd == "box" and not args:
            return box_status()
        if cmd == "docs" and len(args) <= 1:
            return docs(args[0] if args else None)
        if cmd in ("run", "provision", "debug") and maintenance():
            raise Unavailable(maintenance_text())
        if cmd == "box" and args[:1] == ["wait-open"] or cmd == "health" and args[:1] == ["--wait"]:
            # one blocking call until the queue reopens: 0, or 75 at the timeout
            timeout = int(args[2]) if len(args) == 3 and args[1] == "--timeout" and args[2].isdigit() else 24 * 3600
            t0 = time.time()
            while queue_mode() != "open":
                if time.time() - t0 >= timeout:
                    raise Unavailable(f"still {queue_mode()} after {timeout} s: {held_text() or maintenance_text()}")
                time.sleep(5)
            print("open")
            return 0
        if cmd == "bump" and len(args) in (1, 2):
            # a queued job (or one waiting for dependencies) of the project
            # takes a priority level (high by default); no other job changes
            level = args[1] if len(args) == 2 else "high"
            if level not in PRIORITIES:
                raise Refused(f"bad level {level}: high, normal or low")
            d = job_dir(args[0])
            if not Scheduler().prioritize(project_name(), d.name, level):
                raise Refused(f"{d.name} is not queued ({state_of(d)}): only a waiting job can be bumped")
            print(f"mcu-job: {d.name} has {level} priority")
            return 0
        if cmd == "chain":
            # chain [--detach] [priority=<level>] < steps
            rest, deps, ext, prio = take_deps(args)
            if ext or any(deps.values()) or any(w != "--detach" for w in rest):
                raise Refused("usage: chain [--detach] [priority=high|normal|low] < steps", topic="deps")
            if not project_conf(project_name()).get("benchfile"):
                raise Refused("chain needs a Benchfile", topic="benchfile")
            return chain(sys.stdin.read(64 * 1024).splitlines(), detach="--detach" in rest, prio=prio)
        if cmd in ("run", "provision") and args:
            kind = "job" if cmd == "run" else "provision"
            if project_conf(project_name()).get("benchfile"):
                if cmd == "run" and "--" in args:
                    # run a k=v -- b k=v -- c: a straight chain, each step after_ok the one before
                    # a priority= in any step is the whole chain's
                    groups, cur, prios = [], [], set()
                    for w in args + ["--"]:
                        if w == "--":
                            if not cur:
                                raise Refused("run a [key=value...] -- b [key=value...]: an empty step")
                            groups.append(cur)
                            cur = []
                        elif w.startswith("priority="):
                            prios.add(w)
                        else:
                            cur.append(w)
                    if len(prios) > 1:
                        raise Refused(f"run a -- b: one priority for the whole chain ({', '.join(sorted(prios))})", topic="priority")
                    prio = take_deps(list(prios))[3]
                    return chain([f"s{n} = {' '.join(g)}" + (f" after_ok=s{n - 1}" if n > 1 else "")
                                  for n, g in enumerate(groups, 1)], prio=prio)
                # Benchfile: run <job> [key=value...] [after=<id>,...] [after_ok=<id>,...]
                rest, deps, ext, prio = take_deps(args[1:])
                return submit(kind, None, args[0], parse_params(rest), deps, ext, prio)
            if len(args) == 2:
                return submit(kind, args[0], args[1])
            raise Refused(f"usage: {cmd} <branch> <name>")
        if cmd == "push" and len(args) == 1:
            return push(args[0])
        if cmd == "debug" and args:
            project = project_name()
            if project_conf(project).get("benchfile"):
                # Benchfile: debug <job> [key=value...]
                if not sys.stdin.isatty():
                    raise Refused("debug needs a terminal: ssh -t")
                return debug_v5(project, ceiling(project), args[0], parse_params(args[1:]))
            if len(args) == 2:
                return debug(args[0], args[1])
            raise Refused("usage: debug <branch> <job>")
        if cmd == "health" and args in ([], ["--json"]):
            print(json.dumps(health(project_name()), indent=1))
            return 0
        if cmd == "queue" and args in ([], ["--json"]):
            return queue_text(project_name(), args == ["--json"])
        if cmd == "box" and args == ["topology"]:
            print(json.dumps(topology(), indent=1))
            return 0
        if cmd == "cancel" and (len(args) == 1 or len(args) == 2 and args[0] == "--branch"):
            return cancel(args)
        if cmd == "wait" and len(args) == 1:
            return follow(job_dir(args[0]))
        if cmd == "status" and len(args) in (1, 2) and args[1:] in ([], ["--json"]):
            d = job_dir(args[0])
            sm = read_json_file(d / "summary.json")
            state = state_of(d)
            held = queue_flag() if state == "queued" and hold() else {}
            sched = read_state_json("state.json")
            view = queue_view(sched, recent_events(), project_name()) if state in ("queued", "running") else {}
            jid = d.name
            place = dict(view.get("waiting", {}).get(jid, {}))
            if jid in view.get("ends", {}):
                place["eta_end"] = view["ends"][jid]
            for q in sched.get("queue", []):
                if q["id"] == jid and q.get("waiting"):
                    place["waiting"] = q["waiting"]
            for b in sched.get("deps", []):
                if b["id"] == jid:
                    place.update({k: b.get(k) for k in ("after", "after_ok", "waiting_for")},
                                 eta_start=view.get("blocked", {}).get(jid, {}).get("eta_start"))
            if args[1:] == ["--json"]:
                print(json.dumps({"id": d.name, "ext_job_id": read_json_file(d / "request").get("ext_job_id"),
                                  "state": state, "summary": sm,
                                  "held": held or None, "queue": place or None}, indent=1))
                return 0
            print(f"{state} ({held_text()})" if held else state)
            if "waiting_for" in place:
                print(f"waiting for dependencies: {place['waiting_for'] or 'checking'}"
                      + "".join(f"; {k}={','.join(place[k])}" for k in ("after", "after_ok") if place.get(k))
                      + (f"; then queued, not before ~{clock(place['eta_start'], '%H:%M')}" if place.get("eta_start") else ""))
            if place.get("position"):
                print((f"waiting: {place['waiting']}; " if place.get("waiting") else "") + queue_line(view, jid))
            if place.get("eta_end"):
                print(f"expected end ~{clock(place['eta_end'], '%H:%M')} (estimate: the median of its last runs)")
            if summary_line(sm):
                print(summary_line(sm))
            for f in sm.get("failures", []):
                print(f"FAILED {f['test']}: {f['message']}")
            for line in sm.get("errors", []):
                print(line if line.lower().startswith("error") else f"error: {line}")
            for name, o in sm.get("outputs", {}).items():
                print(f"output {name} ({o['kind']}): {o['path']}" + ("" if o.get("present") else " (missing)"))
            return 0
        if cmd == "fetch" and len(args) >= 3 and args[0] == "latest":
            # the latest successful run of a job (with these parameters) that
            # holds this output: fetch latest build-gui branch=main gui
            job, output, want = args[1], args[-1], parse_params(args[2:-1])
            for d in sorted(jobs_dir().glob("2*"), reverse=True):
                req = read_json_file(d / "request")
                if req.get("name") != job or state_of(d) != "exit 0":
                    continue
                if any(str(req.get("params", {}).get(k)) != str(v) for k, v in want.items()):
                    continue
                if output in read_json_file(d / "summary.json").get("outputs", {}):
                    args = [d.name, output]
                    print(f"mcu-job: latest {job} with {output}: {d.name}", file=sys.stderr)
                    break
            else:
                raise Refused(f"no successful run of {job} {' '.join(args[2:-1])} with an output {output} "
                              "(kept for the retention)")
        if cmd == "fetch" and len(args) == 2:
            # one declared output of the job
            d = job_dir(args[0])
            # the outputs as the job declared them, recorded in its summary
            spec = read_json_file(d / "summary.json").get("outputs", {}).get(args[1])
            if not spec:
                raise Refused(f"job {args[0]} has no output {args[1]} (`status {args[0]}` lists them)")
            target = (d / "out" / spec["path"]).resolve()
            if not str(target).startswith(str((d / "out").resolve())) or not target.exists():
                raise Refused(f"output {args[1]}: {spec['path']} is not in the job's results")
            with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as tar:
                tar.add(target, arcname=spec["path"].rstrip("/"))
            return 0
        if cmd == "fetch" and len(args) == 1:
            d = job_dir(args[0])
            with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as tar:
                for name in ("request", "log", "status", "conditions.json", "out"):
                    if (d / name).exists():
                        tar.add(d / name, arcname=name)
            return 0
        if cmd == "config" and args:
            return config(args)
        if cmd == "storage" and args:
            return storage(args)
        if cmd == "volume" and args:
            return volume(args)
        if cmd == "list" and not args:
            for d in sorted(jobs_dir().glob("2*"), reverse=True)[:50]:
                text = (d / "request").read_text().strip() if (d / "request").exists() else ""
                try:
                    req = json.loads(text)
                    where = (f"on {req['branch']}" if req.get("branch")
                             else " ".join(f"{k}={v}" for k, v in req.get("params", {}).items()))
                    what = f"{req['kind']} {req['name']} {where}".rstrip()
                except ValueError:
                    what = text  # a job from before the JSON requests
                brief = summary_line(read_json_file(d / "summary.json"))
                print(f"{d.name}  {state_of(d):<10} {what}" + (f"  [{brief}]" if brief else ""))
            return 0
        print("docs: `docs --list` for the topics, `docs agents` to start, `docs <topic>` for one section\n"
              "usage: run <job> [key=value...] | provision <volume> [key=value...]   (Benchfile)\n"
              "       run <branch> <job> | provision <branch> <volume>   (repo + manifest)\n"
              "       | push <name> < tar | debug <job> [key=value...] (Benchfile) | debug <branch> <job>\n"
              "       | cancel <id> | cancel --branch <branch>\n"
              "       | wait <id> | status <id> [--json] | fetch <id> [<output>] | fetch latest <job> [key=value...] <output>\n"
              "       | list | box | box topology | queue [--json]\n"
              "       | run <job> [key=value...] after=<id>[,<id>...] | after_ok=<id>[,...]   (start once they ended / succeeded)\n"
              "       | run a [key=value...] -- b [key=value...] -- ...   (each step after_ok the one before)\n"
              "       | run <job> ... priority=high|low | bump <id> [high|normal|low]   (order within the project's places)\n"
              "       | chain [--detach] [priority=<level>] < steps   (one \"name = job [key=value...] [after_ok=name,...]\" per line)\n"
              "       | box wait-open [--timeout <s>] | health [--json | --wait]\n"
              "       | config show | config benchfile [<branch>] < file | config benchfile rm <branch>\n"
              "       | config file <name> < file\n"
              "       | config repo <https url> | config manifest <path> | config disk [ssd|hdd] <size>\n"
              "       | storage list | storage rm <name>[/<sub>] | storage prune [--older-than 7d] [--keep <sub>]... [--dry-run]\n"
              "       | volume list | volume create <name> [reader...] | volume readers <name> [reader...]\n"
              "       | docs [--list | <topic>]   (this box's documentation, at the version it runs)")
        return 0 if cmd == "help" else 2
    except Unavailable as e:
        print(f"mcu-job: unavailable: {e}; see: docs degraded", file=sys.stderr)
        return EX_TEMPFAIL
    except Misconfigured as e:
        print(f"mcu-job: refused (exit {EX_CONFIG}, retrying cannot help): {e}", file=sys.stderr)
        return EX_CONFIG
    except Refused as e:
        print(f"mcu-job: refused: {e}", file=sys.stderr)
        return 2


def envelope_summary():
    """what jobs may hold, as the scheduler counts it: the threads
    and memory all jobs share, the limits per job and per project"""
    env = box_conf().get("envelope", {})
    threads, mem = job_threads(), job_memory()
    reserved = box_conf().get("reserved", "0")
    out = {"job_threads": len(threads), "machine_threads": os.cpu_count(), "reserved_threads": reserved,
           "job_memory": mem, "job_max_threads": int(env.get("max_cores", 4)),
           "job_max_memory": parse_size(env.get("memory", "8g")), "project_max_jobs": int(env.get("max_jobs", 2)),
           "build_threads": int(env.get("build_cores", 4))}
    out["summary"] = (f"{len(threads)} of {os.cpu_count()} threads for jobs (reserved {reserved} for the system), "
                      f"{mem / 2**30:.1f} GB of memory shared by all jobs; a job: at most {out['job_max_threads']} threads "
                      f"and {out['job_max_memory'] / 2**30:.0f} GB; a project: {out['project_max_jobs']} jobs at once; "
                      f"an image build: {out['build_threads']} threads")
    return out


def box_status():
    """one line (ok, degraded or maintenance) then the details as JSON:
    cheap enough to ask before submitting"""
    res = read_state_json("resources.json")
    sched = read_state_json("state.json")
    problems = []
    reason = maintenance()
    if time.time() - res.get("updated", 0) > 180:
        problems.append("box monitor stale")
    ejected = gpu_ejected()
    gpu = not ejected and gpu_present()
    flag = gpu_flag()
    gpu_state = ejected or ("present" if gpu else "lost" if flag.get("state") == "lost" else "absent")
    if not gpu:
        problems.append(f"gpu {gpu_state}" + (" by the admin" if ejected else
                                              f" ({flag.get('note')})" if gpu_state == "lost" and flag.get("note") else ""))
    status = (f"maintenance: {reason}{until_text(queue_flag())}" if reason
              else ("degraded: " + ", ".join(problems) if problems else "ok"))
    mode = queue_mode()
    print(status + (f" (queue paused: {hold()})" if mode == "paused" else ""))
    print(json.dumps({
        "status": "maintenance" if reason else ("degraded" if problems else "ok"),
        "maintenance": reason, "queue": mode, "hold": hold(), "problems": problems, "gpu": gpu,
        "maintenance_until": queue_flag().get("until") if reason else None,
        "view": view_line(project_name()),
        "version": mcuconf.version(),
        "hold_until": queue_flag().get("until"),
        "gpu_state": gpu_state, "gpu_name": res.get("gpu_name"), "gpu_since": res.get("gpu_since"),
        "gpu_note": flag.get("note") if gpu_state == "lost" else None,
        "threads": len(job_threads()), "memory": job_memory(),
        "running": len(sched.get("running", [])), "queued": len(sched.get("queue", [])),
        "disk": disk_use(project_name()), "disk_warning": disk_warning(project_name()),
        "purged": purge_notice(project_name()),
        "exclusive": [x for x in EXCLUSIVE if x != "gpu" or gpu],
        "envelope": envelope_summary(),
    }, indent=1))
    return 0


def cancel(args):
    """cancel <id> | cancel --branch <b>: the project's own jobs, queued or
    running: the runner gets SIGTERM, stops the container (SIGTERM, SIGKILL
    after 10 s), keeps the log and out/, and ends with exit 130 (cancelled)"""
    project = project_name()
    sched = read_state_json("state.json")
    mine = {r["id"]: r for r in sched.get("running", []) + sched.get("queue", []) + sched.get("deps", [])
            if r.get("project") == project}
    if args[0] == "--branch":
        b = args[1]
        ids = [i for i, r in mine.items() if not r.get("build")
               and (r.get("branch") == b or f"branch={b}" in str(r.get("branch", "")).split())]
        if not ids:
            print(f"mcu-job: no queued or running job of {project} with branch={b}")
            return 0
    else:
        ids = [job_dir(args[0]).name]
    code = 0
    for jid in ids:
        d = jobs_dir() / jid
        if not d.is_dir():
            raise Refused(f"no job {jid} of {project}")
        if (d / "status").exists():
            print(f"mcu-job: {jid} already ended ({state_of(d)})")
            continue
        try:
            pid = int((d / "pid").read_text())
        except (OSError, ValueError):
            pid = mine.get(jid, {}).get("pid")
        (d / "cancel").write_text(f"`cancel` at {time.strftime('%H:%M:%S')}\n")
        try:
            os.kill(pid, signal.SIGTERM)
        except (TypeError, ProcessLookupError, PermissionError):
            pid = None
        for _ in range(30):
            if (d / "status").exists():
                break
            time.sleep(1)
        if not (d / "status").exists():
            # a runner that did not answer (or is gone): end the job here
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            stop_containers(jid)
            Scheduler().release(jid)
            with open(d / "log", "a") as f:
                f.write("bench: cancelled (the runner did not answer)\n")
            (d / "status").write_text(f"exit {EX_CANCELLED} (cancelled)\n")
            req = read_json_file(d / "request") or {}
            event(kind="done", id=jid, project=project, job=req.get("name"), branch=req.get("branch"),
                  params=req.get("params", {}), status=EX_CANCELLED, reason="cancelled")
        print(f"mcu-job: {jid} {state_of(d)}")
        for b in read_state_json("state.json").get("deps", []):
            if b.get("project") == project and jid in b.get("after_ok", []):
                print(f"mcu-job: {b['id']} ({b['job']}, after_ok) ends with exit {EX_DEPENDENCY}: not run")
            elif b.get("project") == project and jid in b.get("after", []):
                print(f"mcu-job: {b['id']} ({b['job']}, after) goes on: {jid} has ended")
    return code


def recover():
    """root, at boot (mcu-recover.service, before sshd): every job the
    restart killed gets a status, so wait and status never hang on it"""
    for path in (STATE / "projects").glob("*.json"):
        project = path.stem
        home = Path(f"/home/mcu-{project}")
        for d in sorted((home / "jobs").glob("2*")):
            if not d.is_dir() or (d / "status").exists() or not ID_RE.match(d.name):
                continue
            try:
                req = json.loads((d / "request").read_text())
            except (OSError, ValueError):
                req = {}
            with open(d / "log", "a") as f:
                f.write("bench: interrupted: box restarted\n")
            (d / "status").write_text(f"exit {EX_TEMPFAIL}\n")
            owner = (home / "jobs").stat()
            for name in ("log", "status"):
                os.chown(d / name, owner.st_uid, owner.st_gid)
            event(kind="done", id=d.name, project=project, job=req.get("name"), branch=req.get("branch"),
                  params=req.get("params", {}), status=EX_TEMPFAIL, reason="interrupted: box restarted")
    # a restart the admin asked for (mcu-admin shutdown): the queue it
    # closed opens again; the eGPU comes back on its own at boot
    try:
        sd = json.loads((STATE / "shutdown.json").read_text())
        if sd.get("closed_queue"):
            subprocess.run(["/usr/local/lib/mcu/mcu-admin", "queue", "open"], capture_output=True)
        (STATE / "shutdown.json").unlink()
        event(kind="box", shutdown="done", action=sd.get("action"))
    except (OSError, ValueError):
        pass
    # a timing profile a crash left behind
    try:
        from hw import cpufreq
        cpufreq.reset()
    except OSError:
        pass
    # the pids in state.json mean nothing after a reboot, and may be reused
    (STATE / "state.json").write_text(json.dumps({"queue": [], "running": []}))
    event(kind="box", change="restarted")
    return 0


# the box's documentation: installed beside mcu.py; in a checkout, docs/
DOCS = next((p for p in (Path(__file__).resolve().parent / "USING.md",
                         Path(__file__).resolve().parent.parent / "docs" / "USING.md") if p.exists()), None)
TOPIC_RE = re.compile(r"^<!-- topic: ([a-z0-9 -]+) -->\Z")


def doc_sections(text):
    """{topic: (heading, the section's text)}: a section runs from the
    heading above its `<!-- topic: ... -->` marker to the next heading of
    the same level or higher (headings inside code blocks do not count)"""
    lines = text.splitlines()
    heads, fenced = [], False
    for i, line in enumerate(lines):
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(r"^#{1,6} ", line):
            heads.append((i, len(line) - len(line.lstrip("#"))))
    out = {}
    for n, (i, level) in enumerate(heads):
        m = TOPIC_RE.match(lines[i + 1].strip()) if i + 1 < len(lines) else None
        if not m:
            continue
        end = next((j for j, lv in heads[n + 1:] if lv <= level), len(lines))
        body = "\n".join(l for l in lines[i:end] if not TOPIC_RE.match(l.strip())).rstrip() + "\n"
        for topic in m.group(1).split():
            out[topic] = (lines[i].lstrip("# ").strip(), body)
    return out


def docs(topic):
    """docs: the whole guide; docs --list: its topics; docs <topic>: one section"""
    if not DOCS:
        raise Refused("this box has no documentation installed (the admin's deploy installs docs/USING.md)")
    text = DOCS.read_text()
    if topic is None:
        print("\n".join(l for l in text.splitlines() if not TOPIC_RE.match(l.strip())))
        return 0
    sections = doc_sections(text)
    if topic == "--list":
        width = max(map(len, sections))
        for t, (heading, _) in sorted(sections.items()):
            print(f"{t:<{width}}  {heading}")
        return 0
    if topic not in sections:
        raise Refused(f"no topic {topic!r} (topics: {' '.join(sorted(sections))})")
    print(sections[topic][1], end="")
    return 0


def benchfile_summary(conf, label, version):
    """what a stored Benchfile puts in force, for `config show`"""
    path = benchfile_path(label, version)
    out = {"version": version, "stored": int(path.stat().st_mtime) if path.exists() else None,
           "from": (conf.get("benchfile_from") if label is None else conf.get("benchfiles_from", {}).get(label))}
    try:
        bf = load_toml(path)
    except (OSError, tomllib.TOMLDecodeError):
        return out
    jobs = {}
    for name, job in bf.get("jobs", {}).items():
        mounts = job.get("mounts", {})
        jobs[name] = {k: job[k] for k in ("cores", "memory", "timeout", "exclusive") if k in job}
        if job.get("outputs"):
            jobs[name]["outputs"] = sorted(job["outputs"])
        if mounts:
            jobs[name]["storage"] = sorted(m.split("/")[0] for m in mounts)
    out["jobs"] = jobs
    out["storage"] = sorted(storage_decls(bf))
    out["volumes"] = sorted(bf.get("volumes", {}))
    return out


FROM_RE = re.compile(r"^[A-Za-z0-9._/-]{1,80}(@[0-9a-f]{7,40})?\Z")


HELPER = ["sudo", "-n", "/usr/local/lib/mcu/mcu-helper"]
# the Benchfile formats this box reads (`format = N` at the top; none is 1):
# within a format, a Benchfile that worked keeps working
BENCHFILE_FORMAT = 1
URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]+)?/[A-Za-z0-9._~/-]+\Z")
PATH_RE = re.compile(r"^[A-Za-z0-9._-][A-Za-z0-9._/-]{0,199}\Z")


def config(args):
    """the project's self-service settings, inside the box's envelope"""
    project = project_name()
    path = STATE / "projects" / f"{project}.json"
    conf = json.loads(path.read_text())
    env = box_conf().get("envelope", {})
    if args == ["show"]:
        labels = {}
        for label, version in conf.get("benchfiles", {}).items():
            used = label_dir(label) / ".used"
            labels[label] = dict(benchfile_summary(conf, label, version),
                                 last_use=used.stat().st_mtime if used.exists() else None)
        shown = {k: v for k, v in conf.items() if k not in ("benchfile_from", "benchfiles_from")}
        if conf.get("benchfile"):
            # the default Benchfile, in force for every branch without its own
            # ("benchfile" stays its version: clients test it)
            shown["in_force"] = benchfile_summary(conf, None, conf["benchfile"])
        print(json.dumps({"project": project, **shown, "benchfiles": labels, "envelope": env}, indent=1))
        return 0
    if args[:2] == ["benchfile", "rm"] and len(args) == 3:
        label = args[2]
        if label not in conf.get("benchfiles", {}):
            raise Refused(f"no Benchfile {label} (`config show` lists them under benchfiles)")
        sched = read_state_json("state.json")
        busy = [r["job"] for r in sched.get("running", []) + sched.get("queue", [])
                if r["project"] == project and r.get("benchfile") == label]
        if busy:
            raise Refused(f"Benchfile {label} is used by queued or running job(s) ({', '.join(busy)}); remove it when they are done")
        shutil.rmtree(label_dir(label), ignore_errors=True)
        del conf["benchfiles"][label]
        conf.get("label_jobs", {}).pop(label, None)
        conf.get("benchfiles_from", {}).pop(label, None)
        note_disk_source(conf)
        path.write_text(json.dumps(conf, indent=1) + "\n")
        print(f"mcu-job: Benchfile {label} removed; its image goes at the next build or prune")
        return 0
    source = next((a for a in args if a.startswith("from=")), None)
    if source and args[:1] == ["benchfile"]:
        args = [a for a in args if a != source]
        source = source[len("from="):]
        if not FROM_RE.match(source):
            raise Refused(f"bad from={source}: <branch> or <branch>@<commit sha>", topic="benchfile")
    if args[:1] == ["benchfile"] and len(args) <= 2:
        label = args[1] if len(args) == 2 and args[1] != "default" else None
        if label is not None and (not LABEL_RE.match(label) or ".." in label):
            raise Refused(f"bad Benchfile name {label!r}: letters, digits, . _ - and /, 80 at most (a branch name)")
        text = sys.stdin.read(256 * 1024)
        try:
            parsed = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise Refused(f"the Benchfile is not valid TOML: {e}")
        if "from" not in parsed.get("image", {}):
            raise Refused("the Benchfile needs [image] with `from`")
        fmt = parsed.get("format", 1)
        if not isinstance(fmt, int) or not 1 <= fmt <= BENCHFILE_FORMAT:
            raise Refused(f"Benchfile format {fmt!r}: this box runs mcu {mcuconf.version()} and reads formats up to "
                          f"{BENCHFILE_FORMAT}", topic="benchfile")
        if not parsed.get("jobs"):
            raise Refused("the Benchfile declares no [jobs.<name>]")
        for name in parsed["jobs"]:
            if not NAME_RE.match(name):
                raise Refused(f"bad job name {name!r}")
        changes = benchfile_plan(project, conf, parsed, env, label)
        before = conf.get("benchfile") if label is None else conf.get("benchfiles", {}).get(label)
        try:
            old = load_toml(benchfile_path(label, before)) if before else None
        except (OSError, tomllib.TOMLDecodeError):
            old = None
        if old:
            moved = [j for j in parsed["jobs"] if j in old.get("jobs", {})
                     and job_media(parsed, j) != job_media(old, j)]
            if moved and forget_disk(project, moved):
                print(f"mcu-job: storage moved pool type for {' '.join(moved)}: their learned disk needs are reset")
        # each job's about, for the dashboard (which does not read Benchfiles)
        abouts = {j: spec["about"] for j, spec in parsed["jobs"].items()}
        if label is None and old:
            # the default is every branch's Benchfile: say what this one takes away
            gone = sorted(set(old.get("jobs", {})) - set(parsed["jobs"]))
            new = sorted(set(parsed["jobs"]) - set(old.get("jobs", {})))
            if gone or new:
                print(f"mcu-job: warning: replaces the default Benchfile (v{before} -> v{before + 1}), "
                      f"used by every branch without its own: jobs removed: {' '.join(gone) or 'none'}; "
                      f"added: {' '.join(new) or 'none'}. For one branch only: config benchfile <branch>",
                      file=sys.stderr)
        if label is None:
            version = conf.get("benchfile", 0) + 1
            conf["benchfile"] = version
            conf["jobs"] = abouts
            conf.pop("benchfile_from", None)
            if source:
                conf["benchfile_from"] = source
        else:
            version = conf.setdefault("benchfiles", {}).get(label, 0) + 1
            conf["benchfiles"][label] = version
            conf.setdefault("label_jobs", {})[label] = abouts
            conf.setdefault("benchfiles_from", {}).pop(label, None)
            if source:
                conf["benchfiles_from"][label] = source
        dest = benchfile_path(label, version)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        note_disk_source(conf)
        path.write_text(json.dumps(conf, indent=1) + "\n")
        print(f"mcu-job: {benchfile_name(label, version)} stored ({len(parsed['jobs'])} jobs: {' '.join(parsed['jobs'])})"
              + ("; jobs with branch=" + label + " use it" if label else ""))
        return benchfile_apply(project, changes, parsed)
    if len(args) == 2 and args[0] == "file":
        if not FILE_RE.match(args[1]):
            raise Refused("bad file name")
        data = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise Refused("files for the image are limited to 1 MB; larger data belongs in a volume")
        files = benchfile_dir() / "files"
        files.mkdir(parents=True, exist_ok=True)
        (files / args[1]).write_bytes(data)
        print(f"mcu-job: file {args[1]} stored ({len(data)} bytes)")
        return 0
    if args == ["usage"]:
        code = disk_report(project) or duration_report(project)
        print(f"view       read-only, in place: {view_line(project)}")
        print("           (storage/<name>, jobs/<id>, latest/<job>; docs view)")
        return code
    if args[:1] == ["clean"] and len(args) in (2, 3):
        return clean(project, conf, args[1:])
    if len(args) == 3 and args[0] == "disk" and args[1] in ("ssd", "hdd"):
        refuse_if_benchfile_sets_disk(conf, args[1])
        return subprocess.run(HELPER + ["quota", args[1], args[2]]).returncode
    if len(args) != 2:
        raise Refused("usage: config show | config usage | config benchfile [<branch>] | config benchfile rm <branch>"
                      " | config clean images|cache <name>[/<sub>]|src"
                      " | config repo <https url> | config manifest <path> | config disk [ssd|hdd] <size>")
    key, value = args
    if key == "repo":
        if not URL_RE.match(value) or ".." in value:
            raise Refused("repo must be an https URL")
        if value != conf.get("repo"):
            # a new repository starts from a fresh checkout
            shutil.rmtree(Path.home() / "src", ignore_errors=True)
            (Path.home() / "src").mkdir()
        conf["repo"] = value
    elif key == "manifest":
        if not PATH_RE.match(value) or ".." in value:
            raise Refused("manifest must be a relative path in the repository")
        conf["manifest"] = value
    elif key == "disk":
        refuse_if_benchfile_sets_disk(conf, "ssd")
        return subprocess.run(HELPER + ["quota", value]).returncode
    else:
        raise Refused(f"unknown setting {key}")
    path.write_text(json.dumps(conf, indent=1) + "\n")
    print(f"mcu-job: {project} {key} = {value}")
    return 0


def benchfile_disk(manifest):
    """[project] disk: a size (the ssd quota) or { ssd = …, hdd = … }"""
    d = manifest.get("project", {}).get("disk")
    if d is None:
        return {}
    if isinstance(d, dict):
        bad = [k for k in d if k not in ("ssd", "hdd")]
        if bad:
            raise Refused(f"[project] disk: pool types are ssd and hdd, not {', '.join(bad)}; nothing stored", topic="quotas")
        return {k: str(v) for k, v in d.items()}
    return {"ssd": str(d)}


def disk_set_by_benchfile(conf, media):
    """the Benchfile in use that sets the project's quota on a pool type:
    (label or None, version, size), or None. The project has one writer for
    its quota: when a Benchfile declares it, `config disk` defers to it"""
    for label, version in sorted(live_benchfiles(conf).items(), key=lambda kv: kv[0] or ""):
        try:
            d = benchfile_disk(load_toml(benchfile_path(label, version))).get(media)
        except (OSError, tomllib.TOMLDecodeError, Refused):
            continue
        if d is not None:
            return label, version, d
    return None


def note_disk_source(conf):
    """conf["disk_by_benchfile"]: which Benchfile in use sets the quota on
    each pool type, for the dashboard (which cannot read Benchfiles)"""
    by = {}
    for media in ("ssd", "hdd"):
        found = disk_set_by_benchfile(conf, media)
        if found:
            label, version, _ = found
            by[media] = benchfile_name(label, version)
    conf["disk_by_benchfile"] = by


def refuse_if_benchfile_sets_disk(conf, media):
    found = disk_set_by_benchfile(conf, media)
    if found:
        label, version, size = found
        name = benchfile_name(label, version)
        raise Refused(f"your {name} sets the {media} disk ({size}); change it there "
                      f"([project] disk), or remove it from the Benchfile to set it here", topic="quotas")


def benchfile_plan(project, conf, parsed, env, label=None):
    """everything an upload would change, checked before anything is stored
    (the whole Benchfile is refused if one part is above the envelope);
    prints the differences with the previous version"""
    say = lambda m: print(f"mcu-job: {m}", file=sys.stderr)
    declared = box_resources()
    old = {}
    live = live_benchfiles(conf)
    if label in live:
        try:
            old = load_toml(benchfile_path(label, live[label]))
        except (OSError, tomllib.TOMLDecodeError):
            old = {}
    # the other Benchfiles in use: what applies to the whole project (the
    # quota) takes them all into account, so one upload can't take away
    # what another branch still needs
    others = []
    for other, version in live.items():
        if other != label:
            try:
                others.append(load_toml(benchfile_path(other, version)))
            except (OSError, tomllib.TOMLDecodeError):
                pass
    # jobs, and the v5 spellings of resources
    oj, nj = old.get("jobs", {}), parsed["jobs"]
    for name in sorted(set(nj) - set(oj)):
        say(f"job {name}: new")
    for name in sorted(set(oj) - set(nj)):
        say(f"job {name}: removed")
    for name in sorted(set(nj) & set(oj)):
        if nj[name] != oj[name]:
            say(f"job {name}: changed ({', '.join(sorted(k for k in set(nj[name]) | set(oj[name]) if nj[name].get(k) != oj[name].get(k)))})")
    for name, job in nj.items():
        olds = [f"devices = {json.dumps(job['devices'])}"] if job.get("devices") else []
        olds += [f"exclusive {r}" for r in job.get("exclusive", []) if r in declared]
        if olds:
            new = sorted(set([ALIASES.get(d, d) for d in job.get("devices", [])]
                             + [r for r in job.get("exclusive", []) if r in declared]))
            say(f"job {name}: {', '.join(olds)} is the v5 spelling; v6: resources = {json.dumps(new)}")
    # every storage states when it ends: in [storage], or on each
    # of its mounts; the rules are checked here, before anything is stored
    covered, decls = {}, storage_decls(parsed)
    # services: each checked now; their mounts state their end too
    svcs = parsed.get("services") or {}
    if not isinstance(svcs, dict):
        raise Refused("[services]: tables, [services.<name>]; nothing stored")
    for sname in svcs:
        svc = service_spec(parsed, sname)
        for mname in (svc["mounts"] if isinstance(svc["mounts"], dict) else {}):
            if "/" not in mname and mname not in decls:
                say(f"service {sname}: mount {mname} is not a [storage]: it must be a volume {project} may read")
    for jname, job in nj.items():
        names = job.get("services", [])
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise Refused(f"[jobs.{jname}] services: a list of [services] names; nothing stored")
        unknown = [n for n in names if n not in svcs]
        if unknown:
            raise Refused(f"[jobs.{jname}] services: {', '.join(unknown)} not in [services]; nothing stored")
        if names:
            say(f"job {jname}: services {', '.join(names)}, in its pod for the job's duration, removed when it ends")
    unused = sorted(set(svcs) - {n for job in nj.values() for n in job.get("services", [])})
    if unused:
        say(f"[services] {', '.join(unused)}: no job lists it")
    owners = [(f"job {j}", job.get("mounts") or {}) for j, job in nj.items()]
    owners += [(f"service {n}", spec.get("mounts") or {}) for n, spec in svcs.items()]
    for jname, mounts in owners:
        for mname, target in mounts.items():
            base = mname.partition("/")[0]
            ok = isinstance(target, dict) and life_of(target, where=f"{jname} mount {mname}") is not None
            covered.setdefault(base, []).append(ok)
            life = life_of(decls.get(base, {}), target if isinstance(target, dict) else None,
                           where=f"{jname} mount {mname}")
            sub = mname.partition("/")[2]
            if life and life["until"] and until_param(life["until"], sub) != life["until"]:
                key = until_param(life["until"], sub)
                say(f"{jname} mount {mname}: until = \"{life['until']}\", but the sub is keyed on {key}: "
                    f"the rule follows {key}; write until = \"{key}\"")
    undeclared = []
    for n, spec in decls.items():
        if life_of(spec, where=f"storage {n}") is None and not (covered.get(n) and all(covered[n])):
            undeclared.append(n)
    if undeclared:
        hint = ("say when each ends: drop = true, until = \"<the parameter naming the branch>\", ttl = \"14d\", or keep = true + max = \"40g\" "
                "(they combine, the first reached wins)")
        if time.strftime("%Y-%m-%d") >= DECLARE_BY:
            raise Misconfigured(f"storage {', '.join(undeclared)}: no stated end; {hint}", topic="storage")
        say(f"warning: storage {', '.join(undeclared)}: no stated end. Until {DECLARE_BY} it is kept as before "
            f"(subs removed after 14 days unused, evicted when room is short); from then on such a Benchfile "
            f"is refused. {hint}")
    # sizing: a job too big to run beside another one
    threads, pool = len(job_threads()), job_memory()
    for name, job in nj.items():
        cores = int(job.get("cores", 1))
        try:
            mem = parse_size(job.get("memory", env.get("memory", "8g")))
        except Exception:
            mem = 0
        if cores > threads // 2:
            say(f"job {name}: {cores} threads of the {threads} for jobs: it never runs beside another job; "
                f"{threads // 2} or fewer let two run at once")
        if mem > pool / 2:
            say(f"job {name}: {mem / 2**30:.1f} GB of the {pool / 2**30:.1f} GB all jobs share: "
                f"two such jobs can't run together; {pool / 2 / 2**30:.1f} GB or less lets them")
    # media: where storages, volumes and a job's /work go
    MEDIA = ("ssd", "hdd", "ssd!", "hdd!")
    asks = [(f"storage {n}", spec.get("media")) for n, spec in storage_decls(parsed).items()]
    asks += [(f"volume {n}", (spec or {}).get("media")) for n, spec in parsed.get("volumes", {}).items()
             if isinstance(spec, dict)]
    asks += [(f"job {n} work_media", job.get("work_media")) for n, job in nj.items()]
    asks = [(what, m) for what, m in asks if m is not None]
    if any(m not in MEDIA for _, m in asks):
        bad = next(what for what, m in asks if m not in MEDIA)
        raise Refused(f"{bad}: media is one of {', '.join(MEDIA)}; nothing stored")
    if asks:
        r = subprocess.run(HELPER + ["pools-media"], capture_output=True, text=True)
        have = set(json.loads(r.stdout).values()) if r.returncode == 0 and r.stdout.strip() else set()
        for what, m in asks:
            if m.rstrip("!") not in have:
                if m.endswith("!"):
                    raise Misconfigured(f"{what}: media = {m}, and this box has no {m.rstrip('!')} pool; nothing stored")
                say(f"{what}: media = {m}, but this box has no {m} pool: it stays on the usual one")
    for name, job in nj.items():
        for oname, spec in (job.get("outputs") or {}).items():
            path = str((spec or {}).get("path", "")) if isinstance(spec, dict) else ""
            if not NAME_RE.match(oname) or not path or path.startswith("/") or ".." in Path(path).parts \
                    or (spec.get("kind", "file") not in OUTPUT_KINDS):
                raise Refused(f"[jobs.{name}] outputs.{oname}: a relative path in /out (no ..) and a kind among "
                              f"{', '.join(OUTPUT_KINDS)}; nothing stored")
        if job.get("sandbox") is True:
            say(f"job {name}: sandbox = true, its container may run sandboxes (bwrap)")
        elif "sandbox" in job and not isinstance(job["sandbox"], bool):
            raise Refused(f"[jobs.{name}] sandbox: true or false; nothing stored")
        about = job.get("about")
        if not isinstance(about, str) or not about.strip() or "\n" in about or len(about) > ABOUT_MAX:
            raise Refused(f"[jobs.{name}] about: one line of at most {ABOUT_MAX} characters saying what the job "
                          f"does (about = \"the workspace tests\"); every job has one; nothing stored")
        if "checkouts" in job:
            names = job["checkouts"]
            if not isinstance(names, list) or not all(isinstance(c, str) for c in names):
                raise Refused(f"[jobs.{name}] checkouts: a list of [checkout] names; nothing stored")
            unknown = [c for c in names if c not in parsed.get("checkout", {})]
            if unknown:
                raise Refused(f"[jobs.{name}] checkouts: {', '.join(unknown)} not in [checkout]; nothing stored")
    # a checkout's ${param} without a default is needed by every job that
    # checks it out: say which, so one new checkout does not stop the queue
    for cname, spec in (parsed.get("checkout") or {}).items():
        if not isinstance(spec, dict):
            continue
        bare = sorted({m.group(1) for f in ("repo", "ref") for m in re.finditer(
            r"\$\{([a-z_][a-z0-9_]*)\}", str(spec.get(f, "")))})
        users = [j for j, job in nj.items() if not isinstance(job.get("checkouts"), list) or cname in job["checkouts"]]
        if bare and users:
            say(f"[checkout.{cname}]: {', '.join(bare)} has no default: jobs {', '.join(users)} are refused "
                f"unless run with {' '.join(b + '=...' for b in bare)} (a default: ${{{bare[0]}:-main}}; "
                f"or list checkouts in the jobs that do not need it)")
    if parsed.get("cache"):
        say("[cache] is the v5 spelling; v6: [storage] (tmp = true by default)")
    for cname, spec in storage_decls(parsed).items():
        if spec.get("per"):
            say(f"storage {cname}: per = {json.dumps(spec['per'])} is the v5 spelling; v6: mount "
                f"\"{cname}/${{{spec['per']}:-{spec.get('default', 'main')}}}\"")
    if old and parsed.get("image") != old.get("image"):
        say("[image] changed: the next job rebuilds the image (older ones are dropped after)")
    for sname, spec in svcs.items():
        if spec.get("image") and spec.get("image") != ((old.get("services") or {}).get(sname) or {}).get("image"):
            say(f"service {sname}: its image is built at the next job that uses it")
    # disk: [project] disk, within the envelope
    changes = {"disk": None, "create": [], "readers": []}
    mine = benchfile_disk(parsed)
    changes["disk_hdd"] = None
    for media in ("ssd", "hdd"):
        limit = (env.get("max_disk_ssd", env.get("max_disk", "0")) if media == "ssd"
                 else env.get("max_disk_hdd", "0"))
        if isinstance(env.get("max_disk"), dict):           # max_disk = { ssd = …, hdd = … }
            limit = env["max_disk"].get(media, "0")
        disk = mine.get(media)
        if disk is not None and parse_size(disk) > parse_size(str(limit)):
            raise Refused(f"[project] disk {media} = {disk} is above the box's limit per project there ({limit}); nothing stored", topic="quotas")
        asked = [d for d in [disk] + [benchfile_disk(o).get(media) for o in others] if d is not None]
        if asked:
            largest = max(asked, key=parse_size)
            if disk is not None and largest != disk:
                say(f"disk quota {media}: {disk} asked, {largest} kept (another Benchfile in use needs it)")
            disk = largest
        current = conf.get("disk" if media == "ssd" else "disk_hdd")
        if disk is not None and str(disk) != str(current):
            say(f"disk quota {media}: {current or 'default'} -> {disk}")
            changes["disk" if media == "ssd" else "disk_hdd"] = str(disk)
    # volumes: [volumes.<name>] readers = [...], created or synced
    owned = {}
    for vp in (STATE / "volumes").glob("*.json"):
        v = json.loads(vp.read_text())
        if v.get("owner") == project:
            owned[vp.stem] = v
    vols = parsed.get("volumes", {})
    for name, spec in vols.items():
        if not NAME_RE.match(name) or not isinstance(spec, dict):
            raise Refused(f"bad [volumes.{name}]; nothing stored")
        readers = spec.get("readers", [])
        if not isinstance(readers, list) or not all(isinstance(r, str) and NAME_RE.match(r) for r in readers):
            raise Refused(f"[volumes.{name}] readers: a list of project names; nothing stored")
        # readers: every Benchfile in use that declares this volume adds its own
        readers = sorted(set(readers) | {r for o in others for r in (o.get("volumes", {}).get(name) or {}).get("readers", [])
                                         if isinstance(r, str) and NAME_RE.match(r)})
        unknown = [r for r in readers if not (STATE / "projects" / f"{r}.json").exists()]
        if unknown:
            raise Refused(f"[volumes.{name}] readers: no project {', '.join(unknown)} on this box; nothing stored")
        if (STATE / "volumes" / f"{name}.json").exists() and name not in owned:
            raise Refused(f"volume {name} belongs to another project; nothing stored")
        if name not in owned:
            changes["create"].append((name, readers))
            say(f"volume {name}: created (readers: {' '.join(readers) or 'none'}); fill it with `provision {name}`")
        elif sorted(owned[name].get("readers", [])) != sorted(readers):
            changes["readers"].append((name, readers))
            say(f"volume {name}: readers {' '.join(owned[name].get('readers', [])) or 'none'} -> {' '.join(readers) or 'none'}")
    if len(owned) + len(changes["create"]) > int(env.get("max_volumes", 4)):
        raise Refused(f"{len(owned) + len(changes['create'])} volumes would be above the box's limit "
                      f"({env.get('max_volumes', 4)}); nothing stored")
    for name in sorted(set(owned) - set(vols)):
        say(f"volume {name}: no longer declared, kept (its data is never deleted by an upload)")
    return changes


def benchfile_apply(project, changes, parsed=None):
    """what benchfile_plan found, through the root helper; then the
    storages and volumes that ask for a disk type they are not on get a
    queued move"""
    code = 0
    if changes["disk"]:
        code |= subprocess.run(HELPER + ["quota", changes["disk"]]).returncode
    if changes.get("disk_hdd"):
        code |= subprocess.run(HELPER + ["quota", "hdd", changes["disk_hdd"]]).returncode
    for name, readers in changes["create"]:
        code |= subprocess.run(HELPER + ["volume-create", name] + readers).returncode
    for name, readers in changes["readers"]:
        code |= subprocess.run(HELPER + ["volume-readers", name] + readers).returncode
    for n, spec in (storage_decls(parsed).items() if parsed else []):
        if spec.get("media"):
            subprocess.run(HELPER + ["place-media", f"storage/{n}", str(spec["media"])])
    for n, spec in ((parsed or {}).get("volumes") or {}).items():
        if isinstance(spec, dict) and spec.get("media"):
            subprocess.run(HELPER + ["place-media", f"volume/{n}", str(spec["media"])])
    return 1 if code else 0


def du(path):
    # through the link: a unit placed on another pool is a symlink in the home
    r = subprocess.run(["du", "-sb", str(Path(path).resolve())], capture_output=True, text=True)
    try:
        return int(r.stdout.split()[0])
    except (IndexError, ValueError):
        return 0


def disk_breakdown(project):
    """what fills the project's quota, in bytes; volumes it owns too"""
    home = Path.home()
    out = {"quota": disk_use(project).get("quota"), "used": disk_use(project).get("used"),
           "warning": disk_warning(project),
           "images": du(home / ".local/share/containers"), "src": du(home / "src"),
           "jobs": du(home / "jobs"), "caches": {}, "volumes": {}}
    cache = home / "cache"
    for c in sorted(cache.iterdir()) if cache.exists() else []:
        if c.name == ".git":
            out["git_cache"] = du(c)
            continue
        entry = {"bytes": du(c)}
        if (c / ".bench-per").exists():
            entry["per"] = {sub.name: du(sub) for sub in sorted(x for x in c.iterdir() if x.is_dir())}
        out["caches"][c.name] = entry
    for vp in (STATE / "volumes").glob("*.json"):
        try:
            v = json.loads(vp.read_text())
        except ValueError:
            continue
        if v.get("owner") == project:
            current = VOLUMES / vp.stem / "current"
            out["volumes"][vp.stem] = {"readers": v.get("readers", []),
                                       "version": current.resolve().name if current.exists() else None,
                                       "bytes": du(current.resolve()) if current.exists() else 0}
    return out


def quota_lines(u):
    """config usage's quota lines from usage.json's entry: one per pool type
    when the project has several (ssd, hdd), else the one quota"""
    gb = lambda b: f"{b / 2**30:6.2f} GB"
    media = {m: x for m, x in (u.get("media") or {}).items() if x.get("quota")}
    if len(media) > 1:
        return [f"quota {m:<4} {gb(x['used'])} used of {gb(x['quota'])} ({x['used'] / x['quota']:.0%}, measured every 5 min)"
                for m, x in sorted(media.items(), key=lambda kv: MEDIA.index(kv[0]) if kv[0] in MEDIA else 9)]
    if u.get("quota"):
        return [f"quota      {gb(u['used'])} used of {gb(u['quota'])} ({u['used'] / u['quota']:.0%}, measured every 5 min)"]
    return []


def disk_report(project):
    """config usage: what fills the project's quota"""
    home = Path.home()
    gb = lambda b: f"{b / 2**30:6.2f} GB"
    u = disk_use(project)
    for line in quota_lines(u):
        print(line)
    # with more than one pool type, each storage says which quota it fills
    per = disk_media(project) if len(u.get("media") or {}) > 1 else {}
    where = lambda path: f" [{media_of(path, per)}]" if per else ""
    warn = disk_warning(project)
    if warn:
        print(f"warning    {warn}")
    images = subprocess.run(["podman", "images", "--format", "{{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}"],
                            capture_output=True, text=True).stdout.strip()
    print(f"images     {gb(du(home / '.local/share/containers'))}  (podman storage; `config clean images`)")
    for line in images.splitlines():
        print(f"           {line}")
    print(f"src        {gb(du(home / 'src'))}  (repository mode checkout; `config clean src`)")
    cache = home / "cache"
    for c in sorted(cache.iterdir()) if cache.exists() else []:
        if c.name == ".git":
            print(f"git cache  {gb(du(c))}  ([checkout] clones, shared by the jobs)")
            continue
        print(f"cache      {gb(du(c))}  {c.name}{where(c)}  (`config clean cache {c.name}`)")
        if (c / ".bench-per").exists():
            for sub in sorted(x for x in c.iterdir() if x.is_dir()):
                print(f"             {gb(du(sub))}  {c.name}/{sub.name}")
    print(f"jobs       {gb(du(home / 'jobs'))}  (results, removed after the retention)")
    return 0


def duration_report(project):
    """config usage, its end: each job's learned run time (the last 20 runs
    that ended), so a client can order its queue; the queue's estimates use
    the median of the last 5"""
    durations = job_durations([e for e in recent_events() if e.get("project") == project], last=20)
    if not durations:
        return 0
    mins = lambda s: f"{s / 60:5.1f} min"
    print("durations  (last 20 runs of each job; the queue's estimates use the median of the last 5)")
    for (_, job), runs in sorted(durations.items(), key=lambda kv: str(kv[0][1])):
        ordered = sorted(runs)
        p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
        print(f"           {job:<24} median {mins(ordered[len(ordered) // 2])}  p90 {mins(p90)}  ({len(runs)} run{'s' if len(runs) > 1 else ''})")
    return 0


def storage(args):
    """storage list | storage rm <name>[/<sub>]: the project's named storage"""
    project = project_name()
    home = Path.home() / "cache"
    if args == ["list"]:
        declared = {}
        conf = project_conf(project)
        if conf.get("benchfile"):
            declared = {}
            for label, version in live_benchfiles(conf).items():
                declared.update(storage_decls(load_toml(benchfile_path(label, version))))
        rows = []
        for c in sorted(home.iterdir()) if home.exists() else []:
            if c.name == ".git" or not c.is_dir():
                continue
            subs = sorted(x for x in c.iterdir() if x.is_dir()) if (c / ".bench-per").exists() else []
            tmp = (c / ".bench-tmp").exists() or declared.get(c.name, {}).get("tmp", False)
            for d in subs or [c]:
                info = life_info(d)
                life = info.get("life") or life_of(declared.get(c.name, {}))
                rows.append((c.name + (f"/{d.name}" if subs else ""), du(d), d.stat().st_mtime, tmp,
                             c.name in declared, life_text(life, info)))
        print(f"{'storage':<40} {'size':>9}  {'last use':<21} tmp  declared  ends")
        for name, size, used, tmp, decl, ends in rows:
            print(f"{name:<40} {size / 2**30:7.2f} GB  {clock(used):<21}"
                  f" {'yes' if tmp else 'no ':<4} {'yes' if decl else 'no (left over)':<9} {ends}")
        return 0
    if len(args) == 2 and args[0] == "rm":
        return clean(project, project_conf(project), ["cache", args[1]])
    if args[:1] == ["prune"]:
        return storage_prune(project, args[1:])
    raise Refused("usage: storage list | storage rm <name>[/<sub>] | storage prune [--older-than 7d] [--keep <sub>]... [--dry-run]", topic="storage")


def storage_prune(project, args):
    """storage prune: drop the project's tmp storage subs unused for a while
    (7 days unless --older-than), in one call; never one a queued or running
    job mounts, nor one given with --keep (a sub, or name/sub)"""
    older, keep, dry = 7 * 86400, set(), False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--dry-run":
            dry = True
        elif a in ("--older-than", "--keep") and i + 1 < len(args):
            i += 1
            if a == "--keep":
                keep.add(args[i])
            else:
                m = re.fullmatch(r"(\d+)([mhd])", args[i])
                if not m:
                    raise Refused("--older-than takes a number and m, h or d (e.g. 2d)")
                older = int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]
        else:
            raise Refused(f"storage prune: unexpected {a}", topic="storage")
        i += 1
    cutoff = time.time() - older
    freed, n = 0, 0
    for mtime, name, path in tmp_subs(project, in_use_subs(project), keep):
        if mtime > cutoff:
            continue
        size = du(path)
        when = clock(mtime)
        print(f"{'would drop' if dry else 'dropped'} {name:<40} {size / 2**30:7.2f} GB  unused since {when}")
        if not dry:
            shutil.rmtree(path, ignore_errors=True)
            event(kind="evicted", project=project, storage=name, bytes=size, unused_since=mtime, by="prune")
        freed += size
        n += 1
    print(f"mcu-job: {n} storage sub(s), {freed / 2**30:.2f} GB {'would be freed' if dry else 'freed'}")
    if not dry and n:
        subprocess.run(HELPER + ["usage"], capture_output=True)
    return 0


def holds_storage(e, name, sub=None):
    """does a queue entry mount storage name (or its sub name/sub)? A job
    mounting only other subs of it does not hold this one; an entry from
    before the units and subs were recorded holds everything"""
    if "units" not in e:
        return True
    if f"storage/{name}" not in e["units"]:
        return False                    # an image build (units: images) mounts no storage
    if "subs" not in e:
        return True
    own = [x for x in e["subs"] if x.split("/")[0] == name]
    if sub is None or not own:
        return True                     # the whole storage, or a job mounting all of it
    return f"{name}/{sub}" in own


def clean(project, conf, what):
    """config clean: free the project's own disk. Refused while one of its
    queued or running jobs uses what goes: for a storage (or one of its
    subs), only the jobs mounting it count; src, any job. Images go even
    while jobs run, but those kept_tags keeps"""
    sched = read_state_json("state.json")
    mine = [r for r in sched.get("running", []) + sched.get("queue", []) if r["project"] == project]
    if what[:1] == ["cache"] and len(what) == 2:
        name, _, sub = what[1].partition("/")
        busy = [r["job"] for r in mine if holds_storage(r, name, sub or None)]
        if busy:
            raise Refused(f"storage {what[1]} is mounted by {project}'s queued or running job(s) "
                          f"({', '.join(busy)}); remove it when they are done")
    elif what == ["src"]:
        busy = [r["job"] for r in mine]
        if busy:
            raise Refused(f"{project} has jobs queued or running ({', '.join(busy)}); clean when they are done")
    home = Path.home()
    before = disk_use(project).get("used")
    if what == ["images"]:
        # while jobs run too: the images they (and the default Benchfile)
        # use stay, and podman refuses one a container runs on
        why = {}
        n = prune_images(project, keep=kept_tags(project, why))
        print(f"mcu-job: {n} image(s) removed; a job whose image went rebuilds it")
        for tag, reason in sorted(why.items()):
            print(f"  kept {tag.rsplit(':', 1)[1]}: {reason}")
    elif what == ["src"]:
        if not conf.get("benchfile"):
            raise Refused("src is the repository-mode checkout this project still uses")
        shutil.rmtree(home / "src", ignore_errors=True)
        (home / "src").mkdir(mode=0o700)
        print("mcu-job: src removed")
    elif what[0] == "cache" and len(what) == 2:
        name, _, sub = what[1].partition("/")
        target = home / "cache" / name / sub if sub else home / "cache" / name
        if not NAME_RE.match(name) or name == ".git" or (sub and not re.fullmatch(r"[A-Za-z0-9._-]+", sub)) \
                or not target.is_dir():
            raise Refused(f"no storage {what[1]} (see `storage list`)", topic="storage")
        size = du(target)
        try:
            if sub:
                shutil.rmtree(target)
            else:
                # the storage is a link to its pool: empty what it points
                # to, keeping the directory (the mount point) and its markers
                for x in target.resolve().iterdir():
                    if x.name in (".bench-per", ".bench-tmp"):
                        continue
                    if x.is_dir() and not x.is_symlink():
                        shutil.rmtree(x)
                    else:
                        x.unlink()
        except OSError as e:
            raise Refused(f"storage {what[1]}: could not remove everything ({e}); `storage list` shows what is left", topic="storage")
        print(f"mcu-job: storage {what[1]} removed ({size / 2**30:.2f} GB)")
    else:
        raise Refused("usage: config clean images | config clean cache <name>[/<sub>] | config clean src")
    subprocess.run(HELPER + ["usage"], capture_output=True)
    after = disk_use(project).get("used")
    if before and after is not None:
        print(f"mcu-job: disk {after / 2**30:.1f} GB used (was {before / 2**30:.1f})")
    return 0


def parse_params(words):
    params = {}
    for w in words:
        key, sep, value = w.partition("=")
        if not sep or not PARAM_KEY_RE.match(key) or not PARAM_VALUE_RE.match(value):
            raise Refused(f"bad parameter {w!r}: key=value, value in [A-Za-z0-9._/@+-]")
        params[key] = value
    return params


def push(name):
    """local code from the client, a tar on stdin, placed at /work/<name>
    for the project's next jobs; `push <name>` with an empty tar removes it"""
    if not NAME_RE.match(name):
        raise Refused("bad name")
    dest = Path.home() / "pushed" / name
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|*") as tar:
        tar.extractall(dest, filter="data")
    if not any(dest.iterdir()):
        dest.rmdir()
        print(f"mcu-job: {name} removed")
        return 0
    size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
    print(f"mcu-job: {name} pushed ({size / 1e6:.1f} MB), at /work/{name} in the next jobs")
    return 0


def volume(args):
    project = project_name()
    if args == ["list"]:
        for p in sorted((STATE / "volumes").glob("*.json")):
            v = json.loads(p.read_text())
            if v["owner"] == project or project in v.get("readers", []):
                current = VOLUMES / p.stem / "current"
                version = current.resolve().name if current.exists() else "not provisioned"
                print(f"{p.stem:<20} owner {v['owner']:<12} readers {' '.join(v['readers']) or '-':<20} {version}")
        return 0
    if args[0] in ("create", "readers") and len(args) >= 2:
        return subprocess.run(HELPER + [f"volume-{args[0]}"] + args[1:]).returncode
    raise Refused("usage: volume list | volume create <name> [reader...] | volume readers <name> [reader...]")


def branch_gone(value, repos, seen):
    """is this branch absent from every repository it may belong to? Only a
    clear answer counts: a repository that cannot be asked keeps it"""
    asked = alive = False
    for repo in repos:
        key = (repo, value)
        if key not in seen:
            r = subprocess.run(["git", "ls-remote", "--heads", repo, f"refs/heads/{value}"], capture_output=True,
                               text=True, timeout=60, env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
            seen[key] = None if r.returncode != 0 else bool(r.stdout.strip())
        if seen[key] is not None:
            asked = True
            alive = alive or seen[key]
    return asked and not alive


def storage_housekeeping(project):
    """root, daily: each storage (or sub) that states its end, applied: gone
    after its ttl unused, or once its branch is deleted on the remote;
    trimmed to its cap. Never one a queued or running job mounts"""
    sched = read_state_json("state.json")
    busy = {x for e in sched.get("running", []) + sched.get("queue", []) if e.get("project") == project
            for x in e.get("subs", [])}
    cache, seen = Path(f"/home/mcu-{project}/cache"), {}
    for c in sorted(cache.iterdir()) if cache.exists() else []:
        if c.name == ".git" or not c.is_dir():
            continue
        targets = [x for x in c.iterdir() if x.is_dir()] if (c / ".bench-per").exists() else [c]
        for t in targets:
            info = life_info(t)
            life = info.get("life")
            name = c.name + (f"/{t.name}" if t != c else "")
            if not life or name in busy:
                continue
            why = None
            if life.get("ttl") and time.time() - info.get("used", time.time()) > life["ttl"]:
                why = f"unused for {life['ttl'] / 86400:g} days"
            elif life.get("until") and info.get("value") and branch_gone(info["value"], info.get("repos", []), seen):
                why = f"branch {info['value']} is gone"
            if why:
                size = du(t)
                if t != c:
                    shutil.rmtree(t, ignore_errors=True)
                    life_file(t).unlink(missing_ok=True)   # its record goes with it
                else:
                    empty_storage_dir(t)
                event(kind="storage", project=project, storage=name, removed=why, bytes=size)
            elif life.get("max") and du(t) > life["max"]:
                freed = trim_to(t, life["max"], lambda m: None)
                event(kind="storage", project=project, storage=name, trimmed=freed, cap=life["max"])


def prune():
    """root: remove job results past each project's retention"""
    days = box_conf().get("envelope", {}).get("retention_days", 14)
    for path in (STATE / "projects").glob("*.json"):
        project = path.stem
        for d in (Path(f"/home/mcu-{project}") / "jobs").glob("2*"):
            if (d / "status").exists() and time.time() - (d / "status").stat().st_mtime > days * 86400:
                shutil.rmtree(d, ignore_errors=True)
        # per-parameter caches (a build tree per branch): the unused ones;
        # a sub that states its own end follows it instead (below)
        for marker in Path(f"/home/mcu-{project}/cache").glob("*/.bench-per"):
            for sub in marker.parent.iterdir():
                if sub.is_dir() and not life_info(sub) \
                        and time.time() - sub.stat().st_mtime > days * 86400:
                    shutil.rmtree(sub, ignore_errors=True)
        storage_housekeeping(project)
        # the services' pods a killed runner left behind, as the project
        # user (rootless podman); none can run without its runtime dir
        try:
            pw = pwd.getpwnam(f"mcu-{project}")
            if Path(f"/run/user/{pw.pw_uid}").is_dir():
                subprocess.run(["runuser", "-u", pw.pw_name, "--", "env", f"XDG_RUNTIME_DIR=/run/user/{pw.pw_uid}",
                                f"HOME={pw.pw_dir}", f"USER={pw.pw_name}", sys.executable, os.path.abspath(__file__),
                                "reap"], cwd="/tmp", capture_output=True, timeout=300)
        except (KeyError, OSError, subprocess.TimeoutExpired):
            pass
        # branch Benchfiles nobody ran within the retention (never the default)
        try:
            pconf = json.loads(path.read_text())
        except (OSError, ValueError):
            pconf = {}
        expired = []
        for label, version in list(pconf.get("benchfiles", {}).items()):
            d = Path(f"/home/mcu-{project}/benchfiles/labels") / label.replace("/", "~")
            stamps = [f.stat().st_mtime for f in (d / ".used", d / f"v{version}.toml") if f.exists()]
            if not stamps or time.time() - max(stamps) > days * 86400:
                shutil.rmtree(d, ignore_errors=True)
                del pconf["benchfiles"][label]
                pconf.get("label_jobs", {}).pop(label, None)
                expired.append(label)
        if expired:
            path.write_text(json.dumps(pconf, indent=1) + "\n")
            event(kind="benchfile", project=project, expired=expired)
        # the runs' cost sheets follow the same retention
        for f in (STATE / "runs" / project).glob("*.json"):
            if time.time() - f.stat().st_mtime > days * 86400:
                f.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "entry"
    if mode == "run":
        run_job(sys.argv[2])
    elif mode == "prune":
        sys.exit(prune())
    elif mode == "reap":
        # the project user, from the daily prune: pods of ended jobs
        reap_services(lambda m: event(kind="services", project=project_name(), reaped=m))
    elif mode == "recover":
        sys.exit(recover())
    elif mode == "health":
        # for the dashboard's API (mcu-web): everything but a project's disk
        print(json.dumps(health(None)))
    elif mode == "topology":
        # for the dashboard's API (mcu-web): the same view as `box topology`
        print(json.dumps(topology()))
    else:
        sys.exit(entry())
