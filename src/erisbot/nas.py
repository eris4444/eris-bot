"""NAS files for the bot: log in as a user of this server (its own password), browse the shared folders,
upload into them and download from them. Every file operation runs as that user (nasop.py under setpriv),
so it can do exactly what the user can do over SMB, nothing more. Passwords are never stored or logged.
"""
import ctypes
import ctypes.util
import hmac
import json
import os
import pwd
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET

from . import config
from .util import log

SHADOW = config.R("/etc/shadow")
OMV_CONFIG = config.R("/etc/openmediavault/config.xml")
SRV = config.R("/srv")
NASOP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nasop.py")
MAX_FAILS, FAIL_WINDOW = 5, 900

_crypt_lock = threading.Lock()
_libcrypt = None


class NasError(Exception):
    pass


ERRORS_FA = {"permission denied": "اجازه‌ی دسترسی ندارید", "not found": "پیدا نشد", "already exists": "این اسم هست",
             "bad name": "اسم نامعتبر است", "outside the shared folder": "بیرون از پوشه‌ی اشتراکی است",
             "not empty": "پوشه خالی نیست", "not a file": "فایل نیست"}


def crypt(password, setting):
    """crypt(3) from libcrypt (yescrypt, sha512...); Python 3.13 has no crypt module."""
    global _libcrypt
    with _crypt_lock:
        if _libcrypt is None:
            name = ctypes.util.find_library("crypt") or "libcrypt.so.1"
            _libcrypt = ctypes.CDLL(name)
            _libcrypt.crypt.restype = ctypes.c_char_p
            _libcrypt.crypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        r = _libcrypt.crypt(password.encode("utf-8"), setting.encode("utf-8"))
    return r.decode("utf-8", "replace") if r else None


def shadow_hash(user):
    try:
        with open(SHADOW, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split(":")
                if len(parts) > 1 and parts[0] == user:
                    return parts[1]
    except OSError:
        return None
    return None


def account(user):
    """(uid, gid, home) of a normal user of this server (uid 1000-59999), else None."""
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        return None
    if not 1000 <= pw.pw_uid < 60000:
        return None
    return pw.pw_uid, pw.pw_gid, pw.pw_dir


class Auth:
    def __init__(self, verify=None):
        self.fails = {}
        self.lock = threading.Lock()
        self.verify_fn = verify

    def locked(self, chat_id, now=None):
        now = now or time.time()
        with self.lock:
            lst = [t for t in self.fails.get(chat_id, []) if now - t < FAIL_WINDOW]
            self.fails[chat_id] = lst
            return max(0, int(lst[0] + FAIL_WINDOW - now)) if len(lst) >= MAX_FAILS else 0

    def check(self, chat_id, user, password):
        """(uid, gid, home) when the password is right; raises NasError otherwise."""
        if self.locked(chat_id):
            raise NasError("ورودهای اشتباه زیاد بود؛ %s دقیقه‌ی دیگر دوباره امتحان کنید." % (self.locked(chat_id) // 60 + 1))
        acc = account(user) if user.isascii() else None
        ok = False
        if acc:
            if self.verify_fn:
                ok = self.verify_fn(user, password)
            else:
                h = shadow_hash(user)
                if h and h[0] not in "!*" and len(h) > 3:
                    got = crypt(password, h)
                    ok = bool(got) and hmac.compare_digest(got, h)
        if not ok:
            with self.lock:
                self.fails.setdefault(chat_id, []).append(time.time())
            log("nas: failed login for %r from chat %s", user[:32], chat_id)
            raise NasError("نام کاربری یا رمز اشتباه است.")
        with self.lock:
            self.fails.pop(chat_id, None)
        log("nas: %s logged in from chat %s", user, chat_id)
        return acc


def shares():
    """[(name, path)] of the OMV shared folders on data disks (/srv/...), else the folders under /srv/*."""
    out = []
    try:
        root = ET.parse(OMV_CONFIG).getroot()
        mnt = {m.findtext("uuid"): m.findtext("dir") for m in root.iter("mntent")}
        for s in root.iter("sharedfolder"):
            base = mnt.get(s.findtext("mntentref")) or ""
            if not base.startswith("/srv/"):
                continue
            path = os.path.normpath(os.path.join(config.R(base), (s.findtext("reldirpath") or "").strip("/")))
            if os.path.isdir(path):
                out.append((s.findtext("name") or os.path.basename(path), path))
    except (OSError, ET.ParseError):
        pass
    if not out and os.path.isdir(SRV):
        for disk in sorted(os.listdir(SRV)):
            d = os.path.join(SRV, disk)
            if os.path.isdir(d):
                for n in sorted(os.listdir(d)):
                    if os.path.isdir(os.path.join(d, n)) and n != "lost+found":
                        out.append((n, os.path.join(d, n)))
    seen, uniq = set(), []
    for name, path in out:
        if path not in seen:
            seen.add(path)
            uniq.append((name, path))
    return uniq


def share_of(path, lst=None):
    """(name, root) of the shared folder that holds `path`, else None."""
    real = os.path.realpath(path)
    for name, root in lst or shares():
        r = os.path.realpath(root)
        if real == r or real.startswith(r.rstrip("/") + "/"):
            return name, root
    return None


def run_as(sess, root, op, *args, stdin=None, stdout=None, timeout=600):
    """nasop.py as the session's user. Returns the JSON answer (or None when stdout goes to a file)."""
    cmd = [sys.executable, "-I", NASOP, root, op] + list(args)
    if os.geteuid() == 0 and not config.ROOT:
        cmd = ["setpriv", "--reuid=%d" % sess["uid"], "--regid=%d" % sess["gid"], "--init-groups",
               "--no-new-privs", "--inh-caps=-all", "--"] + cmd
    try:
        p = subprocess.run(cmd, stdin=stdin if stdin is not None else subprocess.DEVNULL,
                           stdout=stdout if stdout is not None else subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, cwd="/", env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
    except subprocess.TimeoutExpired:
        raise NasError("کار بیش از حد طول کشید")
    if stdout is not None:
        if p.returncode == 0:
            return None
        stdout.seek(0)                        # the error answer went into the output file
        raw = stdout.read(2000).decode("utf-8", "replace")
    else:
        raw = p.stdout.decode("utf-8", "replace") if isinstance(p.stdout, bytes) else ""
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        data = {"error": (p.stderr.decode("utf-8", "replace").strip().splitlines() or ["failed"])[-1][:200]}
    if p.returncode != 0 or "error" in data:
        e = data.get("error") or "failed"
        raise NasError(ERRORS_FA.get(e, e))
    return data
