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


_EN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫،", "01234567890123456789.,")
_NUM_RX = re.compile(r"^([+-]?)\s*(\d+(?:[.,]\d+)?|[.,]\d+)\s*(.*)$")
UNLIMITED_WORDS = ("نامحدود", "بی‌نهایت", "بینهایت", "بی نهایت", "inf", "unlimited", "∞", "♾")


def fmt_num(x):
    """12 -> ۱۲, 1.5 -> ۱.۵ (no trailing .0)."""
    x = float(x)
    s = ("%d" % x) if x == int(x) else ("%.2f" % x).rstrip("0").rstrip(".")
    return fa(s)


def parse_amount(text, kind, allow_negative=False):
    """A number the user typed, with an optional unit. kind "gb": GB (or MB/TB units); kind "days": days
    (or hour/week/month/year units). Persian/Arabic digits and the Persian decimal point work.
    Returns a float (0 = unlimited) or None when it cannot be read."""
    t = (text or "").strip().translate(_EN).lower().replace("‌", " ")
    if any(w.replace("‌", " ") == t for w in UNLIMITED_WORDS):
        return 0.0
    m = _NUM_RX.match(t)
    if not m:
        return None
    sign, num, unit = m.group(1), m.group(2).replace(",", "."), m.group(3).strip()
    value = float(num)
    if kind == "gb":
        units = {"": 1, "g": 1, "gb": 1, "گیگ": 1, "گیگابایت": 1, "گ": 1,
                 "m": 1 / 1024.0, "mb": 1 / 1024.0, "مگ": 1 / 1024.0, "مگابایت": 1 / 1024.0,
                 "t": 1024, "tb": 1024, "ترا": 1024, "ترابایت": 1024}
    else:
        units = {"": 1, "d": 1, "day": 1, "days": 1, "روز": 1, "h": 1 / 24.0, "hour": 1 / 24.0, "ساعت": 1 / 24.0,
                 "w": 7, "week": 7, "هفته": 7, "month": 30, "ماه": 30, "y": 365, "year": 365, "سال": 365}
    if unit not in units:
        return None
    value *= units[unit]
    if sign == "-":
        if not allow_negative:
            return None
        value = -value
    if abs(value) > (1024 * 1024 if kind == "gb" else 36500):
        return None
    return value


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
