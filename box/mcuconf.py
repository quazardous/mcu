# SPDX-License-Identifier: MIT
"""The box's configuration, in one place: the defaults (computed from the
machine where that is the safe choice), and /etc/mcu/box.toml over them.
box.toml is the only file: the dashboard and `mcu-admin envelope set` write
their changes into it (edit_envelope), line by line, comments kept. Every
component reads it here, so a key has one default and one source.

    python3 mcuconf.py show       each key, its value, and where it comes from
    python3 mcuconf.py show --origin [--json]
                                  by group, with box.toml's line, the default,
                                  who may change it and when it takes effect
    python3 mcuconf.py get <key>  one value (envelope keys as envelope.<key>)
"""

import glob
import json
import os
import shutil
import sys
import tomllib
from pathlib import Path

ETC = Path("/etc/mcu")
STATE = Path("/var/lib/mcu")


def version():
    """mcu's version: the VERSION file installed beside this module, or the
    checkout's"""
    here = Path(__file__).resolve().parent
    for p in (here / "VERSION", here.parent / "VERSION"):
        try:
            return p.read_text().strip()
        except OSError:
            continue
    return "unknown"


def _mem_total():
    try:
        with open("/proc/meminfo") as f:
            return int(next(line for line in f if line.startswith("MemTotal:")).split()[1]) * 1024
    except (OSError, StopIteration, ValueError):
        return 8 * 2**30


def _first_core():
    """the first physical core's threads (cpu0 and its siblings): what a
    dedicated box keeps for the system by default"""
    try:
        return Path("/sys/devices/system/cpu/cpu0/topology/thread_siblings_list").read_text().strip()
    except OSError:
        return "0"


def _gib(n):
    return f"{max(1, round(n / 2**30))}g"


def detected_resources():
    """with no [resources] in box.toml: what this machine actually has, not
    an assumption. A render node from an integrated GPU (Intel, AMD), shared;
    an NVIDIA GPU (its CDI spec or nvidia-smi), one job at a time"""
    found = {}
    drivers = set()
    for node in glob.glob("/sys/class/drm/renderD*/device/driver"):
        drivers.add(os.path.basename(os.path.realpath(node)))
    igpu = sorted(drivers & {"i915", "xe", "amdgpu"})
    if igpu:
        found["render"] = {"about": "the integrated GPU's render node", "drm": {"driver": igpu},
                           "share": "shared", "provides": ["opengl", "vulkan"]}
    if Path("/etc/cdi/nvidia.yaml").exists() or shutil.which("nvidia-smi"):
        found["gpu"] = {"about": "the NVIDIA GPU", "cdi": "nvidia.com/gpu=all", "probe": "nvidia",
                        "share": "exclusive", "provides": ["cuda", "vulkan", "opencl", "opengl"]}
    return found


