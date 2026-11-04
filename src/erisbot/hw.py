"""Fans (pwm in /sys/class/hwmon), CPU frequency (cpufreq) and the like, as they really are on this server.

Nothing here raises the FSB or the voltage: on Core 2 / G41 that is only safe from the BIOS.
"""
import glob
import os
import re

from . import config, system
from .util import read, read_int, write

GPU_CHIPS = ("nouveau", "amdgpu", "radeon", "nvidia")
CPU_CHIPS = ("coretemp", "k10temp", "zenpower", "cpu_thermal")
# Super I/O and other drivers that can drive the CPU / case fans
FAN_DRIVERS = ("it87", "nct6775", "nct6683", "nct6687", "w83627ehf", "w83627hf", "w83781d", "w83791d", "w83792d",
               "w83793", "w83795", "f71882fg", "f71805f", "f75375s", "sch5627", "sch5636", "asc7621", "adt7475",
               "lm85", "lm63", "smsc47m1", "smsc47m192", "pc87360", "pc87427", "vt1211", "dme1737", "emc6w201",
               "fschmd", "asb100", "it87e", "gl518sm", "gl520sm", "adm1031")


def hwmon_dirs():
    return [d for d, _ in system.hwmons()]


def device_name(d):
    dev = os.path.join(d, "device")
    if os.path.exists(dev):
        return os.path.basename(os.path.realpath(dev))
    return os.path.basename(d)


def fans():
    """Every pwm output that exists: [{id, chip, label, kind, dir, n, pwm, enable, input, min, max, temp}]."""
    out = []
    for d, chip in system.hwmons():
        for f in sorted(os.listdir(d)):
            m = re.fullmatch(r"pwm(\d+)", f)
            if not m:
                continue
            n = m.group(1)
            kind = "gpu" if chip in GPU_CHIPS else "cpu"
            p = lambda name: os.path.join(d, name) if os.path.exists(os.path.join(d, name)) else None  # noqa: E731
            fan = {"id": "%s@%s/pwm%s" % (chip, device_name(d), n), "chip": chip, "dir": d, "n": n, "kind": kind,
                   "pwm": os.path.join(d, f), "enable": p("pwm%s_enable" % n), "input": p("fan%s_input" % n),
                   "min": p("pwm%s_min" % n), "max": p("pwm%s_max" % n),
                   "temp": os.path.join(d, "temp1_input") if kind == "gpu" and os.path.exists(os.path.join(d, "temp1_input")) else None}
            fan["label"] = ("فن کارت گرافیک" if kind == "gpu" else "فن CPU/کیس") + " (%s pwm%s)" % (chip, n)
            out.append(fan)
    return out


def pwm_range(fan):
    lo = read_int(fan["min"]) if fan.get("min") else None
    hi = read_int(fan["max"]) if fan.get("max") else None
    lo = 0 if lo is None else lo
    hi = 255 if hi is None or hi <= lo else hi
    return lo, hi


def pct_to_pwm(fan, pct):
    lo, hi = pwm_range(fan)
    pct = max(0.0, min(100.0, float(pct)))
    return int(round(lo + (hi - lo) * pct / 100.0))


def pwm_to_pct(fan, value):
    lo, hi = pwm_range(fan)
    if value is None:
        return None
    return max(0.0, min(100.0, 100.0 * (value - lo) / (hi - lo)))


def rpm(fan):
    return read_int(fan["input"]) if fan.get("input") else None


def fan_temp(fan):
    """GPU fan: its own chip; CPU/case fan: the hottest CPU core."""
    if fan["kind"] == "gpu" and fan.get("temp"):
        v = read_int(fan["temp"])
        return v / 1000.0 if v is not None else None
    return system.cpu_temp()


def gpu_temp():
    for d, chip in system.hwmons():
        if chip in GPU_CHIPS:
            v = read_int(os.path.join(d, "temp1_input"))
            if v is not None:
                return v / 1000.0
    return None


# ---------------------------------------------------------------- cpufreq / sysctl

def cpus():
    return sorted(glob.glob(system.P("/sys/devices/system/cpu/cpu[0-9]*")), key=lambda p: int(re.sub(r"\D", "", os.path.basename(p))))


def cpufreq_dirs():
    return [os.path.join(c, "cpufreq") for c in cpus() if os.path.isdir(os.path.join(c, "cpufreq"))]


