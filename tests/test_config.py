# SPDX-License-Identifier: MIT
"""The configuration in one place (box/mcuconf.py) and the box's small
parsers, without root: python3 -m unittest discover tests"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

BOX = Path(__file__).resolve().parent.parent / "box"
sys.path.insert(0, str(BOX))
import mcuconf  # noqa: E402

spec = importlib.util.spec_from_file_location("mcu", BOX / "mcu.py")
mcu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcu)


class Layers(unittest.TestCase):
    """default < box.toml, and each value says where it came from"""

    def setUp(self):
        self.saved = mcuconf.ETC, mcuconf.STATE
        d = Path(tempfile.mkdtemp())
        mcuconf.ETC, mcuconf.STATE = d / "etc", d / "state"
        mcuconf.ETC.mkdir()
        mcuconf.STATE.mkdir()

    def tearDown(self):
        mcuconf.ETC, mcuconf.STATE = self.saved

    def test_no_file_gives_safe_defaults(self):
        conf, origins = mcuconf.load(with_origins=True)
        self.assertEqual(origins["reserved"], "computed")
        self.assertEqual(conf["envelope"]["network"], "fetch")
        self.assertEqual(origins["envelope.max_jobs"], "default")
        self.assertGreaterEqual(mcuconf._parse_cpus(conf["reserved"]).__len__(), 1)

    def test_file_over_defaults(self):
        (mcuconf.ETC / "box.toml").write_text('reserved = "0,6"\n[envelope]\nmax_cores = 10\nmax_disk = "40g"\n')
        (mcuconf.STATE / "envelope.json").write_text(json.dumps({"max_cores": 8}))   # no longer read
        conf, origins = mcuconf.load(with_origins=True)
        self.assertEqual(conf["reserved"], "0,6")
        self.assertEqual(origins["reserved"], "box.toml")
        self.assertEqual(conf["envelope"]["max_cores"], 10)
        self.assertEqual(origins["envelope.max_cores"], "box.toml")
        self.assertEqual(conf["envelope"]["max_disk_ssd"], "40g")      # the old name, still read

    def test_effective_rows_say_where_and_who(self):
        (mcuconf.ETC / "box.toml").write_text('reserved = "0,6"\n\n[envelope]\nmax_cores = 10\n\n[resources.gpu]\ncdi = "x"\n')
        rows = {r["key"]: r for r in mcuconf.effective()}
        self.assertEqual(set(rows), set(mcuconf.SETTINGS))
        self.assertEqual((rows["reserved"]["source"], rows["reserved"]["line"]), ("box.toml", 1))
        self.assertEqual((rows["envelope.max_cores"]["line"], rows["envelope.max_cores"]["writer"]), (4, "admin"))
        self.assertIsNotNone(rows["envelope.max_cores"]["default"])
        self.assertEqual((rows["resources"]["value"], rows["resources"]["line"]), (["gpu"], 6))
        self.assertEqual(rows["envelope.max_jobs"]["source"], "default")
        self.assertIsNone(rows["envelope.max_jobs"]["line"])
        self.assertEqual(rows["tls_cert"]["source"], "unset")
        self.assertEqual(rows["envelope.jobs_memory"]["source"], "computed")

    def test_declared_resources_win_over_detection(self):
        (mcuconf.ETC / "box.toml").write_text('[resources.gpu]\ncdi = "nvidia.com/gpu=all"\n')
        conf, origins = mcuconf.load(with_origins=True)
        self.assertEqual(list(conf["resources"]), ["gpu"])
        self.assertEqual(origins["resources"], "box.toml")


BOX_TOML = """# the box
reserved = "0,6"

[envelope]
max_cores = 10                # threads one job may request
memory = "10g"
exclusive = ["machine", "gpu"]

