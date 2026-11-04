# SPDX-License-Identifier: MIT
"""mcuctl admin deploy checks its options before it builds or copies
anything: help is help, and a typo stops. python3 -m unittest discover tests"""
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

mcuctl = SourceFileLoader("mcuctl", str(Path(__file__).resolve().parent.parent / "client" / "mcuctl")).load_module()


class DeployArgs(unittest.TestCase):
    def test_help_is_not_a_deploy(self):
        self.assertIsNone(mcuctl.deploy_args(["--help"]))
        self.assertIsNone(mcuctl.deploy_args(["--priority", "-h"]))

    def test_known_options_pass(self):
        args = ["--parts", "admin,conf", "--priority", "--dry-run"]
        self.assertEqual(mcuctl.deploy_args(args), args)

    def test_unknown_option_stops(self):
        with self.assertRaises(SystemExit):
            mcuctl.deploy_args(["--priorty"])
        with self.assertRaises(SystemExit):
            mcuctl.deploy_args(["--parts", "admin", "extra"])

    def test_deploy_with_help_does_nothing(self):
        # no box is touched: deploy returns before using it
        self.assertEqual(mcuctl.deploy(None, ["--help"]), 0)

    def test_default_parts_follow_install_list(self):
        for part in ("conf", "hw", "version", "admin"):
            self.assertIn(part, mcuctl.PARTS)


if __name__ == "__main__":
    unittest.main()
