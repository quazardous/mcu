# SPDX-License-Identifier: MIT
"""The links a rename of the box's tree must move: only those whose target
starts with an old prefix, within the depth asked, a second run changes
nothing; and the dangling links of the volume index. A fake tree in a temp
dir. python3 -m unittest discover tests"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "box"))
import relink  # noqa: E402

PAIRS = [("/srv/bench/", "/srv/mcu/"), ("/home/bench-", "/home/mcu-")]


class Relink(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.top = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def link(self, rel, target):
        p = self.top / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, p)
        return p

    def test_moves_only_old_prefixes(self):
        a = self.link("raw-corpus", "/srv/bench/hdd/helios/volumes/raw-corpus")
        b = self.link("jobs", "/home/bench-helios/x")
        c = self.link("benchmarks", "/srv/benchmarks/x")      # a sibling, not under /srv/bench/
        d = self.link("relative", "../bench/x")
        e = self.link("already", "/srv/mcu/projects/probe/volumes/probe-test")
        changed = relink.relink(self.top, 1, PAIRS)
        self.assertEqual(os.readlink(a), "/srv/mcu/hdd/helios/volumes/raw-corpus")
        self.assertEqual(os.readlink(b), "/home/mcu-helios/x")
        self.assertEqual(os.readlink(c), "/srv/benchmarks/x")
        self.assertEqual(os.readlink(d), "../bench/x")
        self.assertEqual(os.readlink(e), "/srv/mcu/projects/probe/volumes/probe-test")
        self.assertEqual(sorted(p for p, _, _ in changed), sorted([str(a), str(b)]))
        self.assertEqual(relink.relink(self.top, 1, PAIRS), [])
        self.assertEqual([p for p in os.listdir(self.top) if p.endswith(".mcu-relink")], [])

    def test_depth(self):
        near = self.link("a/b/c", "/srv/bench/x")        # 3 levels down
        far = self.link("a/b/c2/d", "/srv/bench/y")      # 4 levels down
        relink.relink(self.top, 3, PAIRS)
        self.assertEqual(os.readlink(near), "/srv/mcu/x")
        self.assertEqual(os.readlink(far), "/srv/bench/y")

    def test_does_not_follow_linked_dirs(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        inner = Path(outside.name) / "l"
        os.symlink("/srv/bench/z", inner)
        self.link("dirlink", outside.name)
        relink.relink(self.top, 5, PAIRS)
        self.assertEqual(os.readlink(inner), "/srv/bench/z")

    def test_reverse(self):
        a = self.link("v", "/srv/bench/hdd/v")
        relink.relink(self.top, 1, PAIRS)
        relink.relink(self.top, 1, [(n, o) for o, n in PAIRS])
        self.assertEqual(os.readlink(a), "/srv/bench/hdd/v")

    def test_dangling(self):
        real = self.top / "real"
        real.mkdir()
        self.link("index/ok", str(real))
        broken = self.link("index/broken", "/srv/bench/nowhere")
        self.assertEqual(relink.dangling(self.top / "index"), [str(broken)])
        self.assertEqual(relink.dangling(self.top / "missing"), [])


if __name__ == "__main__":
    unittest.main()