# the hardware
[resources.gpu]
cdi = "nvidia.com/gpu=all"
"""


class EditEnvelope(unittest.TestCase):
    """box.toml is the only truth: a change rewrites its line, nothing else"""

    def test_set_keeps_the_comment_and_the_rest(self):
        new = mcuconf.edit_envelope(BOX_TOML, {"max_cores": 8})
        self.assertIn("max_cores = 8                 # threads one job may request\n", new)
        self.assertEqual(new.replace("max_cores = 8                 #", "max_cores = 10                #"), BOX_TOML)

    def test_a_new_key_goes_after_the_last_value(self):
        new = mcuconf.edit_envelope(BOX_TOML, {"max_jobs": 3}, stamp="2026-11-04")
        lines = new.split("\n")
        self.assertEqual(lines[lines.index('exclusive = ["machine", "gpu"]') + 1],
                         "max_jobs = 3  # set from the dashboard, 2026-11-04")
        self.assertEqual(mcuconf.tomllib.loads(new)["resources"]["gpu"]["cdi"], "nvidia.com/gpu=all")

    def test_a_new_key_takes_the_place_of_its_example(self):
        text = '[envelope]\nmemory = "10g"\n# max_jobs = 2   # jobs at once\n# network = "fetch"\n'
        new = mcuconf.edit_envelope(text, {"max_jobs": 3}, stamp="d")
        self.assertEqual(new.split("\n")[2], "max_jobs = 3   # jobs at once")
        # an example without a comment of its own stays; the setting goes under it
        new = mcuconf.edit_envelope(text, {"network": "none"}, stamp="d")
        self.assertEqual(new.split("\n")[4], 'network = "none"  # set from the dashboard, d')

    def test_reset_keeps_an_explained_line_as_its_example(self):
        new = mcuconf.edit_envelope(BOX_TOML, {"max_cores": None})
        self.assertIn("# max_cores = 10                # threads one job may request\n", new)
        self.assertEqual(mcuconf.tomllib.loads(new)["envelope"], {"memory": "10g", "exclusive": ["machine", "gpu"]})
        # and a later set comes back to the same line, comment and all
        self.assertEqual(mcuconf.edit_envelope(new, {"max_cores": 10}), BOX_TOML)

    def test_reset_removes_a_bare_or_dashboard_line(self):
        new = mcuconf.edit_envelope(BOX_TOML, {"memory": None})
        self.assertNotIn("memory", new)
        self.assertEqual(mcuconf.tomllib.loads(new)["envelope"], {"max_cores": 10, "exclusive": ["machine", "gpu"]})
        added = mcuconf.edit_envelope(BOX_TOML, {"max_jobs": 3}, stamp="d")
        self.assertEqual(mcuconf.edit_envelope(added, {"max_jobs": None}), BOX_TOML)

    def test_lists_and_no_table(self):
        new = mcuconf.edit_envelope('reserved = "0"\n', {"exclusive": ["machine"], "network": "none"})
        self.assertEqual(mcuconf.tomllib.loads(new)["envelope"], {"exclusive": ["machine"], "network": "none"})
        self.assertEqual(mcuconf.edit_envelope('reserved = "0"\n', {"memory": None}), 'reserved = "0"\n')

    def test_a_multiline_value_is_refused(self):
        with self.assertRaises(ValueError):
            mcuconf.edit_envelope('[envelope]\nexclusive = [\n  "machine",\n]\n', {"exclusive": ["disk"]})

    def test_write_keeps_a_backup(self):
        d = Path(tempfile.mkdtemp())
        f = d / "box.toml"
        f.write_text(BOX_TOML)
        diff, bak = mcuconf.write_box(mcuconf.edit_envelope(BOX_TOML, {"max_cores": 8}), f)
        self.assertIn("-max_cores = 10", diff)
        self.assertEqual(bak.read_text(), BOX_TOML)
        self.assertEqual(mcuconf.write_box(f.read_text(), f), ("", None))


class QuotaWriter(unittest.TestCase):
    """a project's quota has one writer: the Benchfile when one in use sets it"""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.saved = mcu.benchfile_path
        mcu.benchfile_path = lambda label, version: self.d / f"{label or 'default'}-v{version}.toml"

    def tearDown(self):
        mcu.benchfile_path = self.saved

    def test_config_disk_defers_to_the_benchfile(self):
        (self.d / "default-v3.toml").write_text('[project]\ndisk = "40g"\n')
        (self.d / "perf-v1.toml").write_text('[project]\ndisk = { hdd = "300g" }\n')
        conf = {"benchfile": 3, "benchfiles": {"perf": 1}}
        self.assertEqual(mcu.disk_set_by_benchfile(conf, "ssd"), (None, 3, "40g"))
        self.assertEqual(mcu.disk_set_by_benchfile(conf, "hdd"), ("perf", 1, "300g"))
        with self.assertRaises(mcu.Refused) as e:
            mcu.refuse_if_benchfile_sets_disk(conf, "ssd")
        self.assertIn("Benchfile v3 sets the ssd disk (40g)", str(e.exception))

    def test_without_one_config_disk_writes(self):
        (self.d / "default-v1.toml").write_text('[jobs.test]\nrun = "true"\n')
        self.assertIsNone(mcu.disk_set_by_benchfile({"benchfile": 1}, "ssd"))
        mcu.refuse_if_benchfile_sets_disk({"benchfile": 1}, "ssd")
        mcu.refuse_if_benchfile_sets_disk({}, "ssd")


class Bounds(unittest.TestCase):
    def test_from_the_machine(self):
        b = mcuconf.bounds()
        self.assertEqual(b["max_cores"], (1, os.cpu_count()))
        self.assertIsNone(mcuconf.in_bounds("max_cores", 1))
        self.assertIn("between", mcuconf.in_bounds("max_cores", os.cpu_count() + 1))
        self.assertIsNone(mcuconf.in_bounds("timeout_max", "3h"))
        self.assertIn("between 1h and 24h", mcuconf.in_bounds("timeout_max", "48h"))
        self.assertIsNone(mcuconf.in_bounds("max_disk_hdd", "0"))
        self.assertIsNone(mcuconf.in_bounds("network", "full"))      # not a bounded key


class Parsers(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(mcu.parse_size("2g"), 2 * 2**30)
        self.assertEqual(mcu.parse_size("512m"), 512 * 2**20)
        with self.assertRaises(mcu.Refused):
            mcu.parse_size("lots")

    def test_durations(self):
        self.assertEqual(mcu.parse_duration("3h"), 3 * 3600)
        self.assertEqual(mcu.parse_duration("30m"), 1800)
        self.assertEqual(mcu.parse_duration(90), 90)

    def test_cpus(self):
        self.assertEqual(mcu.parse_cpus("0,6"), {0, 6})
        self.assertEqual(mcu.parse_cpus("0-2,8"), {0, 1, 2, 8})


class TrackerRefs(unittest.TestCase):
    def test_pattern(self):
        loader = importlib.machinery.SourceFileLoader("check_refs", str(BOX.parent / "scripts" / "check-refs"))
        src = loader.get_source("check_refs")
        ref = None
        for line in src.splitlines():
            if line.startswith("REF = "):
                ref = eval(line[len("REF = "):], {"re": __import__("re")})
        h = "#"   # kept apart: this file is checked for references too
        self.assertTrue(ref.search("fixed in " + h + "4128"))
        self.assertTrue(ref.search("(see " + h + "3992)"))
        self.assertFalse(ref.search('stroke: "#888"'))
        self.assertFalse(ref.search("anchor #top"))
