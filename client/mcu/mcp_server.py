#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""mcu's MCP server (stdio): the box as typed tools, over the same core as
mcuctl. The key and the host come from MCU_CONFIG (or the environment), see
`mcuctl help config`. The standard library only: MCP's stdio transport is
JSON-RPC 2.0, one message per line.

A project's tools go to its forced command, which checks everything; the
admin's tools exist only when MCU_ADMIN_HOST is set. A tool never blocks for
long: `run` returns the job's id once it is queued (the job goes on), `wait`
gives up after its timeout and says where the job stands.
"""

import json
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcu.core import Box, ConfigError, load_config  # noqa: E402

PROTOCOL = "2025-06-18"
TAIL = 60   # log lines a tool returns at most


def text_of(b):
    return (b or b"").decode(errors="replace") if isinstance(b, (bytes, type(None))) else b


def tail(text, n=TAIL):
    lines = text.splitlines()
    return "\n".join(lines[-n:])


class Tools:
    def __init__(self):
        self.conf = load_config()

    def box(self, admin=False):
        return Box(self.conf, admin=admin)

    def call(self, args, stdin=None):
        code, out, err = self.box().run(args, stdin=stdin, capture=True)
        return code, text_of(out), err

    # -- a project's tools ------------------------------------------------
    def t_box(self):
        code, out, err = self.call(["box"])
        first, _, rest = out.partition("\n")
        try:
            data = json.loads(rest)
        except ValueError:
            data = {"text": out}
        return {"exit": code, "status": first.strip(), **data, **({"error": err.strip()} if err.strip() else {})}

    def t_jobs(self):
        code, out, err = self.call(["health", "--json"])
        if code:
            return {"exit": code, "error": err.strip()}
        return json.loads(out)["queue"]

    def t_run(self, job, params=None, wait=False, timeout=600):
        """queue a job; returns its id once queued (wait=true: follows it up
        to timeout seconds)"""
        words = ["run", job, *[f"{k}={v}" for k, v in (params or {}).items()]]
        proc = subprocess.Popen(self.box().command(words), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        seen, job_id = [], None
        deadline = time.time() + (timeout if wait else 60)
        reader = Reader(proc.stdout)
        while time.time() < deadline:
            line = reader.get(1)
            if line is None:
                if proc.poll() is not None and reader.done:
                    break
                continue
            seen.append(line)
            if line.startswith("mcu-job: job ") and " queued" in line:
                job_id = line.split()[2]
                if not wait:
                    break
        if proc.poll() is None:
            proc.terminate()   # the job goes on: its runner does not depend on this connection
            return {"id": job_id, "state": "queued or running" if job_id else "unknown", "log": tail("\n".join(seen)),
                    "note": "the job continues; use wait or status with its id"}
        code = proc.returncode
        return {"id": job_id, "exit": 75 if code == 255 else code, "log": tail("\n".join(seen))}

    def t_wait(self, id, timeout=600):
        proc = subprocess.Popen(self.box().command(["wait", id]), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            out = proc.communicate()[0]
            return {"id": id, "state": "still going after the timeout", "log": tail(text_of(out))}
        return {"id": id, "exit": 75 if proc.returncode == 255 else proc.returncode, "log": tail(text_of(out))}

    def t_status(self, id):
        code, out, err = self.call(["status", id, "--json"])
        try:
            return json.loads(out)
        except ValueError:
            return {"exit": code, "text": out, "error": err.strip()}

    def t_cancel(self, id=None, branch=None):
        if bool(id) == bool(branch):
            raise ValueError("give either id or branch")
        code, out, err = self.call(["cancel", id] if id else ["cancel", "--branch", branch])
        return {"exit": code, "text": out.strip(), **({"error": err.strip()} if err.strip() else {})}

    def t_list(self, limit=20):
        code, out, err = self.call(["list"])
        return {"exit": code, "jobs": out.splitlines()[:limit], **({"error": err.strip()} if err.strip() else {})}

    def t_fetch(self, id, dest, output=None):
        return self._fetch(["fetch", id, *([output] if output else [])], dest)

    def t_fetch_latest(self, job, output, dest, params=None):
        return self._fetch(["fetch", "latest", job, *[f"{k}={v}" for k, v in (params or {}).items()], output], dest)

    def _fetch(self, words, dest):
        dest = Path(dest).expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(self.box().command(words), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        files = []
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|*") as tar:
                for m in tar:
                    if m.issym() or m.islnk() or m.name.startswith("/") or ".." in Path(m.name).parts:
                        continue
                    tar.extract(m, dest, filter="data")
                    if m.isfile():
                        files.append(str(dest / m.name))
        except tarfile.ReadError:
            pass
        err = text_of(proc.stderr.read()).strip()
        code = proc.wait()
        return {"exit": 75 if code == 255 else code, "files": files, **({"error": err} if err else {})}

    def t_benchfile(self, path, label=None):
        with open(Path(path).expanduser(), "rb") as f:
            code, out, err = self.call(["config", "benchfile", *([label] if label else [])], stdin=f)
        return {"exit": code, "text": out.strip(), **({"error": err.strip()} if err.strip() else {})}

    def t_storage_list(self):
        code, out, err = self.call(["storage", "list"])
        return {"exit": code, "text": out, **({"error": err.strip()} if err.strip() else {})}

    def t_health(self):
        code, out, err = self.call(["health", "--json"])
        try:
            return json.loads(out)
        except ValueError:
            return {"exit": code, "error": err.strip()}

    # -- the admin's tools ------------------------------------------------
    def admin(self, words):
        code, out, err = self.box(admin=True).run(words, capture=True, stdin=subprocess.DEVNULL)
        return code, text_of(out), err

    def t_admin_status(self):
        code, out, err = self.admin(["status"])
        try:
            return json.loads(out)
        except ValueError:
            return {"exit": code, "error": err.strip()}

    def _admin_text(self, words):
        code, out, err = self.admin(words)
        return {"exit": code, "text": out.strip(), **({"error": err.strip()} if err.strip() else {})}

    def t_admin_queue(self, mode, reason=None):
        if mode != "open" and not reason:
            raise ValueError("pause and close need a reason (shown to the agents)")
        return self._admin_text(["queue", mode, *([reason] if reason else [])])

    def t_admin_power(self, profile):
        return self._admin_text(["power", profile])

    def t_admin_envelope_set(self, key, value):
        return self._admin_text(["envelope", "set", key, value])

    def t_admin_envelope_reset(self, key):
        return self._admin_text(["envelope", "reset", key])

    def t_admin_gpu(self, action):
        return self._admin_text(["gpu", action])

    def t_admin_cancel(self, project, id=None, branch=None):
        if bool(id) == bool(branch):
            raise ValueError("give either id or branch")
        return self._admin_text(["cancel", project, *([id] if id else ["--branch", branch])])

    def t_admin_orphans(self, stop=False):
        return self._admin_text(["orphans", *(["--stop"] if stop else [])])


class Reader:
    """lines from a pipe without blocking past a timeout"""

    def __init__(self, pipe):
        import queue
        self.q, self.done = queue.Queue(), False

        def pump():
            for raw in pipe:
                self.q.put(raw.decode(errors="replace").rstrip("\n"))
            self.done = True
        threading.Thread(target=pump, daemon=True).start()

    def get(self, timeout):
        import queue
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None


S = lambda **props: {"type": "object", "properties": props, "additionalProperties": False}
STR, INT, BOOL = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
PARAMS = {"type": "object", "additionalProperties": {"type": "string"}, "description": "the job's key=value parameters"}

PROJECT_TOOLS = {
    "box": ("The box's state for this project: ok/degraded/maintenance, GPU, threads, memory, your disk quota.", S()),
    "jobs": ("The box's queue now: running and waiting jobs (memory reserved and used, why each waits), with your jobs' ids.", S()),
    "run": ("Queue a job of your Benchfile. Returns its id once queued (the job goes on); wait=true follows it up to timeout seconds.",
            {**S(job=STR, params=PARAMS, wait=BOOL, timeout=INT), "required": ["job"]}),
    "wait": ("Follow a job until it ends or timeout seconds pass; returns its exit code and the end of its log.",
             {**S(id=STR, timeout=INT), "required": ["id"]}),
    "status": ("A job's state, test summary, failures and declared outputs.", {**S(id=STR), "required": ["id"]}),
    "cancel": ("Cancel one of your jobs (id) or all of a branch's (branch): queued ones leave the queue, running ones stop (exit 130).",
               S(id=STR, branch=STR)),
    "list": ("Your last jobs, newest first.", S(limit=INT)),
    "fetch": ("Download a job's results (or one declared output) and unpack them into dest; returns the files.",
              {**S(id=STR, output=STR, dest=STR), "required": ["id", "dest"]}),
    "fetch_latest": ("Download an output of the latest successful run of a job with these parameters into dest.",
                     {**S(job=STR, params=PARAMS, output=STR, dest=STR), "required": ["job", "output", "dest"]}),
    "benchfile": ("Upload a local Benchfile, as the default one or as a branch's (label).",
                  {**S(path=STR, label=STR), "required": ["path"]}),
    "storage_list": ("Your storage: each storage and sub with its size and last use.", S()),
    "health": ("Everything about the box before or after a run: queue, expected wait, machine, GPU, your disk.", S()),
}
ADMIN_TOOLS = {
    "admin_status": ("Admin: the box at a glance (queue, jobs and their memory, GPU, machine, power profile, envelope).", S()),
    "admin_queue": ("Admin: open, pause (accept, start none) or close (refuse) the queue; pause and close need a reason.",
                    {**S(mode={"type": "string", "enum": ["open", "pause", "close"]}, reason=STR), "required": ["mode"]}),
    "admin_power": ("Admin: the CPU's power profile.",
                    {**S(profile={"type": "string", "enum": ["fast", "balanced", "quiet"]}), "required": ["profile"]}),
    "admin_envelope_set": ("Admin: set an envelope value (what any project may ask for).",
                           {**S(key=STR, value=STR), "required": ["key", "value"]}),
    "admin_envelope_reset": ("Admin: an envelope value back to box.toml's.", {**S(key=STR), "required": ["key"]}),
    "admin_gpu": ("Admin: eject the eGPU (to unplug it) or attach it again.",
                  {**S(action={"type": "string", "enum": ["eject", "attach"]}), "required": ["action"]}),
    "admin_cancel": ("Admin: cancel any project's job (id) or a branch's jobs.",
                     {**S(project=STR, id=STR, branch=STR), "required": ["project"]}),
    "admin_orphans": ("Admin: job containers still running after their job ended; stop=true stops them.", S(stop=BOOL)),
}


def tool_list(conf):
    tools = dict(PROJECT_TOOLS) if conf.get("host") else {}
    if conf.get("admin_host"):
        tools.update(ADMIN_TOOLS)
    return [{"name": n, "description": d, "inputSchema": schema} for n, (d, schema) in tools.items()]


def handle(tools, msg):
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                "capabilities": {"tools": {}}, "serverInfo": {"name": "mcu", "version": "1"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": tool_list(tools.conf)}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in {t["name"] for t in tool_list(tools.conf)}:
            return {"content": [{"type": "text", "text": f"no tool {name} with this configuration"}], "isError": True}
        try:
            result = getattr(tools, f"t_{name}")(**args)
        except (ConfigError, ValueError, TypeError, OSError) as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        bad = isinstance(result, dict) and result.get("exit") not in (None, 0)
        return {"content": [{"type": "text", "text": json.dumps(result, indent=1)}], "isError": bad}
    raise LookupError(method)


def main():
    tools = Tools()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "id" not in msg:
            continue   # a notification (initialized, cancelled): nothing to answer
        try:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": handle(tools, msg)}
        except LookupError:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": f"no method {msg.get('method')}"}}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
