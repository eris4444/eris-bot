"""Fans, profiles and the stress test on a fake /sys (hwmon with pwm/fan/temp, cpufreq with and without)."""
import base  # noqa: F401  (environment first)
import os
import time
import unittest

from base import ROOT, set_temp
from erisbot import config, fand, hw, perf, stress, system

HW = ROOT + "/sys/class/hwmon"
GPU = "nouveau@0000:01:00.0/pwm1"


def rd(p):
    with open(p) as f:
        return f.read().strip()


class Discovery(unittest.TestCase):
    def setUp(self):
        base.reset_tree()

    def test_only_real_fans(self):
        fans = hw.fans()
        self.assertEqual([f["id"] for f in fans], [GPU])
        f = fans[0]
        self.assertEqual(f["kind"], "gpu")
        self.assertTrue(f["enable"].endswith("pwm1_enable"))
        self.assertTrue(f["input"].endswith("fan1_input"))
        self.assertEqual(hw.fan_temp(f), 45.0)
        base.build_sys(gpu_fan=False)
        self.assertEqual(hw.fans(), [])
        base.build_sys(cpu_fan=True)
        kinds = {f["chip"]: f["kind"] for f in hw.fans()}
        self.assertEqual(kinds, {"nouveau": "gpu", "it87": "cpu"})

    def test_percent_to_pwm(self):
        base.build_sys(cpu_fan=True)
        gpu, cpu = sorted(hw.fans(), key=lambda f: f["chip"], reverse=True)
        self.assertEqual(gpu["chip"], "nouveau")
        self.assertEqual(hw.pct_to_pwm(gpu, 50), 50)             # pwm1_min 0, pwm1_max 100
        self.assertEqual(hw.pct_to_pwm(cpu, 50), 128)            # no min/max: 0..255
        self.assertEqual(hw.pct_to_pwm(cpu, 100), 255)
        base.put(HW + "/hwmon3/pwm1_min", "20\n")
        self.assertEqual(hw.pct_to_pwm(gpu, 0), 20)
        self.assertEqual(hw.pct_to_pwm(gpu, 50), 60)
        self.assertAlmostEqual(hw.pwm_to_pct(gpu, 60), 50.0)

    def test_cpu_temp_is_the_hottest_core(self):
        set_temp("hwmon1/temp3_input", 61)
        self.assertEqual(system.cpu_temp(), 61.0)


