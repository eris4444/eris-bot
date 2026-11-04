"""Paths and settings of eris-bot.

ERISBOT_ROOT moves every file path under another folder (tests); ERISBOT_SYS and ERISBOT_PROC point
at a fake /sys and /proc. On a real server none of them is set.
"""
import copy
import json
import os
import re
import threading

ROOT = os.environ.get("ERISBOT_ROOT", "").rstrip("/")


def R(path):
    """A path of the server, under ERISBOT_ROOT when that is set."""
    return ROOT + path if ROOT and path.startswith("/") else path


ETC = R("/etc/eris-bot")
CONFIG = os.path.join(ETC, "config.json")
FANS = os.path.join(ETC, "fans.json")
PROFILE = os.path.join(ETC, "profile.json")
STATE = R("/var/lib/eris-bot")
RUN = R("/run/eris-bot")
TMP = R("/var/tmp/eris-bot")
LIB = R("/usr/local/lib/eris-bot")
SYS = os.environ.get("ERISBOT_SYS", "/sys").rstrip("/")
PROC = os.environ.get("ERISBOT_PROC", "/proc").rstrip("/")
ERIS_BIN = os.environ.get("ERISBOT_ERIS", R("/usr/local/bin/eris"))
ERIS_LIB = os.environ.get("ERISBOT_ERIS_LIB", R("/usr/local/lib/eris"))
MARZBAN_ENV = R("/opt/marzban/.env")
CERTS = R("/etc/xray-site/certs")
MODULES_LOAD = R("/etc/modules-load.d/eris-bot-fans.conf")
MODPROBE_D = R("/etc/modprobe.d/eris-bot-fans.conf")
GRUB = R("/etc/default/grub")
SENSORS_DETECT = os.environ.get("ERISBOT_SENSORS_DETECT", "/usr/sbin/sensors-detect")

PARTS = ("marzban", "x-ui", "xray-site", "jellyfin", "eris", "claude-f-web", "system")

DEFAULTS = {
    "token": "",
    "allowed_chat_ids": [],
    "roles": {},                       # {"<chat id>": "owner" | "viewer"}; missing = owner
    "api_base": "https://api.telegram.org",
    "proxy": "auto",                   # off | auto | http://... | socks5://...
    "proxy_list": ["http://127.0.0.1:1090", "socks5://127.0.0.1:1090", "http://127.0.0.1:1091"],
    "button_colors": "auto",           # auto | off
    "backup_password_file": "/etc/eris-bot/backup.pass",
    "backup_send": {"mode": "after_nightly", "at": "04:30", "parts": ["all"]},
    "part_size_mb": 45,
    "upload": {"write_timeout": 60, "read_timeout": 180, "retries": 8},
    "daily_report": {"enabled": False, "at": "09:00"},
    "alerts": {"user_days": 3, "user_percent": 10, "cert_days": 10, "cpu_fan_temp": 75, "gpu_fan_temp": 80},
    "public_ip_urls": ["https://api.ipify.org", "https://ipv4.icanhazip.com", "https://checkip.amazonaws.com"],
    "public_ip": {"static": ""},       # the server's static IP: alert only when the agreed IP is another one
    "mz_presets": {"gb": [10, 20, 50, 100, 200], "days": [7, 30, 60, 90, 180]},
    "ai": {"timeout": 900, "claude_models": ["opus", "sonnet", "haiku"],
           "efforts": ["low", "medium", "high", "xhigh", "max"]},
    "nas": {"session_minutes": 30, "max_upload_mb": 20, "send_piece_mb": 45},
    "weather": {"city": "", "lat": None, "lon": None, "daily": False, "at": "07:30"},
    "rate_limit": {"events": 20, "seconds": 10},
    "services": ["ssh", "x-ui", "haproxy", "nginx", "xray-site", "smbd", "docker", "eris-guard", "eris-dashboard",
                 "claude-f-web", "smartmontools", "eris-bot", "eris-fan"],
    "containers": ["marzban-marzban-1", "marzban-node-*", "jellyfin"],
}

TOKEN_RX = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,64}$")
PROXY_RX = re.compile(r"^(http|socks5|socks5h)://(?:[^:@/\s]+(?::[^@/\s]*)?@)?[A-Za-z0-9.\-\[\]:]+:\d{1,5}/?$")
HHMM_RX = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_lock = threading.RLock()


def merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path=None):
    path = path or CONFIG
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    conf = merge(DEFAULTS, data)
    conf["allowed_chat_ids"] = [int(x) for x in conf.get("allowed_chat_ids") or [] if str(x).lstrip("-").isdigit()]
    return conf


def write_json(path, data, mode=0o600):
    """Atomic write; the file never exists with wider permissions than `mode`."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
            f.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return copy.deepcopy(default)


def save(updates, path=None):
    """Change some settings (a dict of top-level keys); config.json stays mode 600."""
    path = path or CONFIG
    with _lock:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        data.update(updates)
        write_json(path, data, 0o600)
    return load(path)


def validate_proxy(value):
    return value in ("off", "auto") or bool(PROXY_RX.match(value or ""))


def role(conf, chat_id):
    if int(chat_id) not in conf["allowed_chat_ids"]:
        return None
    r = (conf.get("roles") or {}).get(str(chat_id), "owner")
    return r if r in ("owner", "viewer") else "owner"


def send_parts(conf):
    want = (conf.get("backup_send") or {}).get("parts") or ["all"]
    if "all" in want:
        return list(PARTS)
    return [p for p in PARTS if p in want]
