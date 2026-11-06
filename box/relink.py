# SPDX-License-Identifier: MIT
"""Absolute links that name the box's old paths, for mcu-admin migrate-name:
the projects' links in their homes (jobs -> /srv/<name>/projects/<p>/jobs) and
the volume index (/srv/<name>/volumes/<v> -> where the volume lives) point
into the tree the rename moved; they must follow it, or jobs and volumes lead
nowhere. A link keeps its owner.

    relink.py <dir> <maxdepth> <old prefix> <new prefix> [<old prefix> <new prefix>...]
        rewrite the links under dir (at most maxdepth deep) whose target starts
        with an old prefix; prints those changed
    relink.py dangling <dir>
        the links directly in dir that lead nowhere, one per line
"""

import os
import sys


def links(top, maxdepth):
    """the symbolic links under top, at most maxdepth levels deep (1: its
    entries), never following a link into another directory"""
    top = os.path.abspath(top)
    base = top.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(top):
        depth = root.rstrip(os.sep).count(os.sep) - base + 1
        for name in dirs + files:
            p = os.path.join(root, name)
            if os.path.islink(p):
                yield p
        if depth >= maxdepth:
            dirs[:] = []


def moved(target, pairs):
    """target with its first matching old prefix replaced, or None"""
    for old, new in pairs:
        if target.startswith(old):
            return new + target[len(old):]
    return None


def relink(top, maxdepth, pairs):
    """rewrite the links under top whose target starts with an old prefix of
    pairs; the (link, old target, new target) changed. A second run changes
    nothing."""
    changed = []
    for p in sorted(links(top, maxdepth)):
        t = os.readlink(p)
        to = moved(t, pairs)
        if to is None or to == t:
            continue
        st = os.lstat(p)
        tmp = p + ".mcu-relink"
        if os.path.lexists(tmp):
            os.unlink(tmp)
        os.symlink(to, tmp)
        try:
            os.lchown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            pass  # not root: the link is ours anyway
        os.replace(tmp, p)
        changed.append((p, t, to))
    return changed


def dangling(top):
    """the links directly in top whose target does not exist"""
    if not os.path.isdir(top):
        return []
    return sorted(os.path.join(top, n) for n in os.listdir(top)
                  if os.path.islink(os.path.join(top, n)) and not os.path.exists(os.path.join(top, n)))


def main(argv):
    if len(argv) == 3 and argv[1] == "dangling":
        for p in dangling(argv[2]):
            print(f"{p} -> {os.readlink(p)}")
        return 0
    if len(argv) >= 5 and len(argv) % 2 == 1 and argv[2].isdigit():
        rest = argv[3:]
        pairs = list(zip(rest[0::2], rest[1::2]))
        if os.path.isdir(argv[1]):
            for p, t, to in relink(argv[1], int(argv[2]), pairs):
                print(f"{p}: {t} -> {to}")
        return 0
    sys.stderr.write(__doc__.split("\n\n", 1)[1])
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