def defaults(resources=None):
    """{key: (value, "default" | "computed")} for the top level and the
    envelope (envelope.<key>), safe on any dedicated box"""
    threads = os.cpu_count() or 1
    reserved = _first_core()
    n_reserved = len(_parse_cpus(reserved))
    job_threads = max(1, threads - n_reserved)
    reserved_memory = max(2 * 2**30, _mem_total() // 10)
    jobs_memory = max(2**30, _mem_total() - reserved_memory)
    names = sorted(resources if resources is not None else detected_resources())
    d = {
        "reserved": (reserved, "computed"),
        "reserved_memory": (_gib(reserved_memory), "computed"),
        "backfill_max_wait": ("30m", "default"),
        "dashboard_port": (8080, "default"),
        "volumes_dir": ("/srv/mcu/volumes", "default"),
        "home_quota": ("5g", "default"),
        "envelope.default_disk": ("10g", "default"),
        "envelope.max_disk_ssd": ("60g", "default"),
        "envelope.max_disk_hdd": ("0", "default"),
        "envelope.max_volumes": (4, "default"),
        "envelope.max_cores": (max(1, job_threads // 2), "computed"),
        "envelope.build_cores": (min(4, job_threads), "computed"),
        "envelope.max_jobs": (2, "default"),
        "envelope.memory": (_gib(jobs_memory // 2), "computed"),
        "envelope.exclusive": (["machine", "disk"] + (["gpu"] if "gpu" in names else []), "computed"),
        "envelope.network": ("fetch", "default"),
        "envelope.resources": (names, "computed"),
        "envelope.timeout_max": ("3h", "default"),
        "envelope.retention_days": (14, "default"),
    }
    return d


def _size(v):
    v = str(v).strip().lower()
    return int(float(v[:-1]) * {"k": 2**10, "m": 2**20, "g": 2**30, "t": 2**40}[v[-1]]) if v[-1:] in "kmgt" else int(v)


def _pool_bytes():
    """the size of the largest pool's filesystem (or /), for disk bounds"""
    paths = ["/"]
    try:
        paths += [p["path"] for p in json.loads((STATE / "pools.json").read_text())["pools"].values()]
    except (OSError, ValueError, KeyError):
        pass
    best = 0
    for p in paths:
        try:
            st = os.statvfs(p)
            best = max(best, st.f_blocks * st.f_frsize)
        except OSError:
            continue
    return best


def bounds():
    """{envelope key: (min, max)} in GB (sizes), threads, hours or a count:
    the one table the admin's command checks and the dashboard draws, from
    this machine (its threads, memory and largest pool)"""
    total = _mem_total()
    mem_gb = max(1, (total - 2**30) // 2**30)
    disk_gb = max(1, _pool_bytes() // 2**30)
    threads = os.cpu_count() or 1
    return {
        "max_cores": (1, threads), "build_cores": (1, threads),
        "jobs_memory": (min(4, mem_gb), mem_gb), "memory": (1, mem_gb),
        "default_disk": (1, disk_gb), "max_disk_ssd": (1, disk_gb), "max_disk_hdd": (0, disk_gb),
        "max_volumes": (1, 50), "max_jobs": (1, 32), "retention_days": (1, 365), "timeout_max": (1, 24),
    }


def in_bounds(key, value):
    """None if the value fits its bounds, else why not"""
    b = bounds().get(key)
    if not b:
        return None
    if key in ("jobs_memory", "memory", "default_disk", "max_disk_ssd", "max_disk_hdd"):
        n = 0 if str(value) == "0" else _size(value) / 2**30
        unit = "g"
    elif key == "timeout_max":
        v = str(value)
        n = int(v[:-1]) / {"s": 3600, "m": 60, "h": 1}[v[-1]] if v[-1:] in "smh" else int(v) / 3600
        unit = "h"
    else:
        n, unit = float(value), ""
    lo, hi = b
    return None if lo <= n <= hi else f"between {lo}{unit} and {hi}{unit} on this box"


def _parse_cpus(spec):
    out = set()
    for part in str(spec).split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.update(range(int(a), int(b) + 1))
        elif part:
            out.add(int(part))
    return out


def _read_file():
    try:
        return tomllib.loads((ETC / "box.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def load(with_origins=False):
    """the effective configuration: box.toml's tables kept as they are
    ([resources], [media], [power]...), every known key filled in. With
    with_origins, also {key: "default" | "computed" | "box.toml"}"""
    conf = _read_file()
    env_file = dict(conf.get("envelope", {}))
    # max_disk_ssd was max_disk before the pool types
    if "max_disk_ssd" not in env_file and "max_disk" in env_file:
        env_file["max_disk_ssd"] = env_file["max_disk"]
    resources = conf.get("resources") or detected_resources()
    origins = {}
    for key, (value, how) in defaults(resources).items():
        if key.startswith("envelope."):
            k = key[len("envelope."):]
            if k in env_file:
                origins[key] = "box.toml"
            else:
                env_file.setdefault(k, value)
                origins[key] = how
        elif key in conf:
            origins[key] = "box.toml"
        else:
            conf[key] = value
            origins[key] = how
    if "jobs_memory" in env_file:
        origins["envelope.jobs_memory"] = "box.toml"
    conf["envelope"] = env_file
    if not conf.get("resources"):
        conf["resources"] = resources
        origins["resources"] = "computed"
    else:
        origins["resources"] = "box.toml"
    return (conf, origins) if with_origins else conf


# every setting: its group on the effective-configuration page, its one
# writer (file: box.toml by hand; admin: the dashboard or `mcu-admin envelope
# set`, which write box.toml too) and when a change takes effect
SETTINGS = {
    "reserved": ("Machine", "file", "next job"),
    "reserved_memory": ("Machine", "file", "next job"),
    "backfill_max_wait": ("Machine", "file", "next job"),
    "dashboard_port": ("Machine", "file", "after init (the dashboard restarts)"),
    "tls_cert": ("Machine", "file", "after init (the dashboard restarts)"),
    "tls_key": ("Machine", "file", "after init (the dashboard restarts)"),
    "power": ("Machine", "file", "at once"),
    "envelope.jobs_memory": ("Machine", "admin", "next job"),
    "envelope.max_cores": ("Per job", "admin", "next job"),
    "envelope.build_cores": ("Per job", "admin", "next build"),
    "envelope.memory": ("Per job", "admin", "next job"),
    "envelope.max_jobs": ("Per job", "admin", "next job"),
    "envelope.timeout_max": ("Per job", "admin", "next job"),
    "projects_dir": ("Disk", "file", "new projects (after init)"),
    "volumes_dir": ("Disk", "file", "new volumes (after init)"),
    "home_quota": ("Disk", "file", "after init"),
    "media": ("Disk", "file", "next job"),
    "envelope.default_disk": ("Disk", "admin", "at once (projects on the default)"),
    "envelope.max_disk_ssd": ("Disk", "admin", "next Benchfile or config disk"),
    "envelope.max_disk_hdd": ("Disk", "admin", "next Benchfile or config disk"),
    "envelope.max_volumes": ("Disk", "admin", "next Benchfile"),
    "resources": ("Network and hardware", "file", "next job"),
    "envelope.resources": ("Network and hardware", "admin", "next job"),
    "envelope.exclusive": ("Network and hardware", "admin", "next job"),
    "envelope.network": ("Network and hardware", "admin", "next job"),
    "envelope.retention_days": ("Housekeeping", "admin", "next prune (daily)"),
}


def _lines(text):
    """{setting: its line number in box.toml}: top-level keys, envelope.<key>,
    and a table's first header ([resources.gpu] for resources)"""
    import re
    out, table = {}, ""
    for n, line in enumerate(text.split("\n"), 1):
        m = re.match(r"\s*\[([A-Za-z0-9_.-]+)\]", line)
        if m:
            table = m.group(1)
            top = table.split(".")[0]
            if top != "envelope":
                out.setdefault(top, n)
            continue
        m = re.match(r"\s*([A-Za-z0-9_]+)\s*=", line)
        if m:
            key = m.group(1) if not table else f"envelope.{m.group(1)}" if table == "envelope" else None
            if key:
                out.setdefault(key, n)
    if "envelope.max_disk" in out:
        out.setdefault("envelope.max_disk_ssd", out["envelope.max_disk"])
    return out


def effective():
    """the configuration in force, one row per setting: value, source
    (default, computed, box.toml), its line in box.toml, the default when it
    differs, the group, who may change it and when it takes effect"""
    conf, origins = load(with_origins=True)
    try:
        lines = _lines((ETC / "box.toml").read_text())
    except OSError:
        lines = {}
    dflt = defaults(conf.get("resources"))
    reserved = _size(conf.get("reserved_memory", "2g"))
    dflt["envelope.jobs_memory"] = (_gib(max(2**30, _mem_total() - reserved)), "computed")
    rows = []
    for key, (group, writer, effect) in SETTINGS.items():
        if key.startswith("envelope."):
            value = conf["envelope"].get(key[len("envelope."):])
        else:
            value = conf.get(key)
        source = origins.get(key) or ("box.toml" if key in lines else None)
        if value is None and key in dflt:
            value, source = dflt[key]
        if key == "resources":
            value = sorted(value or {})
        elif isinstance(value, dict):
            value = dict(value)
        d = dflt.get(key, (None,))[0]
        rows.append({"key": key, "value": value, "source": source or "unset",
                     "line": lines.get(key) if source == "box.toml" else None,
                     "default": d if source == "box.toml" and d != value else None,
                     "group": group, "writer": writer, "effect": effect})
    return rows


def toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return json.dumps(str(v))


def _split_value(rest):
    """value and comment of what follows `=`, a # inside quotes kept"""
    q = None
    for i, ch in enumerate(rest):
        if q:
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
        elif ch == "#":
            return rest[:i].rstrip(), rest[i:]
    return rest.rstrip(), ""


def edit_envelope(text, changes, stamp=None):
    """box.toml's text with [envelope] changed: {key: value} sets a key (only
    its line changes, its comment kept in its column; a missing key goes after
    the table's last value, or takes the place of its commented-out example),
    {key: None} puts it back to the default (a line with its own comment is
    commented out, kept as the example; a line the dashboard added goes).
    Checked: the result parses and its [envelope] is exactly the old one with
    the changes, else ValueError and nothing to write"""
    import datetime
    import re
    stamp = stamp or datetime.date.today().isoformat()
    try:
        before = tomllib.loads(text).get("envelope", {})
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"box.toml does not parse ({e})")
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if re.fullmatch(r"\s*\[envelope\]\s*(#.*)?", line)), None)
    if start is None:
        if all(v is None for v in changes.values()):
            return text
        if lines and lines[-1] == "":
            lines.pop()
        lines += ["", "[envelope]", ""]
        start = len(lines) - 2
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"\s*\[", lines[i])), len(lines))
    for key, val in sorted(changes.items()):
        found = None
        for i in range(start + 1, end):
            m = re.match(rf"(\s*){re.escape(key)}(\s*=\s*)(.*)$", lines[i])
            if m:
                found = i, m
                break
        if found:
            i, m = found
            value, comment = _split_value(m.group(3))
            if value.startswith("[") and not value.endswith("]"):
                raise ValueError(f"{key} spans several lines in box.toml; edit it by hand")
            if val is None:
                # back to the default: a line that explains the key stays,
                # commented out, as the example a later set comes back to; a
                # line the dashboard added (or with no comment) goes
                if comment and not comment.lstrip("# ").startswith("set from the dashboard"):
                    lines[i] = f"{m.group(1)}# {key}{m.group(2)}{m.group(3)}"
                else:
                    del lines[i]
                    end -= 1
                continue
            col = len(m.group(3)) - len(comment)
            pad = " " * max(col - len(toml_value(val)), 1) if comment else ""
            lines[i] = f"{m.group(1)}{key}{m.group(2)}{toml_value(val)}{pad}{comment}"
        elif val is not None:
            # under its commented-out example (`# key = ...`) if the table has
            # one, else after the table's last value, before the comments that
            # belong to the next table
            example = next((i for i in range(start + 1, end) if re.match(rf"\s*#\s*{re.escape(key)}\s*=", lines[i])), None)
            ex = re.match(rf"(\s*)#\s*{re.escape(key)}(\s*=\s*)(.*)$", lines[example]) if example is not None else None
            value, comment = _split_value(ex.group(3)) if ex else ("", "")
            if ex and comment and not value.startswith("["):
                # the example becomes the setting, its comment kept in its column
                col = len(ex.group(3)) - len(comment)
                pad = " " * max(col - len(toml_value(val)), 1)
                lines[example] = f"{ex.group(1)}{key}{ex.group(2)}{toml_value(val)}{pad}{comment}"
                continue
            last = example + 1 if example is not None else max(
                (i for i in range(start + 1, end) if lines[i].strip() and not lines[i].lstrip().startswith("#")),
                default=start) + 1
            lines.insert(last, f"{key} = {toml_value(val)}  # set from the dashboard, {stamp}")
            end += 1
    new = "\n".join(lines)
    try:
        got = tomllib.loads(new).get("envelope", {})
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"the result would not parse ({e})")
    want = {k: v for k, v in dict(before, **changes).items() if v is not None}
    if got != want:
        raise ValueError("the result's [envelope] would differ from the change asked")
    return new


def write_box(new, path=None, keep=5):
    """box.toml replaced atomically by `new`, the previous text kept as
    box.toml.bak.<date> (the `keep` latest); returns (diff, backup path)"""
    import datetime
    import difflib
    path = Path(path or ETC / "box.toml")
    old = path.read_text() if path.exists() else ""
    diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), str(path), f"{path} (new)"))
    if old == new:
        return "", None
    bak = None
    if old:
        bak = Path(f"{path}.bak.{datetime.datetime.now():%Y%m%d-%H%M%S}")
        n = 1
        while bak.exists():   # two changes in the same second
            bak, n = Path(f"{path}.bak.{datetime.datetime.now():%Y%m%d-%H%M%S}-{n}"), n + 1
        bak.write_text(old)
        for stale in sorted(glob.glob(f"{path}.bak.*"))[:-keep]:
            os.unlink(stale)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(new)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return diff, bak


