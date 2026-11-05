# SPDX-License-Identifier: MIT
"""fstab's mount points moved by a rename of the box's data tree: only the
mount-point field changes, the rest of each line and the comments stay, and
a second run changes nothing. python3 -m unittest discover tests"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "box"))
import fstab  # noqa: E402

FSTAB = """\
# /etc/fstab
UUID=aaaa /                       xfs     defaults        0 0
UUID=bbbb /boot                   ext4    defaults        1 2
# the data volumes (/srv/bench/projects is the ssd)
UUID=cccc /srv/bench/projects xfs defaults,prjquota 0 0
UUID=dddd\t/srv/bench/hdd\txfs\tdefaults,noatime,prjquota 0 2
UUID=eeee /srv/benchmarks xfs defaults 0 0
UUID=ffff /srv/bench xfs defaults 0 0
"""


class Rename(unittest.TestCase):
    def test_only_mount_points_under_old_move(self):
        out, changed = fstab.rename(FSTAB, "/srv/bench", "/srv/mcu")
        self.assertEqual(changed, [("/srv/bench/projects", "/srv/mcu/projects"),
                                   ("/srv/bench/hdd", "/srv/mcu/hdd"),
                                   ("/srv/bench", "/srv/mcu")])
        lines = out.splitlines()
        self.assertEqual(lines[4], "UUID=cccc /srv/mcu/projects xfs defaults,prjquota 0 0")
        self.assertEqual(lines[5], "UUID=dddd\t/srv/mcu/hdd\txfs\tdefaults,noatime,prjquota 0 2")
        # a sibling whose name only starts the same is not under it
        self.assertEqual(lines[6], "UUID=eeee /srv/benchmarks xfs defaults 0 0")
        # comments are not entries, even when they name the path
        self.assertEqual(lines[3], "# the data volumes (/srv/bench/projects is the ssd)")
        self.assertEqual(lines[:3], FSTAB.splitlines()[:3])

    def test_a_second_run_changes_nothing(self):
        once, _ = fstab.rename(FSTAB, "/srv/bench", "/srv/mcu")
        twice, changed = fstab.rename(once, "/srv/bench", "/srv/mcu")
        self.assertEqual(changed, [])
        self.assertEqual(twice, once)

    def test_reverse_puts_it_back(self):
        once, _ = fstab.rename(FSTAB, "/srv/bench/", "/srv/mcu/")
        back, _ = fstab.rename(once, "/srv/mcu", "/srv/bench")
        self.assertEqual(back, FSTAB)

    def test_no_trailing_newline_kept(self):
        out, _ = fstab.rename("UUID=x /srv/bench/hdd xfs defaults 0 0", "/srv/bench", "/srv/mcu")
        self.assertEqual(out, "UUID=x /srv/mcu/hdd xfs defaults 0 0")


class Command(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "fstab")
        Path(self.path).write_text(FSTAB)
        os.chmod(self.path, 0o644)

    def tearDown(self):
        self.dir.cleanup()

    def test_rename_rewrites_the_file_in_place(self):
        self.assertEqual(fstab.main(["fstab.py", "rename", self.path, "/srv/bench", "/srv/mcu"]), 0)
        self.assertIn("/srv/mcu/projects", Path(self.path).read_text())
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o644)
        self.assertFalse(os.path.exists(self.path + ".mcu-new"))

    def test_has(self):
        self.assertEqual(fstab.main(["fstab.py", "has", self.path, "/srv/bench/projects"]), 0)
        self.assertEqual(fstab.main(["fstab.py", "has", self.path, "/srv/bench/projects/"]), 0)
        self.assertEqual(fstab.main(["fstab.py", "has", self.path, "/"]), 0)
        self.assertEqual(fstab.main(["fstab.py", "has", self.path, "/srv/mcu/projects"]), 1)


if __name__ == "__main__":
    unittest.main()
