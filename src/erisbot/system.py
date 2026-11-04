"""What the server looks like right now: uptime, temperatures, CPU, memory, disks, services, containers."""
import fnmatch
import glob
import os
import re
import subprocess
import time

from . import config
from .util import read, read_int


def sh(cmd, timeout=30, input=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input,
                              stdin=None if input is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout after %d s" % timeout)
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def P(path):
    """/proc/... or /sys/... under the fake roots of the tests."""
    if path.startswith("/proc/"):
        return config.PROC + path[5:]
    if path.startswith("/sys/"):
        return config.SYS + path[4:]
    return path


def read_raw(path):
    try:
        with open(P(path), "rb") as f:
            return f.read()
    except OSError:
        return None


def uptime():
    v = read(P("/proc/uptime"), "0") or "0"
    try:
        return float(v.split()[0])
    except ValueError:
        return 0.0


def boot_id():
    return read(P("/proc/sys/kernel/random/boot_id"), "") or ""


def hostname():
    return read(P("/proc/sys/kernel/hostname")) or os.uname().nodename


def cpu_times():
    for line in (read(P("/proc/stat"), "") or "").splitlines():
        if line.startswith("cpu "):
            v = [int(x) for x in line.split()[1:9]]
            return sum(v), v[3] + v[4]
    return 0, 0


def cpu_usage(interval=0.4):
    t1, i1 = cpu_times()
    time.sleep(interval)
    t2, i2 = cpu_times()
    dt = t2 - t1
    return max(0.0, min(100.0, 100.0 * (dt - (i2 - i1)) / dt)) if dt > 0 else None


def loadavg():
    v = (read(P("/proc/loadavg"), "0 0 0") or "0 0 0").split()
    return [float(x) for x in v[:3]]


def memory():
    mi = {}
    for line in (read(P("/proc/meminfo"), "") or "").splitlines():
        k, _, v = line.partition(":")
        p = v.split()
        if p and p[0].isdigit():
            mi[k] = int(p[0]) * 1024
    total = mi.get("MemTotal", 0)
    avail = mi.get("MemAvailable", mi.get("MemFree", 0))
    st, sf = mi.get("SwapTotal", 0), mi.get("SwapFree", 0)
    return {"total": total, "used": total - avail, "pct": 100.0 * (total - avail) / total if total else None,
            "swap_total": st, "swap_used": st - sf}


def hwmons():
    """[(dir, name)] of /sys/class/hwmon/*."""
    out = []
    for d in sorted(glob.glob(P("/sys/class/hwmon/hwmon*")), key=lambda x: int(re.sub(r"\D", "", os.path.basename(x)) or 0)):
        out.append((d, read(os.path.join(d, "name"), "") or ""))
    return out


def temps_of(d):
    """{label: °C} of one hwmon dir."""
    out = {}
    for f in sorted(glob.glob(os.path.join(d, "temp*_input"))):
        n = re.match(r"temp(\d+)_input", os.path.basename(f)).group(1)
        v = read_int(f)
        if v is None or v <= -40000 or v >= 150000:
            continue
        label = read(os.path.join(d, "temp%s_label" % n)) or "temp%s" % n
        out[label] = v / 1000.0
    return out


def cpu_temp():
    """The hottest core of coretemp (k10temp & co. on other CPUs); None when there is none."""
    vals = []
    for d, name in hwmons():
        if name in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
            vals += list(temps_of(d).values())
    return max(vals) if vals else None


def disk_temps():
    """{sda: °C} from drivetemp."""
    out = {}
    for d, name in hwmons():
        if name != "drivetemp":
            continue
        blocks = glob.glob(os.path.join(d, "device", "block", "*"))
        dev = os.path.basename(blocks[0]) if blocks else os.path.basename(d)
        t = temps_of(d)
        if t:
            out[dev] = max(t.values())
    return out


def other_temps():
    """acpitz, nouveau and the like: {name: °C}."""
    out = {}
    for d, name in hwmons():
        if name in ("coretemp", "k10temp", "drivetemp", "zenpower"):
            continue
        t = temps_of(d)
        if t:
            out[name] = max(t.values())
    return out


REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "f2fs", "zfs", "jfs"}


