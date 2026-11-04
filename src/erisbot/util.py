"""Small helpers: logging without secrets, Persian number formatting, sizes and times."""
import html
import re
import sys
import threading
import time

_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

# never in a log line: bot tokens, passwords, subscription links, bearer tokens
_SECRETS = [
    (re.compile(r"\b\d{5,15}:[A-Za-z0-9_-]{30,64}\b"), "<token>"),
    (re.compile(r"(/bot)[^/\s]+"), r"\1<token>"),
    (re.compile(r"(?i)(password|passwd|pass|secret|token|authorization)([\"']?\s*[:=]\s*[\"']?)[^\s\"',}&]+"),
     r"\1\2<hidden>"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]+"), "Bearer <hidden>"),
    (re.compile(r"https?://\S*/sub/\S+"), "<subscription link>"),
    (re.compile(r"\b(vless|vmess|trojan|ss)://\S+"), r"\1://<hidden>"),
    (re.compile(r"(socks5h?|http)://[^:@/\s]+:[^@/\s]+@"), r"\1://<user>:<hidden>@"),
]
_log_lock = threading.Lock()
DEBUG = False


def redact(text):
    s = str(text)
    for rx, rep in _SECRETS:
        s = rx.sub(rep, s)
    return s


def log(msg, *args):
    if args:
        try:
            msg = msg % args
        except (TypeError, ValueError):
            msg = "%s %r" % (msg, args)
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), redact(msg))
    with _log_lock:
        try:
            sys.stderr.write(line)
            sys.stderr.flush()
        except (OSError, ValueError):
            pass


def debug(msg, *args):
    if DEBUG:
        log(msg, *args)


def fa(x):
    """Persian digits."""
    return str(x).translate(_FA)


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=False)


def human(n, digits=1):
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024 or unit == "PB":
            if unit == "B":
                return "%d B" % n
            return ("%." + str(digits) + "f %s") % (n, unit)
        n /= 1024.0
    return "—"


def gb(n):
    return human(n, 2) if n else "0"


def duration(sec, parts=2):
    """۳ روز و ۴ ساعت"""
    sec = int(max(0, sec))
    units = [(86400, "روز"), (3600, "ساعت"), (60, "دقیقه"), (1, "ثانیه")]
    out = []
    for size, name in units:
        if sec >= size or (size == 1 and not out):
            v, sec = divmod(sec, size)
            if v or (size == 1 and not out):
                out.append("%s %s" % (fa(v), name))
        if len(out) >= parts:
            break
    return " و ".join(out)


def ago(ts, now=None):
    """«۲ ساعت پیش»"""
    if not ts:
        return "هرگز"
    d = (now or time.time()) - ts
    if d < 0:
        return "در " + duration(-d, 1) + " آینده"
    if d < 60:
        return "همین الان"
    return duration(d, 1) + " پیش"


def until(ts, now=None):
    if not ts:
        return "نامحدود"
    d = ts - (now or time.time())
    if d <= 0:
        return "گذشته (" + duration(-d, 1) + " پیش)"
    return duration(d, 2) + " مانده"


def bar(pct, width=12):
    pct = max(0.0, min(100.0, float(pct or 0)))
    full = int(round(width * pct / 100.0))
    return "█" * full + "░" * (width - full)


def spark(values, lo=None, hi=None):
    vals = [v for v in values if v is not None]
    if not vals:
        return ""
    lo = min(vals) if lo is None else lo
    hi = max(vals) if hi is None else hi
    chars = "▁▂▃▄▅▆▇█"
    span = (hi - lo) or 1
    return "".join(" " if v is None else chars[max(0, min(7, int((v - lo) / span * 7.999)))] for v in values)


def pct(a, b):
    return 100.0 * a / b if b else 0.0


def clip(text, limit=4000):
    if len(text) <= limit:
        return text
    return text[:limit - 20] + "\n…(بریده شد)"


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(s):
    return ANSI.sub("", s or "")


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return default


def read_int(path, default=None):
    v = read(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def write(path, value):
    with open(path, "w") as f:
        f.write(str(value))