def has_cpufreq():
    return bool(cpufreq_dirs())


def governors():
    d = cpufreq_dirs()
    return (read(os.path.join(d[0], "scaling_available_governors"), "") or "").split() if d else []


def governor():
    d = cpufreq_dirs()
    return read(os.path.join(d[0], "scaling_governor")) if d else None


def set_governor(g):
    for d in cpufreq_dirs():
        write(os.path.join(d, "scaling_governor"), g)


def freq_limits():
    """{cpu dir: (min, max, hw_min, hw_max)} in kHz."""
    out = {}
    for d in cpufreq_dirs():
        out[d] = (read_int(os.path.join(d, "scaling_min_freq")), read_int(os.path.join(d, "scaling_max_freq")),
                  read_int(os.path.join(d, "cpuinfo_min_freq")), read_int(os.path.join(d, "cpuinfo_max_freq")))
    return out


def set_min_freq(d, khz):
    write(os.path.join(d, "scaling_min_freq"), khz)


def cur_mhz():
    """Average current frequency in MHz (cpufreq, else /proc/cpuinfo)."""
    vals = [read_int(os.path.join(d, "scaling_cur_freq")) for d in cpufreq_dirs()]
    vals = [v / 1000.0 for v in vals if v]
    if not vals:
        for line in (read(system.P("/proc/cpuinfo"), "") or "").splitlines():
            if line.startswith("cpu MHz"):
                try:
                    vals.append(float(line.split(":")[1]))
                except (IndexError, ValueError):
                    pass
    return sum(vals) / len(vals) if vals else None


SWAPPINESS = "/proc/sys/vm/swappiness"


def swappiness():
    return read_int(system.P(SWAPPINESS))


def set_swappiness(v):
    write(system.P(SWAPPINESS), int(v))


def throttle_count():
    total, found = 0, False
    for c in cpus():
        for f in glob.glob(os.path.join(c, "thermal_throttle", "*_throttle_count")):
            v = read_int(f)
            if v is not None:
                total += v
                found = True
    return total if found else None


# ---------------------------------------------------------------- finding the CPU fan controller

def parse_sensors_detect(text):
    """{"drivers": [...], "fan_drivers": [...], "unknown_ids": ["0x8728", ...]} from sensors-detect --auto."""
    drivers = re.findall(r"Driver [`']([\w-]+)'", text)
    cut = re.search(r"#-+cut here-+\n(.*?)#-+cut here-+", text, re.S)
    if cut:
        for line in cut.group(1).splitlines():
            line = line.strip()
            if re.fullmatch(r"[a-z0-9_]+", line):
                drivers.append(line)
    seen, uniq = set(), []
    for d in drivers:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    ids = sorted(set(re.findall(r"(?:unknown chip with ID|chip ID|Found unknown chip with ID)\s+(0x[0-9a-fA-F]{4})", text)))
    return {"drivers": uniq, "fan_drivers": [d for d in uniq if d in FAN_DRIVERS], "unknown_ids": ids}


def sensors_detect():
    r = system.sh([config.SENSORS_DETECT, "--auto"], timeout=600)
    return (r.stdout + r.stderr), r.returncode


def modprobe(driver, force_id=None):
    if driver not in FAN_DRIVERS:
        raise ValueError("not a fan driver: %s" % driver)
    args = ["modprobe", driver]
    if force_id:
        if not re.fullmatch(r"0x[0-9a-fA-F]{4}", force_id):
            raise ValueError("bad force_id")
        args.append("force_id=%s" % force_id)
    return system.sh(args, timeout=60)


def persist_driver(driver, force_id=None):
    """Load the driver at every boot (/etc/modules-load.d/eris-bot-fans.conf, force_id in /etc/modprobe.d)."""
    have = (read(config.MODULES_LOAD, "") or "").split()
    lines = ["# fan controllers found by eris-bot (eris-bot removes this file on uninstall)"]
    lines += sorted(set([d for d in have if not d.startswith("#") and d in FAN_DRIVERS] + [driver]))
    os.makedirs(os.path.dirname(config.MODULES_LOAD), exist_ok=True)
    write(config.MODULES_LOAD, "\n".join(lines) + "\n")
    if force_id:
        os.makedirs(os.path.dirname(config.MODPROBE_D), exist_ok=True)
        write(config.MODPROBE_D, "# written by eris-bot\noptions %s force_id=%s\n" % (driver, force_id))