class Control(unittest.TestCase):
    def setUp(self):
        base.reset_tree()
        base.build_sys(cpu_fan=True)
        self.cpu_t = [50.0]
        self.c = fand.Controller(temp_fns={"cpu": lambda: self.cpu_t[0], "gpu": None})

    def configure(self, fid, **fc):
        cfg = fand.load_cfg()
        cfg["fans"][fid] = fc
        fand.save_cfg(cfg)

    def cpu_fan(self):
        return next(f for f in self.c.fans if f["kind"] == "cpu")

    def test_curve_values(self):
        pts = fand.CURVES["normal"]
        self.assertEqual(fand.curve_value(pts, 30), 30)
        self.assertEqual(fand.curve_value(pts, 45), 37.5)
        self.assertEqual(fand.curve_value(pts, 80), 100)

    def test_curve_hysteresis_and_steps(self):
        f = self.cpu_fan()
        st = fand.FanState()
        fc = fand.fan_cfg({"fans": {f["id"]: {"mode": "curve", "curve": fand.CURVES["normal"]}}}, f["id"])
        self.assertEqual(self.c.target(f, st, 50, fc), 45)       # first value straight from the curve
        st.pct = 45
        self.assertEqual(self.c.target(f, st, 60, fc), 55)       # up by at most 10 a step
        st.pct = 55
        self.assertEqual(self.c.target(f, st, 60, fc), 65)
        st.pct = 65
        self.assertEqual(self.c.target(f, st, 58, fc), 65)       # cooler by less than 3 °C: hold
        self.assertEqual(self.c.target(f, st, 57, fc), 65)
        self.assertEqual(self.c.target(f, st, 55, fc), 61)       # cooling: follows the curve 3 °C higher
        self.assertEqual(self.c.target(f, st, 40, fc), 60)       # far below: down by at most 5 a step
        st.pct = 26
        self.assertEqual(self.c.target(f, st, 20, fc), 30)       # never below the curve's lowest point

    def test_manual_minimum_25(self):
        f = self.cpu_fan()
        self.configure(f["id"], mode="manual", percent=10)
        self.c.step()
        self.assertEqual(rd(f["pwm"]), str(hw.pct_to_pwm(f, 25)))
        self.assertEqual(rd(f["enable"]), "1")

    def test_protection_overrides_everything(self):
        f = self.cpu_fan()
        self.configure(f["id"], mode="auto")
        self.cpu_t[0] = 76
        s = self.c.step()
        self.assertEqual(rd(f["pwm"]), "255")
        self.assertTrue(s["fans"][f["id"]]["protect"])
        self.assertTrue(any(a["key"] == "fan_hot:" + f["id"] for a in s["alerts"]))
        self.cpu_t[0] = 73                                         # still inside the 3 °C band
        self.c.step()
        self.assertEqual(rd(f["pwm"]), "255")
        self.cpu_t[0] = 70
        s = self.c.step()
        self.assertFalse(s["fans"][f["id"]]["protect"])
        self.assertEqual(rd(f["enable"]), "2")                    # auto again: the original pwm_enable
        set_temp("hwmon3/temp1_input", 81)                        # GPU at 81 °C
        s = self.c.step()
        self.assertEqual(rd(HW + "/hwmon3/pwm1"), "100")
        self.assertTrue(s["fans"][GPU]["protect"])

    def test_stalled_fan(self):
        self.configure(GPU, mode="manual", percent=60)
        base.put(HW + "/hwmon3/fan1_input", "0\n")
        for _ in range(2):
            s = self.c.step()
        self.assertFalse(s["fans"][GPU]["stall"])
        s = self.c.step()
        self.assertTrue(s["fans"][GPU]["stall"])
        self.assertTrue(any(a["key"] == "fan_stall:" + GPU for a in s["alerts"]))

    def test_back_to_auto_on_stop_and_after_a_crash(self):
        self.configure(GPU, mode="manual", percent=80)
        self.c.step()
        self.assertEqual(rd(HW + "/hwmon3/pwm1_enable"), "1")
        self.c.restore_all()
        self.assertEqual(rd(HW + "/hwmon3/pwm1_enable"), "2")
        self.c.step()                                              # manual again, then "kill -9"
        self.assertEqual(rd(HW + "/hwmon3/pwm1_enable"), "1")
        fand.restore_from_file()                                   # ExecStopPost=eris-bot fan-restore
        self.assertEqual(rd(HW + "/hwmon3/pwm1_enable"), "2")

    def test_a_fan_left_manual_is_remembered_as_auto(self):
        os.remove(fand.ORIG)
        base.put(HW + "/hwmon3/pwm1_enable", "1\n")
        c = fand.Controller(temp_fns={"cpu": lambda: 40.0, "gpu": None})
        self.assertEqual(c.orig[GPU]["enable"], "2")

    def test_daemon_writes_status_and_restores(self):
        self.configure(GPU, mode="manual", percent=70)
        fand.run_daemon(interval=0.01, max_cycles=2)
        st = config.read_json(fand.STATUS, {})
        self.assertIn(GPU, st["fans"])
        self.assertEqual(rd(HW + "/hwmon3/pwm1_enable"), "2")     # restored at the end


