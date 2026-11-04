# SPDX-License-Identifier: MIT
"""The root helper's checks against hostile input: names, sizes, and paths
that lead outside a project's own trees (links to another project or the
system, prefix tricks, traversal). No root, no box: fake trees in a temp
dir. python3 -m unittest discover tests"""
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

helper = SourceFileLoader("mcu_helper", str(Path(__file__).resolve().parent.parent / "box" / "mcu-helper")).load_module()


class Names(unittest.TestCase):
    def test_hostile_names(self):
        for bad in ("../etc", "a/b", "-rf", "Upper", "", "x" * 40, "a b", "a;b", "a\nb", "abc\n", ".hidden"):
            self.assertIsNone(helper.NAME_RE.match(bad), repr(bad))
        for good in ("myapp", "my-app", "a1"):
            self.assertIsNotNone(helper.NAME_RE.match(good), good)


class Sizes(unittest.TestCase):
    def test_hostile_sizes(self):
        for bad in ("-5g", "1e9g", "5gb", "../5g", "", "5 g", "0x10g"):
            with self.assertRaises(SystemExit, msg=repr(bad)):
                helper.parse_size(bad)


class Containment(unittest.TestCase):
    """root acts on a project's paths only inside its home and its own
    directory on each pool"""

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.home, self.pool = d / "home" / "mcu-a", d / "pool"
        for x in (self.home, self.pool / "a" / "cache" / "own", self.pool / "b" / "cache" / "secret",
                  self.pool / "ab" / "cache" / "near"):
            x.mkdir(parents=True)
        self.saved = helper.home_of, helper.pools
        helper.home_of = lambda p: d / "home" / f"mcu-{p}"
        helper.pools = lambda: {"pools": {"data": {"path": str(self.pool)}}, "default": "data"}

    def tearDown(self):
        helper.home_of, helper.pools = self.saved

    def link(self, name, target):
        os.symlink(target, self.home / name)
        return self.home / name

    def test_own_trees(self):
        self.assertTrue(helper.inside_project("a", self.link("own", self.pool / "a" / "cache" / "own")))
        self.assertTrue(helper.inside_project("a", self.home / "jobs-not-there-yet"))

    def test_another_project(self):
        self.assertFalse(helper.inside_project("a", self.link("evil", self.pool / "b" / "cache" / "secret")))

    def test_prefix_trick(self):
        # pool/ab is not pool/a
        self.assertFalse(helper.inside_project("a", self.link("near", self.pool / "ab" / "cache" / "near")))

    def test_the_system_and_traversal(self):
        self.assertFalse(helper.inside_project("a", self.link("etc", "/etc")))
        self.assertFalse(helper.inside_project("a", self.home / ".." / ".." / "pool" / "b"))
        self.assertFalse(helper.inside_project("a", "/"))

    def test_a_chain_of_links(self):
        hop = self.link("hop", self.home / "hop2")
        os.symlink(self.pool / "b" / "cache" / "secret", self.home / "hop2")
        self.assertFalse(helper.inside_project("a", hop))
