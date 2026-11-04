# SPDX-License-Identifier: MIT
"""The optional hardware modules (box/hw) on fake /sys trees: an Intel box
with everything, an AMD box without RAPL or GPU, a box with nothing. No
hardware needed: python3 -m unittest discover tests"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "box"))
import hw  # noqa: E402
from hw import cpufreq, drm, egpu, nvidia, rapl, smart, thunderbolt, virt  # noqa: E402


def tree(files, links=()):
    root = Path(tempfile.mkdtemp())
    for p, text in files.items():
        f = root / p.lstrip("/")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    for p, target in links:
        f = root / p.lstrip("/")
        f.parent.mkdir(parents=True, exist_ok=True)
        (root / target.lstrip("/")).mkdir(parents=True, exist_ok=True)
        os.symlink(root / target.lstrip("/"), f)
    return root


INTEL = tree({
    "/sys/devices/system/cpu/intel_pstate/max_perf_pct": "100\n",
    "/sys/devices/system/cpu/intel_pstate/status": "active\n",
    "/sys/class/powercap/intel-rapl:0/energy_uj": "123\n",
    "/sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw": "28000000\n",
    "/sys/bus/pci/devices/0000:06:00.0/vendor": "0x10de\n",
    "/sys/bus/pci/devices/0000:06:00.0/class": "0x030000\n",
    "/etc/cdi/nvidia.yaml": "cdiVersion: 0.5.0\n",
    "/sys/bus/thunderbolt/devices/domain0/uevent": "",
}, links=[("/sys/class/drm/renderD128/device/driver", "/sys/bus/pci/drivers/i915")])

AMD = tree({
    **{f"/sys/devices/system/cpu/cpu{n}/cpufreq/{k}": v for n in range(2)
       for k, v in (("cpuinfo_max_freq", "4000000\n"), ("scaling_max_freq", "4000000\n"),
                    ("scaling_driver", "amd-pstate-epp\n"))},
}, links=[("/sys/class/drm/renderD128/device/driver", "/sys/bus/pci/drivers/amdgpu")])

NONE = tree({"/sys/devices/system/cpu/cpu0/online": "1\n"})


class Detect(unittest.TestCase):
    def test_intel_box_has_everything(self):
        self.assertEqual(cpufreq.method(INTEL), "intel_pstate")
        self.assertEqual(rapl.detect(INTEL)[0], "present")
        self.assertEqual(nvidia.detect(INTEL)[0], "present")
        self.assertEqual(thunderbolt.detect(INTEL)[0], "present")
        self.assertEqual(drm.nodes(INTEL), {"renderD128": "i915"})

    def test_amd_box_without_rapl_or_gpu(self):
        self.assertEqual(cpufreq.method(AMD), "cpufreq")
        self.assertIn("amd-pstate-epp", cpufreq.detect(AMD)[1])
        self.assertEqual(rapl.detect(AMD)[0], "absent")
        self.assertEqual(nvidia.detect(AMD)[0], "absent")
        self.assertEqual(drm.nodes(AMD), {"renderD128": "amdgpu"})

    def test_nothing(self):
        rows = hw.report(NONE)
        # the dashboard finds a feature by its key: one per module, stable
        self.assertEqual([r["key"] for r in rows], ["cpufreq", "drm", "nvidia", "thunderbolt", "rapl", "smart", "virt"])
        states = {r["feature"]: r["state"] for r in rows}
        for feature in ("timing cap", "render node", "NVIDIA GPU", "eGPU (Thunderbolt)", "power (RAPL)"):
            self.assertEqual(states[feature], "absent", feature)

    def test_virtual_machine(self):
        vm = tree({"/proc/cpuinfo": "flags\t\t: fpu vme hypervisor lm\n"})
        self.assertEqual(virt.detect(vm)[0], "present")
        self.assertEqual(virt.detect(NONE)[0], "absent")

    def test_gpu_without_cdi_says_the_fix(self):
        root = tree({"/sys/bus/pci/devices/0000:01:00.0/vendor": "0x10de\n",
                     "/sys/bus/pci/devices/0000:01:00.0/class": "0x030200\n"})
        state, _, fix = nvidia.detect(root)
        self.assertEqual(state, "unsupported")
        self.assertIn("nvidia-container-toolkit", fix)


class FrequencyCap(unittest.TestCase):
    def test_intel_pstate(self):
        root = tree({"/sys/devices/system/cpu/intel_pstate/max_perf_pct": "100\n"})
        self.assertTrue(cpufreq.set_cap(65, root))
        self.assertEqual((root / "sys/devices/system/cpu/intel_pstate/max_perf_pct").read_text(), "65")
        self.assertTrue(cpufreq.capped(root))
        cpufreq.reset(root)
        self.assertFalse(cpufreq.capped(root))

    def test_generic_cpufreq_on_amd(self):
        root = tree({f"/sys/devices/system/cpu/cpu{n}/cpufreq/{k}": "4000000\n" for n in range(2)
                     for k in ("cpuinfo_max_freq", "scaling_max_freq")})
        self.assertTrue(cpufreq.set_cap(50, root))
        self.assertEqual((root / "sys/devices/system/cpu/cpu1/cpufreq/scaling_max_freq").read_text(), "2000000")
        self.assertTrue(cpufreq.capped(root))
        cpufreq.reset(root)
        self.assertEqual((root / "sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq").read_text(), "4000000")

    def test_no_control_at_all(self):
        self.assertFalse(cpufreq.set_cap(65, NONE))
        self.assertFalse(cpufreq.capped(NONE))


class Power(unittest.TestCase):
    def test_limit_read_and_write(self):
        root = tree({"/sys/class/powercap/intel-rapl:0/energy_uj": "1\n",
                     "/sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw": "28000000\n"})
        self.assertEqual(rapl.limit_w(root), 28.0)
        rapl.set_limit_w(19, root)
        self.assertEqual(rapl.limit_w(root), 19.0)
        with self.assertRaises(ValueError):
            rapl.set_limit_w(500, root)

    def test_amd_zone_and_none(self):
        amd = tree({"/sys/class/powercap/amd-rapl:0/energy_uj": "1\n",
                    "/sys/class/powercap/amd-rapl:0/constraint_0_power_limit_uw": "65000000\n"})
        self.assertEqual(rapl.limit_w(amd), 65.0)
        self.assertIsNone(rapl.limit_w(NONE))
        with self.assertRaises(OSError):
            rapl.set_limit_w(30, NONE)


class Smart(unittest.TestCase):
    def test_nvme(self):
        h = smart.parse({"path": "/dev/nvme0n1", "rota": False, "size": 512},
                        {"model_name": "SSD X", "smart_status": {"passed": True}, "temperature": {"current": 41},
                         "nvme_smart_health_information_log": {"percentage_used": 3, "data_units_written": 1000,
                                                               "power_on_hours": 900}})
        self.assertEqual((h["media"], h["wear_pct"], h["written"], h["hours"], h["temp"]), ("ssd", 3, 512000000, 900, 41))

    def test_hard_disk(self):
        h = smart.parse({"path": "/dev/sda", "rota": "1", "size": 1},
                        {"smart_status": {"passed": True}, "ata_smart_attributes": {"table": [
                            {"name": "Reallocated_Sector_Ct", "raw": {"value": 2}},
                            {"name": "Power_On_Hours", "raw": {"value": 12000}}]}})
        self.assertEqual((h["media"], h["reallocated"], h["hours"], h["wear_pct"]), ("hdd", 2, 12000, None))


class NvidiaSmi(unittest.TestCase):
    def run_with(self, stdout, code=0, exc=None):
        import subprocess
        saved = subprocess.run
        def fake(cmd, **kw):
            if exc:
                raise exc
            return subprocess.CompletedProcess(cmd, code, stdout, "")
        subprocess.run = fake
        try:
            return nvidia.query(["utilization.gpu", "memory.used", "power.draw"], units=True)
        finally:
            subprocess.run = saved

    def test_a_row(self):
        self.assertEqual(self.run_with("12, 3001, [N/A]\n"), ["12", "3001", "[N/A]"])
        self.assertIsNone(nvidia.num("[N/A]"))
        self.assertEqual(nvidia.num("3001"), 3001.0)

    def test_no_gpu_or_no_tool(self):
        import subprocess
        self.assertIsNone(self.run_with("", code=9))
        self.assertIsNone(self.run_with("12, 3001\n"))              # a field missing
        self.assertIsNone(self.run_with("", exc=FileNotFoundError()))
        self.assertIsNone(self.run_with("", exc=subprocess.TimeoutExpired("nvidia-smi", 10)))


class Egpu(unittest.TestCase):
    """the eGPU's operations on a fake /sys: what an eject and an attach
    write, and only that"""

    def setUp(self):
        self.root = tree({
            "/sys/bus/pci/devices/0000:00:1c.0/class": "0x060400\n",              # a bridge, suspended
            "/sys/bus/pci/devices/0000:00:1c.0/power/runtime_status": "suspended\n",
            "/sys/bus/pci/devices/0000:00:1c.0/power/control": "auto\n",
            "/sys/bus/pci/devices/0000:00:1d.0/class": "0x060400\n",              # a bridge, awake
            "/sys/bus/pci/devices/0000:00:1d.0/power/runtime_status": "active\n",
            "/sys/bus/pci/devices/0000:00:1d.0/power/control": "auto\n",
            "/sys/bus/pci/devices/0000:05:00.0/vendor": "0x10de\n",               # the card
            "/sys/bus/pci/devices/0000:05:00.0/class": "0x030000\n",
            "/sys/bus/pci/devices/0000:05:00.0/remove": "",
            "/sys/bus/pci/devices/0000:05:00.1/vendor": "0x10de\n",               # its audio
            "/sys/bus/pci/devices/0000:05:00.1/class": "0x040300\n",
            "/sys/bus/pci/devices/0000:05:00.1/remove": "",
            "/sys/bus/pci/devices/0000:00:02.0/vendor": "0x8086\n",               # the integrated GPU
            "/sys/bus/pci/devices/0000:00:02.0/class": "0x030000\n",
            "/sys/bus/pci/rescan": "",
            "/sys/bus/thunderbolt/devices/domain0/deauthorization": "1\n",
            "/sys/bus/thunderbolt/devices/0-0/authorized": "1\n",                 # the host: never touched
            "/sys/bus/thunderbolt/devices/0-1/authorized": "1\n",
            "/sys/bus/thunderbolt/devices/0-1/device_name": "An Enclosure\n",
        })

    def read(self, p):
        return (self.root / p.lstrip("/")).read_text().strip()

    def test_the_cards_functions_companions_first(self):
        self.assertEqual(egpu.functions(self.root), ["0000:05:00.1", "0000:05:00.0"])
        self.assertEqual(egpu.functions(tree({})), [])

    def test_remove_and_rescan(self):
        egpu.remove(egpu.functions(self.root), self.root)
        self.assertEqual(self.read("/sys/bus/pci/devices/0000:05:00.0/remove"), "1")
        self.assertEqual(self.read("/sys/bus/pci/devices/0000:05:00.1/remove"), "1")
        egpu.rescan(self.root)
        self.assertEqual(self.read("/sys/bus/pci/rescan"), "1")

    def test_deauthorize_then_reauthorize(self):
        self.assertEqual(egpu.deauthorize(self.root), ["An Enclosure"])
        self.assertEqual(self.read("/sys/bus/thunderbolt/devices/0-1/authorized"), "0")
        self.assertEqual(self.read("/sys/bus/thunderbolt/devices/0-0/authorized"), "1")
        self.assertEqual(egpu.reauthorize(self.root), ["0-1"])
        self.assertEqual(self.read("/sys/bus/thunderbolt/devices/0-1/authorized"), "1")

    def test_a_host_that_forbids_deauthorizing(self):
        (self.root / "sys/bus/thunderbolt/devices/domain0/deauthorization").write_text("0\n")
        self.assertEqual(egpu.deauthorize(self.root), [])
        self.assertEqual(self.read("/sys/bus/thunderbolt/devices/0-1/authorized"), "1")

    def test_only_suspended_bridges_are_woken(self):
        self.assertEqual(egpu.wake_ports(self.root), ["0000:00:1c.0"])
        self.assertEqual(self.read("/sys/bus/pci/devices/0000:00:1c.0/power/control"), "on")
        self.assertEqual(self.read("/sys/bus/pci/devices/0000:00:1d.0/power/control"), "auto")

    def test_bound(self):
        self.assertFalse(egpu.bound(self.root))
        (self.root / "sys/bus/pci/drivers/nvidia/0000:05:00.0").mkdir(parents=True)
        self.assertTrue(egpu.bound(self.root))


if __name__ == "__main__":
    unittest.main()
