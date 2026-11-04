# SPDX-License-Identifier: MIT
"""The client side of an mcu box: its configuration and the SSH calls, shared
by the mcuctl command line and the MCP server.

The key and the host are configuration, never arguments, so a command line
reads `mcuctl run test branch=x`, not an ssh one. They come from the
environment, else from the file MCU_CONFIG names (default: .env.local next to
the client), as KEY=value lines:

  MCU_HOST        the project's user on the box, mcu-<project>@host
                  (BENCH_HOST, remote.sh's name, is read too)
  MCU_KEY         that project's private key file (BENCH_KEY too); ssh's
                  default keys when unset
  MCU_ADMIN_HOST  the admin's account on the box, user@host (with sudo):
                  the admin commands exist only when it is set
  MCU_ADMIN_KEY   its key file; ssh's default keys when unset

A project's commands go to the box's forced command, which checks everything:
the client adds no power. The admin's go to mcu-admin through sudo.
"""

import os
import shlex
import subprocess
import sys
from pathlib import Path

CLIENT = Path(__file__).resolve().parent.parent
EX_TEMPFAIL = 75
MCU_ADMIN = "/usr/local/lib/mcu/mcu-admin"

# what a project's key may send (the box's forced command refuses the rest)
PROJECT_VERBS = {"run", "provision", "debug", "push", "wait", "status", "cancel", "fetch", "list", "box",
                 "health", "config", "storage", "volume", "help", "docs"}


class ConfigError(Exception):
    pass


def load_config(environ=None):
    """the settings: the environment first, then the MCU_CONFIG file"""
    env = dict(os.environ if environ is None else environ)
    path = Path(env.get("MCU_CONFIG") or CLIENT / ".env.local")
    conf = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                conf[k.strip()] = v.strip().strip('"').strip("'")
    conf.update({k: v for k, v in env.items() if k.startswith(("MCU_", "BENCH_"))})
    return {
        "host": conf.get("MCU_HOST") or conf.get("BENCH_HOST"),
        "key": conf.get("MCU_KEY") or conf.get("BENCH_KEY"),
        "admin_host": conf.get("MCU_ADMIN_HOST"),
        "admin_key": conf.get("MCU_ADMIN_KEY"),
        "file": str(path) if path.is_file() else None,
    }


class Box:
    """one box, as a project (`admin=False`) or as its admin"""

    def __init__(self, conf=None, admin=False):
        self.conf = conf or load_config()
        self.admin = admin
        host = self.conf["admin_host" if admin else "host"]
        if not host:
            raise ConfigError("admin commands need MCU_ADMIN_HOST (see `mcuctl help config`)" if admin
                              else "MCU_HOST is not set (see `mcuctl help config`)")
        key = self.conf["admin_key" if admin else "key"]
        if key and not os.access(key, os.R_OK):
            raise ConfigError(f"{'MCU_ADMIN_KEY' if admin else 'MCU_KEY'} is not readable "
                              "(see `mcuctl help config`)")
        self.host = host
        self.ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30"]
        if key:
            self.ssh += ["-i", key, "-o", "IdentitiesOnly=yes"]

    def command(self, args, tty=False):
        """the ssh command line for these arguments: a project's go as words
        to its forced command (split there on spaces), the admin's as one
        quoted command for mcu-admin through sudo"""
        args = [str(a) for a in args]
        if self.admin:
            remote = " ".join(shlex.quote(a) for a in ["sudo", "-n", MCU_ADMIN, *args])
        else:
            if any(not a or any(c.isspace() for c in a) for a in args):
                raise ConfigError("a box argument cannot be empty or contain spaces")
            remote = " ".join(args)
        return self.ssh + (["-t"] if tty else []) + [self.host, remote]

    def run(self, args, stdin=None, capture=False, tty=False):
        """run one box command: its exit code passes through; SSH failing
        (the box off, the network) is exit 75 like any 'not now'. With
        capture, (code, stdout bytes, stderr text)"""
        cmd = self.command(args, tty=tty)
        if capture:
            r = subprocess.run(cmd, stdin=stdin if stdin is not None else subprocess.DEVNULL,
                               capture_output=True)
            code, out, err = r.returncode, r.stdout, r.stderr.decode(errors="replace")
        else:
            code = subprocess.run(cmd, stdin=stdin).returncode
            out, err = None, ""
        if code == 255:
            msg = f"mcu box unreachable ({self.host}): retry later"
            if capture:
                err = (err + "\n" + msg).strip()
            else:
                print(f"mcuctl: {msg}", file=sys.stderr)
            code = EX_TEMPFAIL
        return (code, out, err) if capture else code

    def shell(self, script, capture=True):
        """the admin's escape hatch for what mcu-admin has no command for
        yet: a fixed script of the client's own, run with sudo"""
        if not self.admin:
            raise ConfigError("admin only")
        cmd = self.ssh + [self.host, "sudo -n bash -s"]
        r = subprocess.run(cmd, input=script.encode(), capture_output=capture)
        return r.returncode, (r.stdout.decode(errors="replace") if capture else None), \
            (r.stderr.decode(errors="replace") if capture else "")
