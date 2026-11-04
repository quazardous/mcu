# SPDX-License-Identifier: MIT
"""The box's optional hardware, one module per feature. Each detect(root)
says present / absent / unsupported, with a detail and the fix; the box works
without any of them, minus that feature. root is "/" on the box, a fake tree
in the tests.

    python3 -m hw          (from box/ or the installed lib) one row per feature
"""

import json
import sys

from . import cpufreq, drm, nvidia, rapl, smart, thunderbolt, virt

MODULES = (("timing cap", cpufreq), ("render node", drm), ("NVIDIA GPU", nvidia), ("eGPU (Thunderbolt)", thunderbolt),
           ("power (RAPL)", rapl), ("disk health (SMART)", smart), ("virtual machine", virt))


def report(root="/"):
    """[{key, feature, state, detail, fix}] for every module; key is the
    module's name (nvidia, rapl...), stable for the dashboard"""
    rows = []
    for name, mod in MODULES:
        try:
            state, detail, fix = mod.detect(root)
        except OSError as e:
            state, detail, fix = "unsupported", str(e), ""
        rows.append({"key": mod.__name__.rsplit(".", 1)[-1], "feature": name, "state": state, "detail": detail, "fix": fix})
    return rows


def main(args):
    rows = report()
    if args == ["--json"]:
        print(json.dumps(rows, indent=1))
        return 0
    for r in rows:
        print(f"{'optional':<9} {r['feature']}: {r['state']}" + (f", {r['detail']}" if r["detail"] else "")
              + (f"  ->  {r['fix']}" if r["fix"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