def disks():
    """[{mount, dev, fs, total, used, pct}] of real filesystems."""
    out, seen = [], set()
    for line in (read(P("/proc/mounts"), "") or "").splitlines():
        p = line.split()
        if len(p) < 3 or p[2] not in REAL_FS or p[0] in seen:
            continue
        mnt = p[1].replace("\\040", " ")
        if mnt.startswith(("/boot/efi", "/snap/")):
            continue
        try:
            st = os.statvfs(mnt)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        if total <= 0:
            continue
        free = st.f_bavail * st.f_frsize
        used = total - st.f_bfree * st.f_frsize
        seen.add(p[0])
        out.append({"mount": mnt, "dev": p[0], "fs": p[2], "total": total, "used": used,
                    "pct": 100.0 * used / (used + free) if used + free else 0.0})
    return out


# ---------------------------------------------------------------- services

def unit_name(name):
    return name if "." in name else name + ".service"


def unit_info(name):
    r = sh(["systemctl", "show", unit_name(name), "-p", "LoadState,ActiveState,SubState,ActiveEnterTimestamp,Description"])
    info = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition("=")
        info[k] = v
    return {"name": name, "kind": "unit", "load": info.get("LoadState", "not-found"),
            "state": info.get("ActiveState", "unknown"), "sub": info.get("SubState", ""),
            "since": info.get("ActiveEnterTimestamp", ""), "desc": info.get("Description", "")}


def containers():
    """{name: {state, status}} of every Docker container (empty without Docker)."""
    if not (os.path.exists("/usr/bin/docker") or os.path.exists("/usr/local/bin/docker")):
        return {}
    r = sh(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"], timeout=20)
    out = {}
    for line in r.stdout.splitlines():
        p = line.split("\t")
        if len(p) >= 3:
            out[p[0]] = {"state": p[1], "status": p[2]}
    return out


def services(conf):
    """The units and containers of the services menu that exist on this server."""
    out = []
    for u in conf.get("services") or []:
        i = unit_info(u)
        if i["load"] != "not-found":
            out.append(i)
    cs = containers()
    for pat in conf.get("containers") or []:
        for name in sorted(cs):
            if fnmatch.fnmatch(name, pat) and not any(x["name"] == name for x in out):
                out.append({"name": name, "kind": "container", "state": "active" if cs[name]["state"] == "running"
                            else ("failed" if cs[name]["state"] in ("exited", "dead") else cs[name]["state"]),
                            "sub": cs[name]["state"], "since": cs[name]["status"], "desc": "Docker"})
    return out


def known_service(conf, name):
    if name in (conf.get("services") or []):
        return "unit"
    for pat in conf.get("containers") or []:
        if fnmatch.fnmatch(name, pat):
            return "container"
    return None


def logs(kind, name, lines=30):
    if kind == "container":
        r = sh(["docker", "logs", "--tail", str(lines), name], timeout=30)
        return (r.stdout + r.stderr).strip()
    r = sh(["journalctl", "-u", unit_name(name), "-n", str(lines), "--no-pager", "-o", "short-iso"], timeout=30)
    return (r.stdout or r.stderr).strip()


def restart(kind, name):
    if kind == "container":
        return sh(["docker", "restart", name], timeout=180)
    if name in ("eris-bot", "eris-bot.service"):
        return sh(["systemctl", "restart", "--no-block", "eris-bot.service"], timeout=30)
    return sh(["systemctl", "restart", unit_name(name)], timeout=180)


def start(kind, name):
    if kind == "container":
        return sh(["docker", "start", name], timeout=120)
    return sh(["systemctl", "start", unit_name(name)], timeout=120)


def reboot():
    return sh(["systemctl", "reboot"], timeout=30)


def poweroff():
    return sh(["systemctl", "poweroff"], timeout=30)