class Profiles(unittest.TestCase):
    def setUp(self):
        base.reset_tree()

    def test_without_cpufreq(self):
        r = perf.apply("performance")
        text = "\n".join(r["changes"])
        self.assertIn("منحنی تند", text)
        self.assertIn("vm.swappiness: از 60 به 10", text)
        self.assertTrue(any("cpufreq" in u for u in r["unavailable"]))
        self.assertEqual(rd(ROOT + "/proc/sys/vm/swappiness"), "10")
        self.assertEqual(fand.load_cfg()["fans"][GPU]["curve_name"], "steep")
        av = perf.availability(perf.PROFILES["max"])
        self.assertFalse(dict((w, ok) for w, ok, _ in av)["حداقل فرکانس = حداکثر"])

    def test_with_cpufreq(self):
        base.build_sys(cpufreq=True)
        cpu0 = ROOT + "/sys/devices/system/cpu/cpu0/cpufreq/"
        r = perf.apply("max")
        self.assertEqual(rd(cpu0 + "scaling_governor"), "performance")
        self.assertEqual(rd(cpu0 + "scaling_min_freq"), "2833000")
        self.assertEqual(fand.load_cfg()["fans"][GPU], {"mode": "manual", "percent": 100})
        self.assertIn("governor: از ondemand به performance", r["changes"])
        r = perf.apply("quiet")
        self.assertEqual(rd(cpu0 + "scaling_governor"), "powersave")
        self.assertEqual(rd(cpu0 + "scaling_min_freq"), "2000000")
        self.assertEqual(rd(ROOT + "/proc/sys/vm/swappiness"), "60")
        r = perf.apply("balanced")                                  # schedutil missing: ondemand
        self.assertEqual(rd(cpu0 + "scaling_governor"), "ondemand")
        perf.apply("performance")
        ch = perf.reset_default()
        self.assertEqual(rd(cpu0 + "scaling_governor"), "ondemand")
        self.assertEqual(rd(ROOT + "/proc/sys/vm/swappiness"), "60")
        self.assertEqual(fand.load_cfg()["fans"][GPU], {"mode": "auto"})
        self.assertIsNone(perf.load()["active"])
        self.assertTrue(ch)

    def test_profile_survives_a_reboot(self):
        base.build_sys(cpufreq=True)
        cpu0 = ROOT + "/sys/devices/system/cpu/cpu0/cpufreq/"
        perf.apply("performance")
        base.build_sys(cpufreq=True)                                # after a reboot the kernel defaults are back
        base.put(ROOT + "/proc/sys/vm/swappiness", "60\n")
        self.assertEqual(rd(cpu0 + "scaling_governor"), "ondemand")
        perf.apply_saved_system()                                   # eris-fan.service at boot
        self.assertEqual(rd(cpu0 + "scaling_governor"), "performance")
        self.assertEqual(rd(ROOT + "/proc/sys/vm/swappiness"), "10")

    def test_custom_profile(self):
        base.build_sys(cpufreq=True)
        perf.save_custom("شب", "quiet", "conservative", None)
        self.assertIn("c:شب", perf.all_profiles())
        perf.apply("c:شب")
        self.assertEqual(rd(ROOT + "/sys/devices/system/cpu/cpu1/cpufreq/scaling_governor"), "conservative")
        perf.delete_custom("شب")
        self.assertNotIn("c:شب", perf.all_profiles())


class Stress(unittest.TestCase):
    def setUp(self):
        base.reset_tree()

    def run_test(self, temp, minutes=1):
        done = []
        t = stress.StressTest(minutes, None, done.append, temp_fn=lambda: temp, workers=1, seconds_per_minute=0.6,
                              sample_every=0.1, check_every=0.05)
        t.start()
        t.thread.join(10)
        self.assertEqual(done, [t])
        for p in t.procs:
            self.assertIsNotNone(p.poll())                         # the load processes are gone
        return t

    def test_stops_by_itself_at_85(self):
        t0 = time.time()
        t = self.run_test(86.0, minutes=60)
        self.assertLess(time.time() - t0, 5)
        self.assertIn("۸۶", t.reason)
        self.assertIn("قطع شد", t.reason)
        self.assertIn("ری‌استارت نشد", t.result_text())

    def test_full_run(self):
        t = self.run_test(55.0)
        self.assertEqual(t.reason, "تمام شد")
        self.assertGreaterEqual(len(t.samples), 3)
        self.assertEqual(t.max_temp, 55.0)
        self.assertIn("بیشترین دمای CPU", t.result_text())
        self.assertIn("RPM", t.progress_text())

    def test_reboot_during_a_test_is_noticed(self):
        config.write_json(stress.STATE, {"running": True, "boot_id": "old-boot", "pids": [], "chat_id": 1}, 0o600)
        st, rebooted = stress.leftover()
        self.assertTrue(rebooted)
        self.assertIsNone(stress.leftover())


class SensorsDetect(unittest.TestCase):
    SAMPLE = """# sensors-detect version 3.6.0
Probing for Super-I/O at 0x2e/0x2f
Trying family `ITE'...                                      Yes
Found unknown chip with ID 0x8728
Trying family `National Semiconductor/Nuvoton'...           No
Driver `coretemp':
  * Chip `Intel digital thermal sensor' (confidence: 9)
Driver `it87':
  * ISA bus, address 0x290
    Chip `ITE IT8718F Super IO Sensors' (confidence: 9)

To load everything that is needed, add this to /etc/modules:
#----cut here----
# Chip drivers
coretemp
it87
#----cut here----
"""

    def test_parse(self):
        p = hw.parse_sensors_detect(self.SAMPLE)
        self.assertEqual(p["drivers"], ["coretemp", "it87"])
        self.assertEqual(p["fan_drivers"], ["it87"])
        self.assertEqual(p["unknown_ids"], ["0x8728"])

    def test_persist(self):
        base.reset_tree()
        hw.persist_driver("it87", "0x8728")
        self.assertIn("it87", rd(config.MODULES_LOAD))
        self.assertIn("options it87 force_id=0x8728", rd(config.MODPROBE_D))
        with self.assertRaises(ValueError):
            hw.modprobe("rm -rf /")


