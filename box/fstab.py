# SPDX-License-Identifier: MIT
"""/etc/fstab's mount points, for mcu-admin: a rename of the box's data tree
(migrate-name moves /srv/<old> to /srv/<new>, the mounted volumes with it)
must move their fstab entries too, or the next boot mounts them back on the
old paths; and init's check warns when a pool's filesystem is mounted where
fstab does not mount it (it moves at the next boot).

    fstab.py rename <fstab> <old dir> <new dir>   rewrite the mount points under old dir; prints those changed
    fstab.py has <fstab> <mount point>            exit 0 when fstab mounts something there
"""

import os
import sys


def _fields(line):
    """(start, end) of each whitespace-separated field of a line"""
    spans, i, n = [], 0, len(line)
    while i < n:
        while i < n and line[i] in " \t":
            i += 1
        if i >= n or line[i] == "\n":
            break
        j = i
        while j < n and line[j] not in " \t\n":
            j += 1
        spans.append((i, j))
        i = j
    return spans


def _entry(line):
    """the spans of an fstab entry's fields, or None for a comment or a blank line"""
    if line.lstrip().startswith("#"):
        return None
    spans = _fields(line)
    return spans if len(spans) >= 2 else None


def _unescape(field):
    # fstab writes a space in a path as \040 (and a tab as \011)
    return field.replace("\\040", " ").replace("\\011", "\t")


def mount_points(text):
    """the mount points fstab names, in order"""
    out = []
    for line in text.splitlines():
        spans = _entry(line)
        if spans:
            a, b = spans[1]
            out.append(_unescape(line[a:b]))
    return out


def rename(text, old, new):
    """text with each mount point at old or under it moved under new, nothing
    else touched (devices, options, comments, spacing); and the (old, new)
    mount points changed. A mount point already under new is left alone, so a
    second run changes nothing."""
    old, new = old.rstrip("/"), new.rstrip("/")
    lines, changed = [], []
    for line in text.splitlines(keepends=True):
        spans = _entry(line)
        if spans:
            a, b = spans[1]
            mp = line[a:b]
            if mp == old or mp.startswith(old + "/"):
                to = new + mp[len(old):]
                line = line[:a] + to + line[b:]
                changed.append((mp, to))
        lines.append(line)
    return "".join(lines), changed


def main(argv):
    if len(argv) == 5 and argv[1] == "rename":
        path, old, new = argv[2:]
        with open(path) as f:
            text = f.read()
        out, changed = rename(text, old, new)
        if changed:
            # written beside it, then swapped in: a crash never leaves half an fstab
            tmp = path + ".mcu-new"
            with open(tmp, "w") as f:
                f.write(out)
            st = os.stat(path)
            os.chmod(tmp, st.st_mode & 0o7777)
            os.chown(tmp, st.st_uid, st.st_gid)
            os.replace(tmp, path)
        for a, b in changed:
            print(f"{a} -> {b}")
        return 0
    if len(argv) == 4 and argv[1] == "has":
        with open(argv[2]) as f:
            points = mount_points(f.read())
        def norm(p):
            return p.rstrip("/") or "/"
        return 0 if norm(argv[3]) in [norm(p) for p in points] else 1
    sys.stderr.write(__doc__.split("\n\n", 1)[1])
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