def show_origin(as_json=False):
    rows = effective()
    if as_json:
        print(json.dumps({"version": version(), "file": str(ETC / "box.toml"), "settings": rows}, indent=1))
        return
    print(f"mcu {version()}, {ETC / 'box.toml'}")
    group = None
    for r in rows:
        if r["group"] != group:
            group = r["group"]
            print(f"\n{group}")
        v = r["value"]
        v = "-" if v is None else v if isinstance(v, str) else json.dumps(v)
        src = f"box.toml:{r['line']}" if r["line"] else r["source"]
        extra = f" (default {json.dumps(r['default'])})" if r["default"] is not None else ""
        who = "dashboard or envelope set" if r["writer"] == "admin" else "box.toml by hand"
        print(f"  {r['key']:<24} {v[:30]:<30} {src + extra:<34} {who}; {r['effect']}")


def show():
    print(f"{'version':<28} {version():<28} mcu")
    conf, origins = load(with_origins=True)
    for key in sorted(origins):
        if key.startswith("envelope."):
            value = conf["envelope"].get(key[len("envelope."):])
        else:
            value = conf.get(key)
        if key == "resources":
            value = ", ".join(sorted(value)) or "none"
        print(f"{key:<28} {json.dumps(value) if not isinstance(value, str) else value:<28} {origins[key]}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["show"]:
        show()
    elif args in (["show", "--origin"], ["show", "--origin", "--json"], ["show", "--json"]):
        show_origin("--json" in args)
    elif args == ["get", "version"]:
        print(version())
    elif len(args) == 2 and args[0] == "get":
        c = load()
        key = args[1]
        v = c["envelope"].get(key[len("envelope."):]) if key.startswith("envelope.") else c.get(key)
        print("" if v is None else v)
    else:
        sys.exit("usage: mcuconf.py show [--origin] [--json] | get <key>")
