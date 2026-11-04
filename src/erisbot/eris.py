"""ERIS Console on this server: its commands (eris backup ... --json, eris fw ...) and its alerts()."""
import json
import os
import re
import sys
import threading
import time

from . import config, system
from .util import log, strip_ansi


class ErisError(Exception):
    pass


def available():
    return os.access(config.ERIS_BIN, os.X_OK)


def run(args, timeout=600):
    r = system.sh([config.ERIS_BIN] + list(args), timeout=timeout)
    r.stdout, r.stderr = strip_ansi(r.stdout), strip_ansi(r.stderr)
    return r


def run_json(args, timeout=600):
    r = run(list(args) + ["--json"], timeout)
    try:
        return json.loads(r.stdout), r
    except ValueError:
        msg = (r.stdout + r.stderr).strip().splitlines()
        raise ErisError(msg[-1] if msg else "eris %s: exit %d" % (" ".join(args), r.returncode))


def version():
    r = run(["version"], 20)
    return r.stdout.strip() if r.returncode == 0 else None


# ---------------------------------------------------------------- backups (ERIS 2.5)

def backup_status():
    return run_json(["backup", "status"], 60)[0]


def backup_list(part=None):
    args = ["backup", "list"] + (["--part", part] if part and part != "full" else ["--full"] if part == "full" else [])
    return run_json(args, 60)[0]


def backup_now(parts, timeout=7200):
    """{"ok", "results": [...]} of eris backup now --part a,b."""
    data, r = run_json(["backup", "now", "--part", ",".join(parts)], timeout)
    return data


def verify(path):
    return run_json(["backup", "verify", path], 600)[0]


def encrypt(path, password_file, out):
    r = run(["backup", "encrypt", path, "--password-file", password_file, "--out", out], 3600)
    if r.returncode != 0:
        raise ErisError((r.stdout + r.stderr).strip() or "eris backup encrypt failed")
    return r.stdout.strip() or out


def restore(part, path):
    return run(["restore", "--part", part, path, "--yes"], 3600)


# ---------------------------------------------------------------- firewall

def fw_status():
    r = run(["fw"], 60)
    return (r.stdout + r.stderr).strip()


def fw(args, timeout=120):
    r = run(["fw"] + list(args), timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def fw_set_up(text):
    return "not set up" not in text and "not managed" not in text


OPEN_RX = re.compile(r"^\s*(\d+)\)\s+(\d+)/(tcp|udp)\s+(.*)$")
RULE_RX = re.compile(r"^\s*(\d+)\)\s+(allow|deny)\s+(\S+)")
LAN_RX = re.compile(r"^\s*(\d+)\)\s+([0-9a-fA-F.:]+/\d+)\s*(.*)$")


def fw_parse(text):
    """{"opens": [(n, port, proto, rest)], "rules": [(n, kind, net)], "lans": [(n, net)], "pending": bool, "on": bool}"""
    out = {"opens": [], "rules": [], "lans": [], "pending": "PENDING" in text, "on": " ON " in text.split("\n", 1)[0] + " "}
    for line in text.splitlines():
        m = OPEN_RX.match(line)
        if m:
            out["opens"].append((int(m.group(1)), int(m.group(2)), m.group(3), m.group(4).strip()))
            continue
        m = RULE_RX.match(line)
        if m:
            out["rules"].append((int(m.group(1)), m.group(2), m.group(3)))
            continue
        m = LAN_RX.match(line)
        if m:
            out["lans"].append((int(m.group(1)), m.group(2)))
    return out


# ---------------------------------------------------------------- alerts() of the dashboard

class Alerts:
    """Runs the parts of the ERIS dashboard that alerts() needs (CPU, disks/SMART, services) and asks
    erisd.alerts() what is wrong. Marzban is left out (d["mz"] = None): the bot watches it itself."""

    def __init__(self):
        self.mod = None
        self.parts = None
        self.error = None
        self.lock = threading.Lock()

    def _load(self):
        if self.mod is not None or not available():
            return self.mod
        try:
            if config.ERIS_LIB not in sys.path:
                sys.path.insert(0, config.ERIS_LIB)
            import erisd  # noqa: F401  (ERIS Console's dashboard module)
            import erislib
            self.mod, self.lib = erisd, erislib
            self.parts = {"cpu": erisd.Cpu(), "storage": erisd.Storage(), "services": erisd.Services()}
            self.parts["storage"].start()
            self.parts["services"].start()
        except Exception as e:  # an older or broken ERIS: the bot works without its alerts
            self.error = "%s: %s" % (type(e).__name__, e)
            log("ERIS alerts unavailable: %s", self.error)
            self.mod = False
        return self.mod

    def current(self):
        """[(level, text)] or [] when ERIS is not here."""
        with self.lock:
            mod = self._load()
            if not mod:
                return []
            try:
                conf = self.lib.load_conf()
                d = {"cpu": self.parts["cpu"].sample(), "mem": mod.mem_sample(), "storage": self.parts["storage"].snap,
                     "svc": self.parts["services"].snap, "mz": None, "backup": mod.read_backup_state()}
                if conf.get("ALERTS", "on") != "on":
                    return []
                return list(mod.alerts(d, conf))
            except Exception as e:
                log("ERIS alerts() failed: %s", e)
                return []


# English alert texts of ERIS -> Persian
TRANSLATE = [
    (re.compile(r"^CPU (\d+)C$"), "🌡 دمای CPU بالاست: {0}°C"),
    (re.compile(r"^CPU throttling$"), "🐢 CPU به خاطر گرما کند شده (throttling)"),
    (re.compile(r"^(\S+) (\d+)C$"), "🌡 دمای دیسک {0} بالاست: {1}°C"),
    (re.compile(r"^(\S+) SMART FAILED$"), "💥 SMART دیسک {0} خرابی گزارش می‌کند"),
    (re.compile(r"^(\S+) SMART unreadable$"), "❓ SMART دیسک {0} خوانده نمی‌شود"),
    (re.compile(r"^(\S+) (\d+)% full$"), "💽 {0} پر است: {1}٪"),
    (re.compile(r"^backup failed$"), "💾 بکاپ کلی ناموفق بود"),
    (re.compile(r"^backup (\S+) failed$"), "💾 بکاپ بخش {0} ناموفق بود"),
    (re.compile(r"^(\S+) failed$"), "🛑 سرویس {0} از کار افتاده (failed)"),
    (re.compile(r"^memory full$"), "🧠 رم و swap پر است"),
    (re.compile(r"^no backup yet$"), "💾 هنوز بکاپی گرفته نشده"),
    (re.compile(r"^backup (\d+)d old$"), "💾 آخرین بکاپ {0} روز پیش است"),
    (re.compile(r"^backup folder missing$"), "💾 پوشه‌ی بکاپ نیست (فلش وصل است؟)"),
]


def translate(text):
    for rx, fmt in TRANSLATE:
        m = rx.match(text)
        if m:
            return fmt.format(*m.groups())
    return text


def alert_key(text):
    """The same problem keeps its key while its numbers change (CPU 81C -> CPU 83C)."""
    return "eris:" + re.sub(r"\d+(\.\d+)?", "#", text)


def is_part_backup_alert(text):
    return re.match(r"^backup (?!failed$)\S+ failed$", text) is not None


def now():
    return time.time()
