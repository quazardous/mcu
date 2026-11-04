# SPDX-License-Identifier: MIT
"""The helper's read-only probes on a machine without the optional tools
(lvm2, xfsprogs; smartctl is guarded in hw/smart.py): empty answers, never a traceback (init runs
them through the usage timer). python3 -m unittest discover tests"""
import os
import shutil
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

helper = SourceFileLoader("mcu_helper", str(Path(__file__).resolve().parent.parent / "box" / "mcu-helper")).load_module()


class WithoutOptionalTools(unittest.TestCase):
    def setUp(self):
        # a PATH with only the base tools the probes rely on
        self.bin = tempfile.mkdtemp()
        for tool in ("lsblk", "findmnt"):
            if shutil.which(tool):
                os.symlink(shutil.which(tool), os.path.join(self.bin, tool))
        self.saved = os.environ["PATH"]
        os.environ["PATH"] = self.bin

    def tearDown(self):
        os.environ["PATH"] = self.saved
        shutil.rmtree(self.bin)

    def test_probe_of_a_missing_tool(self):
        self.assertEqual(helper.probe(["vgs", "--noheadings"]), "")

    def test_disks_without_lvm_or_xfsprogs(self):
        d = helper.disks()
        self.assertEqual(d["vg_free"], [])
        for fs in d["filesystems"]:
            self.assertFalse(fs["prjquota"])


if __name__ == "__main__":
    unittest.main()