if __name__ == "__main__":
    unittest.main()


class AutoDetect(unittest.TestCase):
    """A G41 board whose it87 chip is claimed by ACPI: plain modprobe gives nothing, the option does."""

    def setUp(self):
        base.reset_tree()
        self.calls = []
        self.dmesg = ""
        self.orig = system.sh

        def fake(cmd, timeout=30, input=None):
            self.calls.append(" ".join(cmd))
            ok = system.subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[0] == "dmesg":
                return system.subprocess.CompletedProcess(cmd, 0, self.dmesg, "")
            if cmd[:2] == ["modprobe", "it87"] and len(cmd) == 2:
                self.dmesg += "it87: Found IT8718F chip at 0x290, revision 5\nACPI Warning: SystemIO range 0x295 conflicts with OpRegion (it87)\nit87: resource conflict, acpi_enforce_resources=lax\n"
                return system.subprocess.CompletedProcess(cmd, 1, "", "modprobe: ERROR: could not insert 'it87': Device or resource busy")
            if cmd == ["modprobe", "it87", "ignore_resource_conflict=1"]:
                d = HW + "/hwmon5"
                for f, v in (("name", "it87"), ("pwm1", 255), ("pwm1_enable", 2), ("fan1_input", 2400),
                             ("pwm2", 255), ("pwm2_enable", 2), ("fan2_input", 0)):
                    base.put(d + "/" + f, "%s\n" % v)
                return ok
            if cmd[0] == "modprobe":
                return system.subprocess.CompletedProcess(cmd, 1, "", "No such device")
            return ok
        system.sh = fake

    def tearDown(self):
        system.sh = self.orig

    def fan_follows_pwm(self, _):
        pwm = int(rd(HW + "/hwmon5/pwm1"))
        base.put(HW + "/hwmon5/fan1_input", "%d\n" % (600 + pwm * 7))

    def test_finds_tests_and_keeps_it87(self):
        rep = hw.auto_detect(settle=0, sleep=self.fan_follows_pwm)
        self.assertEqual(rep["driver"], "it87")
        self.assertEqual(rep["options"], "ignore_resource_conflict=1")
        self.assertTrue(rep["conflict"])
        res = {f["id"]: t for f, t in rep["fans"]}
        self.assertTrue(res["it87@hwmon5/pwm1"]["ok"])
        self.assertEqual(res["it87@hwmon5/pwm1"]["input"], "fan1")
        self.assertFalse(res["it87@hwmon5/pwm2"]["ok"])           # nothing turns on pwm2
        self.assertEqual(rd(HW + "/hwmon5/pwm1_enable"), "2")     # given back after the test
        self.assertEqual(rd(HW + "/hwmon5/pwm1"), "255")
        self.assertIn("it87", rd(config.MODULES_LOAD))
        self.assertIn("options it87 ignore_resource_conflict=1", rd(config.MODPROBE_D))
        label = {f["id"]: f["label"] for f in hw.fans()}["it87@hwmon5/pwm1"]
        self.assertIn("fan1", label)
        self.assertFalse(any("force_id" in c for c in self.calls))   # never forced

    def test_nothing_found_is_said_honestly(self):
        system_sh = system.sh

        def none(cmd, timeout=30, input=None):
            if cmd[0] == "modprobe" and "-r" not in cmd:
                return system.subprocess.CompletedProcess(cmd, 1, "", "No such device")
            return system_sh(cmd, timeout, input)
        system.sh = none
        rep = hw.auto_detect(settle=0, sleep=lambda s: None)
        self.assertIsNone(rep["driver"])
        self.assertFalse(rep["conflict"])
        self.assertEqual(len(rep["steps"]), len(hw.TRY_ORDER))

    def test_grub_lax(self):
        base.put(config.GRUB, 'GRUB_DEFAULT=0\nGRUB_CMDLINE_LINUX_DEFAULT="quiet"\n')
        ok, msg = hw.acpi_lax()
        self.assertTrue(ok, msg)
        self.assertIn('GRUB_CMDLINE_LINUX_DEFAULT="quiet acpi_enforce_resources=lax"', rd(config.GRUB))
        self.assertIn("update-grub", self.calls)
        self.assertFalse(hw.acpi_lax()[0])                        # only once
