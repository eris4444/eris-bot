#!/bin/bash
# eris-bot installer (self-contained): a Telegram bot for this server (ERIS Console, Marzban, backups, fans).
#
#   bash install-eris-bot.sh                install or upgrade (config.json, the password and the state stay)
#   bash install-eris-bot.sh --uninstall    remove eris-bot (same as eris-bot-uninstall)
#   bash install-eris-bot.sh --no-start     install, but do not start the services now
#   bash install-eris-bot.sh --no-selftest  skip the checks at the end
# After the first install: eris-bot setup
set -euo pipefail
VERSION=1.2.0
LOG=/var/log/eris-bot-install.log
TS=$(date +%Y%m%d-%H%M%S)
BK=/var/backups/eris-bot/$TS
LIB=/usr/local/lib/eris-bot
STAGE=$LIB.new
say() { printf '\033[1;36m[eris-bot]\033[0m %s\n' "$*"; printf '%s [install] %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }
die() { say "ERROR: $*"; exit 1; }

put_file() {  # DEST MODE < content
  local dest=$1 mode=$2
  install -d -m 0755 "$(dirname "$dest")"
  cat > "$dest.new-file"
  chmod "$mode" "$dest.new-file"
  mv -f "$dest.new-file" "$dest"
}

write_uninstaller() {
  put_file "$1" 0755 << 'ERISBOT_EOF_eris_bot_uninstall'
#!/bin/bash
# eris-bot-uninstall: remove eris-bot and put back what it changed.
# The settings and state are kept in /var/backups/eris-bot/uninstall-<time>/.
set -u
[ "$(id -u)" -eq 0 ] || { echo "eris-bot-uninstall: run as root" >&2; exit 1; }
LOG=/var/log/eris-bot-install.log
say() { printf '[eris-bot] %s\n' "$*"; printf '%s [uninstall] %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }
KEEP=/var/backups/eris-bot/uninstall-$(date +%Y%m%d-%H%M%S)

# 1. the CPU settings of the profile back to how they were, then the services (fans back to the BIOS)
if [ -x /usr/local/bin/eris-bot ]; then
  /usr/local/bin/eris-bot profile-reset > /dev/null 2>&1 && say "profile: CPU settings and fans back to the system defaults"
fi
for u in eris-fan.service eris-bot.service; do
  if systemctl cat "$u" > /dev/null 2>&1; then
    timeout 60 systemctl disable --now "$u" > /dev/null 2>&1
    say "stopped and disabled $u"
  fi
done
[ -x /usr/local/bin/eris-bot ] && /usr/local/bin/eris-bot fan-restore > /dev/null 2>&1

# 2. settings and state are kept, then removed
mkdir -p "$KEEP"
chmod 700 "$KEEP"
for p in /etc/eris-bot /var/lib/eris-bot; do
  [ -e "$p" ] && cp -a "$p" "$KEEP/" && rm -rf "$p"
done
say "settings and state kept in $KEEP (config.json holds the bot token: keep it private)"

# 3. files
rm -f /etc/systemd/system/eris-bot.service /etc/systemd/system/eris-fan.service
rm -rf /etc/systemd/system/eris-bot.service.wants
rm -rf /usr/local/lib/eris-bot /run/eris-bot /var/tmp/eris-bot
rm -f /usr/local/bin/eris-bot
for f in /etc/modules-load.d/eris-bot-fans.conf /etc/modprobe.d/eris-bot-fans.conf; do
  [ -e "$f" ] && mv -f "$f" "$KEEP/" && say "fan driver settings moved to $KEEP ($(basename "$f"))"
done
systemctl daemon-reload > /dev/null 2>&1
rm -f /usr/local/sbin/eris-bot-uninstall
say "eris-bot removed"
ERISBOT_EOF_eris_bot_uninstall
}
write_files() {
  put_file "$STAGE/erisbot/__init__.py" 0644 << 'ERISBOT_EOF___init___py'
"""eris-bot: a Telegram bot for one home server (ERIS Console, Marzban, backups, fans).

Python 3 standard library only.
"""
VERSION = "1.2.0"
REPO = "eris4444/eris-bot"
ERISBOT_EOF___init___py
  put_file "$STAGE/erisbot/ai.py" 0644 << 'ERISBOT_EOF_ai_py'
"""Claude Code and Codex from the bot: plan usage (5-hour / weekly windows), their models and efforts,
and chats (one CLI run per message, continued with --resume / exec resume).

The chats run with no tools (Claude: --tools "") and a read-only sandbox (Codex), in an empty folder:
they answer, they do not touch the server. Tokens are read, never refreshed and never logged.
"""
import base64
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.parse

from . import config, net
from .util import log

CLAUDE_DIR = os.environ.get("ERISBOT_CLAUDE_DIR", config.R("/root/.claude"))
CODEX_HOME = os.environ.get("ERISBOT_CODEX_HOME", config.R("/root/.codex"))
AI_ENV = os.environ.get("ERISBOT_AI_ENV", "/usr/local/lib/eris-ai-env.sh")
AI_PROXY_FILE = os.environ.get("ERISBOT_AI_PROXY", config.R("/etc/eris/ai-proxy"))
WORKDIR = os.path.join(config.STATE, "ai-work")
USAGE_URL = os.environ.get("ERISBOT_CLAUDE_USAGE_URL", "https://api.anthropic.com/api/oauth/usage")
EXTRA_PATH = ["/root/.local/bin", "/usr/local/bin", "/usr/bin", "/bin"]

CLAUDE_NAMES = {"opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku"}
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
EFFORT_FA = {"low": "کم", "medium": "متوسط", "high": "زیاد", "xhigh": "خیلی زیاد", "max": "حداکثر",
             "ultra": "اولترا", "minimal": "حداقل", "none": "بدون فکر"}
CLAUDE_WINDOWS = (("five_hour", "۵ ساعته"), ("seven_day", "هفتگی"), ("seven_day_opus", "هفتگی Opus"),
                  ("seven_day_sonnet", "هفتگی Sonnet"))


class AIError(Exception):
    pass


def which(name):
    env = os.environ.get("ERISBOT_" + name.upper())
    if env:
        return env
    return shutil.which(name, path=os.pathsep.join([os.environ.get("PATH", "")] + EXTRA_PATH))


def epoch(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) / (1000.0 if v > 1e11 else 1.0)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_json(path, limit=4 * 1024 * 1024):
    with open(path, "rb") as f:
        raw = f.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("too large")
    return json.loads(raw.decode("utf-8"))


def ai_proxy():
    """The AI proxy (http://...) from /etc/eris/ai-proxy when set, else None (direct)."""
    try:
        with open(AI_PROXY_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return None if line.lower() == "none" else (line if "://" in line else "http://" + line)
    except OSError:
        pass
    return None


# ---------------------------------------------------------------- usage

class Usage:
    """Claude: GET /api/oauth/usage with Claude Code's own token (at most every 5 minutes, longer after
    429). Codex: the newest rate_limits snapshot in its session logs (no network)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.claude = None
        self.claude_at = 0.0
        self.claude_next = 0.0
        self.backoff = 0
        self.codex = None
        self.codex_at = 0.0

    def get(self, force=False):
        with self.lock:
            now = time.time()
            if force and now - self.claude_at > 60:
                self.claude_next = 0
            if now >= self.claude_next:
                try:
                    self.claude = self._claude()
                except Exception as e:  # noqa: BLE001 - keep the last numbers, say what failed
                    self.claude = dict(self.claude or {"windows": []}, error="%s" % type(e).__name__)
                self.claude_at = now
                self.claude_next = now + (300 if not self.backoff else 600 * self.backoff)
            if self.codex is None or force or now - self.codex_at > 60:
                try:
                    self.codex = self._codex()
                except Exception as e:  # noqa: BLE001
                    self.codex = {"windows": [], "error": type(e).__name__}
                self.codex_at = now
            return {"claude": dict(self.claude or {}), "codex": dict(self.codex or {})}

    def _claude(self):
        try:
            data = read_json(os.path.join(CLAUDE_DIR, ".credentials.json"))
        except (OSError, ValueError):
            return {"login": False, "windows": []}
        creds = (data or {}).get("claudeAiOauth") or {}
        token = creds.get("accessToken")
        if not token:
            return {"login": False, "windows": []}
        sub = creds.get("subscriptionType")
        plan = sub.replace("_", " ").title() if isinstance(sub, str) and sub else None
        m = re.search(r"max_(\d+)x", str(creds.get("rateLimitTier") or ""))
        if m and plan and "Max" in plan:
            plan = "Max %sx" % m.group(1)
        r = net.request("GET", USAGE_URL, proxy=ai_proxy(), timeout=15, read_timeout=15, headers={
            "Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.1.112", "Content-Type": "application/json"})
        if r.status == 429:
            self.backoff = min(3, self.backoff + 1)
            return dict(self.claude or {"windows": []}, plan=plan, login=True, error="محدودیت درخواست (۴۲۹)")
        if r.status == 401:
            return {"login": False, "plan": plan, "windows": [], "error": "ورود منقضی شده"}
        if r.status != 200:
            return dict(self.claude or {"windows": []}, plan=plan, login=True, error="HTTP %d" % r.status)
        self.backoff = 0
        body = json.loads(r.body.decode("utf-8") or "{}")
        windows = []
        for key, label in CLAUDE_WINDOWS:
            w = body.get(key)
            if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
                windows.append({"label": label, "pct": float(w["utilization"]), "reset": epoch(w.get("resets_at"))})
        return {"login": True, "plan": plan, "windows": windows, "asof": time.time()}

    def _codex(self):
        auth = os.path.join(CODEX_HOME, "auth.json")
        if not os.path.exists(auth):
            return {"login": False, "windows": []}
        plan = None
        try:
            tok = (read_json(auth).get("tokens") or {}).get("id_token") or ""
            payload = tok.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            plan = ((claims.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type") or "").title() or None
        except (OSError, ValueError, IndexError, AttributeError, TypeError):
            pass
        newest = None
        for root, _, files in os.walk(os.path.join(CODEX_HOME, "sessions")):
            for f in files:
                if f.startswith("rollout-") and f.endswith(".jsonl"):
                    p = os.path.join(root, f)
                    try:
                        mt = os.path.getmtime(p)
                    except OSError:
                        continue
                    if newest is None or mt > newest[0]:
                        newest = (mt, p)
        if not newest:
            return {"login": True, "plan": plan, "windows": [], "note": "هنوز گفتگویی با Codex نشده"}
        with open(newest[1], "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 400 * 1024))
            lines = f.read().decode("utf-8", "replace").splitlines()
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            p = ev.get("payload") if isinstance(ev.get("payload"), dict) else ev
            rl = p.get("rate_limits") or ((p.get("info") or {}).get("rate_limits") if isinstance(p.get("info"), dict) else None)
            if not isinstance(rl, dict):
                continue
            when = epoch(ev.get("timestamp")) or newest[0]
            windows = []
            for key, mins_default in (("primary", 300), ("secondary", 10080)):
                w = rl.get(key)
                if not isinstance(w, dict) or not isinstance(w.get("used_percent"), (int, float)):
                    continue
                mins = w.get("window_minutes") or mins_default
                reset = epoch(w.get("resets_at")) if w.get("resets_at") is not None else (
                    when + float(w["resets_in_seconds"]) if isinstance(w.get("resets_in_seconds"), (int, float)) else None)
                label = "%s ساعته" % int(mins // 60) if mins < 1440 else ("هفتگی" if mins == 10080 else "%s روزه" % int(mins // 1440))
                windows.append({"label": label, "pct": float(w["used_percent"]), "reset": reset})
            if windows:
                return {"login": True, "plan": plan or (rl.get("plan_type") or "").title() or None,
                        "windows": windows, "asof": when}
        return {"login": True, "plan": plan, "windows": [], "note": "در لاگ‌های Codex هنوز عددی نیست"}


# ---------------------------------------------------------------- models

def codex_models():
    """[(slug, name, [efforts], default effort)] that Codex lists, from its own models cache."""
    try:
        d = read_json(os.path.join(CODEX_HOME, "models_cache.json"), 16 * 1024 * 1024)
    except (OSError, ValueError):
        return []
    out = []
    for m in (d.get("models") if isinstance(d, dict) else d) or []:
        if not isinstance(m, dict) or m.get("visibility") not in (None, "list"):
            continue
        slug = m.get("slug") or m.get("id")
        if not slug:
            continue
        efforts = [e.get("effort") if isinstance(e, dict) else e for e in m.get("supported_reasoning_levels") or []]
        out.append((slug, m.get("display_name") or slug, [e for e in efforts if e], m.get("default_reasoning_level")))
    return out


def codex_default_model():
    try:
        with open(os.path.join(CODEX_HOME, "config.toml"), encoding="utf-8") as f:
            for line in f:
                m = re.match(r'\s*model\s*=\s*"([^"]+)"', line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    lst = codex_models()
    return lst[0][0] if lst else ""


def claude_name(model):
    return CLAUDE_NAMES.get(model, model)


# ---------------------------------------------------------------- running one message

class Job:
    def __init__(self, provider):
        self.provider = provider
        self.proc = None
        self.started = time.time()
        self.stopped = False

    def stop(self):
        self.stopped = True
        p = self.proc
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass


def _env():
    env = dict(os.environ)
    env.setdefault("HOME", "/root" if not config.ROOT else os.path.expanduser("~"))
    env["PATH"] = os.pathsep.join([env.get("PATH", "")] + EXTRA_PATH)
    env.setdefault("LANG", "C.UTF-8")
    return env


def _cmd(argv):
    """Through the ERIS AI environment (the proxy Anthropic/OpenAI need from Iran) when it exists."""
    if os.path.isfile(AI_ENV):
        return ["/bin/bash", "-c", '. "$0" >/dev/null 2>&1; exec "$@"', AI_ENV] + argv
    return argv


def _run(job, argv, timeout):
    os.makedirs(WORKDIR, exist_ok=True)
    job.proc = subprocess.Popen(_cmd(argv), cwd=WORKDIR, env=_env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, err = job.proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        job.stop()
        out, err = job.proc.communicate()
        raise AIError("بیش از %d دقیقه طول کشید و متوقف شد" % (timeout // 60))
    if job.stopped:
        raise AIError("متوقف شد")
    return job.proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def ask_claude(job, prompt, model, effort, session_id=None, timeout=900):
    exe = which("claude")
    if not exe:
        raise AIError("دستور claude روی سرور نیست")
    argv = [exe, "-p", prompt, "--output-format", "json", "--tools", "", "--model", model]
    if effort:
        argv += ["--effort", effort]
    if session_id:
        argv += ["--resume", session_id]
    code, out, err = _run(job, argv, timeout)
    data = None
    for line in reversed(out.strip().splitlines()):
        try:
            data = json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(data, dict):
        tail = (err or out).strip().splitlines()[-3:]
        raise AIError("Claude جواب نداد: %s" % " ".join(tail)[:300] if tail else "Claude جواب نداد (کد %d)" % code)
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        msg = data.get("result") or data.get("subtype") or "error"
        if session_id and "No conversation found" in str(msg):
            return ask_claude(job, prompt, model, effort, None, timeout)
        raise AIError("Claude: %s" % str(msg)[:400])
    u = data.get("usage") or {}
    return {"text": data.get("result") or "", "session": data.get("session_id") or session_id,
            "cost": data.get("total_cost_usd"), "seconds": (data.get("duration_ms") or 0) / 1000.0,
            "tokens": (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
            + (u.get("cache_creation_input_tokens") or 0), "out_tokens": u.get("output_tokens") or 0}


def ask_codex(job, prompt, model, effort, thread_id=None, timeout=900):
    exe = which("codex")
    if not exe:
        raise AIError("دستور codex روی سرور نیست")
    argv = [exe, "exec"]
    if thread_id:
        argv += ["resume"]
    argv += ["--skip-git-repo-check", "--json", "-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"']
    if model:
        argv += ["-m", model]
    if effort:
        argv += ["-c", 'model_reasoning_effort="%s"' % effort]
    if thread_id:
        argv += [thread_id]
    argv += [prompt]
    t0 = time.time()
    code, out, err = _run(job, argv, timeout)
    tid, texts, error, usage = thread_id, [], None, {}
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t = ev.get("type")
        if t == "thread.started":
            tid = ev.get("thread_id") or tid
        elif t == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
            texts.append(ev["item"].get("text") or "")
        elif t == "turn.completed":
            usage = ev.get("usage") or {}
        elif t in ("error", "turn.failed"):
            error = ev.get("message") or (ev.get("error") or {}).get("message") or error
    if not texts:
        raise AIError("Codex: %s" % (error or (err.strip().splitlines() or ["کد %d" % code])[-1])[:400])
    return {"text": "\n\n".join(texts), "session": tid, "cost": None, "seconds": time.time() - t0,
            "tokens": usage.get("input_tokens") or 0, "out_tokens": usage.get("output_tokens") or 0}


# ---------------------------------------------------------------- Markdown -> Telegram HTML

def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline(text):
    parts = re.split(r"(`[^`\n]+`)", text)
    out = []
    for p in parts:
        if len(p) >= 2 and p.startswith("`") and p.endswith("`"):
            out.append("<code>%s</code>" % _esc(p[1:-1]))
            continue
        s = _esc(p)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", s)
        s = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)",
                   lambda m: '<a href="%s">%s</a>' % (m.group(2).replace('"', "%22"), m.group(1)), s)
        out.append(s)
    return "".join(out)


def _fit(raw, render, limit):
    """Rendered pieces of `raw`, each <= limit AFTER rendering (escaping makes text longer): whole lines
    while they fit, a too-long line cut by characters."""
    if len(render(raw)) <= limit:
        return [render(raw)]
    out, cur = [], ""
    for line in raw.split("\n"):
        cand = (cur + "\n" + line) if cur else line
        if len(render(cand)) <= limit:
            cur = cand
            continue
        if cur:
            out.append(render(cur))
        cur = line
        while len(render(cur)) > limit:                 # one line longer than a message
            n = len(cur)
            while n > 1 and len(render(cur[:n])) > limit:
                n = n * 3 // 4
            out.append(render(cur[:n]))
            cur = cur[n:]
    if cur:
        out.append(render(cur))
    return out


def to_messages(md, limit=3800):
    """Telegram HTML messages (each <= limit) from a Markdown answer; code blocks become <pre>."""
    blocks = []
    for i, part in enumerate(re.split(r"```[^\n`]*\n?", md or "")):
        if i % 2:
            blocks.append(("code", part.rstrip("\n")))
        else:
            for para in re.split(r"\n{2,}", part):
                if para.strip():
                    blocks.append(("text", para.strip("\n")))
    pieces = []
    for kind, raw in blocks:
        render = (lambda s: "<pre>%s</pre>" % _esc(s)) if kind == "code" else _inline
        pieces += _fit(raw, render, limit)
    msgs, cur = [], ""
    for p in pieces:
        if cur and len(cur) + 2 + len(p) > limit:
            msgs.append(cur)
            cur = p
        else:
            cur = (cur + "\n\n" + p) if cur else p
    if cur:
        msgs.append(cur)
    return msgs or ["(جواب خالی)"]


def proxy_note():
    p = ai_proxy()
    if not p:
        return "مستقیم"
    try:
        u = urllib.parse.urlsplit(p)
        return "%s:%s" % (u.hostname, u.port)
    except ValueError:
        return "پروکسی"


def log_run(provider, model, effort, res):
    log("ai %s %s/%s: %.0f s, %s in, %s out tokens", provider, model, effort or "-", res.get("seconds") or 0,
        res.get("tokens"), res.get("out_tokens"))
ERISBOT_EOF_ai_py
  put_file "$STAGE/erisbot/alerts.py" 0644 << 'ERISBOT_EOF_alerts_py'
"""Alerts: conditions (on while something is wrong) and events (one-off), sent only when they change.

A condition is reported when it starts and again when it is over ("رفع شد"). A change of the same
alert within 10 minutes of the last message about it is held back and reported when the 10 minutes
are over (if it is still different), so a flapping problem cannot flood the chat.
"""
import calendar
import glob
import ipaddress
import os
import re
import time

from . import config, eris, fand, marzban, net, system
from .store import JsonState
from .util import esc, fa, log

TYPES = {
    "eris": "🖥 سلامت سرور (دما، دیسک، SMART، سرویس‌ها)",
    "user": "👤 کاربرهای مرزبان (انقضا، حجم)",
    "node": "🛰 نودها و پنل مرزبان",
    "reboot": "🔁 ری‌استارت سرور",
    "ssh": "🔑 ورود SSH از IP جدید",
    "cert": "📜 انقضای گواهی‌ها",
    "ip": "🌐 تغییر IP عمومی",
    "backup": "💾 بکاپ و ارسال",
    "fan": "🌀 فن و دما",
}
DEBOUNCE = 600
LEVEL_ICON = {2: "🔴", 1: "🟠"}


class Engine:
    def __init__(self, path=None, clock=time.time, debounce=DEBOUNCE):
        self.st = JsonState(path or os.path.join(config.STATE, "alerts.json"),
                            {"active": {}, "notified": {}, "history": [], "enabled": {}, "mute_until": 0, "events": {}})
        self.clock = clock
        self.debounce = debounce

    # ------------------------------------------------------------ settings
    def enabled(self, typ):
        return (self.st.get("enabled") or {}).get(typ, True)

    def set_enabled(self, typ, on):
        e = self.st.get("enabled") or {}
        e[typ] = bool(on)
        self.st.set("enabled", e)

    def muted(self, now=None):
        return (self.st.get("mute_until") or 0) > (now or self.clock())

    def mute(self, until):
        self.st.set("mute_until", int(until))

    def active(self):
        return self.st.get("active") or {}

    def history(self, n=20):
        return (self.st.get("history") or [])[-n:]

    # ------------------------------------------------------------ the core
    def update(self, conditions, events=(), now=None):
        """conditions: {key: (type, level, text[, resolved text])}; events: [(type, key, level, text)].
        Returns the lines to send now (one message)."""
        now = now or self.clock()
        with self.st.lock:
            old = self.st.get("active") or {}
            notified = self.st.get("notified") or {}
            hist = self.st.get("history") or []
            seen_events = self.st.get("events") or {}
            muted = self.muted(now)
            active = {}
            for k, c in conditions.items():
                typ, level, text = c[0], c[1], c[2]
                active[k] = {"type": typ, "level": level, "text": text, "ok_text": c[3] if len(c) > 3 else None,
                             "since": (old.get(k) or {}).get("since", now)}
            lines = []
            for k in sorted(set(active) | {k for k, n in notified.items() if n.get("state") == "on"}):
                on = k in active
                n = notified.get(k)
                last = n["state"] if n else "off"
                if (on and last == "on") or (not on and last == "off"):
                    continue
                if n and now - n["ts"] < self.debounce:
                    continue                                   # held back; looked at again next round
                info = active.get(k) or n
                typ = info["type"]
                if not self.enabled(typ):
                    notified[k] = {"state": "on" if on else "off", "ts": now, "type": typ, "text": info["text"],
                                   "ok_text": info.get("ok_text")}
                    continue
                if muted:
                    continue
                if on:
                    lines.append("%s %s" % (LEVEL_ICON.get(info["level"], "🟠"), info["text"]))
                    hist.append({"ts": now, "kind": "on", "level": info["level"], "type": typ, "text": info["text"]})
                else:
                    t = n.get("ok_text") or ("✅ رفع شد: " + n["text"])
                    lines.append(t)
                    hist.append({"ts": now, "kind": "off", "level": 0, "type": typ, "text": t})
                notified[k] = {"state": "on" if on else "off", "ts": now, "type": typ, "text": info["text"],
                               "ok_text": info.get("ok_text")}
            for typ, key, level, text in events:
                if now - seen_events.get(key, 0) < self.debounce:
                    continue
                seen_events[key] = now
                hist.append({"ts": now, "kind": "event", "level": level, "type": typ, "text": text})
                if self.enabled(typ) and not muted:
                    lines.append("%s %s" % (LEVEL_ICON.get(level, "🔵") if level else "🔵", text))
            for k in [k for k, n in notified.items() if n["state"] == "off" and now - n["ts"] > 86400]:
                del notified[k]
            for k in [k for k, t in seen_events.items() if now - t > 7 * 86400]:
                del seen_events[k]
            self.st.update(active=active, notified=notified, history=hist[-200:], events=seen_events)
        return lines


# ---------------------------------------------------------------- sources

class Sources:
    """Everything that is watched. Each check() returns (conditions, events)."""

    def __init__(self, app):
        self.app = app
        self.st = JsonState(os.path.join(config.STATE, "watch.json"), {})
        self.eris_alerts = eris.Alerts()
        self.next = {}

    def due(self, name, every, now):
        if now >= self.next.get(name, 0):
            self.next[name] = now + every
            return True
        return False

    def check(self, now=None):
        now = now or time.time()
        cond, events = {}, []
        for name, fn in (("eris", self.c_eris), ("marzban", self.c_marzban), ("reboot", self.c_reboot),
                         ("ssh", self.c_ssh), ("cert", self.c_certs), ("ip", self.c_ip), ("backup", self.c_backup),
                         ("fan", self.c_fans)):
            try:
                c, e = fn(now)
                cond.update(c)
                events += e
            except Exception as ex:  # one broken source must not stop the others
                log("alerts: %s: %s", name, ex)
                cond.update(self.cache_get(name))
        return cond, events

    def cache_get(self, name):
        return (self.st.get("cache") or {}).get(name) or {}

    def cache_set(self, name, cond):
        c = self.st.get("cache") or {}
        c[name] = cond
        self.st.set("cache", c)
        return cond

    # ERIS dashboard alerts (temperatures, SMART, full disks, failed services, the full backup)
    def c_eris(self, now):
        cond = {}
        for level, text in self.eris_alerts.current():
            if eris.is_part_backup_alert(text):
                continue                                       # the backup source reports parts itself
            cond[eris.alert_key(text)] = ("eris", level, esc(eris.translate(text)))
        return cond, []

    def c_marzban(self, now):
        mz = self.app.marzban
        if not mz.available():
            return {}, []
        if not self.due("marzban", 60, now):
            return self.cache_get("marzban"), []
        cond, events = {}, []
        try:
            nodes = mz.nodes()
            users = mz.all_users()
        except marzban.MarzbanError as e:
            cond["mz_down"] = ("node", 2, "🛰 پنل مرزبان جواب نمی‌دهد: %s" % esc(e), "✅ پنل مرزبان دوباره جواب می‌دهد")
            return self.cache_set("marzban", cond), []
        for n in nodes:
            if n.get("status") not in ("connected", "disabled", "connecting"):
                cond["node:%s" % n.get("name")] = (
                    "node", 2, "🛰 نود <b>%s</b> قطع است: %s %s" % (esc(n.get("name")), esc(n.get("status")),
                                                                 esc((n.get("message") or "")[:120])),
                    "✅ نود <b>%s</b> دوباره وصل شد" % esc(n.get("name")))
        a = self.app.conf.get("alerts") or {}
        days, pct = float(a.get("user_days", 3)), float(a.get("user_percent", 10))
        prev = self.st.get("user_status")
        statuses = {}
        for u in users:
            name, status = u.get("username"), u.get("status")
            statuses[name] = status
            if status == "active":
                exp = u.get("expire")
                if exp and now < exp <= now + days * 86400:
                    cond["user_exp:%s" % name] = ("user", 1, "⏳ کاربر <b>%s</b>: %s تا انقضا" % (
                        esc(name), fa("%.1f روز" % ((exp - now) / 86400.0))), "✅ کاربر <b>%s</b> تمدید شد" % esc(name))
                lim, used = u.get("data_limit"), u.get("used_traffic") or 0
                if lim and (lim - used) <= lim * pct / 100.0:
                    cond["user_data:%s" % name] = ("user", 1, "📦 کاربر <b>%s</b>: %s٪ حجم مانده" % (
                        esc(name), fa("%.0f" % max(0, 100.0 * (lim - used) / lim))), "✅ حجم کاربر <b>%s</b> درست شد" % esc(name))
            if prev is not None and prev.get(name) not in (None, status) and status in ("limited", "expired"):
                what = "حجمش تمام شد (limited)" if status == "limited" else "منقضی شد (expired)"
                events.append(("user", "user_status:%s:%s:%d" % (name, status, int(now // 3600)), 1,
                               "🚫 کاربر <b>%s</b> %s" % (esc(name), what)))
        self.st.set("user_status", statuses)
        return self.cache_set("marzban", cond), events

    def c_reboot(self, now):
        bid = system.boot_id()
        old = self.st.get("boot_id")
        events = []
        if bid and old and bid != old:
            up = system.uptime()
            events.append(("reboot", "reboot:" + bid, 1, "🔁 سرور ری‌استارت شد (بالا آمده از %s پیش)" % (
                fa("%d دقیقه" % (up // 60)) if up < 3600 else fa("%.1f ساعت" % (up / 3600.0)))))
        if bid and bid != old:
            self.st.set("boot_id", bid)
        return {}, events

    SSH_RX = re.compile(r"Accepted (\S+) for (\S+) from (\S+) port \d+")

    def c_ssh(self, now):
        if not self.due("ssh", 60, now):
            return {}, []
        since = self.st.get("ssh_since")
        first = since is None
        start = (now - 30 * 86400) if first else since
        r = system.sh(["journalctl", "-u", "ssh", "-u", "sshd", "--since", "@%d" % int(start), "-o", "cat", "--no-pager"], 60)
        known = set(self.st.get("ssh_known") or [])
        events = []
        for m in self.SSH_RX.finditer(r.stdout or ""):
            method, user, ip = m.group(1), m.group(2), m.group(3)
            if ip in known:
                continue
            known.add(ip)
            if not first:
                events.append(("ssh", "ssh:%s" % ip, 2, "🔑 ورود SSH از IP جدید: <code>%s</code> (کاربر %s، روش %s)" % (
                    esc(ip), esc(user), esc(method))))
        if r.returncode in (0, 1) or first:
            self.st.update(ssh_since=int(now) - 5, ssh_known=sorted(known)[-500:])
        return {}, events

    def c_certs(self, now):
        if not self.due("cert", 3600, now):
            return self.cache_get("cert"), []
        days = float((self.app.conf.get("alerts") or {}).get("cert_days", 10))
        cond = {}
        for pem in sorted(glob.glob(os.path.join(config.CERTS, "*", "fullchain.pem"))):
            domain = os.path.basename(os.path.dirname(pem))
            end = cert_end(pem)
            if end is None:
                continue
            left = (end - now) / 86400.0
            if left < days:
                cond["cert:" + domain] = ("cert", 2 if left < 3 else 1, "📜 گواهی <b>%s</b>: %s" % (
                    esc(domain), "منقضی شده!" if left <= 0 else fa("%.1f روز تا انقضا" % left)),
                    "✅ گواهی <b>%s</b> تمدید شد" % esc(domain))
        return self.cache_set("cert", cond), []

    def c_ip(self, now):
        """The public IP from several services: one answer counts only when at least two services agree
        (one odd answer — a provider's other exit — is ignored), and a change only after two rounds in a
        row. With a static IP set, an alert stays on while the agreed IP is another one."""
        if not self.due("ip", 600, now):
            return self.cache_get("ip"), []
        ip, _ = consensus_ip(public_ip_urls(self.app.conf))
        if not ip:
            return self.cache_get("ip"), []           # no agreement this round: change nothing
        static = static_ip(self.app.conf)
        if ip == (static or self.st.get("public_ip")):
            self.st.update(ip_candidate=None, ip_streak=0)
            if not static:
                return {}, []
            return self.cache_set("ip", {}), []
        streak = (self.st.get("ip_streak") or 0) + 1 if self.st.get("ip_candidate") == ip else 1
        self.st.update(ip_candidate=ip, ip_streak=streak)
        if static:
            if streak < CONFIRM_ROUNDS:
                return self.cache_get("ip"), []
            return self.cache_set("ip", {"ip_static": (
                "ip", 2, "🌐 IP عمومی سرور <code>%s</code> است، نه IP ثابت <code>%s</code>" % (esc(ip), esc(static)),
                "✅ IP عمومی دوباره <code>%s</code> است" % esc(static))}), []
        old = self.st.get("public_ip")
        if not old:
            self.st.update(public_ip=ip, ip_candidate=None, ip_streak=0)
            return {}, []
        if streak < CONFIRM_ROUNDS:
            return {}, []
        self.st.update(public_ip=ip, ip_candidate=None, ip_streak=0)
        return {}, [("ip", "ip:%s" % ip, 1, "🌐 IP عمومی عوض شد: از <code>%s</code> به <code>%s</code>" % (esc(old), esc(ip)))]

    def c_backup(self, now):
        cond = {}
        if eris.available() and self.due("backup", 300, now):
            try:
                s = eris.backup_status()
                for name, p in (s.get("parts") or {}).items():
                    if p.get("enabled") and p.get("present") and p.get("failed"):
                        cond["backup:" + name] = ("backup", 2, "💾 بکاپ بخش <b>%s</b> ناموفق بود: %s" % (
                            esc(name), esc((p.get("last_error") or "")[:200])), "✅ بکاپ بخش <b>%s</b> درست شد" % esc(name))
                self.cache_set("backup", cond)
            except eris.ErisError as e:
                log("alerts: eris backup status: %s", e)
                cond = self.cache_get("backup")
        else:
            cond = dict(self.cache_get("backup"))
        for part, r in (self.app.sender.results() if getattr(self.app, "sender", None) else {}).items():
            if r.get("ok") is False:
                cond["send:" + part] = ("backup", 2, "📤 ارسال بکاپ <b>%s</b> ناموفق بود: %s" % (
                    esc(part), esc((r.get("error") or "")[:200])), "✅ ارسال بکاپ <b>%s</b> انجام شد" % esc(part))
        return cond, []

    def c_fans(self, now):
        s = fand.read_status()
        cond = {}
        if s is None:
            cfg = fand.load_cfg()
            if any(f.get("mode") not in (None, "auto") for f in cfg["fans"].values()):
                cond["fan_service"] = ("fan", 2, "🌀 سرویس eris-fan کار نمی‌کند؛ فن‌ها به حالت BIOS برگشته‌اند",
                                       "✅ سرویس eris-fan دوباره کار می‌کند")
            return cond, []
        for a in s.get("alerts") or []:
            cond[a["key"]] = ("fan", a.get("level", 2), esc(a["text"]))
        return cond, []


def cert_end(pem):
    r = system.sh(["openssl", "x509", "-enddate", "-noout", "-in", pem], 20)
    m = re.search(r"notAfter=(.+)", r.stdout or "")
    if not m:
        return None
    s = re.sub(r"\s+", " ", m.group(1).strip())
    try:
        return calendar.timegm(time.strptime(s, "%b %d %H:%M:%S %Y GMT"))
    except ValueError:
        return None


CONFIRM_ROUNDS = 2
DEAD_IP_URLS = ("https://ifconfig.me/ip",)        # answers 403 to scripts now


def public_ip_urls(conf):
    urls = [u for u in conf.get("public_ip_urls") or [] if u not in DEAD_IP_URLS]
    return urls or list(config.DEFAULTS["public_ip_urls"])


def static_ip(conf):
    s = str((conf.get("public_ip") or {}).get("static") or "").strip()
    try:
        return str(ipaddress.ip_address(s)) if s else ""
    except ValueError:
        return ""


def ask_ip(url):
    """One service's answer (an IP string) or None; direct, never through a proxy."""
    try:
        r = net.request("GET", url, proxy=None, timeout=8, read_timeout=8)
        if r.status != 200:
            return None
        ip = r.body.decode("ascii", "replace").strip()
        return str(ipaddress.ip_address(ip))
    except (OSError, ValueError, Exception):  # noqa: B014 - any failure: this service gave no answer
        return None


def consensus_ip(urls, ask=ask_ip):
    """(the IP at least two services agree on — or the only one when one service is set up — or None,
    [(url, answer)])."""
    answers = [(u, ask(u)) for u in urls]
    counts = {}
    for _, ip in answers:
        if ip:
            counts[ip] = counts.get(ip, 0) + 1
    need = 1 if len(urls) == 1 else 2
    best = max(counts.items(), key=lambda kv: kv[1], default=(None, 0))
    return (best[0] if best[1] >= need else None), answers


def public_ip(urls):
    """The agreed public IP (kept for callers of older versions)."""
    return consensus_ip(urls)[0]
ERISBOT_EOF_alerts_py
  put_file "$STAGE/erisbot/app.py" 0644 << 'ERISBOT_EOF_app_py'
"""The bot: long polling (getUpdates), who may talk to it, screens, confirmations and the background jobs."""
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from . import VERSION, alerts, backup, config, eris, marzban, store, system, tg, ui
from .ui import Btn, Screen
from .util import esc, log

INPUT_TTL = 600
COMMANDS = {"/ai": "ai", "/files": "nas", "/weather": "wx"}


class Action:
    def __init__(self, fn, write=False, steps=0, view=True):
        self.fn, self.write, self.steps, self.view = fn, write, steps, view


class Ctx:
    def __init__(self, app, chat_id, role, msg_id=None, cq_id=None, user=None, confirmed=False):
        self.app, self.chat_id, self.role, self.msg_id = app, chat_id, role, msg_id
        self.cq_id, self.user, self.confirmed = cq_id, user, confirmed
        self.answered = False

    @property
    def owner(self):
        return self.role == "owner"

    def answer(self, text=None, alert=False):
        if self.cq_id and not self.answered:
            self.answered = True
            self.app.bot.answer(self.cq_id, text, alert)

    def show(self, screen):
        return self.app.show(self, screen)

    def send(self, text, markup=None):
        return self.app.bot.send(self.chat_id, text, markup)

    def ask_text(self, kind, prompt, *args, back=None):
        """Wait for the next message of this chat (search, a port, a name, a file, a location...)."""
        self.app.session(self.chat_id)["input"] = {"kind": kind, "args": list(args), "exp": time.time() + INPUT_TTL}
        rows = [[Btn("✖️ لغو", "cancel_input", *(back or ("menu", [])), style=ui.PRIMARY)]]
        return Screen(prompt, rows)

    def session(self):
        return self.app.session(self.chat_id)


class App:
    def __init__(self, sleep=time.sleep, workers=4):
        store.ensure_dirs()
        self.conf_mtime = None
        self._conf = config.load()
        self._stamp_conf()
        self.state = store.JsonState(os.path.join(config.STATE, "bot.json"), {})
        self.bot = tg.Bot(lambda: self.conf, self.state, sleep=sleep)
        self.cbs = store.Callbacks(os.path.join(config.STATE, "callbacks.json"))
        rl = self._conf.get("rate_limit") or {}
        self.rate = store.RateLimit(int(rl.get("events", 20)), float(rl.get("seconds", 10)))
        self.sessions = {}
        self.chat_locks = {}
        self.lock = threading.Lock()
        self.actions = {}
        self.inputs = {}
        self.marzban = marzban.Marzban()
        self.alerts = alerts.Engine()
        self.sources = alerts.Sources(self)
        self.sender = backup.Sender(self)
        self.stress = None
        self.pool = ThreadPoolExecutor(max_workers=workers) if workers else None
        self.stop_evt = threading.Event()
        self.ignored = {}
        self.weather_tick = None
        if self.state.get("styles_refused") and tg.stale_refusal(self.state.get("styles_refused_reason") or ""):
            # 1.1.0 turned colours off when a button was pressed twice ("message is not modified")
            self.state.update(styles_refused=False, styles_refused_reason="")
            log("button colours back on (the old refusal was an unchanged edit, not the API refusing colours)")
        from . import screens
        screens.register(self)

    # ------------------------------------------------------------ settings
    def _stamp_conf(self):
        try:
            self.conf_mtime = os.stat(config.CONFIG).st_mtime_ns
        except OSError:
            self.conf_mtime = None

    @property
    def conf(self):
        try:
            m = os.stat(config.CONFIG).st_mtime_ns
        except OSError:
            m = None
        if m != self.conf_mtime:
            old = self._conf
            self._conf = config.load()
            self.conf_mtime = m
            if old.get("proxy") != self._conf.get("proxy") or old.get("proxy_list") != self._conf.get("proxy_list") \
                    or old.get("api_base") != self._conf.get("api_base") or old.get("token") != self._conf.get("token"):
                self.bot.router.reset()
        return self._conf

    def save_conf(self, **updates):
        self._conf = config.save(updates)
        self._stamp_conf()
        self.bot.router.reset() if {"proxy", "proxy_list", "api_base", "token"} & set(updates) else None
        return self._conf

    # ------------------------------------------------------------ registration
    def action(self, name, write=False, steps=0, view=None):
        def deco(fn):
            self.actions[name] = Action(fn, write, steps, (not write) if view is None else view)
            return fn
        return deco

    def text_input(self, kind, write=True):
        def deco(fn):
            self.inputs[kind] = (fn, write)
            return fn
        return deco

    def session(self, chat_id):
        with self.lock:
            return self.sessions.setdefault(chat_id, {"msg_id": None, "input": None, "screen": ("menu", [])})

    def chat_lock(self, chat_id):
        with self.lock:
            return self.chat_locks.setdefault(chat_id, threading.Lock())

    # ------------------------------------------------------------ rendering
    def make_id(self, chat_id, b):
        if b.once:
            return self.cbs.once(chat_id, b.action, b.args, ttl=b.ttl or 60)
        if b.confirm:
            return self.cbs.nav(chat_id, "_ask", [b.action, b.args, b.confirm, b.steps, 1])
        return self.cbs.nav(chat_id, b.action or "noop", b.args)

    def render(self, chat_id, screen):
        rows = list(screen.rows)
        nav = ui.nav_row(screen.refresh, screen.back)
        if nav:
            rows.append(nav)
        text = screen.text if len(screen.text) <= 4096 else screen.text[:4070] + "\n…"
        return text, ui.markup(rows, lambda b: self.make_id(chat_id, b))

    def show(self, ctx, screen):
        text, markup = self.render(ctx.chat_id, screen)
        if ctx.msg_id:
            try:
                self.bot.edit(ctx.chat_id, ctx.msg_id, text, markup)
                self.session(ctx.chat_id)["msg_id"] = ctx.msg_id
                return ctx.msg_id
            except tg.TelegramError as e:
                if e.code != 400:
                    raise
                log("edit failed (%s), sending a new message", e.description)
        m = self.bot.send(ctx.chat_id, text, markup)
        ctx.msg_id = m["message_id"]
        self.session(ctx.chat_id)["msg_id"] = ctx.msg_id
        return ctx.msg_id

    def notify(self, text, chats=None, markup=None):
        for c in chats or self.conf["allowed_chat_ids"]:
            try:
                self.bot.send(c, text, markup)
            except (tg.TelegramError, OSError) as e:
                log("notify %s: %s", c, e)

    # ------------------------------------------------------------ updates
    def poll_once(self, timeout=25, sync=False):
        params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        off = self.state.get("offset")
        if off:
            params["offset"] = off
        updates = self.bot.call("getUpdates", params, timeout=timeout + 15) or []
        for u in updates:
            self.state.set("offset", u["update_id"] + 1)
            if sync or not self.pool:
                self.process(u)
            else:
                self.pool.submit(self.process, u)
        return len(updates)

    def process(self, u):
        try:
            if "callback_query" in u:
                self.on_callback(u["callback_query"])
            elif "message" in u:
                self.on_message(u["message"])
        except Exception as e:
            log("update %s failed: %r\n%s", u.get("update_id"), e, traceback.format_exc())
        finally:
            self.cbs.save()

    def allowed(self, chat_id, what):
        role = config.role(self.conf, chat_id)
        if role is None:
            if time.time() - self.ignored.get(chat_id, 0) > 3600:
                self.ignored[chat_id] = time.time()
                log("ignored %s from chat %s (not in allowed_chat_ids)", what, chat_id)
            return None
        if not self.rate.allow(chat_id):
            if self.rate.should_warn(chat_id):
                try:
                    self.bot.send(chat_id, "⏳ پیام‌ها زیاد است؛ چند ثانیه صبر کنید.")
                except (tg.TelegramError, OSError):
                    pass
            return None
        return role

    def on_message(self, msg):
        chat_id = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if chat_id is None:
            return
        role = self.allowed(chat_id, "a message")
        if role is None:
            return
        with self.chat_lock(chat_id):
            ctx = Ctx(self, chat_id, role, user=msg.get("from"))
            sess = self.session(chat_id)
            if text in ("/start", "/menu", ui.MENU_TEXT, "/help") or text.startswith("/start "):
                sess["input"] = None
                if text.startswith("/start") or text == "/help":
                    self.bot.send(chat_id, "👋 <b>eris-bot %s</b>\nدکمه‌ی «🏠 منو» همیشه پایین صفحه است." % VERSION,
                                  ui.reply_keyboard())
                return self.run(ctx, "menu", [])
            if text in (ui.DASH_TEXT, "/dashboard"):
                sess["input"] = None
                return self.run(ctx, "dash", [])
            if text in COMMANDS:                   # also ends a chat with Claude/Codex or an upload
                sess["input"] = None
                return self.run(ctx, COMMANDS[text], [])
            inp = sess.get("input")
            if inp and inp["exp"] > time.time():
                fn, write = self.inputs.get(inp["kind"], (None, True))
                if fn is None:
                    sess["input"] = None
                    return None
                if write and not ctx.owner:
                    sess["input"] = None
                    return self.bot.send(chat_id, "🔒 این کار فقط برای مالک است.")
                sess["input"] = None
                res = fn(ctx, text, msg, *inp["args"])
                if isinstance(res, Screen):
                    self.show(ctx, res)
                return res
            from .s_files import incoming_file
            f = incoming_file(msg)
            if f:                                  # a file without a question: offer to save it on the NAS
                return self.run(ctx, "nas_incoming", list(f))
            loc = msg.get("location")
            if isinstance(loc, dict) and "latitude" in loc:
                return self.run(ctx, "wx_pick", ["موقعیت من", loc["latitude"], loc["longitude"]])
            if text and not text.startswith("/") and self.marzban.available():
                return self.run(ctx, "mz_search", [text, 0])
            return self.run(ctx, "menu", [])

    def on_callback(self, cq):
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        if chat_id is None:
            return
        role = self.allowed(chat_id, "a button")
        if role is None:
            return
        with self.chat_lock(chat_id):
            ctx = Ctx(self, chat_id, role, msg_id=msg.get("message_id"), cq_id=cq.get("id"), user=cq.get("from"))
            entry = self.cbs.take(cq.get("data"), chat_id)
            if entry is None:
                log("unknown or expired button from chat %s", chat_id)
                ctx.answer("⌛ این دکمه منقضی شده؛ از «🏠 منو» دوباره باز کنید.", alert=True)
                return None
            action, args = entry
            if action == "noop":
                return ctx.answer()
            if action != "_ask":
                self.session(chat_id)["input"] = None     # a button ends any "send me a ..." question
            res = self.run(ctx, action, args)
            ctx.answer(res.notice if isinstance(res, Screen) else None)
            return res

    def run(self, ctx, name, args, confirmed=False):
        act = self.actions.get(name)
        if act is None:
            ctx.answer("❓ کار ناشناخته")
            return None
        if act.write and not ctx.owner:
            ctx.answer("🔒 این کار فقط برای مالک است؛ شما فقط می‌توانید ببینید.", alert=True)
            return None
        if act.steps and not confirmed:
            log("refused %s from chat %s without confirmation", name, ctx.chat_id)
            ctx.answer("⛔ این کار تأیید لازم دارد.", alert=True)
            return None
        ctx.confirmed = confirmed
        try:
            res = act.fn(ctx, *args)
        except marzban.MarzbanError as e:
            res = Screen("❌ مرزبان: %s" % esc(e), back=("menu", []))
        except eris.ErisError as e:
            res = Screen("❌ ERIS: %s" % esc(e), back=("menu", []))
        except (tg.TelegramError, OSError, ValueError) as e:
            log("action %s: %s", name, e)
            res = Screen("❌ خطا: %s" % esc(e), back=("menu", []))
        if act.view and isinstance(res, Screen):
            self.session(ctx.chat_id)["screen"] = (name, list(args))
        if isinstance(res, Screen):
            self.show(ctx, res)
        return res

    # ------------------------------------------------------------ background
    def alert_round(self, now=None):
        cond, events = self.sources.check(now)
        lines = self.alerts.update(cond, events, now)
        if lines:
            self.notify("🔔 <b>هشدار</b>\n" + "\n".join(lines))
        return lines

    def loop_alerts(self):
        self.stop_evt.wait(20)
        while not self.stop_evt.is_set():
            try:
                self.alert_round()
            except Exception as e:
                log("alerts round failed: %r", e)
            self.stop_evt.wait(60)

    def loop_schedule(self):
        while not self.stop_evt.is_set():
            try:
                self.sender.tick()
                self.daily_report()
                if self.weather_tick:
                    self.weather_tick()
                self.cbs.save()
            except Exception as e:
                log("schedule round failed: %r", e)
            self.stop_evt.wait(30)

    def daily_report(self, now=None):
        dr = self.conf.get("daily_report") or {}
        if not dr.get("enabled"):
            return False
        now = now or time.time()
        at = dr.get("at", "09:00")
        if not config.HHMM_RX.match(at):
            return False
        lt = time.localtime(now)
        day = time.strftime("%Y-%m-%d", lt)
        start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, int(at[:2]), int(at[3:]), 0, 0, 0, -1))
        if not (start <= now < start + 1800) or self.state.get("report_day") == day:
            return False
        self.state.set("report_day", day)
        from . import screens
        self.notify("🗓 <b>گزارش روزانه</b>\n" + screens.dashboard_text(self))
        return True

    def start_background(self):
        if not config.ROOT:      # the fan control runs while the bot runs (eris-fan.service, BindsTo=eris-bot)
            system.sh(["systemctl", "start", "--no-block", "eris-fan.service"], 20)
        for name, fn in (("alerts", self.loop_alerts), ("schedule", self.loop_schedule)):
            threading.Thread(target=fn, name=name, daemon=True).start()
        self.sender.start()
        from . import screens
        screens.after_start(self)

    def run_forever(self):
        log("eris-bot %s starting", VERSION)
        while not self.conf.get("token") or not self.conf.get("allowed_chat_ids"):
            log("not set up yet: run eris-bot setup (checking again every 30 s)")
            if self.stop_evt.wait(30):
                return 0
        try:
            me = self.bot.get_me()
            log("logged in as @%s", me.get("username"))
        except (tg.TelegramError, OSError) as e:
            log("getMe: %s (going on; retries in the poll loop)", e)
        self.start_background()
        delay = 2
        while not self.stop_evt.is_set():
            try:
                self.poll_once()
                delay = 2
            except tg.TelegramError as e:
                if e.code in (401, 404):
                    log("the token is not accepted by Telegram (%s); run eris-bot setup", e)
                    self.stop_evt.wait(60)
                elif e.code == 409:
                    log("another program reads this bot's updates (409); waiting")
                    self.stop_evt.wait(30)
                else:
                    log("getUpdates: %s", e)
                    self.stop_evt.wait(delay)
            except (OSError, tg.NoRoute) as e:
                log("getUpdates: %s", e)
                self.stop_evt.wait(delay)
                delay = min(60, delay * 2)
        return 0
ERISBOT_EOF_app_py
  put_file "$STAGE/erisbot/backup.py" 0644 << 'ERISBOT_EOF_backup_py'
"""Sending the backups of the parts to Telegram (never the full backup).

Each archive is encrypted first (eris backup encrypt, a temporary .enc in /var/tmp/eris-bot, removed
after sending). Bigger than part_size_mb (45 MB) it goes as pieces .part01, .part02 ... cut straight
from the .enc file. Every piece is retried with backoff; what was sent is saved after each piece, so
after a lost connection or a restart the job goes on from the next piece.
"""
import math
import os
import queue
import socket
import threading
import time
import uuid

from . import config, eris, system, tg
from .store import JsonState
from .util import bar, esc, fa, human, log

BACKOFF = [5, 15, 30, 60, 120, 300, 600, 900]


def piece_name(base, i, n):
    return base if n == 1 else "%s.part%02d" % (base, i + 1)


def caption(part, res, enc_name, i, n, host):
    t = time.strftime("%Y-%m-%d %H:%M", time.localtime(res.get("epoch") or time.time()))
    lines = ["💾 <b>بکاپ %s</b> — %s" % (esc(part), esc(host)),
             "📅 %s" % t,
             "📦 حجم: %s · فایل‌ها: %s · دیتابیس: %s" % (human(res.get("size")), fa(res.get("files", "؟")),
                                                       fa(res.get("databases", "؟"))),
             "🔐 sha256 فایل اصلی:", "<code>%s</code>" % esc(res.get("sha256") or "؟")]
    tar = enc_name[:-4] if enc_name.endswith(".enc") else enc_name + ".tar"
    if n > 1:
        lines.append("🧩 تکه %s از %s" % (fa(i + 1), fa(n)))
        lines.append("🔓 باز کردن (همه‌ی تکه‌ها در یک پوشه):")
        lines.append("<code>cat %s.part* &gt; %s</code>" % (esc(enc_name), esc(enc_name)))
    else:
        lines.append("🔓 باز کردن:")
    lines.append("<code>openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in %s -out %s</code>" % (esc(enc_name), esc(tar)))
    return "\n".join(lines)


class Progress:
    """One message per chat that shows how far a job is (edited at most every few seconds)."""

    def __init__(self, app, chats, every=3.0):
        self.app, self.every = app, every
        self.msgs = {}
        self.last = 0.0
        self.text = ""
        for c in chats:
            try:
                m = app.bot.send(c, "⏳ شروع…")
                self.msgs[c] = m["message_id"]
            except (tg.TelegramError, OSError) as e:
                log("progress message to %s: %s", c, e)

    def set(self, text, force=False):
        self.text = text
        if not force and time.monotonic() - self.last < self.every:
            return
        self.last = time.monotonic()
        for c, mid in self.msgs.items():
            try:
                self.app.bot.edit(c, mid, text)
            except (tg.TelegramError, OSError):
                pass


class Sender:
    def __init__(self, app):
        self.app = app
        self.q = queue.Queue()
        self.st = JsonState(os.path.join(config.STATE, "send.json"),
                            {"job": None, "results": {}, "auto_last": 0, "at_last_day": ""})
        self.thread = None
        self.busy = None
        self.sleep = time.sleep

    # ------------------------------------------------------------ public
    def start(self):
        job = self.st.get("job")
        if job:
            log("backup send: resuming job %s (%s)", job["id"], ", ".join(job["parts"]))
            self.q.put(job)
        self.thread = threading.Thread(target=self.loop, name="sender", daemon=True)
        self.thread.start()

    def results(self):
        return self.st.get("results") or {}

    def submit(self, parts, make=True, files=None, chats=None, trigger="manual", failed=None):
        """parts: names; files: {part: archive path} to send instead of the newest one;
        failed: {part: error} of parts whose last backup failed (reported, nothing sent)."""
        parts = [p for p in parts if p in config.PARTS]
        job = {"id": uuid.uuid4().hex[:8], "parts": parts, "make": make, "files": files or {},
               "chats": chats or self.app.conf["allowed_chat_ids"], "trigger": trigger, "made": None,
               "summary": False, "done": [], "cur": None, "created": time.time(), "failed": failed or {}}
        self.q.put(job)
        return job["id"]

    def loop(self):
        while True:
            job = self.q.get()
            if job is None:
                return
            try:
                self.process(job)
            except Exception as e:  # never let the queue die
                log("backup send job %s failed: %r", job.get("id"), e)
                self.st.set("job", None)
            finally:
                self.busy = None

    # ------------------------------------------------------------ one job
    def save_job(self, job):
        self.st.set("job", job)

    def password_file(self):
        p = self.app.conf.get("backup_password_file") or os.path.join(config.ETC, "backup.pass")
        return config.R(p) if config.ROOT and not p.startswith(config.ROOT) else p

    def process(self, job):
        self.busy = job
        self.save_job(job)
        prog = Progress(self.app, job["chats"])
        host = system.hostname()
        pw = self.password_file()
        if not (os.path.isfile(pw) and os.path.getsize(pw) > 0):
            prog.set("❌ رمز بکاپ تنظیم نشده؛ بدون رمز چیزی فرستاده نمی‌شود.\n⚙️ تنظیمات ← 🔑 رمز بکاپ، یا: <code>eris-bot password</code>", True)
            for p in job["parts"]:
                self.set_result(p, False, "no backup password")
            self.st.set("job", None)
            return
        if job["made"] is None:
            if job["make"]:
                prog.set("💾 در حال گرفتن بکاپ: %s" % "، ".join(job["parts"]), True)
                try:
                    data = eris.backup_now(job["parts"])
                    made = {r["part"]: r for r in data.get("results", [])}
                except eris.ErisError as e:
                    made = {p: {"part": p, "ok": False, "error": str(e)} for p in job["parts"]}
            else:
                made = self.existing(job)
            for r in made.values():
                if r.get("ok") and not r.get("epoch"):
                    r["epoch"] = time.time() if job["make"] else (os.path.getmtime(r["path"]) if os.path.exists(r.get("path", "")) else time.time())
            job["made"] = made
            self.save_job(job)
        made = job["made"]
        if not job["summary"]:
            lines = ["📦 <b>بکاپ‌های %s</b> — %s" % ("شبانه" if job["trigger"] == "auto" else "دستی", esc(host))]
            for p in job["parts"]:
                r = made.get(p) or {}
                if r.get("ok"):
                    lines.append("✅ %s — %s" % (p, human(r.get("size"))))
                elif r.get("skipped"):
                    lines.append("⏭ %s — روی این سرور نیست" % p)
                else:
                    lines.append("❌ %s — %s" % (p, esc((r.get("error") or "ناموفق")[:150])))
            ok = [p for p in job["parts"] if (made.get(p) or {}).get("ok")]
            lines.append("📤 %s فایل در راه است (رمزنگاری‌شده)." % fa(len(ok)) if ok else "📭 فایلی برای ارسال نیست.")
            for c in job["chats"]:
                try:
                    self.app.bot.send(c, "\n".join(lines))
                except (tg.TelegramError, OSError) as e:
                    log("backup summary to %s: %s", c, e)
            job["summary"] = True
            self.save_job(job)
        for p in job["parts"]:
            r = made.get(p) or {}
            if p in job["done"]:
                continue
            if not r.get("ok"):
                if not r.get("skipped"):
                    self.set_result(p, False, r.get("error") or "backup failed", sent=False)
                job["done"].append(p)
                self.save_job(job)
                continue
            try:
                self.send_part(job, p, r, prog, host, pw)
                self.set_result(p, True, None)
            except Exception as e:  # noqa: B902 - the reason goes to the result and the alerts
                log("backup send %s: %s", p, e)
                self.set_result(p, False, str(e))
                prog.set("❌ ارسال %s ناموفق بود: %s" % (p, esc(str(e)[:200])), True)
                self.cleanup(job)
            job["done"].append(p)
            job["cur"] = None
            self.save_job(job)
        res = self.results()
        okn = sum(1 for p in job["parts"] if (res.get(p) or {}).get("ok") and (made.get(p) or {}).get("ok"))
        prog.set("✅ ارسال تمام شد: %s از %s بخش" % (fa(okn), fa(len([p for p in job["parts"] if not (made.get(p) or {}).get("skipped")]))), True)
        self.st.set("job", None)

    def existing(self, job):
        """The newest archive of each part (or the chosen file), without making a new one."""
        out = {}
        try:
            groups = eris.backup_list()["groups"]
        except (eris.ErisError, KeyError) as e:
            return {p: {"part": p, "ok": False, "error": str(e)} for p in job["parts"]}
        for p in job["parts"]:
            if p in (job.get("failed") or {}):
                out[p] = {"part": p, "ok": False, "error": job["failed"][p]}
                continue
            items = groups.get(p) or []
            path = job["files"].get(p) or (items[0]["path"] if items else None)
            if not path:
                out[p] = {"part": p, "ok": False, "error": "no backup of this part yet"}
                continue
            if not any(it["path"] == path for it in items):
                out[p] = {"part": p, "ok": False, "error": "not a backup of %s" % p}
                continue
            try:
                v = eris.verify(path)
            except eris.ErisError as e:
                out[p] = {"part": p, "ok": False, "error": str(e)}
                continue
            item = next(it for it in items if it["path"] == path)
            out[p] = {"part": p, "ok": bool(v.get("ok")), "path": path, "size": v.get("size"), "sha256": v.get("sha256"),
                      "files": v.get("files"), "databases": v.get("databases"), "epoch": item.get("epoch"),
                      "error": None if v.get("ok") else v.get("message")}
        return out

    def set_result(self, part, ok, error, sent=True):
        res = self.results()
        r = res.get(part) or {}
        r.update(ok=ok, error=error, at=time.time())
        if ok:
            r["last_sent"] = time.time()
        res[part] = r
        self.st.set("results", res)

    def cleanup(self, job):
        cur = job.get("cur") or {}
        if cur.get("enc") and os.path.exists(cur["enc"]):
            try:
                os.remove(cur["enc"])
            except OSError:
                pass

    def send_part(self, job, part, res, prog, host, pw):
        conf = self.app.conf
        size_mb = max(1, min(49, int(conf.get("part_size_mb", 45))))
        piece = size_mb * 1024 * 1024
        cur = job.get("cur")
        if cur and cur.get("part") == part and os.path.exists(cur.get("enc", "")) and os.path.getsize(cur["enc"]) == cur["size"]:
            log("backup send %s: going on from piece %d of %d", part, len(cur["sent"]) + 1, cur["pieces"])
        else:
            if cur and cur.get("enc") and os.path.exists(cur["enc"]):
                os.remove(cur["enc"])
            os.makedirs(config.TMP, mode=0o700, exist_ok=True)
            name = os.path.basename(res["path"]) + ".enc"
            enc = os.path.join(config.TMP, name)
            prog.set("🔐 رمزنگاری %s…" % part, True)
            eris.encrypt(res["path"], pw, enc)
            size = os.path.getsize(enc)
            cur = {"part": part, "enc": enc, "name": name, "size": size, "pieces": max(1, math.ceil(size / float(piece))),
                   "piece": piece, "sent": [], "file_ids": {}}
            job["cur"] = cur
            self.save_job(job)
        n = cur["pieces"]
        up = conf.get("upload") or {}
        retries = int(up.get("retries", 8))
        for i in range(n):
            if i in cur["sent"]:
                continue
            off = i * cur["piece"]
            length = min(cur["piece"], cur["size"] - off)
            fname = piece_name(cur["name"], i, n)
            cap = caption(part, res, cur["name"], i, n, host)
            attempt = 0
            while True:
                try:
                    self._send_piece(job, cur, i, n, fname, off, length, cap, prog, part)
                    break
                except (tg.TelegramError, OSError, socket.timeout) as e:
                    code = getattr(e, "code", None)
                    if isinstance(e, tg.TelegramError) and code and 400 <= code < 500 and code != 429:
                        raise
                    attempt += 1
                    if attempt > retries:
                        raise
                    wait = BACKOFF[min(attempt - 1, len(BACKOFF) - 1)]
                    log("backup send %s piece %d: %s; again in %d s (try %d of %d)", part, i + 1, e, wait, attempt + 1, retries + 1)
                    prog.set("⚠️ %s تکه %s: قطع شد (%s)؛ تلاش دوباره تا %s ثانیه‌ی دیگر" % (
                        part, fa(i + 1), esc(str(e)[:80]), fa(wait)), True)
                    self.sleep(wait)
            cur["sent"].append(i)
            self.save_job(job)
        try:
            os.remove(cur["enc"])
        except OSError:
            pass

    def _send_piece(self, job, cur, i, n, fname, off, length, cap, prog, part):
        first = job["chats"][0]
        t0 = time.monotonic()

        def progress(sent, total):
            rate = sent / max(0.1, time.monotonic() - t0)
            prog.set("📤 ارسال <b>%s</b> — تکه %s از %s\n<code>%s</code> %s٪ · %s / %s · %s/s" % (
                part, fa(i + 1), fa(n), bar(100.0 * sent / total if total else 100), fa(int(100.0 * sent / total) if total else 100),
                human(sent), human(total), human(rate)))
        fid = cur["file_ids"].get(str(i))
        if not fid:
            r = self.app.bot.send_file("sendDocument", "document", first, fname, path=cur["enc"], offset=off,
                                       length=length, caption=cap, progress=progress)
            fid = ((r or {}).get("document") or {}).get("file_id")
            cur["file_ids"][str(i)] = fid
            self.save_job(job)
        for c in job["chats"][1:]:
            if fid:
                self.app.bot.call("sendDocument", {"chat_id": c, "document": fid, "caption": cap, "parse_mode": "HTML"})

    # ------------------------------------------------------------ schedule
    def tick(self, now=None):
        """After the nightly backup, or at the set time: queue the parts to send."""
        now = now or time.time()
        conf = self.app.conf
        bs = conf.get("backup_send") or {}
        mode = bs.get("mode", "after_nightly")
        if mode == "off" or not eris.available() or self.busy or not self.q.empty():
            return None
        parts = config.send_parts(conf)
        if mode == "at":
            at = bs.get("at", "04:30")
            if not config.HHMM_RX.match(at):
                return None
            lt = time.localtime(now)
            h, m = int(at[:2]), int(at[3:])
            start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
            day = time.strftime("%Y-%m-%d", lt)
            if start <= now < start + 1800 and self.st.get("at_last_day") != day:
                self.st.set("at_last_day", day)
                return self.submit(parts, make=True, trigger="auto")
            return None
        try:
            s = eris.backup_status()
        except eris.ErisError:
            return None
        pst = s.get("parts") or {}
        want = [p for p in parts if (pst.get(p) or {}).get("present") and (pst.get(p) or {}).get("enabled")]
        newest = max([max((pst[p].get("last_ok_epoch") or 0), (pst[p].get("last_error_epoch") or 0)) for p in want] or [0])
        last = self.st.get("auto_last") or 0
        if not last:
            self.st.set("auto_last", newest or int(now))
            return None
        if newest <= last or now - newest < 60:
            return None
        if system.sh(["systemctl", "is-active", "eris-backup.service"], 10).stdout.strip() in ("active", "activating"):
            return None
        fresh = [p for p in want if max(pst[p].get("last_ok_epoch") or 0, pst[p].get("last_error_epoch") or 0) > last]
        self.st.set("auto_last", newest)
        if not fresh:
            return None
        failed = {p: pst[p].get("last_error") or "backup failed" for p in fresh if pst[p].get("failed")}
        return self.submit(fresh, make=False, trigger="auto", failed=failed)
ERISBOT_EOF_backup_py
  put_file "$STAGE/erisbot/cli.py" 0644 << 'ERISBOT_EOF_cli_py'
"""eris-bot - a Telegram bot for this server.

  eris-bot setup                  token (hidden while you type) and chat IDs
  eris-bot status                 what is set up and running
  eris-bot test                   connect to Telegram directly and through each proxy (getMe, with latency)
  eris-bot proxy [off|auto|URL|test]   the way to Telegram (URL: http://host:port or socks5://[user:pass@]host:port)
  eris-bot password               set the password that encrypts the backups sent to Telegram
  eris-bot logs [-f] [-n N]       the log (journalctl -u eris-bot -u eris-fan)
  eris-bot update [--check|--force]   install the latest release (through the bot's proxy)
  eris-bot version
  eris-bot run | fan-daemon | fan-restore | selftest   (used by the services and the installer)
"""
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
import time

from . import VERSION, REPO, config, net, store, tg
from .util import log

GREEN, RED, AMBER, GREY = "\033[1;32m", "\033[1;31m", "\033[33m", "\033[1;30m"
TTY = sys.stdout.isatty()


def c(code, s):
    return "%s%s\033[0m" % (code, s) if TTY else s


def need_root():
    if os.geteuid() != 0 and not config.ROOT:
        sys.exit("eris-bot: run as root")


def bot_for(conf):
    return tg.Bot(lambda: conf, store.JsonState(os.path.join(config.STATE, "bot.json"), {}))


def routes(conf):
    mode = conf.get("proxy", "auto")
    lst = list(conf.get("proxy_list") or [])
    if mode == "off":
        return [None]
    if mode == "auto":
        return [None] + lst
    return [mode]


def cmd_test(conf, every=True):
    if not conf.get("token"):
        print("no token yet: eris-bot setup")
        return 1
    b = bot_for(conf)
    cands = [None] + list(conf.get("proxy_list") or [])
    if conf["proxy"] not in ("auto", "off") and conf["proxy"] not in cands:
        cands.append(conf["proxy"])
    if not every:
        cands = routes(conf)
    ok_any = False
    print("Telegram API: %s   (setting proxy=%s)" % (conf["api_base"], conf["proxy"] if conf["proxy"] in ("auto", "off") else net.describe(conf["proxy"])))
    for r in cands:
        ok, sec, err = b.probe(r, timeout=10)
        ok_any |= ok
        print("  %-38s %s" % (net.describe(r) if r else "direct", c(GREEN, "ok  %4d ms" % (sec * 1000)) if ok else c(RED, "no  %s" % (err or "")[:70])))
    return 0 if ok_any else 1


def cmd_status(conf):
    print("eris-bot %s" % VERSION)
    print("  settings : %s (%s)" % (config.CONFIG, oct(os.stat(config.CONFIG).st_mode & 0o777) if os.path.exists(config.CONFIG) else "missing"))
    print("  token    : %s" % ("set" if conf.get("token") else c(RED, "not set (eris-bot setup)")))
    print("  chats    : %s" % (", ".join("%s (%s)" % (i, config.role(conf, i)) for i in conf["allowed_chat_ids"]) or c(RED, "none")))
    print("  proxy    : %s; list: %s" % (conf["proxy"] if conf["proxy"] in ("auto", "off") else net.describe(conf["proxy"]),
                                         ", ".join(net.describe(p) for p in conf.get("proxy_list") or [])))
    for u in ("eris-bot", "eris-fan"):
        st = subprocess.run(["systemctl", "is-active", u], capture_output=True, text=True).stdout.strip() if os.path.exists("/usr/bin/systemctl") or os.path.exists("/bin/systemctl") else "?"
        print("  %-8s : %s" % (u, c(GREEN, st) if st == "active" else c(AMBER, st or "?")))
    pw = conf.get("backup_password_file")
    print("  password : %s" % ("set" if pw and os.path.isfile(pw) and os.path.getsize(pw) > 0 else c(AMBER, "not set (eris-bot password)")))
    from . import eris, hw, marzban
    print("  ERIS     : %s" % (eris.version() or "?") if eris.available() else "  ERIS     : not installed (backup and firewall menus are hidden)")
    print("  Marzban  : %s" % ("found (%s)" % config.MARZBAN_ENV if marzban.Marzban().available() else "not found (its menu is hidden)"))
    print("  fans     : %s; cpufreq: %s" % (", ".join(f["id"] for f in hw.fans()) or "none", "yes" if hw.has_cpufreq() else "no"))
    send = config.read_json(os.path.join(config.STATE, "send.json"), {}) or {}
    for part, r in sorted((send.get("results") or {}).items()):
        print("  sent %-12s %s %s" % (part, "ok" if r.get("ok") else "FAILED", time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("at", 0)))))
    return 0


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def write_password(path, pw):
    from .s_main import write_password as wp
    wp(path, pw)


def cmd_password(conf):
    path = config.R(conf["backup_password_file"]) if config.ROOT else conf["backup_password_file"]
    print("The backups sent to Telegram are encrypted with this password (AES-256, openssl).")
    print("Without it nobody (you included) can open them: keep it somewhere safe.")
    a = getpass.getpass("new backup password (at least 12 characters): ")
    if len(a) < 12:
        print(c(RED, "too short, nothing changed"))
        return 1
    if getpass.getpass("again: ") != a:
        print(c(RED, "not the same, nothing changed"))
        return 1
    write_password(path, a)
    print(c(GREEN, "saved in %s (mode 600)" % path))
    return 0


def systemctl(*args):
    try:
        return subprocess.run(["systemctl"] + list(args), capture_output=True, text=True, timeout=60).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1


def cmd_setup(conf):
    need_root()
    print("eris-bot setup. Nothing you type here is shown or printed.")
    token = conf.get("token") or ""
    t = getpass.getpass("bot token from @BotFather%s: " % (" (Enter = keep the current one)" if token else ""))
    if t.strip():
        if not config.TOKEN_RX.match(t.strip()):
            print(c(RED, "that does not look like a bot token (123456:ABC...); nothing changed"))
            return 1
        token = t.strip()
    if not token:
        print(c(RED, "a token is needed"))
        return 1
    conf = dict(conf, token=token)
    p = ask("proxy to Telegram [%s] (auto, off, http://host:port, socks5://host:port): " % conf["proxy"])
    if p:
        if not config.validate_proxy(p):
            print(c(RED, "not a proxy URL; nothing changed"))
            return 1
        conf["proxy"] = p
    print("testing the connection ...")
    b = bot_for(conf)
    try:
        route = b.route()
        me = b.get_me()
    except (tg.TelegramError, OSError) as e:
        print(c(RED, "Telegram does not answer with this token: %s" % e))
        print("check the token and the proxy (eris-bot test), then run eris-bot setup again")
        return 1
    print(c(GREEN, "ok: @%s via %s" % (me.get("username"), net.describe(route) if route else "a direct connection")))
    was_active = systemctl("is-active", "--quiet", "eris-bot") == 0
    if was_active:
        systemctl("stop", "eris-bot")
    try:
        ids = list(conf.get("allowed_chat_ids") or [])
        print("Allowed chat IDs now: %s" % (", ".join(map(str, ids)) or "none"))
        a = ask("chat IDs (numbers, comma separated), or Enter to find yours: send any message to @%s now: " % me.get("username"))
        if a:
            if not re.fullmatch(r"\s*-?\d+(\s*,\s*-?\d+)*\s*", a):
                print(c(RED, "chat IDs are numbers; nothing changed"))
                return 1
            ids = [int(x) for x in a.split(",")]
        elif not ids:
            print("waiting up to 120 s for a message to @%s ..." % me.get("username"))
            found = {}
            off = None
            deadline = time.time() + 120
            while time.time() < deadline and not found:
                upd = b.call("getUpdates", {"timeout": 20, "offset": off} if off else {"timeout": 20}, timeout=35) or []
                for u in upd:
                    off = u["update_id"] + 1
                    m = u.get("message") or {}
                    ch = m.get("chat") or {}
                    if ch.get("id"):
                        found[ch["id"]] = ch.get("username") or ch.get("first_name") or ch.get("title") or ""
            if off:
                b.call("getUpdates", {"offset": off, "timeout": 0})
            if not found:
                print(c(RED, "no message came; run eris-bot setup again (or type the chat ID)"))
                return 1
            for cid, name in found.items():
                if ask("allow chat %s (%s)? [Y/n] " % (cid, name)).lower() in ("", "y", "yes"):
                    ids.append(cid)
        if not ids:
            print(c(RED, "no chat allowed; nothing changed"))
            return 1
        config.save({"token": token, "proxy": conf["proxy"], "allowed_chat_ids": ids})
        print(c(GREEN, "saved %s (mode 600)" % config.CONFIG))
        pw = config.R(conf["backup_password_file"]) if config.ROOT else conf["backup_password_file"]
        if not (os.path.isfile(pw) and os.path.getsize(pw) > 0):
            if ask("set the backup password now? [Y/n] ").lower() in ("", "y", "yes"):
                cmd_password(config.load())
    finally:
        systemctl("enable", "eris-bot")
        systemctl("restart", "eris-bot")
    print("eris-bot runs. Send /start to @%s." % me.get("username"))
    return 0


def cmd_proxy(conf, args):
    if not args:
        print("proxy=%s" % (conf["proxy"] if conf["proxy"] in ("auto", "off") else net.describe(conf["proxy"])))
        print("proxy_list: %s" % ", ".join(net.describe(p) for p in conf.get("proxy_list") or []))
        return 0
    if args[0] == "test":
        return cmd_test(conf)
    need_root()
    if not config.validate_proxy(args[0]):
        print("eris-bot proxy: off, auto, http://host:port or socks5://[user:pass@]host:port")
        return 2
    config.save({"proxy": args[0]})
    print("proxy=%s (eris-bot picks it up at once)" % (args[0] if args[0] in ("auto", "off") else net.describe(args[0])))
    return cmd_test(config.load(), every=False)


def http_get(url, conf, timeout=60, max_redirects=5):
    """GET through the bot's way out (auto: direct first, then each proxy), following redirects."""
    last = None
    for r in routes(conf):
        u = url
        try:
            for _ in range(max_redirects):
                resp = net.request("GET", u, proxy=r, headers={"User-Agent": "eris-bot/%s" % VERSION,
                                                                "Accept": "application/vnd.github+json, */*"},
                                   timeout=20, read_timeout=timeout)
                if resp.status in (301, 302, 303, 307, 308):
                    u = resp.headers.get("Location") or resp.headers.get("location")
                    continue
                if resp.status != 200:
                    raise OSError("HTTP %d from %s" % (resp.status, u.split("?")[0]))
                return resp.body
        except (OSError, ValueError) as e:
            last = e
    raise OSError(str(last) if last else "no route")


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def cmd_update(conf, args):
    need_root()
    try:
        tag = json.loads(http_get("https://api.github.com/repos/%s/releases/latest" % REPO, conf, 30))["tag_name"]
    except (OSError, ValueError, KeyError) as e:
        print(c(RED, "eris-bot update: GitHub did not answer (%s)" % e))
        return 1
    print("installed %s, latest release %s" % (VERSION, tag))
    if "--check" in args:
        return 0 if version_tuple(tag) <= version_tuple(VERSION) else 10
    if version_tuple(tag) <= version_tuple(VERSION) and "--force" not in args:
        print(c(GREEN, "up to date"))
        return 0
    url = "https://github.com/%s/releases/download/%s/install-eris-bot.sh" % (REPO, tag)
    try:
        data = http_get(url, conf, 300)
    except OSError as e:
        print(c(RED, "download failed: %s" % e))
        return 1
    if not data.startswith(b"#!/bin/bash") or b"eris-bot installer" not in data[:2048]:
        print(c(RED, "%s is not the eris-bot installer" % url))
        return 1
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "install-eris-bot.sh")
        with open(path, "wb") as f:
            f.write(data)
        print("installing %s ..." % tag)
        return subprocess.call(["bash", path])


def cmd_selftest():
    ok = True
    lib = os.path.dirname(os.path.abspath(__file__))
    for f in sorted(os.listdir(lib)):
        if f.endswith(".py"):
            try:
                compile(open(os.path.join(lib, f), encoding="utf-8").read(), f, "exec")
            except SyntaxError as e:
                print("FAIL python syntax %s: %s" % (f, e))
                ok = False
    if os.path.exists(config.CONFIG):
        mode = os.stat(config.CONFIG).st_mode & 0o777
        print("%s %s mode %o" % ("PASS" if mode == 0o600 else "FAIL", config.CONFIG, mode))
        ok &= mode == 0o600
    try:
        from . import app  # noqa: F401
        print("PASS modules import")
    except Exception as e:  # noqa: B902
        print("FAIL import: %s" % e)
        ok = False
    print("selftest %s" % ("passed" if ok else "FAILED"))
    return 0 if ok else 1


def main(argv):
    cmd = argv[0] if argv else "status"
    args = argv[1:]
    conf = config.load()
    if cmd in ("version", "--version"):
        print(VERSION)
        return 0
    if cmd == "run":
        need_root()
        from .app import App
        return App().run_forever()
    if cmd == "fan-daemon":
        need_root()
        from . import fand
        return fand.run_daemon()
    if cmd == "fan-restore":
        from . import fand
        return fand.restore_from_file()
    if cmd == "profile-reset":
        need_root()
        from . import perf
        for line in perf.reset_default():
            print(line)
        return 0
    if cmd == "setup":
        return cmd_setup(conf)
    if cmd == "status":
        return cmd_status(conf)
    if cmd == "test":
        return cmd_test(conf)
    if cmd == "proxy":
        return cmd_proxy(conf, args)
    if cmd == "password":
        need_root()
        return cmd_password(conf)
    if cmd == "logs":
        n = args[args.index("-n") + 1] if "-n" in args and args.index("-n") + 1 < len(args) else "100"
        cmd_ = ["journalctl", "-u", "eris-bot", "-u", "eris-fan", "-n", n, "--no-pager"] + (["-f"] if "-f" in args else [])
        os.execvp(cmd_[0], cmd_)
    if cmd in ("update", "upgrade"):
        return cmd_update(conf, args)
    if cmd == "selftest":
        return cmd_selftest()
    print(__doc__.strip())
    return 0 if cmd in ("help", "-h", "--help") else 2


def run():
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print()
        sys.exit(130)
    except Exception as e:  # noqa: B902 - never print a token in a traceback
        log("eris-bot: %r", e)
        sys.exit(1)
ERISBOT_EOF_cli_py
  put_file "$STAGE/erisbot/config.py" 0644 << 'ERISBOT_EOF_config_py'
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
ERISBOT_EOF_config_py
  put_file "$STAGE/erisbot/eris.py" 0644 << 'ERISBOT_EOF_eris_py'
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
ERISBOT_EOF_eris_py
  put_file "$STAGE/erisbot/fand.py" 0644 << 'ERISBOT_EOF_fand_py'
"""Fan control (eris-fan.service): every 2 seconds read the temperatures and set each pwm fan.

Modes of a fan (fans.json): auto (the BIOS / driver, as before), manual (a percent, at least 25 %)
and curve (temperature -> percent with 3 °C hysteresis and gradual steps).
Protection that no setting can turn off: CPU >= 75 °C or GPU >= 80 °C puts that fan at 100 % at once.
On stop (or a crash: ExecStopPost=eris-bot fan-restore) every fan gets its original pwm_enable back.
"""
import os
import signal
import socket
import time

from . import config, hw
from .util import log, read, write

MIN_PCT = 25
HYST = 3.0
STEP_UP, STEP_DOWN = 10.0, 5.0
CPU_LIMIT, GPU_LIMIT = 75.0, 80.0
STALL_PCT = 30.0
INTERVAL = 2.0
ORIG = os.path.join(config.STATE, "fan-orig.json")
STATUS = os.path.join(config.RUN, "fans-status.json")

CURVES = {
    "quiet": [[45, 25], [55, 35], [65, 55], [72, 100]],
    "normal": [[40, 30], [50, 45], [60, 65], [70, 100]],
    "steep": [[35, 40], [45, 60], [55, 80], [65, 100]],
}
CURVE_TITLES = {"quiet": "منحنی آرام", "normal": "منحنی معمولی", "steep": "منحنی تند"}


def curve_value(points, temp):
    pts = sorted((float(t), float(p)) for t, p in points)
    if not pts:
        return 100.0
    if temp <= pts[0][0]:
        return pts[0][1]
    for (t1, p1), (t2, p2) in zip(pts, pts[1:]):
        if temp <= t2:
            return p1 + (p2 - p1) * (temp - t1) / (t2 - t1) if t2 > t1 else p2
    return pts[-1][1]


def load_cfg():
    c = config.read_json(config.FANS, {}) or {}
    if not isinstance(c.get("fans"), dict):
        c["fans"] = {}
    return c


def save_cfg(c):
    config.write_json(config.FANS, c, 0o600)


def fan_cfg(cfg, fan_id):
    f = (cfg.get("fans") or {}).get(fan_id) or {}
    mode = f.get("mode", "auto")
    if mode not in ("auto", "manual", "curve"):
        mode = "auto"
    return {"mode": mode, "percent": float(f.get("percent", 50)), "curve": f.get("curve") or CURVES["normal"],
            "curve_name": f.get("curve_name")}


def describe_cfg(fc):
    if fc["mode"] == "auto":
        return "🔁 خودکار"
    if fc["mode"] == "manual":
        return "🎚 دستی %d٪" % fc["percent"]
    return "📈 " + CURVE_TITLES.get(fc.get("curve_name") or "", "منحنی سفارشی")


class FanState:
    def __init__(self):
        self.pct = None          # what we set last (None = not ours / auto)
        self.taken = False       # pwm_enable was changed by us
        self.protect = False
        self.stall = 0


class Controller:
    def __init__(self, temp_fns=None):
        self.temp_fns = temp_fns or {"cpu": hw.system.cpu_temp, "gpu": None}
        self.fans = hw.fans()
        self.states = {}
        self.cfg = load_cfg()
        self.cfg_mtime = self._mtime()
        self.orig = config.read_json(ORIG, {}) or {}
        self.last_scan = time.monotonic()
        self.record_orig()

    @staticmethod
    def _mtime():
        try:
            return os.stat(config.FANS).st_mtime_ns
        except OSError:
            return None

    def record_orig(self):
        """The pwm_enable / pwm each fan had before eris-bot touched it (kept for good)."""
        changed = False
        for f in self.fans:
            if f["id"] in self.orig:
                continue
            en = read(f["enable"]) if f["enable"] else None
            if en == "1":            # already manual: probably left by us after a hard crash; auto is "2"
                en = "2"
            self.orig[f["id"]] = {"enable": en, "pwm": read(f["pwm"]), "enable_path": f["enable"], "pwm_path": f["pwm"]}
            changed = True
        if changed:
            config.write_json(ORIG, self.orig, 0o600)

    def temp_for(self, fan):
        if fan["kind"] == "gpu":
            fn = self.temp_fns.get("gpu")
            return fn(fan) if fn else hw.fan_temp(fan)
        return self.temp_fns["cpu"]()

    def target(self, fan, st, temp, fc):
        """Percent to set, or None for the BIOS/driver (auto)."""
        limit = CPU_LIMIT if fan["kind"] == "cpu" else GPU_LIMIT
        if temp is not None:
            if temp >= limit:
                st.protect = True
            elif st.protect and temp <= limit - HYST:
                st.protect = False
        if st.protect:
            return 100.0
        if fc["mode"] == "auto":
            return None
        if fc["mode"] == "manual":
            return max(MIN_PCT, min(100.0, fc["percent"]))
        if temp is None:              # no sensor: leave it to the BIOS
            return None
        up = curve_value(fc["curve"], temp)
        down = curve_value(fc["curve"], temp + HYST)
        cur = st.pct if st.pct is not None else up
        if up > cur:
            new = min(up, cur + STEP_UP)
        elif down < cur:
            new = max(down, cur - STEP_DOWN)
        else:
            new = cur
        return max(MIN_PCT, min(100.0, new))

    def apply(self, fan, st, pct):
        if pct is None:
            if st.taken:
                self.restore_one(fan["id"])
                st.taken = False
            st.pct = None
            return
        if fan["enable"] and read(fan["enable"]) != "1":
            write(fan["enable"], "1")
        st.taken = True
        value = hw.pct_to_pwm(fan, pct)
        if read(fan["pwm"]) != str(value):
            write(fan["pwm"], value)
        st.pct = pct

    def restore_one(self, fan_id):
        o = self.orig.get(fan_id)
        if not o:
            return
        try:
            if o.get("enable_path") and o.get("enable") is not None and os.path.exists(o["enable_path"]):
                write(o["enable_path"], o["enable"])
            elif o.get("pwm_path") and o.get("pwm") is not None and os.path.exists(o["pwm_path"]):
                write(o["pwm_path"], o["pwm"])
        except OSError as e:
            log("fan %s: could not restore: %s", fan_id, e)

    def restore_all(self):
        for fid in list(self.orig):
            self.restore_one(fid)
        for st in self.states.values():
            st.taken, st.pct = False, None

    def step(self, now=None):
        now = now or time.time()
        m = self._mtime()
        if m != self.cfg_mtime:
            self.cfg, self.cfg_mtime = load_cfg(), m
        if time.monotonic() - self.last_scan > 60:
            self.fans = hw.fans()
            self.record_orig()
            self.last_scan = time.monotonic()
        cpu_t = self.temp_fns["cpu"]()
        status = {"ts": now, "cpu_temp": cpu_t, "gpu_temp": None, "fans": {}, "alerts": []}
        for fan in self.fans:
            st = self.states.setdefault(fan["id"], FanState())
            fc = fan_cfg(self.cfg, fan["id"])
            temp = cpu_t if fan["kind"] == "cpu" else self.temp_for(fan)
            if fan["kind"] == "gpu":
                status["gpu_temp"] = temp
            was = st.protect
            try:
                pct = self.target(fan, st, temp, fc)
                self.apply(fan, st, pct)
            except OSError as e:
                log("fan %s: %s", fan["id"], e)
            speed = hw.rpm(fan)
            raw = read(fan["pwm"])
            real = hw.pwm_to_pct(fan, int(raw)) if raw and raw.lstrip("-").isdigit() else None
            if speed == 0 and real is not None and real > STALL_PCT:
                st.stall += 1
            else:
                st.stall = 0
            what = "CPU" if fan["kind"] == "cpu" else "کارت گرافیک"
            if st.protect:
                status["alerts"].append({"key": "fan_hot:" + fan["id"], "level": 2,
                                         "text": "🔥 دمای %s %.0f°C شد؛ فن %s روی ۱۰۰٪ رفت" % (what, temp or 0, fan["label"])})
            if st.stall >= 3:
                status["alerts"].append({"key": "fan_stall:" + fan["id"], "level": 2,
                                         "text": "🛑 فن %s نمی‌چرخد (۰ RPM با %d٪)؛ گیر کرده یا خراب است" % (fan["label"], real)})
            if st.protect and not was:
                log("fan %s: %s at %.0f C, 100%%", fan["id"], what, temp or 0)
            status["fans"][fan["id"]] = {"label": fan["label"], "kind": fan["kind"], "chip": fan["chip"], "rpm": speed,
                                         "pct": real, "set": st.pct, "mode": fc["mode"], "desc": describe_cfg(fc),
                                         "protect": st.protect, "stall": st.stall >= 3, "temp": temp}
        return status


def write_status(status):
    try:
        config.write_json(STATUS, status, 0o644)
    except OSError:
        pass


def read_status(max_age=15):
    s = config.read_json(STATUS, None)
    if not s or time.time() - s.get("ts", 0) > max_age:
        return None
    return s


def sd_notify(msg):
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return
    if path.startswith("@"):
        path = "\0" + path[1:]
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.connect(path)
        s.sendall(msg.encode())
    except OSError:
        pass
    finally:
        s.close()


def run_daemon(interval=INTERVAL, max_cycles=None):
    from . import perf
    stop = {"now": False}

    def on_signal(signum, frame):
        stop["now"] = True
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        perf.apply_saved_system()
    except Exception as e:  # the profile's CPU settings are not worth stopping the fans for
        log("profile at start: %s", e)
    c = Controller()
    log("eris-fan: %d fan(s): %s", len(c.fans), ", ".join(f["id"] for f in c.fans) or "none")
    sd_notify("READY=1")
    n = 0
    try:
        while not stop["now"]:
            write_status(c.step())
            sd_notify("WATCHDOG=1")
            n += 1
            if max_cycles and n >= max_cycles:
                break
            t_end = time.monotonic() + interval
            while not stop["now"] and time.monotonic() < t_end:
                time.sleep(0.2)
    finally:
        c.restore_all()
        log("eris-fan: fans back to their original mode")
    return 0


def restore_from_file():
    """ExecStopPost: whatever happened to the daemon, the fans go back to how they were."""
    orig = config.read_json(ORIG, {}) or {}
    c = Controller.__new__(Controller)
    c.orig, c.states = orig, {}
    c.restore_all()
    try:
        os.remove(STATUS)
    except OSError:
        pass
    return 0
ERISBOT_EOF_fand_py
  put_file "$STAGE/erisbot/hw.py" 0644 << 'ERISBOT_EOF_hw_py'
"""Fans (pwm in /sys/class/hwmon), CPU frequency (cpufreq) and the like, as they really are on this server.

Nothing here raises the FSB or the voltage: on Core 2 / G41 that is only safe from the BIOS.
"""
import glob
import json
import os
import re
import time

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
    names = fan_names()
    for f in out:
        if names.get(f["id"]):
            f["label"] = names[f["id"]]
    return out


def fan_names():
    """Names found by the pwm test ({fan id: label}), kept in fans.json."""
    try:
        with open(config.FANS, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("names") or {}
    except (OSError, ValueError, AttributeError):
        return {}


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


def modprobe(driver, force_id=None, options=None):
    if driver not in FAN_DRIVERS:
        raise ValueError("not a fan driver: %s" % driver)
    args = ["modprobe", driver]
    if force_id:
        if not re.fullmatch(r"0x[0-9a-fA-F]{4}", force_id):
            raise ValueError("bad force_id")
        args.append("force_id=%s" % force_id)
    if options:
        if options not in CONFLICT_OPTS.values():
            raise ValueError("bad module option")
        args.append(options)
    return system.sh(args, timeout=60)


def unload(driver):
    if driver in FAN_DRIVERS:
        system.sh(["modprobe", "-r", driver], timeout=60)


def persist_driver(driver, force_id=None, options=None):
    """Load the driver at every boot (/etc/modules-load.d/eris-bot-fans.conf; its options in /etc/modprobe.d)."""
    have = (read(config.MODULES_LOAD, "") or "").split()
    lines = ["# fan controllers found by eris-bot (eris-bot removes this file on uninstall)"]
    lines += sorted(set([d for d in have if not d.startswith("#") and d in FAN_DRIVERS] + [driver]))
    os.makedirs(os.path.dirname(config.MODULES_LOAD), exist_ok=True)
    write(config.MODULES_LOAD, "\n".join(lines) + "\n")
    opts = " ".join(x for x in ("force_id=%s" % force_id if force_id else "", options or "") if x)
    if opts:
        os.makedirs(os.path.dirname(config.MODPROBE_D), exist_ok=True)
        write(config.MODPROBE_D, "# written by eris-bot\noptions %s %s\n" % (driver, opts))


# ---------------------------------------------------------------- finding the CPU fan controller by itself

# Super I/O drivers check the chip ID themselves and refuse to load on another chip, so trying them one
# by one is safe (unlike force_id). The usual chips of G41/ICH7 boards first.
TRY_ORDER = ("it87", "w83627ehf", "nct6775", "w83627hf", "f71882fg", "f71805f", "smsc47m1", "pc87360", "sch5627")
CONFLICT_RX = re.compile(r"resource conflict|conflicts with ACPI|ACPI region|acpi_enforce_resources", re.I)
# a module option that lets the driver share its ports with an ACPI claim of the BIOS
CONFLICT_OPTS = {"it87": "ignore_resource_conflict=1"}


def kernel_log(lines=60):
    r = system.sh(["dmesg"], timeout=20)
    return "\n".join((r.stdout or "").splitlines()[-lines:])


def module_loaded(driver):
    mods = read(system.P("/proc/modules"), "") or ""
    return any(line.split(" ", 1)[0] == driver for line in mods.splitlines())


def cpu_fans():
    return [f for f in fans() if f["kind"] == "cpu"]


def pwm_test(fan, settle=6.0, sleep=time.sleep):
    """Does this pwm really drive a fan? 100 %, then 35 %; a fan input that slows down by 15 % or more
    is the fan it drives. The pwm and pwm_enable are put back afterwards whatever happens."""
    inputs = sorted(glob.glob(os.path.join(fan["dir"], "fan*_input")))
    en0 = read(fan["enable"]) if fan.get("enable") else None
    pwm0 = read(fan["pwm"])
    res = {"ok": False, "input": None, "hi": None, "lo": None}
    try:
        if fan.get("enable"):
            write(fan["enable"], "1")
        write(fan["pwm"], pct_to_pwm(fan, 100))
        sleep(settle)
        hi = {i: read_int(i) or 0 for i in inputs}
        write(fan["pwm"], pct_to_pwm(fan, 35))
        sleep(settle)
        lo = {i: read_int(i) or 0 for i in inputs}
        best = max(inputs, key=lambda i: hi[i] - lo[i], default=None)
        if best and hi[best] > 0 and lo[best] <= hi[best] * 0.85:
            res.update(ok=True, input=os.path.basename(best).split("_")[0], hi=hi[best], lo=lo[best])
        elif best:
            res.update(hi=hi[best], lo=lo[best])
    except OSError as e:
        res["error"] = str(e)
    finally:
        try:
            if pwm0 is not None:
                write(fan["pwm"], pwm0)
            if fan.get("enable") and en0 is not None:
                write(fan["enable"], "2" if en0 == "1" else en0)
        except OSError:
            pass
    return res


def save_name(fan_id, label):
    try:
        with open(config.FANS, encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except (OSError, ValueError):
        data = {}
    data.setdefault("fans", {})
    data.setdefault("names", {})[fan_id] = label
    config.write_json(config.FANS, data, 0o600)


def auto_detect(progress=lambda text: None, settle=6.0, sleep=time.sleep):
    """Find a driver that gives the CPU/case fans a pwm, keep it for the next boots and test each pwm.
    Returns {"driver", "options", "fans": [(fan, test)], "steps": [...], "conflict": bool, "sensors": text}."""
    rep = {"driver": None, "options": None, "fans": [], "steps": [], "conflict": False, "sensors": None}
    cands = []
    if os.path.exists(config.SENSORS_DETECT):
        progress("sensors-detect --auto …")
        out, _ = sensors_detect()
        rep["sensors"] = out
        p = parse_sensors_detect(out)
        cands += p["fan_drivers"]
        rep["unknown_ids"] = p["unknown_ids"]
    cands += [d for d in TRY_ORDER if d not in cands]
    if cpu_fans():
        rep["steps"].append("قبلاً هم pwm برای فن CPU/کیس هست")
    for drv in cands:
        if cpu_fans():
            break
        was = module_loaded(drv)
        progress("امتحان درایور %s …" % drv)
        r = modprobe(drv)
        found = cpu_fans()
        log = (r.stderr or "") + "\n" + kernel_log()
        conflict = bool(CONFLICT_RX.search(log)) and drv in log
        if not found and drv in CONFLICT_OPTS and (conflict or r.returncode != 0):
            unload(drv)
            progress("%s با %s …" % (drv, CONFLICT_OPTS[drv]))
            r = modprobe(drv, options=CONFLICT_OPTS[drv])
            found = cpu_fans()
            if found:
                rep["options"] = CONFLICT_OPTS[drv]
        rep["conflict"] |= conflict
        if found:
            rep["driver"] = drv
            rep["steps"].append("✅ %s: %d خروجی pwm" % (drv, len(found)))
            break
        rep["steps"].append("✖️ %s: %s" % (drv, "تداخل با ACPI" if conflict else ("چیپ این درایور نیست" if r.returncode else "لود شد ولی pwm نداد")))
        if not was and module_loaded(drv):
            unload(drv)
    for f in cpu_fans():
        progress("تست %s (۱۰۰٪ و ۳۵٪) …" % f["label"])
        t = pwm_test(f, settle, sleep)
        rep["fans"].append((f, t))
        if t["ok"]:
            save_name(f["id"], "فن CPU/کیس %s (%s pwm%s)" % (t["input"], f["chip"], f["n"]))
    if rep["driver"]:
        persist_driver(rep["driver"], options=rep["options"])
    return rep


def acpi_lax():
    """Add acpi_enforce_resources=lax to the kernel command line (GRUB); works after a reboot.
    Returns (changed, message)."""
    text = read(config.GRUB)
    if text is None:
        return False, "%s نیست" % config.GRUB
    if "acpi_enforce_resources=lax" in text:
        return False, "از قبل تنظیم شده؛ سرور را ری‌استارت کنید"
    m = re.search(r'^GRUB_CMDLINE_LINUX_DEFAULT="([^"]*)"', text, re.M)
    if m:
        new = text[:m.start(1)] + (m.group(1) + " acpi_enforce_resources=lax").strip() + text[m.end(1):]
    else:
        new = text.rstrip("\n") + '\nGRUB_CMDLINE_LINUX_DEFAULT="acpi_enforce_resources=lax"\n'
    backup = config.GRUB + ".eris-bot-" + time.strftime("%Y%m%d-%H%M%S")
    write(backup, text + "\n")
    write(config.GRUB, new)
    r = system.sh(["update-grub"], timeout=300)
    if r.returncode != 0:
        write(config.GRUB, text + "\n")
        return False, "update-grub نشد؛ فایل برگشت: %s" % (r.stderr or r.stdout)[-300:]
    return True, "اضافه شد (نسخه‌ی قبلی: %s)؛ بعد از ری‌استارت سرور دوباره «پیدا کردن خودکار» را بزنید" % backup
ERISBOT_EOF_hw_py
  put_file "$STAGE/erisbot/marzban.py" 0644 << 'ERISBOT_EOF_marzban_py'
"""Marzban 0.8.4 API on this server: https://127.0.0.1:<UVICORN_PORT> as the .env admin.

The certificate is not valid for 127.0.0.1, so it is not checked for this address only; no proxy is used.
"""
import calendar
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

STATUSES = ("active", "disabled", "limited", "expired", "on_hold")
NOW_WINDOW = 120         # "connected right now" = traffic (online_at) in the last 2 minutes


class MarzbanError(Exception):
    def __init__(self, msg, code=None):
        Exception.__init__(self, msg)
        self.code = code


def read_env(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def utc_epoch(s):
    """'2026-11-04T09:57:30.375208' (UTC without Z) -> epoch."""
    if not s:
        return None
    s = s.rstrip("Z").split("+")[0]
    base = s.split(".")[0]
    try:
        t = time.strptime(base, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return calendar.timegm(t)


def online_now(users, now=None):
    now = now or time.time()
    out = []
    for u in users:
        t = utc_epoch(u.get("online_at"))
        if t is not None and now - t <= NOW_WINDOW:
            out.append(u.get("username") or "?")
    return sorted(out)


class Marzban:
    def __init__(self, env_path=None):
        self.env_path = env_path or config.MARZBAN_ENV
        self.token = None
        self.lock = threading.Lock()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))

    def available(self):
        return os.path.isfile(self.env_path)

    def env(self):
        return read_env(self.env_path)

    def base(self):
        e = self.env()
        scheme = "https" if e.get("UVICORN_SSL_CERTFILE") else "http"
        return "%s://127.0.0.1:%s" % (scheme, e.get("UVICORN_PORT", "8000"))

    def _open(self, method, path, body=None, form=None, timeout=20):
        headers = {"Accept": "application/json"}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.token and form is None:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.base() + path, data=data, headers=headers, method=method)
        with self.opener.open(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def login(self):
        e = self.env()
        r = self._open("POST", "/api/admin/token", form={"username": e.get("SUDO_USERNAME", ""),
                                                          "password": e.get("SUDO_PASSWORD", "")})
        self.token = (r or {}).get("access_token")
        if not self.token:
            raise MarzbanError("login failed")

    def req(self, method, path, body=None, query=None):
        if query:
            q = {k: v for k, v in query.items() if v not in (None, "")}
            if q:
                path += "?" + urllib.parse.urlencode(q, doseq=True)
        with self.lock:
            for attempt in (1, 2):
                try:
                    if not self.token:
                        self.login()
                    return self._open(method, path, body=body)
                except urllib.error.HTTPError as e:
                    if e.code == 401 and attempt == 1:
                        self.token = None
                        continue
                    try:
                        detail = json.loads(e.read() or b"{}").get("detail")
                    except (ValueError, AttributeError, OSError):
                        detail = None
                    raise MarzbanError("HTTP %d%s" % (e.code, ": %s" % detail if detail else ""), e.code)
                except (OSError, ValueError) as e:
                    self.token = None
                    raise MarzbanError("no answer from %s (%s)" % (self.base(), getattr(e, "reason", e)))
        raise MarzbanError("login failed")

    # ------------------------------------------------------------ API
    def system(self):
        return self.req("GET", "/api/system") or {}

    def users(self, offset=None, limit=None, search=None, status=None):
        return self.req("GET", "/api/users", query={"offset": offset, "limit": limit, "search": search,
                                                    "status": status}) or {"users": [], "total": 0}

    def all_users(self):
        return (self.users() or {}).get("users", [])

    def user(self, name):
        return self.req("GET", "/api/user/%s" % urllib.parse.quote(name, safe=""))

    def modify(self, name, **fields):
        return self.req("PUT", "/api/user/%s" % urllib.parse.quote(name, safe=""), body=fields)

    def reset(self, name):
        return self.req("POST", "/api/user/%s/reset" % urllib.parse.quote(name, safe=""))

    def delete(self, name):
        """DELETE; Marzban 0.8.4 answers 500 for users made by the .env admin but deletes them anyway,
        so the answer that counts is a GET afterwards (404 = gone)."""
        err = None
        try:
            self.req("DELETE", "/api/user/%s" % urllib.parse.quote(name, safe=""))
        except MarzbanError as e:
            err = e
        try:
            self.user(name)
        except MarzbanError as e:
            if e.code == 404:
                return True
            raise
        raise err or MarzbanError("the user is still there")

    def create(self, username, inbounds, expire=None, data_limit=0, note=""):
        """inbounds: {protocol: [tags]}"""
        body = {"username": username, "proxies": {p: {} for p, tags in inbounds.items() if tags},
                "inbounds": {p: tags for p, tags in inbounds.items() if tags},
                "expire": expire, "data_limit": data_limit or 0, "status": "active", "note": note}
        return self.req("POST", "/api/user", body=body)

    def inbounds(self):
        return self.req("GET", "/api/inbounds") or {}

    def nodes(self):
        return self.req("GET", "/api/nodes") or []

    def reconnect(self, node_id):
        return self.req("POST", "/api/node/%d/reconnect" % int(node_id))

    def core(self):
        return self.req("GET", "/api/core") or {}

    def today_usage(self, now=None):
        """(uplink, downlink) since local midnight (Marzban keeps UTC)."""
        now = now or time.time()
        lt = time.localtime(now)
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        start = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(midnight))
        r = self.req("GET", "/api/nodes/usage", query={"start": start}) or {}
        up = sum(u.get("uplink") or 0 for u in r.get("usages", []))
        down = sum(u.get("downlink") or 0 for u in r.get("usages", []))
        return up, down

    # ------------------------------------------------------------ changes made by the bot
    def extend(self, name, days):
        u = self.user(name)
        if not u.get("expire"):
            return u, False
        base = max(time.time(), u["expire"])
        return self.modify(name, expire=int(base + days * 86400)), True

    def add_data(self, name, gib):
        u = self.user(name)
        if not u.get("data_limit"):
            return u, False
        return self.modify(name, data_limit=int(u["data_limit"] + gib * 1024 ** 3)), True

    def set_status(self, name, status):
        return self.modify(name, status=status)
ERISBOT_EOF_marzban_py
  put_file "$STAGE/erisbot/nas.py" 0644 << 'ERISBOT_EOF_nas_py'
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
ERISBOT_EOF_nas_py
  put_file "$STAGE/erisbot/nasop.py" 0644 << 'ERISBOT_EOF_nasop_py'
"""One file operation on the NAS, run AS the logged-in user (setpriv drops root before this starts),
so the system's own permissions decide what is allowed. Standard library only; run with python3 -I.

    nasop.py ROOT OP ARGS...      ROOT: the shared folder every path must stay inside
    list PATH | stat PATH | mkdir DIR NAME | put DIR NAME (data on stdin) | get PATH (data to stdout)
    delete PATH | rename PATH NEWNAME
Answers one JSON object on stdout (except get), {"error": "..."} on failure (exit code 1).
"""
import json
import os
import stat
import sys

BAD_NAMES = ("", ".", "..")


def fail(msg):
    sys.stdout.write(json.dumps({"error": msg}, ensure_ascii=False))
    sys.exit(1)


def is_inside(root, path):
    real_root = os.path.realpath(root)
    real = os.path.realpath(path)
    return real == real_root or real.startswith(real_root.rstrip("/") + "/")


def inside(root, path):
    """The real path of `path` when it is root or under it (symlinks resolved), else fail."""
    if not is_inside(root, path):
        fail("outside the shared folder")
    return os.path.realpath(path)


def check_name(name):
    if name in BAD_NAMES or "/" in name or "\0" in name or len(name.encode("utf-8")) > 255:
        fail("bad name")
    return name


def entry(path, name):
    st = os.lstat(os.path.join(path, name))
    return {"name": name, "size": st.st_size, "mtime": int(st.st_mtime),
            "dir": stat.S_ISDIR(st.st_mode), "link": stat.S_ISLNK(st.st_mode)}


def op_list(root, path):
    p = inside(root, path)
    try:
        names = os.listdir(p)
    except PermissionError:
        fail("permission denied")
    except FileNotFoundError:
        fail("not found")
    items = []
    for n in names:
        try:
            e = entry(p, n)
            if e["link"]:
                if not is_inside(root, os.path.join(p, n)):
                    continue                       # a link out of the shared folder is not shown
                try:
                    t = os.stat(os.path.join(p, n))
                except OSError:
                    continue
                e.update(dir=stat.S_ISDIR(t.st_mode), size=t.st_size)
            items.append(e)
        except OSError:
            continue
    return {"path": p, "items": items, "writable": os.access(p, os.W_OK | os.X_OK)}


def op_stat(root, path):
    p = inside(root, path)
    try:
        st = os.stat(p)
    except PermissionError:
        fail("permission denied")
    except FileNotFoundError:
        fail("not found")
    return {"path": p, "size": st.st_size, "mtime": int(st.st_mtime), "dir": stat.S_ISDIR(st.st_mode),
            "readable": os.access(p, os.R_OK), "writable": os.access(os.path.dirname(p), os.W_OK | os.X_OK)}


def op_mkdir(root, d, name):
    p = os.path.join(inside(root, d), check_name(name))
    try:
        os.mkdir(p, 0o775)
    except FileExistsError:
        fail("already exists")
    except PermissionError:
        fail("permission denied")
    return {"path": p}


def free_name(d, name):
    base, ext = os.path.splitext(name)
    cand, i = name, 2
    while os.path.lexists(os.path.join(d, cand)):
        cand = "%s (%d)%s" % (base, i, ext)
        i += 1
    return cand


def op_put(root, d, name):
    d = inside(root, d)
    name = free_name(d, check_name(name))
    final = os.path.join(d, name)
    tmp = os.path.join(d, ".%s.eris-bot-upload" % name)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o664)
    except PermissionError:
        fail("permission denied")
    except FileExistsError:
        fail("an upload with this name is already running")
    size = 0
    try:
        with os.fdopen(fd, "wb") as f:
            while True:
                chunk = sys.stdin.buffer.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                size += len(chunk)
        os.rename(tmp, final)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return {"path": final, "name": name, "size": size}


def op_get(root, path):
    p = inside(root, path)
    try:
        f = open(p, "rb")
    except PermissionError:
        fail("permission denied")
    except (FileNotFoundError, IsADirectoryError):
        fail("not a file")
    with f:
        out = sys.stdout.buffer
        while True:
            chunk = f.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
        out.flush()
    sys.exit(0)


def op_delete(root, path):
    p = inside(root, path)
    if p == os.path.realpath(root):
        fail("the shared folder itself cannot be deleted")
    try:
        if os.path.isdir(p) and not os.path.islink(p):
            os.rmdir(p)
        else:
            os.remove(p)
    except OSError as e:
        fail("not empty" if e.errno == 39 else ("permission denied" if isinstance(e, PermissionError) else str(e)))
    return {"deleted": p}


def op_rename(root, path, new):
    p = inside(root, path)
    if p == os.path.realpath(root):
        fail("the shared folder itself cannot be renamed")
    dest = os.path.join(os.path.dirname(p), check_name(new))
    if os.path.lexists(dest):
        fail("already exists")
    try:
        os.rename(p, dest)
    except PermissionError:
        fail("permission denied")
    return {"path": dest}


OPS = {"list": (op_list, 1), "stat": (op_stat, 1), "mkdir": (op_mkdir, 2), "put": (op_put, 2), "get": (op_get, 1),
       "delete": (op_delete, 1), "rename": (op_rename, 2)}


def main(argv):
    if len(argv) < 3 or argv[2] not in OPS:
        fail("usage: nasop.py ROOT OP ARGS")
    fn, n = OPS[argv[2]]
    args = argv[3:]
    if len(args) != n:
        fail("wrong arguments")
    sys.stdout.write(json.dumps(fn(argv[1], *args), ensure_ascii=False))


if __name__ == "__main__":
    os.umask(0o002)
    main(sys.argv)
ERISBOT_EOF_nasop_py
  put_file "$STAGE/erisbot/net.py" 0644 << 'ERISBOT_EOF_net_py'
"""HTTP(S) over a direct connection, an HTTP proxy (CONNECT) or a SOCKS5 proxy (DNS on the proxy side).

urllib has no SOCKS, so the tunnels are made here with plain sockets and handed to http.client.
"""
import base64
import http.client
import ipaddress
import socket
import ssl
import struct
import time
import urllib.parse


class ProxyError(OSError):
    pass


def parse_proxy(url):
    """None for a direct connection, else {scheme, host, port, user, password}."""
    if not url or url in ("off", "direct", "none"):
        return None
    u = urllib.parse.urlsplit(url)
    scheme = u.scheme.lower()
    if scheme not in ("http", "socks5", "socks5h"):
        raise ValueError("unknown proxy type %s (http:// or socks5://)" % scheme)
    if not u.hostname or not u.port:
        raise ValueError("a proxy needs host:port")
    return {"scheme": "socks5" if scheme.startswith("socks5") else "http", "host": u.hostname, "port": u.port,
            "user": urllib.parse.unquote(u.username) if u.username else None,
            "password": urllib.parse.unquote(u.password) if u.password else None}


def describe(url):
    """The proxy without its password, for logs and messages."""
    if not url or url in ("off", "direct"):
        return "direct"
    p = parse_proxy(url)
    auth = (p["user"] + ":***@") if p["user"] else ""
    return "%s://%s%s:%d" % (p["scheme"], auth, p["host"], p["port"])


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ProxyError("the proxy closed the connection")
        buf += chunk
    return buf


def _http_connect(sock, host, port, p):
    target = "%s:%d" % (("[%s]" % host) if ":" in host else host, port)
    req = "CONNECT %s HTTP/1.1\r\nHost: %s\r\n" % (target, target)
    if p["user"] is not None:
        cred = base64.b64encode(("%s:%s" % (p["user"], p["password"] or "")).encode()).decode()
        req += "Proxy-Authorization: Basic %s\r\n" % cred
    sock.sendall((req + "\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            raise ProxyError("the HTTP proxy closed the connection")
        head += chunk
        if len(head) > 65536:
            raise ProxyError("the HTTP proxy answer is too long")
    status = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = status.split()
    if len(parts) < 2 or parts[1] != "200":
        raise ProxyError("the HTTP proxy refused CONNECT: %s" % status)


def _socks5_connect(sock, host, port, p):
    methods = [0x00] + ([0x02] if p["user"] is not None else [])
    sock.sendall(bytes([5, len(methods)] + methods))
    ver, method = _recv_exact(sock, 2)
    if ver != 5 or method == 0xFF:
        raise ProxyError("the SOCKS5 proxy accepts none of our login methods")
    if method == 0x02:
        u, pw = (p["user"] or "").encode(), (p["password"] or "").encode()
        sock.sendall(bytes([1, len(u)]) + u + bytes([len(pw)]) + pw)
        if _recv_exact(sock, 2)[1] != 0:
            raise ProxyError("the SOCKS5 proxy refused the user name or password")
    elif method != 0x00:
        raise ProxyError("unknown SOCKS5 method %d" % method)
    try:
        ip = ipaddress.ip_address(host)
        addr = (b"\x01" + ip.packed) if ip.version == 4 else (b"\x04" + ip.packed)
    except ValueError:                      # a name: the proxy resolves it (no DNS leak, no filtered DNS)
        name = host.encode("idna")
        addr = b"\x03" + bytes([len(name)]) + name
    sock.sendall(b"\x05\x01\x00" + addr + struct.pack(">H", port))
    ver, rep, _, atyp = _recv_exact(sock, 4)
    if rep != 0:
        reasons = {1: "general failure", 2: "not allowed", 3: "network unreachable", 4: "host unreachable",
                   5: "connection refused", 6: "TTL expired", 7: "command not supported", 8: "address type not supported"}
        raise ProxyError("the SOCKS5 proxy could not connect: %s" % reasons.get(rep, rep))
    if atyp == 1:
        _recv_exact(sock, 4 + 2)
    elif atyp == 4:
        _recv_exact(sock, 16 + 2)
    elif atyp == 3:
        _recv_exact(sock, _recv_exact(sock, 1)[0] + 2)


def open_socket(host, port, proxy=None, timeout=15.0):
    """A TCP socket to host:port, through the proxy URL (or direct)."""
    p = parse_proxy(proxy) if isinstance(proxy, str) or proxy is None else proxy
    if p is None:
        return socket.create_connection((host, port), timeout=timeout)
    sock = socket.create_connection((p["host"], p["port"]), timeout=timeout)
    try:
        sock.settimeout(timeout)
        if p["scheme"] == "http":
            _http_connect(sock, host, port, p)
        else:
            _socks5_connect(sock, host, port, p)
    except BaseException:
        sock.close()
        raise
    return sock


_SSL = None


def ssl_context():
    global _SSL
    if _SSL is None:
        _SSL = ssl.create_default_context()
    return _SSL


class Connection(http.client.HTTPConnection):
    """http.client connection whose socket comes from open_socket() (and TLS for https)."""

    def __init__(self, url, proxy=None, timeout=15.0, context=None):
        u = urllib.parse.urlsplit(url)
        self.tls = u.scheme == "https"
        port = u.port or (443 if self.tls else 80)
        http.client.HTTPConnection.__init__(self, u.hostname, port, timeout=timeout)
        self.proxy = proxy
        self.context = context

    def connect(self):
        sock = open_socket(self.host, self.port, self.proxy, self.timeout)
        if self.tls:
            ctx = self.context or ssl_context()
            sock = ctx.wrap_socket(sock, server_hostname=self.host)
        self.sock = sock


class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body


def request(method, url, proxy=None, body=None, headers=None, timeout=15.0, read_timeout=None,
            conn=None, context=None):
    """One HTTP request; returns Response. `conn` (a Connection) is reused when given."""
    u = urllib.parse.urlsplit(url)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    own = conn is None
    c = conn or Connection(url, proxy, timeout, context)
    try:
        c.request(method, path, body=body, headers=headers or {})
        if c.sock is not None and read_timeout:
            c.sock.settimeout(read_timeout)
        r = c.getresponse()
        data = r.read()
        return Response(r.status, dict(r.getheaders()), data)
    finally:
        if own:
            c.close()


def probe(url, proxy, timeout=10.0):
    """(ok, seconds, error) for one GET through the proxy."""
    t0 = time.monotonic()
    try:
        r = request("GET", url, proxy=proxy, timeout=timeout, read_timeout=timeout)
        return r.status < 500, time.monotonic() - t0, None if r.status < 500 else "HTTP %d" % r.status
    except (OSError, http.client.HTTPException, ValueError) as e:
        return False, time.monotonic() - t0, str(e) or type(e).__name__
ERISBOT_EOF_net_py
  put_file "$STAGE/erisbot/perf.py" 0644 << 'ERISBOT_EOF_perf_py'
"""Performance profiles: fan curves, cpufreq governor, min frequency and vm.swappiness.

A profile only touches what this server really has; the rest is reported as "not available here".
Raising the FSB or the voltage (real overclocking) is only possible from the BIOS and is never done here.
"""
import os

from . import config, fand, hw

PROFILES = {
    "quiet": {"title": "🤫 بی‌صدا", "fan": "quiet", "governor": ["powersave"], "swappiness": None, "minmax": False},
    "balanced": {"title": "⚖️ متعادل", "fan": "normal", "governor": ["schedutil", "ondemand"], "swappiness": None,
                 "minmax": False},
    "performance": {"title": "🚀 عملکرد", "fan": "steep", "governor": ["performance"], "swappiness": 10,
                    "minmax": False},
    "max": {"title": "🔥 حداکثر", "fan": "full", "governor": ["performance"], "swappiness": 10, "minmax": True},
}
DEFAULT = "balanced"
FAN_CHOICES = {"auto": "🔁 خودکار (BIOS)", "quiet": "منحنی آرام", "normal": "منحنی معمولی", "steep": "منحنی تند",
               "full": "۱۰۰٪"}
ORIG = os.path.join(config.STATE, "perf-orig.json")


def load():
    d = config.read_json(config.PROFILE, {}) or {}
    d.setdefault("active", None)
    d.setdefault("custom", {})
    return d


def save(d):
    config.write_json(config.PROFILE, d, 0o600)


def all_profiles():
    out = dict(PROFILES)
    for name, p in (load().get("custom") or {}).items():
        out["c:" + name] = {"title": "⭐ " + name, "fan": p.get("fan", "normal"),
                            "governor": [p["governor"]] if p.get("governor") else [],
                            "swappiness": p.get("swappiness"), "minmax": bool(p.get("minmax")), "custom": True}
    return out


def record_orig():
    o = config.read_json(ORIG, None)
    if o:
        return o
    o = {"governor": hw.governor(), "swappiness": hw.swappiness(),
         "min_freq": {d: lim[0] for d, lim in hw.freq_limits().items()}}
    config.write_json(ORIG, o, 0o600)
    return o


def fan_settings(choice):
    if choice == "full":
        return {"mode": "manual", "percent": 100}
    if choice in fand.CURVES:
        return {"mode": "curve", "curve": fand.CURVES[choice], "curve_name": choice}
    return {"mode": "auto"}


def availability(p):
    """[(what, available, note)] for the profile page."""
    fans = hw.fans()
    out = [("فن‌ها (%s)" % FAN_CHOICES.get(p["fan"], p["fan"]), bool(fans),
            "%d فن" % len(fans) if fans else "هیچ pwm پیدا نشد")]
    if p["governor"]:
        g = [x for x in p["governor"] if x in hw.governors()]
        out.append(("governor %s" % "/".join(p["governor"]), bool(g),
                    g[0] if g else ("cpufreq نیست" if not hw.has_cpufreq() else "این governor نیست")))
    if p.get("minmax"):
        out.append(("حداقل فرکانس = حداکثر", hw.has_cpufreq(), "" if hw.has_cpufreq() else "cpufreq نیست"))
    if p.get("swappiness") is not None:
        out.append(("vm.swappiness=%d" % p["swappiness"], hw.swappiness() is not None, ""))
    return out


def _system(p, orig, changes, unavailable):
    if p["governor"]:
        if not hw.has_cpufreq():
            unavailable.append("cpufreq / governor (در دسترس نیست روی این سرور)")
        else:
            g = next((x for x in p["governor"] if x in hw.governors()), None)
            cur = hw.governor()
            if g is None:
                unavailable.append("governor %s (در دسترس نیست روی این سرور)" % "/".join(p["governor"]))
            elif g != cur:
                hw.set_governor(g)
                changes.append("governor: از %s به %s" % (cur, g))
    elif hw.has_cpufreq() and orig.get("governor") and hw.governor() != orig["governor"]:
        cur = hw.governor()
        hw.set_governor(orig["governor"])
        changes.append("governor: از %s به %s" % (cur, orig["governor"]))
    if hw.has_cpufreq():
        for d, (mn, mx, hwmin, hwmax) in hw.freq_limits().items():
            want = hwmax if p.get("minmax") else (orig.get("min_freq") or {}).get(d) or hwmin
            if want and mn != want:
                hw.set_min_freq(d, want)
                changes.append("%s: حداقل فرکانس از %d به %d MHz" % (os.path.basename(os.path.dirname(d)),
                                                                       (mn or 0) // 1000, want // 1000))
    elif p.get("minmax"):
        unavailable.append("حداقل فرکانس = حداکثر (cpufreq نیست)")
    want = p.get("swappiness")
    if want is None:
        want = orig.get("swappiness")
    cur = hw.swappiness()
    if want is not None and cur is not None and cur != want:
        hw.set_swappiness(want)
        changes.append("vm.swappiness: از %d به %d" % (cur, want))


def apply(name):
    """Switch to a profile. Returns {"changes": [...], "unavailable": [...]}."""
    profiles = all_profiles()
    if name not in profiles:
        raise ValueError("unknown profile %s" % name)
    p = profiles[name]
    orig = record_orig()
    changes, unavailable = [], []
    fans = hw.fans()
    if not fans:
        unavailable.append("کنترل فن (هیچ pwm قابل کنترلی پیدا نشد)")
    else:
        cfg = fand.load_cfg()
        for f in fans:
            old = fand.fan_cfg(cfg, f["id"])
            new = fan_settings(p["fan"])
            cfg["fans"][f["id"]] = new
            nd = fand.describe_cfg(fand.fan_cfg(cfg, f["id"]))
            if fand.describe_cfg(old) != nd:
                changes.append("%s: از %s به %s" % (f["label"], fand.describe_cfg(old), nd))
        fand.save_cfg(cfg)
    _system(p, orig, changes, unavailable)
    d = load()
    d["active"] = name
    save(d)
    return {"changes": changes, "unavailable": unavailable}


def apply_saved_system():
    """At boot (eris-fan.service start): the CPU settings of the saved profile (fans.json keeps the fans)."""
    name = load().get("active")
    profiles = all_profiles()
    if not name or name not in profiles:
        return None
    changes, unavailable = [], []
    _system(profiles[name], record_orig(), changes, unavailable)
    return changes


def reset_default():
    """Everything back to how the system was before: governor, min frequency, swappiness, fans auto."""
    orig = config.read_json(ORIG, None) or {}
    changes = []
    if hw.has_cpufreq():
        if orig.get("governor") and hw.governor() != orig["governor"]:
            cur = hw.governor()
            hw.set_governor(orig["governor"])
            changes.append("governor: از %s به %s" % (cur, orig["governor"]))
        for d, (mn, _, hwmin, _) in hw.freq_limits().items():
            want = (orig.get("min_freq") or {}).get(d) or hwmin
            if want and mn != want:
                hw.set_min_freq(d, want)
                changes.append("%s: حداقل فرکانس %d MHz" % (os.path.basename(os.path.dirname(d)), want // 1000))
    if orig.get("swappiness") is not None and hw.swappiness() not in (None, orig["swappiness"]):
        cur = hw.swappiness()
        hw.set_swappiness(orig["swappiness"])
        changes.append("vm.swappiness: از %d به %d" % (cur, orig["swappiness"]))
    cfg = fand.load_cfg()
    for fid, fc in list(cfg["fans"].items()):
        if fc.get("mode") != "auto":
            changes.append("%s: 🔁 خودکار" % fid)
        cfg["fans"][fid] = {"mode": "auto"}
    fand.save_cfg(cfg)
    d = load()
    d["active"] = None
    save(d)
    return changes


def save_custom(name, fan, governor, swappiness):
    if not name or len(name) > 24:
        raise ValueError("bad name")
    d = load()
    d["custom"][name] = {"fan": fan, "governor": governor or None, "swappiness": swappiness}
    save(d)


def delete_custom(name):
    d = load()
    d["custom"].pop(name, None)
    if d.get("active") == "c:" + name:
        d["active"] = None
    save(d)
ERISBOT_EOF_perf_py
  put_file "$STAGE/erisbot/qr.py" 0644 << 'ERISBOT_EOF_qr_py'
"""A small QR code encoder (byte mode, versions 1-40, ECC L/M/Q/H) and a PNG writer, pure Python.

Follows ISO/IEC 18004: Reed-Solomon over GF(256) (0x11D), block interleaving, the 8 masks with the
standard penalty rules, BCH-coded format and version information.
"""
import struct
import zlib

ECL = {"L": (0, 1), "M": (1, 0), "Q": (2, 3), "H": (3, 2)}   # name -> (table row, format bits)

ECC_CODEWORDS_PER_BLOCK = (
    (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
)
NUM_ERROR_CORRECTION_BLOCKS = (
    (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
    (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
    (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
    (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),
)

MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def raw_modules(ver):
    n = (16 * ver + 128) * ver + 64
    if ver >= 2:
        a = ver // 7 + 2
        n -= (25 * a - 10) * a - 55
        if ver >= 7:
            n -= 36
    return n


def data_codewords(ver, ecl):
    row = ECL[ecl][0]
    return raw_modules(ver) // 8 - ECC_CODEWORDS_PER_BLOCK[row][ver] * NUM_ERROR_CORRECTION_BLOCKS[row][ver]


def gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def rs_divisor(degree):
    res = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            res[j] = gf_mul(res[j], root)
            if j + 1 < degree:
                res[j] ^= res[j + 1]
        root = gf_mul(root, 0x02)
    return res


def rs_remainder(data, divisor):
    res = [0] * len(divisor)
    for b in data:
        factor = b ^ res.pop(0)
        res.append(0)
        for i, coef in enumerate(divisor):
            res[i] ^= gf_mul(coef, factor)
    return res


class QR:
    def __init__(self, data, ecl="M", version=None, mask=None):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.ecl = ecl
        for ver in ([version] if version else range(1, 41)):
            cc = 8 if ver < 10 else 16
            if 4 + cc + 8 * len(data) <= data_codewords(ver, ecl) * 8:
                break
        else:
            raise ValueError("too long for a QR code")
        self.version = ver
        self.size = ver * 4 + 17
        bits = []

        def put(val, n):
            bits.extend((val >> i) & 1 for i in reversed(range(n)))
        put(0b0100, 4)
        put(len(data), cc)
        for b in data:
            put(b, 8)
        cap = data_codewords(ver, ecl) * 8
        put(0, min(4, cap - len(bits)))
        put(0, -len(bits) % 8)
        pad = 0xEC
        while len(bits) < cap:
            put(pad, 8)
            pad ^= 0xEC ^ 0x11
        codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]
        self._function_patterns()
        self._draw_codewords(self._interleave(codewords))
        if mask is None:
            best, best_pen = 0, None
            for m in range(8):
                self._apply_mask(m)
                self._format_bits(m)
                pen = self._penalty()
                if best_pen is None or pen < best_pen:
                    best, best_pen = m, pen
                self._apply_mask(m)
            mask = best
        self.mask = mask
        self._apply_mask(mask)
        self._format_bits(mask)

    # ------------------------------------------------------------ function patterns
    def _set(self, x, y, dark):
        self.modules[y][x] = dark
        self.function[y][x] = True

    def _function_patterns(self):
        s = self.size
        for i in range(s):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for (cx, cy) in ((3, 3), (s - 4, 3), (3, s - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < s and 0 <= y < s:
                        self._set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = self._alignment_positions()
        n = len(pos)
        for i in range(n):
            for j in range(n):
                if (i == 0 and j == 0) or (i == 0 and j == n - 1) or (i == n - 1 and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(pos[i] + dx, pos[j] + dy, max(abs(dx), abs(dy)) != 1)
        self._format_bits(0)
        if self.version >= 7:
            rem = self.version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.version << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = s - 11 + i % 3, i // 3
                self._set(a, b, bit)
                self._set(b, a, bit)

    def _alignment_positions(self):
        ver = self.version
        if ver == 1:
            return []
        n = ver // 7 + 2
        step = 26 if ver == 32 else (ver * 4 + n * 2 + 1) // (n * 2 - 2) * 2
        return list(reversed([self.size - 7 - i * step for i in range(n - 1)] + [6]))

    def _format_bits(self, mask):
        data = ECL[self.ecl][1] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 == 1  # noqa: E731
        s = self.size
        for i in range(0, 6):
            self._set(8, i, bit(i))
        self._set(8, 7, bit(6))
        self._set(8, 8, bit(7))
        self._set(7, 8, bit(8))
        for i in range(9, 15):
            self._set(14 - i, 8, bit(i))
        for i in range(0, 8):
            self._set(s - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self._set(8, s - 15 + i, bit(i))
        self._set(8, s - 8, True)

    # ------------------------------------------------------------ data
    def _interleave(self, data):
        row = ECL[self.ecl][0]
        nblocks = NUM_ERROR_CORRECTION_BLOCKS[row][self.version]
        ecclen = ECC_CODEWORDS_PER_BLOCK[row][self.version]
        raw = raw_modules(self.version) // 8
        nshort = nblocks - raw % nblocks
        shortlen = raw // nblocks
        div = rs_divisor(ecclen)
        blocks, k = [], 0
        for i in range(nblocks):
            dat = data[k:k + shortlen - ecclen + (0 if i < nshort else 1)]
            k += len(dat)
            ecc = rs_remainder(dat, div)
            if i < nshort:
                dat = dat + [0]
            blocks.append(dat + ecc)
        out = []
        for i in range(len(blocks[0])):
            for j, blk in enumerate(blocks):
                if i != shortlen - ecclen or j >= nshort:
                    out.append(blk[i])
        return out

    def _draw_codewords(self, data):
        s = self.size
        i = 0
        right = s - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(s):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = (s - 1 - vert) if upward else vert
                    if not self.function[y][x] and i < len(data) * 8:
                        self.modules[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def _apply_mask(self, m):
        f = MASKS[m]
        for y in range(self.size):
            for x in range(self.size):
                if not self.function[y][x] and f(x, y):
                    self.modules[y][x] = not self.modules[y][x]

    def _penalty(self):
        s, M = self.size, self.modules
        pen = 0
        lines = [M[y] for y in range(s)] + [[M[y][x] for y in range(s)] for x in range(s)]
        pat1 = [True, False, True, True, True, False, True, False, False, False, False]
        pat2 = list(reversed(pat1))
        for line in lines:
            run, prev = 0, None
            for v in line:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        pen += 3 + run - 5
                    run, prev = 1, v
            if run >= 5:
                pen += 3 + run - 5
            for i in range(s - 10):
                seg = line[i:i + 11]
                if seg == pat1 or seg == pat2:
                    pen += 40
        for y in range(s - 1):
            for x in range(s - 1):
                c = M[y][x]
                if c == M[y][x + 1] == M[y + 1][x] == M[y + 1][x + 1]:
                    pen += 3
        dark = sum(sum(1 for v in row if v) for row in M)
        total = s * s
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        pen += k * 10
        return pen

    # ------------------------------------------------------------ output
    def png(self, scale=8, border=4):
        dim = (self.size + 2 * border) * scale
        white, black = b"\xff", b"\x00"
        raw = bytearray()
        blank = b"\x00" + white * dim
        for _ in range(border * scale):
            raw += blank
        for row in self.modules:
            line = bytearray(b"\x00")
            line += white * (border * scale)
            for v in row:
                line += (black if v else white) * scale
            line += white * (border * scale)
            for _ in range(scale):
                raw += line
        for _ in range(border * scale):
            raw += blank

        def chunk(tag, body):
            return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", dim, dim, 8, 0, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))

    def text(self):
        return "\n".join("".join("██" if v else "  " for v in row) for row in self.modules)


def png(data, ecl="M"):
    return QR(data, ecl).png()
ERISBOT_EOF_qr_py
  put_file "$STAGE/erisbot/s_ai.py" 0644 << 'ERISBOT_EOF_s_ai_py'
"""🤖 Claude and Codex: plan usage, model and effort, and chatting (each message continues the chat)."""
import os
import threading
import time

from . import ai, config, store, tg
from .ui import DANGER, PRIMARY, SUCCESS, Btn, Screen
from .util import bar, duration, esc, fa, log

CHAT_TTL = 6 * 3600
PROVIDERS = {"claude": "Claude", "codex": "Codex"}


def register(app):
    st = store.JsonState(os.path.join(config.STATE, "ai.json"), {})
    usage = ai.Usage()
    jobs = {}
    jobs_lock = threading.Lock()
    app.ai_jobs = jobs

    def conf():
        return app.conf.get("ai") or config.DEFAULTS["ai"]

    def prefs(chat_id):
        p = (st.get("chats") or {}).get(str(chat_id)) or {}
        c = p.setdefault("claude", {})
        c.setdefault("model", "sonnet")
        c.setdefault("effort", "high")
        x = p.setdefault("codex", {})
        if not x.get("model"):
            x["model"] = ai.codex_default_model()
        x.setdefault("effort", "medium")
        p.setdefault("provider", "claude")
        return p

    def save_prefs(chat_id, p):
        chats = st.get("chats") or {}
        chats[str(chat_id)] = p
        st.set("chats", chats)

    def model_label(provider, p):
        q = p[provider]
        if provider == "claude":
            name = ai.claude_name(q["model"])
        else:
            name = dict((m[0], m[1]) for m in ai.codex_models()).get(q["model"], q["model"]) or "پیش‌فرض"
        return "%s · %s" % (name, ai.EFFORT_FA.get(q.get("effort"), q.get("effort") or "پیش‌فرض"))

    def window_lines(info):
        out = []
        for w in info.get("windows") or []:
            pct = max(0.0, min(100.0, w["pct"]))
            reset = ""
            if w.get("reset"):
                left = w["reset"] - time.time()
                reset = " · ریست %s" % ("تا " + duration(left, 2) if left > 0 else "الان")
            icon = "🔴" if pct >= 90 else ("🟠" if pct >= 70 else "🟢")
            out.append("  %s %s: <code>%s</code> %s٪%s" % (icon, w["label"], bar(pct, 10), fa("%.0f" % pct), reset))
        return out

    def usage_text(u):
        lines = []
        for key, title in (("claude", "🟣 <b>Claude</b>"), ("codex", "🟢 <b>Codex</b>")):
            info = u.get(key) or {}
            head = title + (" — %s" % esc(info["plan"]) if info.get("plan") else "")
            if info.get("login") is False:
                lines.append(head + ": ❌ وارد نشده (در Claude F Web، تنظیمات ← حساب‌ها)")
                continue
            lines.append(head)
            wl = window_lines(info)
            lines += wl or ["  " + esc(info.get("note") or "عددی نیست")]
            if info.get("error"):
                lines.append("  ⚠️ %s" % esc(info["error"]))
            if info.get("asof") and time.time() - info["asof"] > 900:
                lines.append("  <i>(به‌روز تا %s پیش)</i>" % duration(time.time() - info["asof"], 1))
        return lines

    # -------------------------------------------------------- the main screen
    @app.action("ai")
    def main(ctx, notice=None, force=False):
        ctx.session()["ai_chat"] = None
        p = prefs(ctx.chat_id)
        lines = ["🤖 <b>هوش مصنوعی</b>", ""] + usage_text(usage.get(force)) + [
            "", "⚙️ Claude: %s" % esc(model_label("claude", p)),
            "⚙️ Codex: %s" % esc(model_label("codex", p)),
            "🌐 مسیر: %s" % esc(ai.proxy_note()),
            "", "💬 چت: هر پیام شما ادامه‌ی همان گفتگو است؛ «🆕 گفتگوی تازه» از اول شروع می‌کند.",
            "🔒 در چت، مدل فقط جواب می‌دهد (به فایل‌ها و دستورهای سرور دسترسی ندارد)."]
        rows = [[Btn("💬 چت با Claude", "ai_chat", "claude", style=SUCCESS), Btn("💬 چت با Codex", "ai_chat", "codex", style=SUCCESS)],
                [Btn("🧠 مدل Claude", "ai_models", "claude", style=PRIMARY), Btn("🧠 مدل Codex", "ai_models", "codex", style=PRIMARY)],
                [Btn("⚡️ افورت Claude", "ai_efforts", "claude", style=PRIMARY), Btn("⚡️ افورت Codex", "ai_efforts", "codex", style=PRIMARY)],
                [Btn("🔄 مصرف تازه", "ai_fresh", style=PRIMARY)]]
        s = Screen("\n".join(lines), rows, refresh=("ai", []), back=("menu", []))
        s.notice = notice
        return s

    @app.action("ai_fresh", view=False)
    def fresh(ctx):
        return main(ctx, "مصرف دوباره خوانده شد", True)

    # -------------------------------------------------------- model / effort
    @app.action("ai_models")
    def models(ctx, provider):
        p = prefs(ctx.chat_id)
        cur = p[provider]["model"]
        rows = []
        if provider == "claude":
            for m in conf().get("claude_models") or ai.CLAUDE_NAMES:
                rows.append([Btn(("✅ " if m == cur else "") + ai.claude_name(m), "ai_model_to", provider, m,
                                 style=SUCCESS if m == cur else PRIMARY)])
            note = "نام کوتاه = تازه‌ترین نسخه‌ای که Claude Code روی سرور می‌شناسد."
        else:
            lst = ai.codex_models()
            for slug, name, efforts, _ in lst:
                rows.append([Btn(("✅ " if slug == cur else "") + name, "ai_model_to", provider, slug,
                                 style=SUCCESS if slug == cur else PRIMARY)])
            note = "از فهرست خود Codex." if lst else "فهرست مدل‌های Codex پیدا نشد (یک بار Codex را اجرا کنید)."
        rows.append([Btn("✏️ اسم مدل دیگر", "ai_model_ask", provider, style=PRIMARY)])
        return Screen("🧠 <b>مدل %s</b>\nالان: <code>%s</code>\n%s" % (PROVIDERS[provider], esc(cur), note), rows,
                      back=("ai", []))

    @app.action("ai_model_to", write=True, view=False)
    def model_to(ctx, provider, model):
        p = prefs(ctx.chat_id)
        p[provider]["model"] = model
        if provider == "codex":
            efforts = dict((m[0], m[2]) for m in ai.codex_models()).get(model)
            if efforts and p[provider].get("effort") not in efforts:
                p[provider]["effort"] = dict((m[0], m[3]) for m in ai.codex_models()).get(model) or efforts[0]
        save_prefs(ctx.chat_id, p)
        log("ai %s model set to %s by chat %s", provider, model, ctx.chat_id)
        return back_to(ctx, provider, "✅ مدل: %s" % model)

    @app.action("ai_model_ask", write=True, view=False)
    def model_ask(ctx, provider):
        return ctx.ask_text("ai_model", "✏️ اسم کامل مدل %s را بفرستید (مثلاً <code>%s</code>):" % (
            PROVIDERS[provider], "claude-sonnet-4-6" if provider == "claude" else "gpt-5.6-terra"), provider,
            back=("ai_models", [provider]))

    @app.text_input("ai_model")
    def model_input(ctx, text, msg, provider):
        m = text.strip()
        ctx.msg_id = None
        if not m or len(m) > 80 or not all(ch.isalnum() or ch in "-._:[]" for ch in m):
            return Screen("❌ اسم مدل نامعتبر است.", back=("ai_models", [provider]))
        return model_to(ctx, provider, m)

    @app.action("ai_efforts")
    def efforts(ctx, provider):
        p = prefs(ctx.chat_id)
        cur = p[provider].get("effort")
        if provider == "claude":
            lst = conf().get("efforts") or ai.CLAUDE_EFFORTS
        else:
            lst = dict((m[0], m[2]) for m in ai.codex_models()).get(p[provider]["model"]) or ["low", "medium", "high"]
        btns = [Btn(("✅ " if e == cur else "") + ai.EFFORT_FA.get(e, e), "ai_effort_to", provider, e,
                    style=SUCCESS if e == cur else PRIMARY) for e in lst]
        rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
        return Screen("⚡️ <b>افورت %s</b> (%s)\nافورت بیشتر = فکر بیشتر، جواب کندتر و مصرف بیشتر.\nالان: %s" % (
            PROVIDERS[provider], esc(p[provider]["model"]), esc(ai.EFFORT_FA.get(cur, cur or "—"))), rows, back=("ai", []))

    @app.action("ai_effort_to", write=True, view=False)
    def effort_to(ctx, provider, effort):
        p = prefs(ctx.chat_id)
        p[provider]["effort"] = effort
        save_prefs(ctx.chat_id, p)
        return back_to(ctx, provider, "✅ افورت: %s" % ai.EFFORT_FA.get(effort, effort))

    def back_to(ctx, provider, notice):
        if ctx.session().get("ai_chat") == provider:
            return chat(ctx, provider, notice)
        return main(ctx, notice)

    # -------------------------------------------------------- chatting
    def arm(ctx, provider):
        ctx.session()["input"] = {"kind": "ai_msg", "args": [provider], "exp": time.time() + CHAT_TTL}
        ctx.session()["ai_chat"] = provider

    def chat_rows(provider):
        return [[Btn("🆕 گفتگوی تازه", "ai_new", provider, style=SUCCESS), Btn("🚪 خروج از چت", "ai_exit", style=PRIMARY)],
                [Btn("🧠 مدل", "ai_models", provider, style=PRIMARY), Btn("⚡️ افورت", "ai_efforts", provider, style=PRIMARY)]]

    @app.action("ai_chat", write=True, view=False)
    def chat(ctx, provider, notice=None):
        p = prefs(ctx.chat_id)
        p["provider"] = provider
        save_prefs(ctx.chat_id, p)
        arm(ctx, provider)
        sid = p[provider].get("session")
        s = Screen("💬 <b>چت با %s</b> — %s\n%s\n\nپیامتان را بفرستید. تا وقتی «🚪 خروج» را نزنید، هر پیام به %s می‌رود." % (
            PROVIDERS[provider], esc(model_label(provider, p)),
            "🧵 ادامه‌ی گفتگوی قبلی" if sid else "🆕 گفتگوی تازه", PROVIDERS[provider]), chat_rows(provider))
        s.notice = notice
        return s

    @app.action("ai_new", write=True, view=False)
    def new(ctx, provider):
        p = prefs(ctx.chat_id)
        p[provider]["session"] = None
        save_prefs(ctx.chat_id, p)
        return chat(ctx, provider, "🆕 گفتگوی تازه")

    @app.action("ai_exit", view=False)
    def exit_chat(ctx):
        ctx.session()["input"] = None
        ctx.session()["ai_chat"] = None
        return main(ctx, "از چت خارج شدید")

    @app.action("ai_stop", write=True, view=False)
    def stop(ctx):
        with jobs_lock:
            j = jobs.get(ctx.chat_id)
        if ctx.session().get("ai_chat"):
            arm(ctx, ctx.session()["ai_chat"])       # a button ends text questions; the chat goes on
        if j:
            j.stop()
            ctx.answer("⏹ متوقف می‌شود…")
        else:
            ctx.answer("کاری در جریان نیست")
        return None

    @app.text_input("ai_msg")
    def on_msg(ctx, text, msg, provider):
        arm(ctx, provider)                      # stays in chat mode
        text = (text or msg.get("caption") or "").strip()
        if not text:
            app.bot.send(ctx.chat_id, "✍️ در چت فقط متن فرستاده می‌شود.")
            return None
        with jobs_lock:
            if ctx.chat_id in jobs:
                app.bot.send(ctx.chat_id, "⏳ هنوز جواب پیام قبلی آماده نشده.",
                             app.render(ctx.chat_id, Screen("", [[Btn("⏹ توقف", "ai_stop", style=DANGER)]]))[1])
                return None
            job = jobs[ctx.chat_id] = ai.Job(provider)
        p = prefs(ctx.chat_id)
        q = dict(p[provider])
        wait = app.bot.send(ctx.chat_id, "⏳ %s (%s) دارد فکر می‌کند…" % (PROVIDERS[provider], esc(model_label(provider, p))),
                            app.render(ctx.chat_id, Screen("", [[Btn("⏹ توقف", "ai_stop", style=DANGER)]]))[1])
        threading.Thread(target=answer, args=(ctx.chat_id, provider, q, text, job, wait.get("message_id")),
                         name="ai-%s" % ctx.chat_id, daemon=True).start()
        return None

    def answer(chat_id, provider, q, text, job, wait_id):
        typing = threading.Event()

        def keep_typing():
            while not typing.wait(5):
                try:
                    app.bot.call("sendChatAction", {"chat_id": chat_id, "action": "typing"}, attempts=1)
                except (tg.TelegramError, OSError):
                    pass
        threading.Thread(target=keep_typing, daemon=True).start()
        timeout = int(conf().get("timeout") or 900)
        try:
            if provider == "claude":
                res = ai.ask_claude(job, text, q["model"], q.get("effort"), q.get("session"), timeout)
            else:
                res = ai.ask_codex(job, text, q.get("model"), q.get("effort"), q.get("session"), timeout)
            ai.log_run(provider, q.get("model"), q.get("effort"), res)
            p = prefs(chat_id)
            p[provider]["session"] = res.get("session")
            save_prefs(chat_id, p)
            msgs = ai.to_messages(res["text"])
            foot = "<i>%s · %s · %s%s</i>" % (esc(PROVIDERS[provider]), esc(model_label(provider, p)),
                                               duration(res.get("seconds") or 0, 1),
                                               " · $%.3f" % res["cost"] if res.get("cost") else "")
            if len(msgs[-1]) + len(foot) + 2 <= 4000:
                msgs[-1] += "\n\n" + foot
            else:
                msgs.append(foot)
        except ai.AIError as e:
            msgs = ["❌ %s" % esc(e)]
        except Exception as e:  # noqa: BLE001 - the chat must hear about any failure
            log("ai answer failed: %r", e)
            msgs = ["❌ خطا: %s" % esc(e)]
        finally:
            typing.set()
            with jobs_lock:
                jobs.pop(chat_id, None)
        app.bot.delete(chat_id, wait_id)
        for m in msgs:
            try:
                app.bot.send(chat_id, m)
            except tg.TelegramError as e:
                if e.code != 400:
                    raise
                app.bot.send(chat_id, esc(m.replace("<pre>", "").replace("</pre>", "")))   # HTML Telegram refused
ERISBOT_EOF_s_ai_py
  put_file "$STAGE/erisbot/s_alerts.py" 0644 << 'ERISBOT_EOF_s_alerts_py'
"""Alerts: the active ones, history, each type on/off, silence for a while."""
import time

from .alerts import TYPES
from .ui import PRIMARY, SUCCESS, Btn, Screen
from .util import fa


def register(app):
    @app.action("al")
    def main(ctx, notice=None):
        eng = app.alerts
        act = eng.active()
        lines = ["🔔 <b>هشدارها</b>"]
        if act:
            lines.append("فعال (%s):" % fa(len(act)))
            for a in sorted(act.values(), key=lambda a: (-a["level"], a["since"])):
                lines.append("%s %s%s" % ("🔴" if a["level"] >= 2 else "🟠", a["text"],
                                          "" if eng.enabled(a["type"]) else " <i>(خاموش)</i>"))
        else:
            lines.append("✅ هیچ هشدار فعالی نیست.")
        mu = app.alerts.st.get("mute_until") or 0
        lines.append("")
        lines.append("🔕 سکوت تا %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(mu)) if mu > time.time() else "🔔 سکوت: ندارد")
        rows = [[Btn("🔕 ۱ ساعت", "al_mute", 3600, style=PRIMARY), Btn("🔕 ۸ ساعت", "al_mute", 8 * 3600, style=PRIMARY),
                 Btn("🔕 تا صبح", "al_mute", -1, style=PRIMARY)],
                [Btn("🔔 لغو سکوت", "al_mute", 0, style=SUCCESS)],
                [Btn("⚙️ انواع هشدار", "al_types", style=PRIMARY), Btn("📜 تاریخچه", "al_hist", style=PRIMARY)]]
        s = Screen("\n".join(lines), rows, refresh=("al", []), back=("menu", []))
        s.notice = notice
        return s

    @app.action("al_mute", write=True, view=False)
    def mute(ctx, seconds):
        now = time.time()
        if seconds == 0:
            until = 0
        elif seconds < 0:          # until 08:00 tomorrow (or today, before 08:00)
            lt = time.localtime(now)
            until = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 8, 0, 0, 0, 0, -1))
            if until <= now:
                until += 86400
        else:
            until = now + seconds
        app.alerts.mute(until)
        return main(ctx, "🔕 سکوت شد" if until else "🔔 سکوت لغو شد")

    @app.action("al_types")
    def types(ctx):
        rows = [[Btn(("✅ " if app.alerts.enabled(t) else "🚫 ") + title, "al_toggle", t,
                     style=SUCCESS if app.alerts.enabled(t) else PRIMARY)] for t, title in TYPES.items()]
        return Screen("⚙️ <b>انواع هشدار</b>\nبا زدن هر کدام روشن یا خاموش می‌شود.", rows, back=("al", []))

    @app.action("al_toggle", write=True, view=False)
    def toggle(ctx, t):
        if t in TYPES:
            app.alerts.set_enabled(t, not app.alerts.enabled(t))
        return types(ctx)

    @app.action("al_hist")
    def hist(ctx):
        h = app.alerts.history(25)
        lines = ["📜 <b>تاریخچه‌ی هشدارها</b>"]
        for e in reversed(h):
            icon = {"on": "🔴" if e.get("level", 1) >= 2 else "🟠", "off": "✅", "event": "🔵"}.get(e["kind"], "•")
            lines.append("%s %s %s" % (time.strftime("%m-%d %H:%M", time.localtime(e["ts"])), icon, e["text"]))
        if not h:
            lines.append("خالی.")
        return Screen("\n".join(lines), refresh=("al_hist", []), back=("al", []))
ERISBOT_EOF_s_alerts_py
  put_file "$STAGE/erisbot/s_backup.py" 0644 << 'ERISBOT_EOF_s_backup_py'
"""Backups: the state of each part, back up and send now, the copies on the flash disk, restore."""
from . import config, eris
from .ui import PRIMARY, SUCCESS, Btn, Screen
from .util import ago, clip, esc, fa, human, log


def part_line(name, p, sent):
    if not p.get("present"):
        icon, what = "⏭", "روی این سرور نیست"
    elif not p.get("enabled"):
        icon, what = "⚪️", "در BACKUP_PARTS نیست"
    elif p.get("failed"):
        icon, what = "❌", "ناموفق: %s" % esc((p.get("last_error") or "")[:80])
    elif p.get("last_ok_epoch"):
        icon, what = "✅", "%s · %s · %s نسخه" % (ago(p["last_ok_epoch"]), human(p.get("last_size")), fa(p.get("count", 0)))
    else:
        icon, what = "⏳", "هنوز بکاپی نیست"
    s = "%s <b>%s</b> — %s" % (icon, name, what)
    if sent:
        s += "\n     📤 %s" % ("ارسال %s" % ago(sent.get("last_sent")) if sent.get("ok") else
                             "ارسال ناموفق: %s" % esc((sent.get("error") or "")[:60]))
    return s


def register(app):
    @app.action("bk")
    def main(ctx, notice=None):
        s = eris.backup_status()
        res = app.sender.results()
        bs = app.conf.get("backup_send") or {}
        full = s.get("full") or {}
        lines = ["💾 <b>بکاپ</b>",
                 "📁 پوشه: <code>%s</code> %s · شبانه ساعت %s" % (esc(s.get("backup_dir") or "تنظیم نشده"),
                                                                "✅" if s.get("dir_ok") else "❌", esc(s.get("at"))),
                 "🗄 بکاپ کلی (فقط روی فلش، هرگز ارسال نمی‌شود): %s" % (
                     ("❌ ناموفق" if full.get("failed") else ("✅ %s" % ago(full.get("last_ok_epoch")) if full.get("last_ok_epoch") else "—"))
                     if full.get("enabled") else "خاموش"), "", "<b>بخش‌ها</b>"]
        for name, p in (s.get("parts") or {}).items():
            lines.append(part_line(name, p, res.get(name)))
        lines += ["", "📤 ارسال خودکار: %s" % {"after_nightly": "بعد از بکاپ شبانه", "at": "ساعت %s" % bs.get("at"),
                                               "off": "خاموش"}.get(bs.get("mode"), "؟")]
        if app.sender.busy:
            lines.append("⏳ در حال کار: %s" % "، ".join(app.sender.busy.get("parts", [])))
        rows = [[Btn("📤 بکاپ و ارسال همین الان (همه)", "bk_now", "all", style=SUCCESS)],
                [Btn("📤 یک بخش…", "bk_pick", "now", style=PRIMARY), Btn("📂 بکاپ‌های روی فلش", "bk_pick", "flash", style=PRIMARY)],
                [Btn("⚙️ زمان و بخش‌های ارسال", "set_send", style=PRIMARY), Btn("🔑 رمز بکاپ", "set_pw", style=PRIMARY)]]
        sc = Screen("\n".join(lines), rows, refresh=("bk", []), back=("menu", []))
        sc.notice = notice
        return sc

    def present_parts():
        s = eris.backup_status()
        return [n for n, p in (s.get("parts") or {}).items() if p.get("present")]

    @app.action("bk_pick")
    def pick(ctx, what):
        names = present_parts()
        btns = [Btn(n, "bk_now" if what == "now" else "bk_flash", n, style=SUCCESS if what == "now" else PRIMARY) for n in names]
        rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
        title = "📤 <b>کدام بخش؟</b> (بکاپ تازه + ارسال)" if what == "now" else "📂 <b>بکاپ‌های کدام بخش؟</b>"
        return Screen(title, rows, back=("bk", []))

    @app.action("bk_now", write=True, view=False)
    def now(ctx, which):
        names = present_parts()
        parts = [p for p in config.send_parts(app.conf) if p in names] if which == "all" else [which] if which in names else []
        if not parts:
            return main(ctx, "بخشی برای ارسال نیست")
        app.sender.submit(parts, make=True, chats=[ctx.chat_id], trigger="manual")
        log("backup + send %s asked by chat %s", ",".join(parts), ctx.chat_id)
        return main(ctx, "در صف قرار گرفت: %s" % "، ".join(parts))

    @app.action("bk_flash")
    def flash(ctx, part):
        if part not in config.PARTS:
            return None
        items = (eris.backup_list(part).get("groups") or {}).get(part) or []
        lines = ["📂 <b>بکاپ‌های %s روی فلش</b> (جدید به قدیم)" % part]
        rows = []
        for it in items[:15]:
            label = it["time"] + " · " + human(it.get("size")) + (" · قبل از بازگردانی" if it.get("label") == "before-restore" else "")
            rows.append([Btn("📄 " + label, "bk_file", part, it["path"], style=PRIMARY)])
        if not items:
            lines.append("هنوز نسخه‌ای نیست.")
        return Screen("\n".join(lines), rows, refresh=("bk_flash", [part]), back=("bk_pick", ["flash"]))

    def find(part, path):
        items = (eris.backup_list(part).get("groups") or {}).get(part) or []
        return next((it for it in items if it["path"] == path), None)

    @app.action("bk_file")
    def file(ctx, part, path):
        it = find(part, path)
        if not it:
            return Screen("این نسخه دیگر نیست.", back=("bk_flash", [part]))
        lines = ["📄 <b>%s</b>" % esc(it["name"]), "🕒 %s · %s" % (it["time"], human(it.get("size"))),
                 "🔐 sha256: <code>%s</code>" % esc(it.get("sha256") or "؟")]
        rows = [[Btn("📤 ارسال این نسخه", "bk_send_file", part, path, style=SUCCESS)],
                [Btn("♻️ بازگردانی این نسخه", "bk_restore", part, path, steps=2,
                     confirm="بخش <b>%s</b> از نسخه‌ی %s بازگردانی شود؟\nفایل‌های فعلی این بخش رونویسی می‌شوند؛ ERIS اول از وضعیت فعلی بکاپ می‌گیرد و سرویس‌های مربوط را لحظه‌ای متوقف می‌کند." % (part, it["time"]))]]
        return Screen("\n".join(lines), rows, back=("bk_flash", [part]))

    @app.action("bk_send_file", write=True, view=False)
    def send_file(ctx, part, path):
        if not find(part, path):
            return Screen("این نسخه دیگر نیست.", back=("bk_flash", [part]))
        app.sender.submit([part], make=False, files={part: path}, chats=[ctx.chat_id], trigger="manual")
        return main(ctx, "در صف ارسال قرار گرفت")

    @app.action("bk_restore", write=True, steps=2, view=False)
    def restore(ctx, part, path):
        if part not in config.PARTS or not find(part, path):
            return Screen("این نسخه دیگر نیست.", back=("bk", []))
        log("restore %s from %s asked by chat %s", part, path, ctx.chat_id)
        ctx.answer("در حال بازگردانی…")
        ctx.show(Screen("♻️ در حال بازگردانی <b>%s</b>… (ممکن است چند دقیقه طول بکشد)" % part))
        r = eris.restore(part, path)
        ok = r.returncode == 0
        return Screen("%s بازگردانی %s\n<pre>%s</pre>" % ("✅" if ok else "❌", part, esc(clip((r.stdout + r.stderr).strip(), 3000))),
                      back=("bk", []))
ERISBOT_EOF_s_backup_py
  put_file "$STAGE/erisbot/s_fans.py" 0644 << 'ERISBOT_EOF_s_fans_py'
"""Fans, performance profiles, finding the CPU fan controller, the BIOS overclocking guide, the stress test."""
import os
import threading

from . import fand, hw, perf, stress, system
from .ui import PRIMARY, SUCCESS, Btn, Screen
from .util import clip, esc, fa, log

BIOS_GUIDE = """📖 <b>راهنمای اورکلاک از BIOS</b> (Core 2 روی چیپ‌ست G41)

<b>واقعیت:</b> اورکلاک واقعی یعنی بالا بردن FSB یا ولتاژ، و روی Core 2 و G41 فقط از داخل BIOS امن است. ابزارهایی مثل setfsb مستقیم روی چیپ PLL می‌نویسند و ممکن است سرور را هنگ کنند یا داده را خراب کنند؛ این ربات چنین کاری نمی‌کند. «پروفایل‌ها»ی ربات فقط فن، governor و swappiness را عوض می‌کنند.

<b>Core 2 Quad Q9550</b> (ضریب قفل ۸٫۵، FSB 333 MHz = 1333 QDR، یعنی ۲٫۸۳ گیگاهرتز):
۱. اول از همه یک بکاپ بگیرید و دمای فعلی را با «🔥 تست پایداری» ۱۰ دقیقه‌ای بسنجید.
۲. در BIOS، FSB را قدم‌به‌قدم بالا ببرید: 333 ← 350 ← 366 (حدود ۳٫۱ گیگ) ← 380 (حدود ۳٫۲۳ گیگ). هر بار فقط یک قدم.
۳. فرکانس رم را با نسبت DRAM پایین نگه دارید تا رم بیش از حدش نرود. اگر گزینه‌اش هست، PCI-E را روی 100 MHz قفل کنید.
۴. ولتاژ: Vcore را تا جایی که پایدار است پایین نگه دارید و از حدود 1.3625V (حد Intel برای Q9550) بالاتر نروید. هر بار فقط 0.0125 تا 0.025 ولت اضافه کنید.
۵. بعد از هر قدم، «🔥 تست پایداری» ۳۰ دقیقه‌ای بگیرید. دمای CPU زیر بار باید <b>زیر ۷۰°C</b> بماند (TCase کوادکور Q9550 حدود ۷۱°C است). اگر throttle دیدید یا سرور ری‌استارت شد، یک قدم برگردید.
۶. اگر EIST را در BIOS روشن کنید، cpufreq در لینوکس فعال می‌شود و پروفایل‌ها governor را هم تنظیم می‌کنند.

<b>Pentium E5700</b> (ضریب ۱۵، FSB 200 MHz، یعنی ۳٫۰ گیگاهرتز): همین روش، با FSB از 200 به 210 و بعد 220.

⚠️ <b>پاور:</b> پاور ۲۳۰ واتی ارزان است و حاشیه‌ی کمی دارد. Q9550 (95 وات) به‌علاوه‌ی GTX 650 و هاردها نزدیک حد آن است و اورکلاک و ولتاژ بیشتر، مصرف را بالاتر می‌برد. ری‌استارت ناگهانی زیر بار یعنی پاور کم آورده. برای اورکلاک یک پاور معتبر ۴۰۰ تا ۴۵۰ واتی توصیه می‌شود.
⚠️ VRM بردهای ارزان G41 ضعیف است؛ کیس باید هوای خوب داشته باشد.
↩️ اگر سرور بالا نیامد: Clear CMOS (با جامپر یا برداشتن باتری)."""


def register(app):
    def status():
        return fand.read_status()

    @app.action("fan")
    def main(ctx, notice=None):
        st = status()
        fans = hw.fans()
        prof = perf.load().get("active")
        profiles = perf.all_profiles()
        lines = ["🌀 <b>فن و عملکرد</b>",
                 "🎛 پروفایل: %s" % (profiles[prof]["title"] if prof in profiles else "پیش‌فرض سیستم")]
        ct = system.cpu_temp()
        gt = hw.gpu_temp()
        lines.append("🌡 CPU: %s · GPU: %s" % ("%s°C" % fa("%.0f" % ct) if ct is not None else "؟",
                                              "%s°C" % fa("%.0f" % gt) if gt is not None else "—"))
        if not fans:
            lines.append("فن قابل کنترلی پیدا نشد (هیچ pwm در /sys/class/hwmon نیست).")
        for f in fans:
            s = (st or {}).get("fans", {}).get(f["id"]) or {}
            fc = fand.fan_cfg(fand.load_cfg(), f["id"])
            r = hw.rpm(f)
            raw = hw.read_int(f["pwm"])
            pct = s.get("pct") if s else hw.pwm_to_pct(f, raw)
            lines.append("• %s: %s RPM · %s · %s%s" % (esc(f["label"]), fa(r) if r is not None else "؟",
                                                      "%s٪" % fa("%.0f" % pct) if pct is not None else "؟",
                                                      fand.describe_cfg(fc), " 🔥 حفاظت دمایی" if s.get("protect") else ""))
        if not any(f["kind"] == "cpu" for f in fans):
            lines.append("ℹ️ فن CPU و کیس فعلاً از لینوکس کنترل نمی‌شود؛ «🔍 تشخیص کنترلر» را امتحان کنید.")
        if fans and st is None:
            lines.append("⚠️ سرویس eris-fan کار نمی‌کند (<code>systemctl status eris-fan</code>)؛ فن‌ها دست BIOS است.")
        lines.append("⚙️ cpufreq: %s" % ("governor %s" % esc(hw.governor()) if hw.has_cpufreq() else
                                        "ندارد (EIST در BIOS خاموش است یا پشتیبانی نمی‌شود)"))
        rows = [[Btn("🌀 " + f["label"], "fan_one", f["id"], style=PRIMARY)] for f in fans]
        rows += [[Btn("🎛 پروفایل‌ها", "pf", style=PRIMARY), Btn("🔍 تشخیص کنترلر فن CPU", "fan_detect", style=PRIMARY)],
                 [Btn("🔥 تست پایداری", "stress", style=PRIMARY), Btn("📖 راهنمای اورکلاک از BIOS", "bios", style=PRIMARY)]]
        s = Screen("\n".join(lines), rows, refresh=("fan", []), back=("menu", []))
        s.notice = notice
        return s

    def find(fid):
        return next((f for f in hw.fans() if f["id"] == fid), None)

    @app.action("fan_one")
    def one(ctx, fid, notice=None):
        f = find(fid)
        if not f:
            return Screen("این فن دیگر پیدا نمی‌شود.", back=("fan", []))
        st = (status() or {}).get("fans", {}).get(fid) or {}
        fc = fand.fan_cfg(fand.load_cfg(), fid)
        lo, hi = hw.pwm_range(f)
        t = st.get("temp") if st else hw.fan_temp(f)
        lines = ["🌀 <b>%s</b>" % esc(f["label"]),
                 "دور: %s RPM" % (fa(hw.rpm(f)) if hw.rpm(f) is not None else "؟ (fan_input ندارد)"),
                 "قدرت: %s" % ("%s٪" % fa("%.0f" % st["pct"]) if st.get("pct") is not None else "؟"),
                 "دما (%s): %s" % ("خود کارت" if f["kind"] == "gpu" else "داغ‌ترین هسته‌ی CPU",
                                  "%s°C" % fa("%.0f" % t) if t is not None else "؟"),
                 "حالت: %s" % fand.describe_cfg(fc),
                 "محدوده‌ی pwm: %s تا %s" % (lo, hi),
                 "🛡 حفاظت همیشگی: CPU ≥ ۷۵°C یا GPU ≥ ۸۰°C ⇐ ۱۰۰٪. کمترین مقدار دستی ۲۵٪."]
        if st.get("protect"):
            lines.append("🔥 حفاظت دمایی الان فعال است (۱۰۰٪).")
        if st.get("stall"):
            lines.append("🛑 فن نمی‌چرخد!")
        rows = [[Btn("🔁 خودکار (BIOS/درایور)", "fan_set", fid, "auto", 0, style=SUCCESS)],
                [Btn("📈 " + fand.CURVE_TITLES[c], "fan_set", fid, "curve", c, style=PRIMARY) for c in ("quiet", "normal", "steep")],
                [Btn("%s٪" % fa(p), "fan_set", fid, "manual", p, style=PRIMARY) for p in (30, 40, 50, 60)],
                [Btn("%s٪" % fa(p), "fan_set", fid, "manual", p, style=PRIMARY) for p in (70, 80, 100)],
                [Btn("➖ ۵٪", "fan_set", fid, "step", -5, style=PRIMARY), Btn("➕ ۵٪", "fan_set", fid, "step", 5, style=PRIMARY)]]
        s = Screen("\n".join(lines), rows, refresh=("fan_one", [fid]), back=("fan", []))
        s.notice = notice
        return s

    @app.action("fan_set", write=True, view=False)
    def fan_set(ctx, fid, mode, value):
        if not find(fid):
            return main(ctx)
        cfg = fand.load_cfg()
        cur = fand.fan_cfg(cfg, fid)
        if mode == "auto":
            new = {"mode": "auto"}
        elif mode == "curve" and value in fand.CURVES:
            new = {"mode": "curve", "curve": fand.CURVES[value], "curve_name": value}
        elif mode in ("manual", "step"):
            base = cur["percent"] if cur["mode"] == "manual" else 50
            p = float(value) if mode == "manual" else base + float(value)
            new = {"mode": "manual", "percent": max(fand.MIN_PCT, min(100, p))}
        else:
            return one(ctx, fid)
        cfg["fans"][fid] = new
        fand.save_cfg(cfg)
        log("fan %s -> %s by chat %s", fid, new, ctx.chat_id)
        return one(ctx, fid, "✅ %s (تا ۲ ثانیه اعمال می‌شود)" % fand.describe_cfg(fand.fan_cfg(cfg, fid)))

    # -------------------------------------------------------- profiles
    @app.action("pf")
    def profiles(ctx, notice=None):
        d = perf.load()
        lines = ["🎛 <b>پروفایل‌های عملکرد</b>",
                 "هر پروفایل فقط چیزهایی را عوض می‌کند که روی این سرور هست. اورکلاک FSB فقط از BIOS ممکن است (📖 راهنما)."]
        rows = []
        for name, p in perf.all_profiles().items():
            on = d.get("active") == name
            av = perf.availability(p)
            miss = [w for w, ok, _ in av if not ok]
            lines.append("%s %s%s" % ("✅" if on else "▫️", p["title"], " — در دسترس نیست روی این سرور: %s" % "، ".join(miss) if miss else ""))
            rows.append([Btn(("✅ " if on else "") + p["title"], "pf_one", name, style=SUCCESS if on else PRIMARY)])
        rows.append([Btn("⭐ پروفایل سفارشی جدید", "pf_new", style=SUCCESS)])
        rows.append([Btn("↩️ برگشت به پیش‌فرض سیستم", "pf_reset", confirm="همه‌چیز (فن‌ها، governor، حداقل فرکانس، swappiness) به حالت اولیه‌ی سیستم برگردد؟")])
        s = Screen("\n".join(lines), rows, refresh=("pf", []), back=("fan", []))
        s.notice = notice
        return s

    @app.action("pf_one")
    def pf_one(ctx, name):
        ps = perf.all_profiles()
        if name not in ps:
            return profiles(ctx)
        p = ps[name]
        lines = [p["title"]]
        for what, ok, note in perf.availability(p):
            lines.append("%s %s%s" % ("✅" if ok else "🚫", what, (" — " + note) if note and ok else
                                      (" — در دسترس نیست روی این سرور (%s)" % note if not ok else "")))
        rows = [[Btn("✅ اعمال این پروفایل", "pf_apply", name, style=SUCCESS)]]
        if p.get("custom"):
            rows.append([Btn("🗑 حذف این پروفایل", "pf_del", name[2:], confirm="پروفایل %s حذف شود؟" % esc(name[2:]))])
        return Screen("\n".join(lines), rows, back=("pf", []))

    @app.action("pf_apply", write=True, view=False)
    def pf_apply(ctx, name):
        r = perf.apply(name)
        p = perf.all_profiles()[name]
        log("profile %s by chat %s: %s", name, ctx.chat_id, r)
        lines = ["✅ پروفایل %s فعال شد (بعد از ری‌استارت هم می‌ماند)." % p["title"], "", "<b>چه چیزهایی عوض شد:</b>"]
        lines += ["• " + esc(c) for c in r["changes"]] or ["• چیزی لازم نبود عوض شود."]
        if r["unavailable"]:
            lines += ["", "<b>در دسترس نیست روی این سرور:</b>"] + ["• " + esc(u) for u in r["unavailable"]]
        return Screen("\n".join(lines), back=("pf", []))

    @app.action("pf_reset", write=True, steps=1, view=False)
    def pf_reset(ctx):
        ch = perf.reset_default()
        log("profiles reset by chat %s", ctx.chat_id)
        return Screen("↩️ همه‌چیز به پیش‌فرض سیستم برگشت.\n" + ("\n".join("• " + esc(c) for c in ch) or "چیزی تغییر نکرده بود."),
                      back=("pf", []))

    @app.action("pf_del", write=True, steps=1, view=False)
    def pf_del(ctx, name):
        perf.delete_custom(name)
        return profiles(ctx, "حذف شد")

    @app.action("pf_new", write=True, view=False)
    def pf_new(ctx):
        ctx.session()["pf_new"] = {}
        return ctx.ask_text("pf_name", "⭐ اسم پروفایل جدید را بفرستید (حداکثر ۲۴ نویسه):", back=("pf", []))

    @app.text_input("pf_name")
    def pf_name(ctx, text, msg):
        name = text.strip()[:24]
        if not name or any(c in name for c in "<>&"):
            return Screen("❌ اسم نامعتبر.", back=("pf", []))
        ctx.session()["pf_new"] = {"name": name}
        ctx.msg_id = None
        rows = [[Btn(t, "pf_new_fan", k, style=PRIMARY)] for k, t in perf.FAN_CHOICES.items()]
        return Screen("⭐ <b>%s</b> — منحنی فن:" % esc(name), rows, back=("pf", []))

    @app.action("pf_new_fan", write=True, view=False)
    def pf_new_fan(ctx, fan):
        w = ctx.session().get("pf_new") or {}
        if "name" not in w:
            return profiles(ctx)
        w["fan"] = fan
        govs = hw.governors()
        rows = [[Btn(g, "pf_new_gov", g, style=PRIMARY) for g in govs[i:i + 3]] for i in range(0, len(govs), 3)]
        rows.append([Btn("بدون تغییر governor", "pf_new_gov", "", style=PRIMARY)])
        note = "" if govs else "\n(cpufreq روی این سرور نیست؛ governor در دسترس نیست)"
        return Screen("⭐ <b>%s</b> — governor:%s" % (esc(w["name"]), note), rows, back=("pf", []))

    @app.action("pf_new_gov", write=True, view=False)
    def pf_new_gov(ctx, gov):
        w = ctx.session().get("pf_new") or {}
        if "name" not in w:
            return profiles(ctx)
        w["governor"] = gov
        rows = [[Btn("بدون تغییر", "pf_new_save", -1, style=SUCCESS), Btn("۱۰", "pf_new_save", 10, style=SUCCESS),
                 Btn("۶۰", "pf_new_save", 60, style=SUCCESS)]]
        return Screen("⭐ <b>%s</b> — vm.swappiness:" % esc(w["name"]), rows, back=("pf", []))

    @app.action("pf_new_save", write=True, view=False)
    def pf_new_save(ctx, sw):
        w = ctx.session().get("pf_new") or {}
        if "name" not in w:
            return profiles(ctx)
        perf.save_custom(w["name"], w.get("fan", "normal"), w.get("governor") or None, None if sw < 0 else int(sw))
        ctx.session()["pf_new"] = {}
        return pf_one(ctx, "c:" + w["name"])

    # -------------------------------------------------------- finding the CPU fan controller
    @app.action("fan_detect")
    def detect(ctx):
        txt = ("🔍 <b>تشخیص کنترلر فن CPU</b>\n"
               "الان هیچ درایور Super I/O (it87، nct6775، w83627ehf، f71882fg …) لود نیست، پس فن CPU و کیس pwm ندارند.\n"
               "این کار <code>sensors-detect --auto</code> را اجرا می‌کند؛ روی بعضی بردها پرس‌وجوی سخت‌افزار کمی ریسک دارد.\n"
               "بعد از آن، هر درایور پیشنهادی را جدا و با تأیید لود می‌کنیم.")
        if not os.path.exists(hw.config.SENSORS_DETECT):
            txt += "\n\nℹ️ sensors-detect پیدا نشد (<code>apt install lm-sensors</code>)؛ پیدا کردن خودکار بدون آن هم کار می‌کند."
            return Screen(txt, [[Btn("🪄 پیدا کردن خودکار و تست فن CPU", "fan_auto", confirm="درایورهای Super I/O یکی‌یکی امتحان شوند؟")]],
                          back=("fan", []))
        auto = Btn("🪄 پیدا کردن خودکار و تست فن CPU", "fan_auto",
                   confirm="ربات درایورهای Super I/O (it87، w83627ehf، nct6775، f71882fg …) را یکی‌یکی امتحان می‌کند؛ "
                           "هر درایور خودش شناسه‌ی چیپ را چک می‌کند و روی چیپ دیگر لود نمی‌شود.\n"
                           "بعد هر pwm پیدا شده چند ثانیه روی ۱۰۰٪ و ۳۵٪ می‌رود تا معلوم شود کدام فن را می‌چرخاند، "
                           "و به حالت قبل برمی‌گردد. (حدود ۱ تا ۲ دقیقه)")
        return Screen(txt, [[auto], [Btn("🔍 فقط اجرای sensors-detect", "fan_detect_go", confirm="sensors-detect --auto اجرا شود؟")]],
                      back=("fan", []))

    @app.action("fan_detect_go", write=True, steps=1, view=False)
    def detect_go(ctx):
        ctx.answer("در حال اجرا… (تا یک دقیقه)")
        ctx.show(Screen("⏳ sensors-detect در حال اجراست…"))
        out, rc = hw.sensors_detect()
        p = hw.parse_sensors_detect(out)
        log("sensors-detect: drivers %s, fan drivers %s, ids %s", p["drivers"], p["fan_drivers"], p["unknown_ids"])
        tail = "\n".join(out.strip().splitlines()[-30:])
        lines = ["🔍 <b>نتیجه‌ی sensors-detect</b>", "<pre>%s</pre>" % esc(clip(tail, 2200))]
        rows = []
        for d in p["fan_drivers"]:
            rows.append([Btn("📥 لود %s" % d, "fan_modprobe", d, "", confirm="درایور <b>%s</b> لود شود؟ اگر فن جدیدی پیدا شد، در بوت هم لود می‌شود." % d)])
        for i in p["unknown_ids"]:
            rows.append([Btn("📥 it87 force_id=%s" % i, "fan_modprobe", "it87", i,
                             confirm="it87 با <b>force_id=%s</b> لود شود؟\n⚠️ force_id یعنی چیپ را به زور یک مدل فرض می‌کنیم؛ اگر اشتباه باشد مقادیر غلط می‌شوند. اول بدون آن امتحان کنید." % i)])
        if not rows:
            lines.append("😕 هیچ کنترلر فنی پیدا نشد. یعنی فن CPU این برد از لینوکس قابل کنترل نیست و فقط BIOS (مثلاً Smart Fan) آن را کنترل می‌کند.")
        elif "it87" in p["fan_drivers"] and not p["unknown_ids"]:
            lines.append("ℹ️ اگر it87 لود نشد، روی بردهای قدیمی force_id لازم است (ID را از خروجی بالا ببینید).")
        return Screen("\n".join(lines), rows, back=("fan", []))

    @app.action("fan_modprobe", write=True, steps=1, view=False)
    def modprobe(ctx, driver, force_id=""):
        before = {f["id"] for f in hw.fans()}
        r = hw.modprobe(driver, force_id or None)
        if r.returncode != 0:
            return Screen("❌ modprobe %s نشد:\n<pre>%s</pre>" % (esc(driver), esc(clip(r.stderr or r.stdout, 1500))), back=("fan", []))
        new = [f for f in hw.fans() if f["id"] not in before]
        if not new:
            return Screen("ℹ️ درایور %s لود شد ولی pwm تازه‌ای پیدا نشد؛ پس این درایور فن را کنترل نمی‌کند." % esc(driver), back=("fan", []))
        hw.persist_driver(driver, force_id or None)
        log("fan driver %s%s loaded and kept by chat %s", driver, " force_id=" + force_id if force_id else "", ctx.chat_id)
        return Screen("✅ %s لود شد و در بوت هم لود می‌شود.\nفن‌های تازه:\n%s" % (
            esc(driver), "\n".join("• " + esc(f["label"]) for f in new)), back=("fan", []))

    @app.action("fan_auto", write=True, steps=1, view=False)
    def fan_auto(ctx):
        chat, mid = ctx.chat_id, ctx.msg_id
        ctx.answer("شروع شد…")
        if getattr(app, "fan_auto_running", False):
            return Screen("⏳ پیدا کردن خودکار از قبل در حال اجراست.", back=("fan", []))
        app.fan_auto_running = True
        lines = ["🪄 <b>پیدا کردن کنترلر فن CPU</b>"]

        def show(text):
            try:
                t, mk = app.render(chat, Screen("\n".join(lines + ["⏳ " + text])))
                app.bot.edit(chat, mid, t, mk)
            except Exception:  # a failed edit must not stop the search
                pass

        def run():
            try:
                rep = hw.auto_detect(progress=show)
                t, mk = app.render(chat, auto_result(rep))
            except Exception as e:  # noqa: B902 - show it instead of dying in a thread
                log("fan auto-detect: %r", e)
                t, mk = app.render(chat, Screen("❌ خطا: %s" % esc(e), back=("fan", [])))
            finally:
                app.fan_auto_running = False
            try:
                app.bot.edit(chat, mid, t, mk)
            except Exception:  # noqa: B902
                app.notify(t, chats=[chat])
        log("fan auto-detect asked by chat %s", chat)
        show("شروع…")
        threading.Thread(target=run, name="fan-auto", daemon=True).start()
        return None

    def auto_result(rep):
        lines = ["🪄 <b>نتیجه‌ی پیدا کردن کنترلر فن CPU</b>"] + rep["steps"]
        rows = []
        if rep["driver"]:
            lines.append("")
            lines.append("✅ درایور <b>%s</b>%s لود شد و از این به بعد در بوت هم لود می‌شود." % (
                esc(rep["driver"]), " (با %s)" % esc(rep["options"]) if rep["options"] else ""))
            for f, t in rep["fans"]:
                if t["ok"]:
                    lines.append("🌀 %s: کار می‌کند — %s RPM در ۱۰۰٪ و %s RPM در ۳۵٪" % (esc(f["id"]), fa(t["hi"]), fa(t["lo"])))
                    rows.append([Btn("📈 منحنی معمولی برای %s" % t["input"], "fan_set", f["id"], "curve", "normal", style=SUCCESS),
                                 Btn("🌀 تنظیم این فن", "fan_one", f["id"], style=PRIMARY)])
                else:
                    lines.append("▫️ %s: دور هیچ فنی با آن عوض نشد (یا فنی به آن وصل نیست%s)" % (
                        esc(f["id"]), "، یا BIOS آن را قفل کرده" if t.get("hi") else ""))
            if not any(t["ok"] for _, t in rep["fans"]):
                lines.append("⚠️ pwm هست ولی دور فن عوض نمی‌شود. در BIOS «CPU Fan Control / Smart Fan» را روی Manual یا "
                             "Disabled بگذارید تا BIOS با لینوکس سر فن دعوا نکند، و اگر فن ۳ سیم است حالت DC/Voltage را انتخاب کنید؛ بعد دوباره امتحان کنید.")
        else:
            lines.append("")
            if rep["conflict"]:
                lines.append("⚠️ چیپ فن هست ولی BIOS پورت‌هایش را برای ACPI گرفته (resource conflict). "
                             "راه رایج: پارامتر کرنل <code>acpi_enforce_resources=lax</code> و یک ری‌استارت. "
                             "این کار رایج و معمولاً بی‌خطر است، ولی یعنی BIOS و درایور هر دو به یک چیپ دست می‌زنند.")
                rows.append([Btn("🧩 افزودن acpi_enforce_resources=lax به GRUB", "fan_acpi_lax", steps=2,
                                 confirm="پارامتر <code>acpi_enforce_resources=lax</code> به خط فرمان کرنل (GRUB) اضافه شود؟ "
                                         "از /etc/default/grub نسخه‌ی پشتیبان گرفته می‌شود و بعد از ری‌استارت اعمال می‌شود.")])
            elif rep.get("unknown_ids"):
                lines.append("ℹ️ sensors-detect یک چیپ ناشناخته دید (%s)؛ دکمه‌ی it87 با force_id را از «🔍 فقط اجرای sensors-detect» امتحان کنید."
                             % "، ".join(rep["unknown_ids"]))
            else:
                lines.append("😕 هیچ کنترلر فنی پیدا نشد. یعنی فن CPU این برد از لینوکس قابل کنترل نیست و فقط BIOS "
                             "(Smart Fan / CPU Fan Control) آن را کنترل می‌کند. منحنی را در BIOS تنظیم کنید.")
        return Screen("\n".join(lines), rows, back=("fan", []))

    @app.action("fan_acpi_lax", write=True, steps=2, view=False)
    def fan_acpi_lax(ctx):
        ok, msg = hw.acpi_lax()
        log("acpi_enforce_resources=lax by chat %s: %s", ctx.chat_id, msg)
        rows = [[Btn("🔁 ری‌استارت سرور", "server_reboot", confirm="سرور ری‌استارت شود؟", steps=2)]] if ok else []
        return Screen(("✅ " if ok else "ℹ️ ") + esc(msg), rows, back=("fan", []))

    @app.action("bios")
    def bios(ctx):
        return Screen(BIOS_GUIDE, [[Btn("🔥 تست پایداری", "stress", style=PRIMARY)]], back=("fan", []))

    # -------------------------------------------------------- stress test
    @app.action("stress")
    def stress_page(ctx):
        t = app.stress
        if t and t.running():
            return Screen(t.progress_text(), [[Btn("⏹ توقف تست", "stress_stop", style=SUCCESS)]], refresh=("stress", []), back=("fan", []))
        lines = ["🔥 <b>تست پایداری</b>",
                 "همه‌ی هسته‌ها با پروسس‌های پایتون زیر بار کامل می‌روند. هر ۳۰ ثانیه دما، دور فن‌ها و فرکانس گزارش می‌شود.",
                 "اگر دمای CPU به ۸۵°C برسد، تست خودکار قطع می‌شود.",
                 "برای بررسی اورکلاک BIOS و پاور ضعیف."]
        rows = [[Btn("%s دقیقه" % fa(m), "stress_go", m, confirm="سرور %s دقیقه زیر بار کامل برود؟ سرویس‌ها کندتر می‌شوند." % fa(m))
                 for m in (10, 30, 60)]]
        return Screen("\n".join(lines), rows, back=("fan", []))

    @app.action("stress_go", write=True, steps=1, view=False)
    def stress_go(ctx, minutes):
        if app.stress and app.stress.running():
            return stress_page(ctx)
        chat, mid = ctx.chat_id, ctx.msg_id

        def update(t):
            text, mk = app.render(chat, Screen(t.progress_text(), [[Btn("⏹ توقف تست", "stress_stop", style=SUCCESS)]]))
            app.bot.edit(chat, mid, text, mk)

        def done(t):
            try:
                text, mk = app.render(chat, Screen(t.result_text(), back=("fan", [])))
                app.bot.edit(chat, mid, text, mk)
            except Exception:  # the message may be gone; send the result anyway
                app.notify(t.result_text(), chats=[chat])
        log("stress test %d min started by chat %s", minutes, chat)
        app.stress = stress.StressTest(int(minutes), update, done, meta={"chat_id": chat, "msg_id": mid}).start()
        return None

    @app.action("stress_stop", write=True, view=False)
    def stress_stop(ctx):
        if app.stress and app.stress.running():
            app.stress.stop()
        ctx.answer("⏹ متوقف می‌شود…")
        return None


def after_start(app):
    left = stress.leftover()
    if left:
        st, rebooted = left
        chat = st.get("chat_id")
        text = ("⚠️ <b>سرور وسط تست پایداری ری‌استارت شد!</b>\nاحتمالاً پاور یا اورکلاک ناپایدار است." if rebooted
                else "ℹ️ تست پایداری قبلی با ری‌استارت ربات قطع شد.")
        app.notify(text, chats=[chat] if chat else None)
ERISBOT_EOF_s_fans_py
  put_file "$STAGE/erisbot/s_files.py" 0644 << 'ERISBOT_EOF_s_files_py'
"""📁 NAS files: log in with a user of this server, browse the shared folders, upload files sent to the
bot into any folder, send NAS files to Telegram, new folder / rename / delete. Owner chats only."""
import os
import threading
import time

from . import config, nas, tg
from .ui import PRIMARY, SUCCESS, Btn, Screen, pager
from .util import esc, fa, human, log

PAGE = 10
UP_TTL = 1800


def incoming_file(msg):
    """(file_id, name, size) of a file in a message (document, photo, video, audio, voice...), else None."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(msg.get("date") or time.time()))
    for key, ext in (("document", ""), ("video", ".mp4"), ("audio", ".mp3"), ("voice", ".ogg"),
                     ("animation", ".mp4"), ("video_note", ".mp4")):
        f = msg.get(key)
        if isinstance(f, dict) and f.get("file_id"):
            return f["file_id"], f.get("file_name") or "%s-%s%s" % (key, stamp, ext), int(f.get("file_size") or 0)
    photos = msg.get("photo")
    if isinstance(photos, list) and photos:
        big = max(photos, key=lambda p: p.get("file_size") or p.get("width", 0) * p.get("height", 0))
        return big["file_id"], "photo-%s.jpg" % stamp, int(big.get("file_size") or 0)
    return None


def safe_name(name):
    name = (name or "file").replace("/", "_").replace("\0", "").strip() or "file"
    return name if name not in (".", "..") else "file"


def register(app):
    auth = nas.Auth()
    app.nas_auth = auth

    def nconf():
        return app.conf.get("nas") or config.DEFAULTS["nas"]

    def sess(ctx):
        s = ctx.session().get("nas")
        if not s or s["exp"] < time.time():
            ctx.session()["nas"] = None
            return None
        s["exp"] = time.time() + int(nconf().get("session_minutes", 30)) * 60
        return s

    def login_screen(ctx, note=None):
        pend = ctx.session().get("nas_pending")
        lines = ["📁 <b>فایل‌های NAS</b>", "",
                 "با یکی از کاربرهای همین سرور (همان که در SMB/OMV دارید) وارد شوید.",
                 "هر کاری که می‌کنید با دسترسی همان کاربر انجام می‌شود.",
                 "🔒 رمز هیچ‌جا ذخیره نمی‌شود و پیامش بلافاصله پاک می‌شود."]
        if pend:
            lines += ["", "📥 فایل منتظر: <b>%s</b>" % esc(pend["name"])]
        if note:
            lines = [note, ""] + lines
        return Screen("\n".join(lines), [[Btn("🔐 ورود", "nas_login", style=SUCCESS)]], back=("menu", []))

    def guard(ctx):
        """The NAS session, or a Screen to show instead (login / owner only)."""
        if not ctx.owner:
            return None, Screen("🔒 فایل‌های NAS فقط برای مالک است.", back=("menu", []))
        s = sess(ctx)
        if not s:
            return None, login_screen(ctx, "⌛ برای ادامه دوباره وارد شوید." if ctx.session().get("nas_was") else None)
        return s, None

    def where(path, lst=None):
        sh = nas.share_of(path, lst)
        if not sh:
            return None, None, None
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(sh[1]))
        return sh[0], sh[1], "" if rel == "." else rel

    def title(path):
        name, root, rel = where(path)
        return "📁 <b>%s</b>%s" % (esc(name or "?"), (" / " + esc(rel.replace("/", " / "))) if rel else "")

    # -------------------------------------------------------- log in / out
    @app.action("nas")
    def main(ctx, notice=None):
        s, other = guard(ctx)
        if other:
            return other
        lst = nas.shares()
        lines = ["📁 <b>فایل‌های NAS</b> — 👤 %s" % esc(s["user"]), "", "پوشه‌های اشتراکی:"]
        rows = [[Btn("📁 %s" % n, "nas_ls", p, 0, style=PRIMARY)] for n, p in lst]
        if not lst:
            lines.append("هیچ پوشه‌ی اشتراکی پیدا نشد.")
        if s.get("home") and os.path.isdir(s["home"]) and nas.share_of(s["home"], lst):
            rows.append([Btn("🏠 پوشه‌ی خانه‌ی من", "nas_ls", s["home"], 0, style=PRIMARY)])
        if s.get("last") and os.path.isdir(s["last"]):
            rows.append([Btn("🕘 آخرین پوشه: %s" % os.path.basename(s["last"].rstrip("/")), "nas_ls", s["last"], 0, style=PRIMARY)])
        rows.append([Btn("🚪 خروج از حساب", "nas_logout", style=PRIMARY)])
        sc = Screen("\n".join(lines), rows, refresh=("nas", []), back=("menu", []))
        sc.notice = notice
        return sc

    @app.action("nas_login", write=True, view=False)
    def login(ctx):
        return ctx.ask_text("nas_user", "👤 نام کاربری NAS را بفرستید:", back=("nas", []))

    @app.text_input("nas_user")
    def user_input(ctx, text, msg):
        user = text.strip()
        ctx.msg_id = None
        if not user or len(user) > 32:
            return login_screen(ctx, "❌ نام کاربری نامعتبر است.")
        return ctx.ask_text("nas_pass", "🔑 رمز کاربر <b>%s</b> را بفرستید (پیام بلافاصله پاک می‌شود):" % esc(user), user,
                            back=("nas", []))

    @app.text_input("nas_pass")
    def pass_input(ctx, text, msg, user):
        app.bot.delete(ctx.chat_id, msg.get("message_id"))
        ctx.msg_id = None
        try:
            uid, gid, home = auth.check(ctx.chat_id, user, text)
        except nas.NasError as e:
            return login_screen(ctx, "❌ %s" % esc(e))
        ctx.session()["nas"] = {"user": user, "uid": uid, "gid": gid, "home": home, "last": None,
                                "exp": time.time() + int(nconf().get("session_minutes", 30)) * 60}
        ctx.session()["nas_was"] = True
        if ctx.session().get("nas_pending"):
            return incoming_screen(ctx)
        return main(ctx, "✅ وارد شدید")

    @app.action("nas_logout", view=False)
    def logout(ctx):
        ctx.session()["nas"] = None
        ctx.session()["nas_was"] = False
        return login_screen(ctx, "🚪 از حساب NAS خارج شدید.")

    # -------------------------------------------------------- browse
    @app.action("nas_ls")
    def ls(ctx, path, page=0, notice=None):
        s, other = guard(ctx)
        if other:
            return other
        name, root, rel = where(path)
        if not root:
            return main(ctx, "این مسیر جزو پوشه‌های اشتراکی نیست")
        try:
            r = nas.run_as(s, root, "list", path)
        except nas.NasError as e:
            return Screen("❌ %s: %s" % (title(path), esc(e)), back=("nas", []))
        s["last"] = r["path"]
        items = sorted(r["items"], key=lambda e: (not e["dir"], e["name"].lower()))
        items = [e for e in items if not e["name"].startswith(".") or e["name"] in (".", "..")]
        pages = max(1, (len(items) + PAGE - 1) // PAGE)
        page = max(0, min(int(page), pages - 1))
        dirs = sum(1 for e in items if e["dir"])
        lines = [title(r["path"]), "%s پوشه · %s فایل%s" % (fa(dirs), fa(len(items) - dirs),
                                                         "" if r["writable"] else " · 🔒 فقط خواندنی")]
        rows = []
        for e in items[page * PAGE:(page + 1) * PAGE]:
            child = os.path.join(r["path"], e["name"])
            if e["dir"]:
                rows.append([Btn("📁 %s" % e["name"], "nas_ls", child, 0, style=PRIMARY)])
            else:
                rows.append([Btn("📄 %s · %s" % (e["name"], human(e["size"])), "nas_file", child, style=PRIMARY)])
        if not items:
            lines.append("این پوشه خالی است.")
        rows.append(pager("nas_ls", [path], page, pages))
        pend = ctx.session().get("nas_pending")
        if pend and r["writable"]:
            rows.append([Btn("📥 ذخیره‌ی «%s» اینجا" % pend["name"][:30], "nas_save_here", r["path"], style=SUCCESS)])
        tools = []
        if r["writable"]:
            tools += [Btn("📤 آپلود اینجا", "nas_up", r["path"], style=SUCCESS), Btn("➕ پوشه‌ی تازه", "nas_mkdir", r["path"], style=SUCCESS)]
        rows.append(tools)
        up = os.path.dirname(r["path"])
        nav = [Btn("🏠 پوشه‌ها", "nas", style=PRIMARY)]
        if rel:
            nav.insert(0, Btn("⬆️ بالا", "nas_ls", up, 0, style=PRIMARY))
        rows.append(nav)
        sc = Screen("\n".join(lines), rows, refresh=("nas_ls", [path, page]))
        sc.notice = notice
        return sc

    @app.action("nas_file")
    def file_page(ctx, path, notice=None):
        s, other = guard(ctx)
        if other:
            return other
        name, root, rel = where(path)
        if not root:
            return main(ctx)
        try:
            st = nas.run_as(s, root, "stat", path)
        except nas.NasError as e:
            return Screen("❌ %s" % esc(e), back=("nas_ls", [os.path.dirname(path), 0]))
        lines = ["📄 <b>%s</b>" % esc(os.path.basename(path)), title(os.path.dirname(path)),
                 "📦 حجم: %s" % human(st["size"]),
                 "🕒 تغییر: %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(st["mtime"]))]
        rows = []
        if st.get("readable"):
            rows.append([Btn("📥 بفرست به تلگرام", "nas_send", path, style=SUCCESS)])
        if st.get("writable"):
            rows.append([Btn("✏️ تغییر نام", "nas_rename", path, style=PRIMARY),
                         Btn("🗑 حذف", "nas_del", path, confirm="فایل <b>%s</b> برای همیشه حذف شود؟" % esc(os.path.basename(path)))])
        sc = Screen("\n".join(lines), rows, back=("nas_ls", [os.path.dirname(path), 0]))
        sc.notice = notice
        return sc

    # -------------------------------------------------------- change things
    @app.action("nas_mkdir", write=True, view=False)
    def mkdir(ctx, d):
        return ctx.ask_text("nas_mkdir", "➕ اسم پوشه‌ی تازه را بفرستید:", d, back=("nas_ls", [d, 0]))

    @app.text_input("nas_mkdir")
    def mkdir_input(ctx, text, msg, d):
        s, other = guard(ctx)
        ctx.msg_id = None
        if other:
            return other
        try:
            nas.run_as(s, where(d)[1], "mkdir", d, text.strip())
        except nas.NasError as e:
            return ls(ctx, d, 0, "❌ %s" % e)
        log("nas: %s made folder %r in %s", s["user"], text.strip(), d)
        return ls(ctx, d, 0, "✅ پوشه ساخته شد")

    @app.action("nas_rename", write=True, view=False)
    def rename(ctx, path):
        return ctx.ask_text("nas_rename", "✏️ اسم تازه‌ی <b>%s</b> را بفرستید:" % esc(os.path.basename(path)), path,
                            back=("nas_file", [path]))

    @app.text_input("nas_rename")
    def rename_input(ctx, text, msg, path):
        s, other = guard(ctx)
        ctx.msg_id = None
        if other:
            return other
        try:
            r = nas.run_as(s, where(path)[1], "rename", path, text.strip())
        except nas.NasError as e:
            return file_page(ctx, path, "❌ %s" % e)
        log("nas: %s renamed %s", s["user"], path)
        return file_page(ctx, r["path"], "✅ اسم عوض شد")

    @app.action("nas_del", write=True, steps=1, view=False)
    def delete(ctx, path):
        s, other = guard(ctx)
        if other:
            return other
        try:
            nas.run_as(s, where(path)[1], "delete", path)
        except nas.NasError as e:
            return file_page(ctx, path, "❌ %s" % e)
        log("nas: %s deleted %s", s["user"], path)
        return ls(ctx, os.path.dirname(path), 0, "🗑 حذف شد")

    # -------------------------------------------------------- NAS -> Telegram
    @app.action("nas_send", write=True, view=False)
    def send(ctx, path):
        s, other = guard(ctx)
        if other:
            return other
        root = where(path)[1]
        try:
            st = nas.run_as(s, root, "stat", path)
        except nas.NasError as e:
            return file_page(ctx, path, "❌ %s" % e)
        if st["size"] > 2 * 1024 ** 3:
            return file_page(ctx, path, "❌ بیشتر از ۲ گیگ است؛ از SMB بردارید")
        ctx.answer("📤 در حال فرستادن…")
        threading.Thread(target=send_job, args=(ctx.chat_id, dict(s), root, path, st["size"]), daemon=True).start()
        return None

    def send_job(chat_id, s, root, path, size):
        name = os.path.basename(path)
        os.makedirs(config.TMP, exist_ok=True)
        tmp = os.path.join(config.TMP, "nas-%d-%d" % (chat_id, int(time.time() * 1000)))
        status = None
        try:
            status = app.bot.send(chat_id, "📤 <b>%s</b> (%s) — آماده‌سازی…" % (esc(name), human(size)))
            with open(os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600), "w+b") as out:
                nas.run_as(s, root, "get", path, stdout=out, timeout=1800)
            piece = int(nconf().get("send_piece_mb", 45)) * 1024 * 1024
            total = os.path.getsize(tmp)
            parts = max(1, (total + piece - 1) // piece)
            for i in range(parts):
                off = i * piece
                fname = name if parts == 1 else "%s.part%02d" % (name, i + 1)
                cap = "📁 %s" % esc(name) + ("" if parts == 1 else " — تکه‌ی %s از %s" % (fa(i + 1), fa(parts)))
                for attempt in range(4):
                    try:
                        app.bot.send_file("sendDocument", "document", chat_id, fname, path=tmp, offset=off,
                                          length=min(piece, total - off), caption=cap)
                        break
                    except (OSError, tg.TelegramError) as e:
                        if attempt == 3:
                            raise
                        log("nas send %s: %s (again)", fname, e)
                        time.sleep(3 * (attempt + 1))
                if status and parts > 1:
                    app.bot.edit(chat_id, status["message_id"], "📤 <b>%s</b>: %s از %s تکه فرستاده شد" % (esc(name), fa(i + 1), fa(parts)))
            if parts > 1:
                app.bot.send(chat_id, "🧩 «%s» در %s تکه آمد. برای یکی کردن: <code>cat \"%s\".part* &gt; \"%s\"</code> "
                             "(ویندوز: <code>copy /b \"%s.part01\"+\"%s.part02\"… \"%s\"</code>)" % (
                                 esc(name), fa(parts), esc(name), esc(name), esc(name), esc(name), esc(name)))
            if status:
                app.bot.delete(chat_id, status["message_id"])
            log("nas: %s sent %s (%d bytes) to chat %s", s["user"], path, total, chat_id)
        except (nas.NasError, OSError, tg.TelegramError) as e:
            app.bot.send(chat_id, "❌ فرستادن «%s» نشد: %s" % (esc(name), esc(e)))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    # -------------------------------------------------------- Telegram -> NAS
    @app.action("nas_up", write=True, view=False)
    def upload(ctx, d, notice=None):
        s, other = guard(ctx)
        if other:
            return other
        ctx.session()["input"] = {"kind": "nas_up", "args": [d], "exp": time.time() + UP_TTL}
        lim = int(nconf().get("max_upload_mb", 20))
        sc = Screen("📤 <b>آپلود به</b> %s\n\nهر چند فایل که می‌خواهید بفرستید (سند، عکس، ویدیو، صدا). هر کدام همین‌جا "
                    "ذخیره می‌شود.\n⚠️ تلگرام به ربات‌ها فقط فایل تا %s مگ می‌دهد؛ بزرگ‌تر را با SMB بریزید.\n"
                    "💡 عکس را «به صورت فایل» بفرستید تا کیفیتش کم نشود." % (title(d), fa(lim)),
                    [[Btn("✅ تمام", "nas_ls", d, 0, style=SUCCESS)]])
        sc.notice = notice
        return sc

    @app.text_input("nas_up")
    def up_input(ctx, text, msg, d):
        s, other = guard(ctx)
        if other:
            ctx.msg_id = None
            return other
        ctx.session()["input"] = {"kind": "nas_up", "args": [d], "exp": time.time() + UP_TTL}
        f = incoming_file(msg)
        if not f:
            app.bot.send(ctx.chat_id, "📎 یک فایل بفرستید (یا «✅ تمام» را بزنید).")
            return None
        start_save(ctx.chat_id, dict(s), d, f)
        return None

    def start_save(chat_id, s, d, f):
        threading.Thread(target=save_job, args=(chat_id, s, d, f), daemon=True).start()

    def save_job(chat_id, s, d, f):
        file_id, name, size = f
        name = safe_name(name)
        lim = int(nconf().get("max_upload_mb", 20)) * 1024 * 1024
        if size and size > lim:
            app.bot.send(chat_id, "❌ «%s» %s است؛ تلگرام فایل بیشتر از %s مگ را به ربات نمی‌دهد. با SMB بریزید." % (
                esc(name), human(size), fa(lim // 1048576)))
            return
        os.makedirs(config.TMP, exist_ok=True)
        tmp = os.path.join(config.TMP, "up-%d-%d" % (chat_id, int(time.time() * 1000)))
        status = None
        try:
            status = app.bot.send(chat_id, "⏳ ذخیره‌ی «%s»…" % esc(name))
            app.bot.download(file_id, tmp, limit=lim)
            os.chmod(tmp, 0o600)
            with open(tmp, "rb") as src:
                r = nas.run_as(s, nas.share_of(d)[1], "put", d, name, stdin=src, timeout=1800)
            text = "✅ <b>%s</b> ذخیره شد (%s)\n%s" % (esc(r["name"]), human(r["size"]), title(d))
            if r["name"] != name:
                text += "\n(اسم «%s» بود، پس با اسم تازه ذخیره شد)" % esc(name)
            app.bot.edit(chat_id, status["message_id"], text)
            log("nas: %s uploaded %s (%d bytes) into %s", s["user"], r["name"], r["size"], d)
        except (nas.NasError, OSError, tg.TelegramError) as e:
            msg = "❌ «%s» ذخیره نشد: %s" % (esc(name), esc(e))
            if status:
                app.bot.edit(chat_id, status["message_id"], msg)
            else:
                app.bot.send(chat_id, msg)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def incoming_screen(ctx):
        pend = ctx.session().get("nas_pending")
        s = sess(ctx)
        lines = ["📥 فایل <b>%s</b> (%s) رسید. کجا در NAS ذخیره شود؟" % (esc(pend["name"]), human(pend["size"]))]
        rows = []
        if s and s.get("last"):
            rows.append([Btn("💾 در «%s»" % os.path.basename(s["last"].rstrip("/")), "nas_save_here", s["last"], style=SUCCESS)])
        rows.append([Btn("📁 انتخاب پوشه", "nas", style=PRIMARY), Btn("✖️ ذخیره نکن", "nas_forget", style=PRIMARY)])
        return Screen("\n".join(lines), rows)

    @app.action("nas_incoming", view=False)
    def incoming(ctx, file_id, name, size):
        if not ctx.owner:
            return None
        ctx.session()["nas_pending"] = {"file_id": file_id, "name": safe_name(name), "size": int(size or 0)}
        if not sess(ctx):
            return login_screen(ctx, "📥 فایل <b>%s</b> رسید. برای ذخیره در NAS اول وارد شوید." % esc(name))
        return incoming_screen(ctx)

    @app.action("nas_save_here", write=True, view=False)
    def save_here(ctx, d):
        s, other = guard(ctx)
        if other:
            return other
        pend = ctx.session().pop("nas_pending", None)
        if not pend:
            return ls(ctx, d, 0, "فایلی منتظر نیست")
        start_save(ctx.chat_id, dict(s), d, (pend["file_id"], pend["name"], pend["size"]))
        return ls(ctx, d, 0, "⏳ در حال ذخیره…")

    @app.action("nas_forget", view=False)
    def forget(ctx):
        ctx.session().pop("nas_pending", None)
        return Screen("باشه، ذخیره نشد.", back=("menu", []))
ERISBOT_EOF_s_files_py
  put_file "$STAGE/erisbot/s_main.py" 0644 << 'ERISBOT_EOF_s_main_py'
"""Main menu, dashboard, confirmations, server power and settings."""
import os
import re
import time

from . import alerts, config, eris, fand, marzban, net, system, tg
from .ui import DANGER, PRIMARY, SUCCESS, Btn, Screen
from .util import ago, bar, duration, esc, fa, human, log


def dashboard_text(app):
    lines = ["📊 <b>داشبورد</b> — %s" % esc(system.hostname())]
    up = system.uptime()
    lines.append("⏱ آپتایم: %s" % duration(up, 2))
    ct = system.cpu_temp()
    dt = system.disk_temps()
    t = ["CPU %s°C" % fa("%.0f" % ct)] if ct is not None else []
    t += ["%s %s°C" % (d, fa("%.0f" % v)) for d, v in sorted(dt.items())]
    ot = system.other_temps()
    t += ["%s %s°C" % (n, fa("%.0f" % v)) for n, v in sorted(ot.items()) if n in ("nouveau", "amdgpu", "radeon")]
    lines.append("🌡 دما: %s" % ("، ".join(t) if t else "سنسوری پیدا نشد"))
    cu = system.cpu_usage(0.3)
    la = system.loadavg()
    lines.append("⚙️ CPU: %s  %s٪ · بار %s" % ("<code>%s</code>" % bar(cu or 0, 10), fa("%.0f" % (cu or 0)), fa("%.2f" % la[0])))
    m = system.memory()
    if m["total"]:
        lines.append("🧠 رم: <code>%s</code> %s٪ · %s از %s" % (bar(m["pct"] or 0, 10), fa("%.0f" % (m["pct"] or 0)),
                                                             human(m["used"]), human(m["total"])))
    for d in system.disks()[:6]:
        lines.append("💽 %s: <code>%s</code> %s٪ · %s از %s" % (esc(d["mount"]), bar(d["pct"], 10), fa("%.0f" % d["pct"]),
                                                               human(d["used"]), human(d["total"])))
    if app.marzban.available():
        try:
            users = app.marzban.all_users()
            now_names = marzban.online_now(users)
            lines.append("👥 وصل همین الان (%s): %s" % (fa(len(now_names)), "، ".join(esc(n) for n in now_names[:30]) or "هیچ‌کس"))
            upl, down = app.marzban.today_usage()
            lines.append("📶 ترافیک امروز: %s (↑ %s · ↓ %s)" % (human(upl + down), human(upl), human(down)))
            nodes = app.marzban.nodes()
            if nodes:
                lines.append("🛰 نودها: " + "  ".join("%s %s" % (node_icon(n.get("status")), esc(n.get("name"))) for n in nodes))
        except marzban.MarzbanError as e:
            lines.append("👥 مرزبان: ❌ %s" % esc(e))
    act = app.alerts.active()
    if act:
        lines.append("🔔 هشدارهای فعال (%s):" % fa(len(act)))
        for a in sorted(act.values(), key=lambda a: -a["level"])[:8]:
            lines.append("  %s %s" % ("🔴" if a["level"] >= 2 else "🟠", a["text"]))
    else:
        lines.append("🔔 هشدار فعالی نیست ✅")
    if eris.available():
        try:
            s = eris.backup_status()
            parts = []
            full = s.get("full") or {}
            if full.get("enabled"):
                parts.append("کلی %s" % backup_icon(full))
            for name, p in (s.get("parts") or {}).items():
                if p.get("present") and p.get("enabled"):
                    parts.append("%s %s" % (name, backup_icon(p)))
            if parts:
                lines.append("💾 آخرین بکاپ: " + "، ".join(parts))
        except eris.ErisError as e:
            lines.append("💾 بکاپ: %s" % esc(e))
    lines.append("<i>%s</i>" % time.strftime("%H:%M:%S"))
    return "\n".join(lines)


def route_fa(r):
    return "مستقیم" if not r else net.describe(r)


def backup_icon(p):
    if p.get("failed"):
        return "❌"
    if p.get("last_ok_epoch"):
        return "✅ %s" % ago(p["last_ok_epoch"])
    return "—"


def node_icon(status):
    return {"connected": "🟢", "connecting": "🟡", "disabled": "⚪️"}.get(status, "🔴")


def menu_screen(app, ctx):
    lines = ["🏠 <b>منوی ERIS</b> — %s" % esc(system.hostname()),
             "⏱ آپتایم: %s" % duration(system.uptime(), 2)]
    act = app.alerts.active()
    lines.append("🔔 %s" % ("%s هشدار فعال" % fa(len(act)) if act else "هشدار فعالی نیست"))
    if ctx.role == "viewer":
        lines.append("👁 دسترسی شما: فقط دیدن")
    btns = [Btn("📊 داشبورد", "dash", style=PRIMARY)]
    if app.marzban.available():
        btns.append(Btn("👥 مرزبان", "mz", style=PRIMARY))
    btns.append(Btn("🖥 سرویس‌ها", "svc", 0, style=PRIMARY))
    if eris.available():
        btns.append(Btn("🛡 فایروال", "fw", style=PRIMARY))
        btns.append(Btn("💾 بکاپ", "bk", style=PRIMARY))
    btns += [Btn("🤖 هوش مصنوعی", "ai", style=PRIMARY), Btn("📁 فایل‌های NAS", "nas", style=PRIMARY),
             Btn("🌤 آب و هوا", "wx", style=PRIMARY),
             Btn("🔔 هشدارها", "al", style=PRIMARY), Btn("🌀 فن و عملکرد", "fan", style=PRIMARY),
             Btn("🔁 سرور", "server", style=PRIMARY), Btn("⚙️ تنظیمات", "set", style=PRIMARY)]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    return Screen("\n".join(lines), rows, refresh=("menu", []))


def register(app):
    @app.action("noop")
    def noop(ctx):
        return None

    @app.action("menu")
    def menu(ctx):
        return menu_screen(app, ctx)

    @app.action("dash")
    def dash(ctx):
        return Screen(dashboard_text(app), refresh=("dash", []), back=("menu", []))

    @app.action("cancel_input", view=False)
    def cancel_input(ctx, back="menu", args=None):
        ctx.session()["input"] = None
        return app.run(ctx, back, args or []) and None

    # -------------------------------------------------------- confirmations (red buttons, 60 s)
    def ask_screen(ctx, action, args, question, steps, step):
        title = "⚠️ <b>تأیید لازم است</b>" if step == 1 else "⚠️⚠️ <b>تأیید نهایی</b>"
        lines = [title, "", question]
        if steps > 1:
            lines.append("")
            lines.append("مرحله‌ی %s از %s" % (fa(step), fa(steps)))
        lines.append("⏳ این دکمه فقط ۶۰ ثانیه کار می‌کند.")
        back = ctx.session().get("screen") or ("menu", [])
        yes = Btn("🔴 بله، ادامه" if step < steps else "🔴 بله، انجام بده", "_yes", action, args, question, steps, step,
                  style=DANGER, once=True, ttl=60)
        no = Btn("✖️ لغو", back[0], *back[1], style=PRIMARY)
        return Screen("\n".join(lines), [[yes], [no]])

    @app.action("_ask", view=False)
    def ask(ctx, action, args, question, steps, step):
        act = app.actions.get(action)
        if act is None:
            return None
        if act.write and not ctx.owner:
            ctx.answer("🔒 این کار فقط برای مالک است.", alert=True)
            return None
        return ask_screen(ctx, action, args, question, steps, step)

    @app.action("_yes", view=False)
    def yes(ctx, action, args, question, steps, step):
        if step < steps:
            return ask_screen(ctx, action, args, question, steps, step + 1)
        log("confirmed %s %s by chat %s", action, args, ctx.chat_id)
        app.run(ctx, action, args, confirmed=True)
        return None

    # -------------------------------------------------------- server
    @app.action("server")
    def server(ctx):
        up = system.uptime()
        lines = ["🔁 <b>سرور</b> — %s" % esc(system.hostname()),
                 "⏱ آپتایم: %s" % duration(up, 3),
                 "🕒 روشن از: %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - up)),
                 "", "ری‌استارت و خاموش کردن دو بار تأیید می‌خواهند."]
        rows = [[Btn("🔁 ری‌استارت سرور", "server_reboot", confirm="سرور <b>ری‌استارت</b> شود؟ همه‌ی سرویس‌ها و کاربرها چند دقیقه قطع می‌شوند.", steps=2)],
                [Btn("⏻ خاموش کردن سرور", "server_off", confirm="سرور <b>خاموش</b> شود؟ برای روشن کردن دوباره باید کسی دکمه‌ی پاور را بزند.", steps=2)]]
        return Screen("\n".join(lines), rows, refresh=("server", []), back=("menu", []))

    @app.action("server_reboot", write=True, steps=2)
    def server_reboot(ctx):
        log("reboot asked by chat %s", ctx.chat_id)
        ctx.show(Screen("🔁 سرور در حال ری‌استارت است… بعد از بالا آمدن خبر می‌دهم."))
        r = system.reboot()
        if r.returncode != 0:
            return Screen("❌ ری‌استارت نشد: %s" % esc(r.stderr or r.stdout), back=("server", []))
        return None

    @app.action("server_off", write=True, steps=2)
    def server_off(ctx):
        log("poweroff asked by chat %s", ctx.chat_id)
        ctx.show(Screen("⏻ سرور در حال خاموش شدن است."))
        r = system.poweroff()
        if r.returncode != 0:
            return Screen("❌ خاموش نشد: %s" % esc(r.stderr or r.stdout), back=("server", []))
        return None

    # -------------------------------------------------------- settings
    @app.action("set")
    def settings(ctx):
        c = app.conf
        bs = c.get("backup_send") or {}
        pw = app.sender.password_file()
        mode = {"after_nightly": "بعد از بکاپ شبانه", "at": "ساعت %s" % bs.get("at"), "off": "خاموش"}.get(bs.get("mode"), "؟")
        parts = config.send_parts(c)
        dr = c.get("daily_report") or {}
        colors = "خاموش" if c.get("button_colors") == "off" else ("رد شده توسط API" if app.state.get("styles_refused") else "روشن")
        lines = ["⚙️ <b>تنظیمات</b>",
                 "🌐 پروکسی: <code>%s</code>%s" % (esc(c.get("proxy")), " (الان: %s)" % esc(route_fa(app.bot.router.current))
                                                  if c.get("proxy") == "auto" and app.bot.router.chosen else ""),
                 "👥 chat IDها: %s" % fa(len(c["allowed_chat_ids"])),
                 "💾 ارسال بکاپ: %s · بخش‌ها: %s" % (mode, "همه" if len(parts) == len(config.PARTS) else "، ".join(parts) or "هیچ"),
                 "🔑 رمز بکاپ: %s" % ("تنظیم شده ✅" if os.path.isfile(pw) and os.path.getsize(pw) > 0 else "❌ تنظیم نشده"),
                 "🗓 گزارش روزانه: %s" % (("ساعت " + dr.get("at", "09:00")) if dr.get("enabled") else "خاموش"),
                 "🎨 رنگ دکمه‌ها: %s" % colors,
                 "🧩 اندازه‌ی هر تکه‌ی ارسال: %s MB" % fa(c.get("part_size_mb", 45)),
                 "📍 IP ثابت: %s" % (("<code>%s</code>" % esc(alerts.static_ip(c))) if alerts.static_ip(c) else "تنظیم نشده")]
        rows = [[Btn("🌐 پروکسی", "set_proxy", style=PRIMARY), Btn("👥 chat IDها", "set_chats", style=PRIMARY)],
                [Btn("💾 ارسال بکاپ", "set_send", style=PRIMARY), Btn("🔑 رمز بکاپ", "set_pw", style=PRIMARY)],
                [Btn("🗓 گزارش روزانه", "set_report", style=PRIMARY), Btn("🎨 رنگ دکمه‌ها", "set_colors", style=PRIMARY)],
                [Btn("🧩 اندازه‌ی تکه", "set_piece", style=PRIMARY), Btn("📍 IP عمومی", "set_ip", style=PRIMARY)]]
        return Screen("\n".join(lines), rows, refresh=("set", []), back=("menu", []))

    @app.action("set_proxy")
    def set_proxy(ctx, result=None):
        c = app.conf
        lines = ["🌐 <b>پروکسی تلگرام</b>", "تنظیم: <code>%s</code>" % esc(net.describe(c["proxy"]) if c["proxy"] not in ("auto", "off") else c["proxy"])]
        if app.bot.router.chosen:
            lines.append("مسیر فعلی: %s" % esc(route_fa(app.bot.router.current)))
        lines.append("auto = اول مستقیم، بعد به ترتیب:")
        for p in c.get("proxy_list") or []:
            lines.append("  • <code>%s</code>" % esc(net.describe(p)))
        if result:
            lines += ["", "🧪 <b>تست زنده (getMe)</b>"] + result
        choices = [Btn(("✅ " if c["proxy"] == v else "") + t, "set_proxy_to", v, style=SUCCESS if c["proxy"] == v else PRIMARY)
                   for v, t in (("auto", "خودکار"), ("off", "مستقیم"))]
        rows = [choices]
        for p in c.get("proxy_list") or []:
            rows.append([Btn(("✅ " if c["proxy"] == p else "") + net.describe(p), "set_proxy_to", p,
                             style=SUCCESS if c["proxy"] == p else PRIMARY)])
        rows.append([Btn("🧪 تست همه", "proxy_test", style=SUCCESS), Btn("➕ پروکسی دیگر", "proxy_add", style=PRIMARY)])
        return Screen("\n".join(lines), rows, refresh=("set_proxy", []), back=("set", []))

    @app.action("proxy_test", view=False)
    def proxy_test(ctx):
        ctx.answer("در حال تست…")
        result = []
        for r in [None] + list(app.conf.get("proxy_list") or []):
            ok, sec, err = app.bot.probe(r, timeout=10)
            result.append("%s %s — %s" % ("✅" if ok else "❌", esc(route_fa(r)),
                                          ("%s ms" % fa(int(sec * 1000))) if ok else esc((err or "")[:80])))
        if app.conf["proxy"] not in ("auto", "off") and app.conf["proxy"] not in (app.conf.get("proxy_list") or []):
            ok, sec, err = app.bot.probe(app.conf["proxy"], timeout=10)
            result.append("%s %s — %s" % ("✅" if ok else "❌", esc(net.describe(app.conf["proxy"])),
                                          ("%s ms" % fa(int(sec * 1000))) if ok else esc((err or "")[:80])))
        return set_proxy(ctx, result)

    @app.action("set_proxy_to", write=True, view=False)
    def set_proxy_to(ctx, value):
        if not config.validate_proxy(value):
            return Screen("❌ پروکسی نامعتبر", back=("set_proxy", []))
        app.save_conf(proxy=value)
        log("proxy set to %s by chat %s", net.describe(value) if value not in ("auto", "off") else value, ctx.chat_id)
        return set_proxy(ctx)

    @app.action("proxy_add", write=True, view=False)
    def proxy_add(ctx):
        return ctx.ask_text("proxy", "➕ آدرس پروکسی را بفرستید:\n<code>http://127.0.0.1:1090</code>\n<code>socks5://user:pass@127.0.0.1:1080</code>",
                            back=("set_proxy", []))

    @app.text_input("proxy")
    def proxy_input(ctx, text, msg):
        v = text.strip()
        app.bot.delete(ctx.chat_id, msg.get("message_id"))     # it may hold a password
        if not config.validate_proxy(v) or v in ("auto", "off"):
            return Screen("❌ نامعتبر. مثال: <code>socks5://127.0.0.1:1080</code>", back=("set_proxy", []))
        lst = list(app.conf.get("proxy_list") or [])
        if v not in lst:
            lst.append(v)
        app.save_conf(proxy_list=lst)
        ctx.msg_id = None
        return set_proxy(ctx)

    @app.action("set_chats")
    def set_chats(ctx):
        c = app.conf
        lines = ["👥 <b>chat IDهای مجاز</b>", "بقیه هیچ جوابی نمی‌گیرند (فقط در لاگ ثبت می‌شوند)."]
        rows = []
        for cid in c["allowed_chat_ids"]:
            role = config.role(c, cid)
            lines.append("• <code>%s</code> — %s" % (cid, "مالک (همه‌چیز)" if role == "owner" else "بیننده (فقط دیدن)"))
            rows.append([Btn("🔄 %s: %s" % (cid, "بیننده شود" if role == "owner" else "مالک شود"), "chat_role", cid, style=PRIMARY),
                         Btn("🗑 %s" % cid, "chat_del", cid, confirm="chat ID <code>%s</code> حذف شود؟" % cid)])
        rows.append([Btn("➕ افزودن chat ID", "chat_add", style=SUCCESS)])
        return Screen("\n".join(lines), rows, refresh=("set_chats", []), back=("set", []))

    def owners_after(c, ids, roles):
        return [i for i in ids if (roles.get(str(i), "owner")) == "owner"]

    @app.action("chat_role", write=True, view=False)
    def chat_role(ctx, cid):
        c = app.conf
        roles = dict(c.get("roles") or {})
        cur = config.role(c, cid)
        roles[str(cid)] = "viewer" if cur == "owner" else "owner"
        if not owners_after(c, c["allowed_chat_ids"], roles):
            ctx.answer("حداقل یک مالک باید بماند.", alert=True)
            return set_chats(ctx)
        app.save_conf(roles=roles)
        return set_chats(ctx)

    @app.action("chat_del", write=True, steps=1, view=False)
    def chat_del(ctx, cid):
        c = app.conf
        ids = [i for i in c["allowed_chat_ids"] if i != cid]
        roles = {k: v for k, v in (c.get("roles") or {}).items() if k != str(cid)}
        if not owners_after(c, ids, roles):
            return Screen("❌ حداقل یک مالک باید بماند.", back=("set_chats", []))
        app.save_conf(allowed_chat_ids=ids, roles=roles)
        return set_chats(ctx)

    @app.action("chat_add", write=True, view=False)
    def chat_add(ctx):
        return ctx.ask_text("chat_id", "➕ chat ID را بفرستید (عدد؛ گروه‌ها منفی‌اند):", back=("set_chats", []))

    @app.text_input("chat_id")
    def chat_input(ctx, text, msg):
        if not re.fullmatch(r"-?\d{3,20}", text.strip()):
            return Screen("❌ chat ID باید عدد باشد.", back=("set_chats", []))
        ids = list(app.conf["allowed_chat_ids"])
        if int(text) not in ids:
            ids.append(int(text))
        roles = dict(app.conf.get("roles") or {})
        roles.setdefault(str(int(text)), "viewer")
        app.save_conf(allowed_chat_ids=ids, roles=roles)
        return set_chats(ctx)

    @app.action("set_send")
    def set_send(ctx):
        c = app.conf
        bs = c.get("backup_send") or {}
        sel = config.send_parts(c)
        lines = ["💾 <b>ارسال خودکار بکاپ</b>", "فقط بکاپ بخش‌ها فرستاده می‌شود (رمزنگاری‌شده)؛ بکاپ کلی هرگز.",
                 "حالت: %s" % {"after_nightly": "بعد از بکاپ شبانه", "at": "ساعت %s (بکاپ تازه)" % bs.get("at"),
                               "off": "خاموش"}.get(bs.get("mode"), "؟")]
        rows = [[Btn(("✅ " if bs.get("mode") == m else "") + t, "send_mode", m, style=SUCCESS if bs.get("mode") == m else PRIMARY)
                 for m, t in (("after_nightly", "بعد از شبانه"), ("at", "ساعت جدا"), ("off", "خاموش"))]]
        if bs.get("mode") == "at":
            rows.append([Btn(("✅ " if bs.get("at") == h else "") + h, "send_at", h, style=PRIMARY) for h in ("02:30", "04:30", "06:30")]
                        + [Btn("✏️ ساعت دیگر", "send_at_ask", style=PRIMARY)])
        btns = [Btn(("✅ " if p in sel else "⬜️ ") + p, "send_part", p, style=SUCCESS if p in sel else PRIMARY) for p in config.PARTS]
        rows += [btns[i:i + 3] for i in range(0, len(btns), 3)]
        return Screen("\n".join(lines), rows, refresh=("set_send", []), back=("set", []))

    def save_send(**kw):
        bs = dict(app.conf.get("backup_send") or {})
        bs.update(kw)
        app.save_conf(backup_send=bs)

    @app.action("send_mode", write=True, view=False)
    def send_mode(ctx, mode):
        if mode in ("after_nightly", "at", "off"):
            save_send(mode=mode)
        return set_send(ctx)

    @app.action("send_at", write=True, view=False)
    def send_at(ctx, at):
        if config.HHMM_RX.match(at):
            save_send(at=at)
        return set_send(ctx)

    @app.action("send_at_ask", write=True, view=False)
    def send_at_ask(ctx):
        return ctx.ask_text("send_at", "🕒 ساعت ارسال را به شکل HH:MM بفرستید (مثلاً 05:15):", back=("set_send", []))

    @app.text_input("send_at")
    def send_at_input(ctx, text, msg):
        if not config.HHMM_RX.match(text.strip()):
            return Screen("❌ به شکل HH:MM بفرستید.", back=("set_send", []))
        save_send(at=text.strip())
        return set_send(ctx)

    @app.action("send_part", write=True, view=False)
    def send_part(ctx, part):
        sel = config.send_parts(app.conf)
        sel = [p for p in sel if p != part] if part in sel else sel + [part]
        save_send(parts=[p for p in config.PARTS if p in sel] or [])
        return set_send(ctx)

    @app.action("set_pw", write=True)
    def set_pw(ctx):
        pw = app.sender.password_file()
        have = os.path.isfile(pw) and os.path.getsize(pw) > 0
        return ctx.ask_text("password", "🔑 <b>رمز بکاپ</b>\n%s\nرمز جدید را بفرستید (حداقل ۱۲ نویسه). پیام شما بلافاصله پاک می‌شود و رمز هیچ‌جا نشان داده نمی‌شود.\n⚠️ بدون این رمز فایل‌های ارسال‌شده باز نمی‌شوند؛ جای امنی نگهش دارید." % (
            "الان تنظیم شده است." if have else "هنوز تنظیم نشده."), back=("set", []))

    @app.text_input("password")
    def pw_input(ctx, text, msg):
        app.bot.delete(ctx.chat_id, msg.get("message_id"))
        if len(text) < 12:
            return Screen("❌ رمز کوتاه است (حداقل ۱۲ نویسه). پیام پاک شد.", back=("set", []))
        write_password(app.sender.password_file(), text)
        log("backup password changed by chat %s", ctx.chat_id)
        ctx.msg_id = None
        return Screen("✅ رمز بکاپ ذخیره شد (و پیام شما پاک شد).", back=("set", []))

    @app.action("set_report")
    def set_report(ctx):
        dr = app.conf.get("daily_report") or {}
        on = dr.get("enabled")
        rows = [[Btn("✅ روشن" if not on else "⏹ خاموش کن", "report_toggle", style=SUCCESS if not on else DANGER)],
                [Btn(("✅ " if dr.get("at") == h else "") + h, "report_at", h, style=PRIMARY) for h in ("08:00", "09:00", "12:00", "21:00")]]
        return Screen("🗓 <b>گزارش روزانه</b>\nهر روز یک داشبورد خلاصه.\nوضعیت: %s · ساعت %s" % (
            "روشن" if on else "خاموش", dr.get("at", "09:00")), rows, back=("set", []))

    @app.action("report_toggle", write=True, view=False)
    def report_toggle(ctx):
        dr = dict(app.conf.get("daily_report") or {})
        dr["enabled"] = not dr.get("enabled")
        app.save_conf(daily_report=dr)
        return set_report(ctx)

    @app.action("report_at", write=True, view=False)
    def report_at(ctx, at):
        dr = dict(app.conf.get("daily_report") or {})
        if config.HHMM_RX.match(at):
            dr["at"] = at
        app.save_conf(daily_report=dr)
        return set_report(ctx)

    @app.action("set_colors")
    def set_colors(ctx):
        c = app.conf
        lines = ["🎨 <b>رنگ دکمه‌ها</b> (Bot API 9.4، فیلد style)",
                 "سبز = کار امن، آبی = منو، قرمز = کار خطرناک.",
                 "تلگرام‌های قدیمی‌تر از فوریه ۲۰۲۶ رنگ را نشان نمی‌دهند (دکمه‌ها کار می‌کنند).",
                 "وضعیت: %s" % ("خاموش" if c.get("button_colors") == "off" else "روشن")]
        if app.state.get("styles_refused"):
            lines.append("⚠️ API رنگ را رد کرد: %s" % esc(app.state.get("styles_refused_reason") or ""))
        rows = [[Btn("روشن (auto)", "colors_to", "auto", style=SUCCESS), Btn("خاموش", "colors_to", "off", style=PRIMARY)]]
        return Screen("\n".join(lines), rows, back=("set", []))

    @app.action("colors_to", write=True, view=False)
    def colors_to(ctx, v):
        if v in ("auto", "off"):
            app.save_conf(button_colors=v)
            if v == "auto":
                app.state.set("styles_refused", False)
        return set_colors(ctx)

    # -------------------------------------------------------- public IP (static, the alert)
    @app.action("set_ip")
    def set_ip(ctx, check=False, notice=None):
        c = app.conf
        st = alerts.static_ip(c)
        lines = ["📍 <b>IP عمومی</b>",
                 "IP از چند سرویس پرسیده می‌شود و فقط وقتی حساب است که دست‌کم دو تا یکی بگویند؛ تغییر هم فقط "
                 "بعد از دو بار پشت هم اعلام می‌شود (جواب‌های پرت حساب نمی‌شوند).",
                 "IP ثابت: %s" % ("<code>%s</code> — هشدار فقط وقتی IP چیز دیگری باشد" % esc(st) if st else "تنظیم نشده")]
        found = None
        if check:
            found, answers = alerts.consensus_ip(alerts.public_ip_urls(c))
            lines += ["", "🧪 <b>همین الان</b>"]
            for u, ip in answers:
                lines.append("• %s: %s" % (esc(u.split("//", 1)[-1].split("/", 1)[0]), "<code>%s</code>" % esc(ip) if ip else "❌"))
            lines.append("نتیجه: %s" % ("<code>%s</code>" % esc(found) if found else "توافقی نشد"))
        rows = [[Btn("🧪 بررسی الان", "set_ip", True, style=PRIMARY)]]
        if found and found != st:
            rows.append([Btn("📌 %s را IP ثابت کن" % found, "ip_static", found, style=SUCCESS)])
        rows.append([Btn("✏️ نوشتن IP ثابت", "ip_static_ask", style=PRIMARY)] +
                    ([Btn("❌ بدون IP ثابت", "ip_static", "", style=PRIMARY)] if st else []))
        s = Screen("\n".join(lines), rows, back=("set", []))
        s.notice = notice
        return s

    @app.action("ip_static", write=True, view=False)
    def ip_static(ctx, ip):
        pi = dict(app.conf.get("public_ip") or {})
        pi["static"] = ip
        app.save_conf(public_ip=pi)
        app.sources.st.update(ip_candidate=None, ip_streak=0)
        log("static IP set to %r by chat %s", ip, ctx.chat_id)
        return set_ip(ctx, False, "✅ IP ثابت: %s" % ip if ip else "IP ثابت برداشته شد")

    @app.action("ip_static_ask", write=True, view=False)
    def ip_static_ask(ctx):
        return ctx.ask_text("ip_static", "✏️ IP ثابت سرور را بفرستید (مثلاً <code>203.0.113.7</code>):", back=("set_ip", []))

    @app.text_input("ip_static")
    def ip_static_input(ctx, text, msg):
        import ipaddress
        try:
            ip = str(ipaddress.ip_address(text.strip()))
        except ValueError:
            return Screen("❌ IP نامعتبر است.", back=("set_ip", []))
        ctx.msg_id = None
        return ip_static(ctx, ip)

    @app.action("set_piece")
    def set_piece(ctx):
        cur = int(app.conf.get("part_size_mb", 45))
        rows = [[Btn(("✅ " if cur == n else "") + "%s MB" % fa(n), "piece_to", n, style=PRIMARY) for n in (10, 20, 30, 45)]]
        return Screen("🧩 <b>اندازه‌ی هر تکه</b>\nفایل بزرگ‌تر به تکه‌های .part01، .part02… تقسیم می‌شود.\n"
                      "اگر آپلودها وسط کار قطع می‌شوند، تکه‌ی کوچک‌تر انتخاب کنید.\nالان: %s MB" % fa(cur), rows, back=("set", []))

    @app.action("piece_to", write=True, view=False)
    def piece_to(ctx, n):
        if n in (10, 20, 30, 45):
            app.save_conf(part_size_mb=n)
        return set_piece(ctx)


def write_password(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text.strip() + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def fans_line():
    s = fand.read_status()
    return s


def tg_errors():
    return (tg.TelegramError, OSError)
ERISBOT_EOF_s_main_py
  put_file "$STAGE/erisbot/s_mz.py" 0644 << 'ERISBOT_EOF_s_mz_py'
"""Marzban: overview, users (pages, filters, search), one user, create a user, nodes."""
import re
import time

from . import config, marzban, qr
from .ui import DANGER, PRIMARY, SUCCESS, Btn, Screen, pager
from .util import ago, esc, fa, fmt_num, human, log, parse_amount, until


def config_defaults():
    return config.DEFAULTS["mz_presets"]

PAGE = 8
STATUS_FA = {"active": "🟢 فعال", "disabled": "⚪️ غیرفعال", "limited": "🟠 محدود (حجم)", "expired": "🔴 منقضی",
             "on_hold": "🟡 در انتظار"}
STATUS_ICON = {"active": "🟢", "disabled": "⚪️", "limited": "🟠", "expired": "🔴", "on_hold": "🟡"}
NAME_RX = re.compile(r"^[a-z0-9_]{3,32}$")
GB = 1024 ** 3


def user_line(u):
    lim = u.get("data_limit")
    used = u.get("used_traffic") or 0
    return "%s %s · %s/%s" % (STATUS_ICON.get(u.get("status"), "❔"), u.get("username"), human(used, 1),
                              human(lim, 0) if lim else "∞")


def register(app):
    mz = app.marzban

    @app.action("mz")
    def overview(ctx):
        s = mz.system()
        users = mz.all_users()
        now_names = marzban.online_now(users)
        up, down = mz.today_usage()
        core = mz.core()
        lines = ["👥 <b>مرزبان</b> %s · Xray %s %s" % (esc(s.get("version") or ""), esc(core.get("version") or "؟"),
                                                     "🟢" if core.get("started") else "🔴"),
                 "کاربرها: %s · فعال %s · محدود %s · منقضی %s · غیرفعال %s · در انتظار %s" % tuple(
                     fa(s.get(k, 0)) for k in ("total_user", "users_active", "users_limited", "users_expired",
                                               "users_disabled", "users_on_hold")),
                 "🟢 وصل همین الان (۲ دقیقه‌ی اخیر): %s" % fa(len(now_names)),
                 "  " + ("، ".join(esc(n) for n in now_names[:40]) or "—"),
                 "🕘 آنلاین در ۲۴ ساعت: %s" % fa(s.get("online_users", 0)),
                 "📶 ترافیک امروز: %s (↑ %s · ↓ %s)" % (human(up + down), human(up), human(down)),
                 "⚡️ سرعت: ↓ %s/s · ↑ %s/s" % (human(s.get("incoming_bandwidth_speed")), human(s.get("outgoing_bandwidth_speed"))),
                 "", "🔍 برای جستجو، اسم کاربر را بفرستید."]
        rows = [[Btn("👥 کاربرها", "mz_users", "", 0, style=PRIMARY), Btn("🛰 نودها", "mz_nodes", style=PRIMARY)],
                [Btn("➕ ساخت کاربر", "mz_new", style=SUCCESS), Btn("🔍 جستجو", "mz_search_ask", style=PRIMARY)]]
        return Screen("\n".join(lines), rows, refresh=("mz", []), back=("menu", []))

    @app.action("mz_users")
    def users(ctx, status="", page=0, search=""):
        r = mz.users(offset=page * PAGE, limit=PAGE, status=status or None, search=search or None)
        total = r.get("total", 0)
        pages = max(1, (total + PAGE - 1) // PAGE)
        title = {"": "همه"}.get(status, STATUS_FA.get(status, status))
        lines = ["👥 <b>کاربرها</b> — %s%s · %s نفر" % (title, " · «%s»" % esc(search) if search else "", fa(total))]
        rows = []
        for u in r.get("users", []):
            rows.append([Btn(user_line(u), "mz_user", u["username"], style=PRIMARY)])
        if not r.get("users"):
            lines.append("کسی پیدا نشد.")
        if search:
            rows.append(pager("mz_search", [search], page, pages))
        else:
            rows.append(pager("mz_users", [status], page, pages))
            rows.append([Btn(("✅ " if status == s else "") + t, "mz_users", s, 0, style=PRIMARY)
                         for s, t in (("", "همه"), ("active", "فعال"), ("limited", "محدود"))])
            rows.append([Btn(("✅ " if status == s else "") + t, "mz_users", s, 0, style=PRIMARY)
                         for s, t in (("expired", "منقضی"), ("disabled", "غیرفعال"), ("on_hold", "در انتظار"))])
        return Screen("\n".join(lines), rows, refresh=("mz_users", [status, page]), back=("mz", []))

    @app.action("mz_search")
    def search(ctx, text, page=0):
        text = text.strip()[:32]
        if not text:
            return overview(ctx)
        if not page:
            one = mz.users(search=text, limit=2)
            if one.get("total") == 1 and one.get("users"):
                return user_page(ctx, one["users"][0]["username"])
        r = users(ctx, "", page, text)
        r.refresh = ("mz_search", [text, page])
        return r

    @app.action("mz_search_ask", view=False)
    def search_ask(ctx):
        return ctx.ask_text("mz_search", "🔍 اسم (یا بخشی از اسم) کاربر را بفرستید:", back=("mz", []))

    @app.text_input("mz_search", write=False)
    def search_input(ctx, text, msg):
        ctx.msg_id = None
        return app.run(ctx, "mz_search", [text, 0]) and None

    @app.action("mz_user")
    def user_page(ctx, name, notice=None):
        u = mz.user(name)
        now = time.time()
        lim, used = u.get("data_limit"), u.get("used_traffic") or 0
        exp = u.get("expire")
        on = marzban.utc_epoch(u.get("online_at"))
        lines = ["👤 <b>%s</b> — %s" % (esc(u["username"]), STATUS_FA.get(u.get("status"), esc(u.get("status")))),
                 "📦 مصرف: %s از %s%s" % (human(used), human(lim) if lim else "نامحدود",
                                         " (%s٪ مانده)" % fa("%.0f" % max(0, 100.0 * (lim - used) / lim)) if lim else ""),
                 "⏳ انقضا: %s" % (("%s · %s" % (time.strftime("%Y-%m-%d", time.localtime(exp)), until(exp, now))) if exp else "نامحدود"),
                 "🟢 آخرین آنلاین: %s" % ("همین الان (وصل)" if on and now - on <= marzban.NOW_WINDOW else (ago(on, now) if on else "هرگز")),
                 "📊 کل مصرف تا امروز: %s" % human(u.get("lifetime_used_traffic")),
                 "🕒 ساخته شده: %s" % esc((u.get("created_at") or "")[:16].replace("T", " "))]
        if u.get("note"):
            lines.append("📝 %s" % esc(u["note"][:200]))
        inb = u.get("inbounds") or {}
        if inb:
            lines.append("🔌 اینباندها: %s" % esc("، ".join(t for tags in inb.values() for t in tags)))
        rows = [[Btn("🔗 لینک ساب", "mz_sub", name, style=PRIMARY), Btn("📱 QR", "mz_qr", name, style=PRIMARY)],
                [Btn("⏳ +۷ روز", "mz_ext", name, 7, style=SUCCESS), Btn("⏳ +۳۰ روز", "mz_ext", name, 30, style=SUCCESS),
                 Btn("⏳ +۹۰ روز", "mz_ext", name, 90, style=SUCCESS)],
                [Btn("📦 +۱۰ گیگ", "mz_gb", name, 10, style=SUCCESS), Btn("📦 +۵۰ گیگ", "mz_gb", name, 50, style=SUCCESS),
                 Btn("📦 +۱۰۰ گیگ", "mz_gb", name, 100, style=SUCCESS)],
                [Btn("✍️ روز دلخواه", "mz_ext_ask", name, style=SUCCESS), Btn("✍️ حجم دلخواه", "mz_gb_ask", name, style=SUCCESS)],
                [Btn("♻️ ریست مصرف", "mz_reset", name, confirm="مصرف <b>%s</b> صفر شود؟ (برگشت ندارد)" % esc(name))]]
        if u.get("status") == "disabled":
            rows.append([Btn("▶️ فعال کردن", "mz_enable", name, style=SUCCESS)])
        else:
            rows.append([Btn("⏸ غیرفعال کردن", "mz_disable", name, style=DANGER)])
        rows.append([Btn("🗑 حذف کاربر", "mz_del", name, confirm="کاربر <b>%s</b> برای همیشه حذف شود؟" % esc(name))])
        s = Screen("\n".join(lines), rows, refresh=("mz_user", [name]), back=("mz_users", ["", 0]))
        s.notice = notice
        return s

    @app.action("mz_sub", write=True, view=False)
    def sub(ctx, name):
        u = mz.user(name)
        url = u.get("subscription_url") or ""
        if url.startswith("/"):
            prefix = mz.env().get("XRAY_SUBSCRIPTION_URL_PREFIX", "").rstrip("/")
            url = prefix + url
        log("subscription link of %s shown to chat %s", name, ctx.chat_id)
        ctx.send("🔗 لینک ساب <b>%s</b>:\n<code>%s</code>" % (esc(name), esc(url)))
        ctx.answer("لینک فرستاده شد")
        return None

    @app.action("mz_qr", write=True, view=False)
    def qr_code(ctx, name):
        u = mz.user(name)
        url = u.get("subscription_url") or ""
        if url.startswith("/"):
            url = mz.env().get("XRAY_SUBSCRIPTION_URL_PREFIX", "").rstrip("/") + url
        try:
            png = qr.png(url)
        except ValueError:
            ctx.send("❌ لینک برای QR خیلی بلند است:\n<code>%s</code>" % esc(url))
            return None
        app.bot.send_file("sendPhoto", "photo", ctx.chat_id, "%s.png" % name, data=png, caption="📱 QR ساب %s" % esc(name))
        ctx.answer("QR فرستاده شد")
        return None

    @app.action("mz_ext", write=True, view=False)
    def extend(ctx, name, days):
        u, done = mz.extend(name, int(days))
        log("user %s +%d days by chat %s", name, days, ctx.chat_id)
        return user_page(ctx, name, "✅ %s روز اضافه شد" % fa(days) if done else "انقضای این کاربر نامحدود است")

    @app.action("mz_gb", write=True, view=False)
    def add_gb(ctx, name, gb):
        u, done = mz.add_data(name, int(gb))
        log("user %s +%d GB by chat %s", name, gb, ctx.chat_id)
        return user_page(ctx, name, "✅ %s گیگ اضافه شد" % fa(gb) if done else "حجم این کاربر نامحدود است")

    # -------------------------------------------------------- any number of days / GB for one user
    @app.action("mz_ext_ask", write=True, view=False)
    def ext_ask(ctx, name):
        return ctx.ask_text("mz_ext_in", "⏳ <b>%s</b>: چند روز اضافه شود؟\nمثال: <code>12</code> · <code>2 ماه</code> · "
                            "<code>6 ساعت</code> · منفی کم می‌کند: <code>-3</code>\n<code>0</code> = انقضا برداشته شود "
                            "(نامحدود)\nاگر الان نامحدود است، از همین حالا حساب می‌شود." % esc(name), name, back=("mz_user", [name]))

    @app.text_input("mz_ext_in")
    def ext_input(ctx, text, msg, name):
        days = parse_amount(text, "days", allow_negative=True)
        ctx.msg_id = None
        if days is None:
            return Screen("❌ «%s» عدد روز نیست." % esc(text[:30]), [[Btn("✍️ دوباره", "mz_ext_ask", name, style=SUCCESS)]],
                          back=("mz_user", [name]))
        u = mz.user(name)
        if days == 0:
            mz.modify(name, expire=0)
            note = "♾ انقضا برداشته شد (نامحدود)"
        elif not u.get("expire"):
            if days < 0:
                return user_page(ctx, name, "انقضای این کاربر نامحدود است؛ چیزی کم نشد")
            mz.modify(name, expire=int(time.time() + days * 86400))
            note = "⏳ انقضا: %s از الان" % days_text(days)
        else:
            mz.extend(name, days)
            note = "⏳ %s %s" % (days_text(abs(days)), "اضافه شد" if days > 0 else "کم شد")
        log("user %s expire %+.2f days by chat %s", name, days, ctx.chat_id)
        return user_page(ctx, name, note)

    @app.action("mz_gb_ask", write=True, view=False)
    def gb_ask(ctx, name):
        return ctx.ask_text("mz_gb_in", "📦 <b>%s</b>: چند گیگ اضافه شود؟\nمثال: <code>7</code> · <code>2.5</code> · "
                            "<code>300 مگ</code> · منفی کم می‌کند: <code>-5</code>\n<code>0</code> = حجم نامحدود\n"
                            "اگر الان نامحدود است، سقف = مصرف فعلی + این مقدار." % esc(name), name, back=("mz_user", [name]))

    @app.text_input("mz_gb_in")
    def gb_input(ctx, text, msg, name):
        gb = parse_amount(text, "gb", allow_negative=True)
        ctx.msg_id = None
        if gb is None:
            return Screen("❌ «%s» عدد حجم نیست." % esc(text[:30]), [[Btn("✍️ دوباره", "mz_gb_ask", name, style=SUCCESS)]],
                          back=("mz_user", [name]))
        u = mz.user(name)
        if gb == 0:
            mz.modify(name, data_limit=0)
            note = "♾ حجم نامحدود شد"
        elif not u.get("data_limit"):
            if gb < 0:
                return user_page(ctx, name, "حجم این کاربر نامحدود است؛ چیزی کم نشد")
            mz.modify(name, data_limit=int((u.get("used_traffic") or 0) + gb * GB))
            note = "📦 سقف: مصرف فعلی + %s" % gb_text(gb)
        else:
            new = int(u["data_limit"] + gb * GB)
            if new <= 0:
                return user_page(ctx, name, "❌ حجم کمتر از صفر می‌شد؛ چیزی تغییر نکرد")
            mz.modify(name, data_limit=new)
            note = "📦 %s %s" % (gb_text(abs(gb)), "اضافه شد" if gb > 0 else "کم شد")
        log("user %s data %+.3f GB by chat %s", name, gb, ctx.chat_id)
        return user_page(ctx, name, note)

    @app.action("mz_reset", write=True, steps=1, view=False)
    def reset(ctx, name):
        mz.reset(name)
        log("user %s usage reset by chat %s", name, ctx.chat_id)
        return user_page(ctx, name, "✅ مصرف صفر شد")

    @app.action("mz_disable", write=True, view=False)
    def disable(ctx, name):
        mz.set_status(name, "disabled")
        return user_page(ctx, name, "⏸ غیرفعال شد")

    @app.action("mz_enable", write=True, view=False)
    def enable(ctx, name):
        mz.set_status(name, "active")
        return user_page(ctx, name, "▶️ فعال شد")

    @app.action("mz_del", write=True, steps=1, view=False)
    def delete(ctx, name):
        mz.delete(name)
        log("user %s deleted by chat %s", name, ctx.chat_id)
        return Screen("🗑 کاربر <b>%s</b> حذف شد." % esc(name), back=("mz_users", ["", 0]))

    # -------------------------------------------------------- create a user: name, data, days, inbounds
    def wiz(ctx):
        return ctx.session().setdefault("mz_new", {})

    @app.action("mz_new", write=True, view=False)
    def new(ctx):
        ctx.session()["mz_new"] = {}
        return ctx.ask_text("mz_new_name", "➕ <b>ساخت کاربر</b> — قدم ۱ از ۴\nاسم کاربر را بفرستید (۳ تا ۳۲ حرف کوچک انگلیسی، عدد یا _):",
                            back=("mz", []))

    @app.text_input("mz_new_name")
    def new_name(ctx, text, msg):
        name = text.strip()
        if not NAME_RX.match(name):
            return ctx.ask_text("mz_new_name", "❌ اسم نامعتبر است. دوباره بفرستید (a-z، 0-9، _ ؛ ۳ تا ۳۲):", back=("mz", []))
        try:
            mz.user(name)
            return ctx.ask_text("mz_new_name", "❌ کاربر <b>%s</b> هست. اسم دیگری بفرستید:" % esc(name), back=("mz", []))
        except marzban.MarzbanError as e:
            if e.code != 404:
                raise
        wiz(ctx).update(name=name)
        ctx.msg_id = None
        return step_data(ctx)

    def presets(kind):
        p = (app.conf.get("mz_presets") or {}).get(kind) or config_defaults()[kind]
        return [x for x in p if isinstance(x, (int, float)) and x > 0][:8]

    def arm(ctx, kind, *args):
        """Take the next text message as the answer to this step (the buttons still work)."""
        ctx.session()["input"] = {"kind": kind, "args": list(args), "exp": time.time() + 600}

    def gb_text(gb):
        return "%s گیگ" % fmt_num(gb) if gb else "نامحدود"

    def days_text(days):
        if not days:
            return "بی‌نهایت"
        if days < 1:
            return "%s ساعت" % fmt_num(round(days * 24, 2))
        return "%s روز" % fmt_num(days)

    def preset_rows(action, values, label, unlimited):
        btns = [Btn(label(v), action, v, style=SUCCESS) for v in values] + [Btn(unlimited, action, 0, style=SUCCESS)]
        return [btns[i:i + 3] for i in range(0, len(btns), 3)]

    def step_data(ctx, error=None):
        w = wiz(ctx)
        arm(ctx, "mz_new_gb_in")
        rows = preset_rows("mz_new_gb", presets("gb"), gb_text, "♾ نامحدود") + [[Btn("✖️ لغو", "mz", style=PRIMARY)]]
        return Screen("%s➕ <b>ساخت کاربر %s</b> — قدم ۲ از ۴\n📦 <b>حجم</b>: یکی از دکمه‌ها، یا هر عددی بفرستید.\n"
                      "مثال: <code>15</code> (گیگ) · <code>2.5</code> · <code>500 مگ</code> · <code>1 ترا</code> · "
                      "<code>0</code> = نامحدود" % (error and error + "\n\n" or "", esc(w.get("name"))), rows)

    @app.text_input("mz_new_gb_in")
    def new_gb_input(ctx, text, msg):
        gb = parse_amount(text, "gb")
        ctx.msg_id = None
        if gb is None or gb < 0:
            return step_data(ctx, "❌ «%s» عدد حجم نیست." % esc(text[:30]))
        return new_gb(ctx, gb)

    @app.action("mz_new_gb", write=True, view=False)
    def new_gb(ctx, gb, error=None):
        w = wiz(ctx)
        if "name" not in w:
            return Screen("⌛ این ساخت منقضی شده؛ دوباره شروع کنید.", back=("mz", []))
        w["gb"] = float(gb)
        arm(ctx, "mz_new_days_in")
        rows = preset_rows("mz_new_days", presets("days"), days_text, "♾ بی‌نهایت") + [[Btn("✖️ لغو", "mz", style=PRIMARY)]]
        return Screen("%s➕ <b>ساخت کاربر %s</b> — قدم ۳ از ۴\nحجم: %s\n⏳ <b>مدت</b>: یکی از دکمه‌ها، یا هر عددی بفرستید.\n"
                      "مثال: <code>45</code> (روز) · <code>2 ماه</code> · <code>12 ساعت</code> · <code>1 سال</code> · "
                      "<code>0</code> = بی‌نهایت" % (error and error + "\n\n" or "", esc(w.get("name")), gb_text(w["gb"])), rows)

    @app.text_input("mz_new_days_in")
    def new_days_input(ctx, text, msg):
        days = parse_amount(text, "days")
        ctx.msg_id = None
        if days is None or days < 0:
            return new_gb(ctx, wiz(ctx).get("gb", 0), "❌ «%s» عدد روز نیست." % esc(text[:30]))
        return new_days(ctx, days)

    @app.action("mz_new_days", write=True, view=False)
    def new_days(ctx, days):
        w = wiz(ctx)
        if "name" not in w or "gb" not in w:
            return Screen("⌛ این ساخت منقضی شده؛ دوباره شروع کنید.", back=("mz", []))
        w["days"] = float(days)
        inb = mz.inbounds()
        w["all"] = {p: [i["tag"] for i in lst] for p, lst in inb.items() if lst}
        w["sel"] = {p: list(t) for p, t in w["all"].items() if p == "vless"} or {p: list(t) for p, t in w["all"].items()}
        return step_inbounds(ctx)

    def step_inbounds(ctx):
        w = wiz(ctx)
        if "name" not in w:
            return Screen("⌛ این ساخت منقضی شده؛ دوباره شروع کنید.", back=("mz", []))
        rows = []
        for p, tags in w.get("all", {}).items():
            for t in tags:
                on = t in w["sel"].get(p, [])
                rows.append([Btn(("✅ " if on else "⬜️ ") + "%s (%s)" % (t, p), "mz_new_inb", p, t, style=SUCCESS if on else PRIMARY)])
        rows.append([Btn("✅ ساخت کاربر", "mz_new_go", style=SUCCESS), Btn("✖️ لغو", "mz", style=PRIMARY)])
        return Screen("➕ <b>ساخت کاربر %s</b> — قدم ۴ از ۴\nحجم: %s · مدت: %s\nاینباندها را انتخاب کنید:" % (
            esc(w["name"]), gb_text(w.get("gb")), days_text(w.get("days"))), rows)

    @app.action("mz_new_inb", write=True, view=False)
    def new_inb(ctx, proto, tag):
        w = wiz(ctx)
        sel = w.setdefault("sel", {}).setdefault(proto, [])
        if tag in sel:
            sel.remove(tag)
        else:
            sel.append(tag)
        return step_inbounds(ctx)

    @app.action("mz_new_go", write=True, view=False)
    def new_go(ctx):
        w = wiz(ctx)
        if "name" not in w or "days" not in w:
            return Screen("⌛ این ساخت منقضی شده؛ دوباره شروع کنید.", back=("mz", []))
        sel = {p: t for p, t in (w.get("sel") or {}).items() if t}
        if not sel:
            ctx.answer("حداقل یک اینباند انتخاب کنید.", alert=True)
            return step_inbounds(ctx)
        expire = int(time.time() + w["days"] * 86400) if w.get("days") else None
        mz.create(w["name"], sel, expire=expire, data_limit=int(w["gb"] * GB) if w.get("gb") else 0, note="eris-bot")
        log("user %s created by chat %s", w["name"], ctx.chat_id)
        name = w["name"]
        ctx.session()["mz_new"] = {}
        return user_page(ctx, name, "✅ کاربر ساخته شد")

    # -------------------------------------------------------- nodes
    @app.action("mz_nodes")
    def nodes(ctx):
        lst = mz.nodes()
        lines = ["🛰 <b>نودها</b>"]
        rows = []
        for n in lst:
            icon = {"connected": "🟢", "connecting": "🟡", "disabled": "⚪️"}.get(n.get("status"), "🔴")
            lines.append("%s <b>%s</b> — %s · %s:%s · Xray %s" % (icon, esc(n.get("name")), esc(n.get("status")), esc(n.get("address")),
                                                                esc(n.get("port")), esc(n.get("xray_version") or "؟")))
            if n.get("message") and n.get("status") != "connected":
                lines.append("   ↳ %s" % esc(n["message"][:200]))
            rows.append([Btn("🔌 ری‌کانکت %s" % n.get("name"), "mz_reconnect", n.get("id"), style=SUCCESS)])
        if not lst:
            lines.append("نودی تعریف نشده.")
        return Screen("\n".join(lines), rows, refresh=("mz_nodes", []), back=("mz", []))

    @app.action("mz_reconnect", write=True, view=False)
    def reconnect(ctx, node_id):
        mz.reconnect(node_id)
        log("node %s reconnect by chat %s", node_id, ctx.chat_id)
        s = nodes(ctx)
        s.notice = "🔌 دستور ری‌کانکت فرستاده شد"
        return s
ERISBOT_EOF_s_mz_py
  put_file "$STAGE/erisbot/s_ops.py" 0644 << 'ERISBOT_EOF_s_ops_py'
"""Services (state, the last 30 log lines, restart) and the ERIS firewall (eris fw)."""
import ipaddress
import re
import threading
import time

from . import eris, system
from .ui import PRIMARY, SUCCESS, Btn, Screen, pager
from .util import clip, esc, fa, log

PAGE = 12
STATE_ICON = {"active": "🟢", "activating": "🟡", "reloading": "🟡", "inactive": "⚪️", "failed": "🔴"}


def register(app):
    # -------------------------------------------------------- services
    @app.action("svc")
    def services(ctx, page=0):
        lst = system.services(app.conf)
        pages = max(1, (len(lst) + PAGE - 1) // PAGE)
        page = min(page, pages - 1)
        lines = ["🖥 <b>سرویس‌ها</b>"]
        for s in lst:
            lines.append("%s %s — %s%s" % (STATE_ICON.get(s["state"], "❔"), esc(s["name"]), esc(s["state"]),
                                           " (%s)" % esc(s["sub"]) if s["sub"] and s["sub"] != s["state"] else ""))
        btns = [Btn("%s %s" % (STATE_ICON.get(s["state"], "❔"), s["name"]), "svc_one", s["kind"], s["name"], style=PRIMARY)
                for s in lst[page * PAGE:(page + 1) * PAGE]]
        rows = [btns[i:i + 2] for i in range(0, len(btns), 2)] + [pager("svc", [], page, pages)]
        return Screen("\n".join(lines), rows, refresh=("svc", [page]), back=("menu", []))

    def check(kind, name):
        if system.known_service(app.conf, name) != kind:
            raise ValueError("unknown service")

    @app.action("svc_one")
    def one(ctx, kind, name, notice=None):
        check(kind, name)
        if kind == "container":
            c = system.containers().get(name) or {}
            st = {"state": c.get("state", "?"), "sub": "", "since": c.get("status", ""), "desc": "Docker"}
        else:
            st = system.unit_info(name)
        lines = ["%s <b>%s</b> (%s)" % (STATE_ICON.get(st["state"], "❔") if kind != "container" else
                                         ("🟢" if st["state"] == "running" else "🔴"), esc(name),
                                         "کانتینر" if kind == "container" else "systemd"),
                 "وضعیت: %s %s" % (esc(st["state"]), esc(st.get("sub") or "")),
                 "از: %s" % esc(st.get("since") or "—"), esc(st.get("desc") or "")]
        rows = [[Btn("📜 ۳۰ خط آخر لاگ", "svc_log", kind, name, style=PRIMARY)],
                [Btn("🔄 ری‌استارت", "svc_restart", kind, name,
                     confirm="سرویس <b>%s</b> ری‌استارت شود؟%s" % (esc(name), "\n⚠️ SSH: اتصال‌های فعلی معمولاً می‌مانند."
                                                                   if name == "ssh" else ""))]]
        if st["state"] in ("inactive", "failed", "exited"):
            rows.append([Btn("▶️ روشن کردن", "svc_start", kind, name, style=SUCCESS)])
        s = Screen("\n".join(lines), rows, refresh=("svc_one", [kind, name]), back=("svc", [0]))
        s.notice = notice
        return s

    @app.action("svc_log", write=True, view=False)
    def svc_log(ctx, kind, name):
        check(kind, name)
        text = system.logs(kind, name, 30) or "(خالی)"
        ctx.send("📜 <b>%s</b>\n<pre>%s</pre>" % (esc(name), esc(clip(text, 3600))))
        ctx.answer()
        return None

    @app.action("svc_restart", write=True, steps=1, view=False)
    def svc_restart(ctx, kind, name):
        check(kind, name)
        log("restart %s %s by chat %s", kind, name, ctx.chat_id)
        r = system.restart(kind, name)
        if r.returncode != 0:
            return Screen("❌ ری‌استارت %s نشد:\n<pre>%s</pre>" % (esc(name), esc(clip(r.stderr or r.stdout, 1500))),
                          back=("svc_one", [kind, name]))
        time.sleep(1)
        return one(ctx, kind, name, "✅ ری‌استارت شد")

    @app.action("svc_start", write=True, view=False)
    def svc_start(ctx, kind, name):
        check(kind, name)
        r = system.start(kind, name)
        return one(ctx, kind, name, "✅ روشن شد" if r.returncode == 0 else "❌ روشن نشد")

    # -------------------------------------------------------- firewall
    def fw_screen(text, extra=""):
        p = eris.fw_parse(text)
        lines = ["🛡 <b>فایروال</b>"]
        if extra:
            lines += [extra, ""]
        lines.append("<pre>%s</pre>" % esc(clip(text, 3000)))
        rows = []
        if p["pending"]:
            rows.append([Btn("✅ نگه دار (eris fw confirm)", "fw_confirm", style=SUCCESS)])
        if eris.fw_set_up(text):
            rows += [[Btn("➕ باز کردن پورت", "fw_open_ask", style=PRIMARY), Btn("➖ بستن پورت", "fw_close_list", style=PRIMARY)],
                     [Btn("✅ allow IP", "fw_net_ask", "allow", style=PRIMARY), Btn("⛔ deny IP", "fw_net_ask", "deny", style=PRIMARY)],
                     [Btn("🗑 حذف قانون", "fw_remove_list", style=PRIMARY)]]
        return Screen("\n".join(lines), rows, refresh=("fw", []), back=("menu", []))

    @app.action("fw")
    def fw(ctx):
        return fw_screen(eris.fw_status())

    @app.action("fw_open_ask", write=True, view=False)
    def fw_open_ask(ctx):
        return ctx.ask_text("fw_open", "➕ پورت را بفرستید، مثلاً:\n<code>8443</code>\n<code>8443/tcp limit 300</code>\n<code>51820/udp</code>\n"
                            "(SSH از اینترنت از طریق ربات باز نمی‌شود)", back=("fw", []))

    PORT_RX = re.compile(r"^(\d{1,5})(?:/(tcp|udp))?(?:\s+limit\s+(\d{1,6}))?$", re.I)

    @app.text_input("fw_open")
    def fw_open_input(ctx, text, msg):
        m = PORT_RX.match(text.strip())
        if not m or not 1 <= int(m.group(1)) <= 65535:
            return Screen("❌ نامعتبر. مثال: <code>8443/tcp limit 300</code>", back=("fw", []))
        port, proto, lim = int(m.group(1)), (m.group(2) or "tcp").lower(), m.group(3)
        if port == 22:
            return Screen("⛔ باز کردن SSH از اینترنت از ربات ممکن نیست (و توصیه نمی‌شود).", back=("fw", []))
        args = ["open", "%d/%s" % (port, proto)] + (["limit", lim] if lim else [])
        ctx.msg_id = None
        return ask(ctx, args, "پورت <b>%d/%s</b>%s به اینترنت باز شود؟" % (port, proto, " با حداکثر %s اتصال" % fa(lim) if lim else ""))

    def ask(ctx, args, question):
        return app.actions["_ask"].fn(ctx, "fw_do", [args], question + "\nبدون «✅ نگه دار» بعد از ۶۰ ثانیه خودش برمی‌گردد.", 1, 1)

    @app.action("fw_net_ask", write=True, view=False)
    def fw_net_ask(ctx, kind):
        return ctx.ask_text("fw_net", "%s آدرس یا شبکه را بفرستید، مثلاً <code>192.0.2.10</code> یا <code>192.0.2.0/24</code>:" % (
            "✅ allow (دسترسی کامل):" if kind == "allow" else "⛔ deny (مسدود):"), kind, back=("fw", []))

    @app.text_input("fw_net")
    def fw_net_input(ctx, text, msg, kind):
        try:
            net = ipaddress.ip_network(text.strip(), strict=False)
        except ValueError:
            return Screen("❌ آدرس نامعتبر.", back=("fw", []))
        if kind not in ("allow", "deny"):
            return None
        ctx.msg_id = None
        return ask(ctx, [kind, str(net)], "%s <code>%s</code>؟" % ("دسترسی کامل برای" if kind == "allow" else "مسدود کردن", esc(net)))

    @app.action("fw_close_list", write=True)
    def fw_close_list(ctx):
        p = eris.fw_parse(eris.fw_status())
        rows = [[Btn("➖ %d/%s %s" % (port, proto, rest[:20]), "_ask", "fw_do", [["close", "%d/%s" % (port, proto)]],
                     "پورت <b>%d/%s</b> بسته شود؟\nبدون «✅ نگه دار» بعد از ۶۰ ثانیه برمی‌گردد." % (port, proto), 1, 1, style="danger")]
                for n, port, proto, rest in p["opens"]]
        return Screen("➖ <b>کدام پورت بسته شود؟</b>" if rows else "پورت بازی نیست.", rows, back=("fw", []))

    @app.action("fw_remove_list", write=True)
    def fw_remove_list(ctx):
        p = eris.fw_parse(eris.fw_status())
        rows = [[Btn("🗑 %s) %s %s" % (n, kind, net), "_ask", "fw_do", [["remove", str(n)]],
                     "قانون %s) <code>%s %s</code> حذف شود؟" % (n, kind, esc(net)), 1, 1, style="danger")] for n, kind, net in p["rules"]]
        rows += [[Btn("🗑 %s) open %d/%s" % (n, port, proto), "_ask", "fw_do", [["remove", str(n)]],
                      "قانون %s) <code>open %d/%s</code> حذف شود؟" % (n, port, proto), 1, 1, style="danger")]
                 for n, port, proto, _ in p["opens"]]
        return Screen("🗑 <b>کدام قانون حذف شود؟</b>" if rows else "قانون سفارشی‌ای نیست.", rows, back=("fw", []))

    @app.action("fw_do", write=True, steps=1, view=False)
    def fw_do(ctx, args, force=False):
        args = [str(a) for a in args]
        if args[0] not in ("open", "close", "allow", "deny", "remove") or "--open-ssh" in args:
            return None
        log("eris fw %s%s by chat %s", " ".join(args), " --force" if force else "", ctx.chat_id)
        rc, out = eris.fw(args + (["--force"] if force else []))
        if "--open-ssh" in out:
            return fw_screen(eris.fw_status(), "⛔ این تغییر SSH را از اینترنت باز می‌کند؛ از ربات انجام نمی‌شود.\n<pre>%s</pre>" % esc(clip(out, 1200)))
        if "--force" in out and not force:
            s = fw_screen(eris.fw_status(), "⚠️ ERIS این تغییر را خطرناک می‌داند:\n<pre>%s</pre>" % esc(clip(out, 1500)))
            s.rows.insert(0, [Btn("⚠️ با این حال انجام بده (--force)", "fw_do", args, True,
                                  confirm="مطمئنید؟ ERIS این تغییر را خطرناک دانسته:\n<code>eris fw %s --force</code>" % esc(" ".join(args)))])
            return s
        status = eris.fw_status()
        if eris.fw_parse(status)["pending"]:
            deadline = time.time() + 60
            m = re.search(r"come back in (\d+) s", status)
            if m:
                deadline = time.time() + int(m.group(1))
            ctx.show(countdown_screen(out, deadline))
            start_countdown(ctx.chat_id, ctx.msg_id, out, deadline)
            return None
        return fw_screen(status, "نتیجه:\n<pre>%s</pre>" % esc(clip(out, 1500)))

    def countdown_screen(out, deadline):
        left = max(0, int(deadline - time.time()))
        return Screen("🛡 <b>تغییر اعمال شد</b> ولی هنوز موقت است.\n<pre>%s</pre>\n⏳ <b>%s ثانیه</b> تا برگشت خودکار.\n"
                      "اگر همه‌چیز (SSH، سایت، ...) کار می‌کند، «✅ نگه دار» را بزنید." % (esc(clip(out, 1500)), fa(left)),
                      [[Btn("✅ نگه دار", "fw_confirm", style=SUCCESS)]])

    def start_countdown(chat_id, msg_id, out, deadline):
        key = (chat_id, msg_id)
        app.fw_countdowns = getattr(app, "fw_countdowns", {})
        app.fw_countdowns[key] = True

        def run():
            while time.time() < deadline and app.fw_countdowns.get(key):
                app.stop_evt.wait(5)
                if not app.fw_countdowns.get(key):
                    return
                try:
                    if time.time() < deadline:
                        t, mk = app.render(chat_id, countdown_screen(out, deadline))
                        app.bot.edit(chat_id, msg_id, t, mk)
                except Exception:  # an edit that fails must not stop the countdown
                    pass
            if not app.fw_countdowns.pop(key, None):
                return
            time.sleep(3)
            status = eris.fw_status()
            t, mk = app.render(chat_id, fw_screen(status, "↩️ تأیید نشد؛ تغییر برگشت خورد." if not eris.fw_parse(status)["pending"] else ""))
            try:
                app.bot.edit(chat_id, msg_id, t, mk)
            except Exception:  # noqa: B902
                pass
        threading.Thread(target=run, name="fw-countdown", daemon=True).start()

    @app.action("fw_confirm", write=True, view=False)
    def fw_confirm(ctx):
        for k in list(getattr(app, "fw_countdowns", {})):
            if k[0] == ctx.chat_id:
                app.fw_countdowns.pop(k, None)
        rc, out = eris.fw(["confirm"])
        log("eris fw confirm by chat %s: %d", ctx.chat_id, rc)
        return fw_screen(eris.fw_status(), ("✅ نگه داشته شد." if rc == 0 else "❌ ") + "\n<pre>%s</pre>" % esc(clip(out, 1000)))
ERISBOT_EOF_s_ops_py
  put_file "$STAGE/erisbot/s_weather.py" 0644 << 'ERISBOT_EOF_s_weather_py'
"""🌤 Weather: the saved city (or any other), and an optional daily message at a set time."""
import time

from . import config, weather
from .ui import PRIMARY, SUCCESS, Btn, Screen
from .util import esc, fa, log


def register(app):
    def wconf():
        return dict(config.DEFAULTS["weather"], **(app.conf.get("weather") or {}))

    def routes():
        return [None] + [p for p in app.conf.get("proxy_list") or [] if p]

    def report(city, lat, lon):
        return weather.render(city, weather.forecast(lat, lon, routes()))

    @app.action("wx")
    def main(ctx, notice=None):
        w = wconf()
        if w.get("lat") is None:
            return ask_city(ctx, first=True)
        try:
            text = report(w["city"], w["lat"], w["lon"])
        except weather.WeatherError as e:
            text = "❌ %s" % esc(e)
        daily = "روشن، ساعت %s" % fa(w["at"]) if w.get("daily") else "خاموش"
        rows = [[Btn("📍 شهر دیگر", "wx_city", style=PRIMARY), Btn("🗓 ارسال روزانه: %s" % daily, "wx_daily", style=PRIMARY)]]
        sc = Screen(text + "\n\n<i>Open-Meteo · %s</i>" % time.strftime("%H:%M"), rows, refresh=("wx", []), back=("menu", []))
        sc.notice = notice
        return sc

    @app.action("wx_city", view=False)
    def ask_city(ctx, first=False):
        return ctx.ask_text("wx_city", "%s📍 اسم شهر را بفرستید (فارسی یا انگلیسی)، یا از 📎 «Location» موقعیتتان را بفرستید." % (
            "🌤 <b>آب و هوا</b>\nاول شهرتان را بگویید.\n\n" if first else ""), back=("wx", []) if not first else ("menu", []))

    @app.text_input("wx_city", write=False)
    def city_input(ctx, text, msg):
        ctx.msg_id = None
        loc = msg.get("location")
        if loc:
            return pick(ctx, "موقعیت من", loc["latitude"], loc["longitude"], save=ctx.owner)
        try:
            res = weather.search(text, routes())
        except weather.WeatherError as e:
            return Screen("❌ %s" % esc(e), [[Btn("✍️ دوباره", "wx_city", style=SUCCESS)]], back=("menu", []))
        if not res:
            return Screen("❌ شهری با اسم «%s» پیدا نشد." % esc(text[:40]), [[Btn("✍️ دوباره", "wx_city", style=SUCCESS)]],
                          back=("menu", []))
        if len(res) == 1:
            return pick(ctx, *res[0][:3], save=ctx.owner)
        rows = [[Btn(label, "wx_pick", label, lat, lon, style=PRIMARY)] for label, lat, lon, _ in res]
        return Screen("📍 کدام یکی؟", rows, back=("wx", []))

    @app.action("wx_pick", view=False)
    def pick_btn(ctx, label, lat, lon):
        return pick(ctx, label, lat, lon, save=ctx.owner)

    def pick(ctx, label, lat, lon, save=True):
        if save:
            w = wconf()
            w.update(city=label, lat=float(lat), lon=float(lon))
            app.save_conf(weather=w)
            log("weather city set to %s by chat %s", label, ctx.chat_id)
            return main(ctx, "📍 %s ذخیره شد" % label)
        try:
            return Screen(report(label, float(lat), float(lon)), back=("wx", []))
        except weather.WeatherError as e:
            return Screen("❌ %s" % esc(e), back=("wx", []))

    @app.action("wx_daily")
    def daily(ctx):
        w = wconf()
        on = w.get("daily")
        rows = [[Btn("⏹ خاموش کن" if on else "✅ روشن کن", "wx_daily_toggle", style=SUCCESS)],
                [Btn(("✅ " if w.get("at") == h else "") + fa(h), "wx_daily_at", h, style=PRIMARY)
                 for h in ("06:30", "07:30", "08:00", "09:00")],
                [Btn("✏️ ساعت دیگر", "wx_daily_ask", style=PRIMARY)]]
        return Screen("🗓 <b>آب و هوای روزانه</b>\nهر روز در ساعت انتخابی آب و هوای %s فرستاده می‌شود.\nوضعیت: %s · ساعت %s" % (
            esc(w.get("city") or "—"), "روشن" if on else "خاموش", fa(w.get("at"))), rows, back=("wx", []))

    def save_w(**kw):
        w = wconf()
        w.update(kw)
        app.save_conf(weather=w)

    @app.action("wx_daily_toggle", write=True, view=False)
    def daily_toggle(ctx):
        save_w(daily=not wconf().get("daily"))
        return daily(ctx)

    @app.action("wx_daily_at", write=True, view=False)
    def daily_at(ctx, at):
        if config.HHMM_RX.match(at):
            save_w(at=at)
        return daily(ctx)

    @app.action("wx_daily_ask", write=True, view=False)
    def daily_ask(ctx):
        return ctx.ask_text("wx_at", "🕒 ساعت را به شکل HH:MM بفرستید (مثلاً 07:15):", back=("wx_daily", []))

    @app.text_input("wx_at")
    def at_input(ctx, text, msg):
        t = text.strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
        if not config.HHMM_RX.match(t):
            return Screen("❌ به شکل HH:MM بفرستید.", back=("wx_daily", []))
        save_w(at=t)
        return daily(ctx)

    def tick(now=None):
        """Called by the schedule loop: the daily weather message, once a day in a 30-minute window."""
        w = wconf()
        if not w.get("daily") or w.get("lat") is None or not config.HHMM_RX.match(w.get("at") or ""):
            return False
        now = now or time.time()
        lt = time.localtime(now)
        start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, int(w["at"][:2]), int(w["at"][3:]), 0, 0, 0, -1))
        day = time.strftime("%Y-%m-%d", lt)
        if not (start <= now < start + 1800) or app.state.get("weather_day") == day:
            return False
        app.state.set("weather_day", day)
        try:
            app.notify(report(w["city"], w["lat"], w["lon"]))
        except weather.WeatherError as e:
            log("daily weather: %s", e)
        return True

    app.weather_tick = tick
ERISBOT_EOF_s_weather_py
  put_file "$STAGE/erisbot/screens.py" 0644 << 'ERISBOT_EOF_screens_py'
"""All screens of the bot (registered on the App)."""
from . import s_ai, s_alerts, s_backup, s_fans, s_files, s_main, s_mz, s_ops, s_weather
from .s_main import dashboard_text  # noqa: F401  (daily report)


def register(app):
    for m in (s_main, s_mz, s_ops, s_backup, s_alerts, s_fans, s_ai, s_files, s_weather):
        m.register(app)


def after_start(app):
    s_fans.after_start(app)
ERISBOT_EOF_screens_py
  put_file "$STAGE/erisbot/store.py" 0644 << 'ERISBOT_EOF_store_py'
"""Small JSON state files and the map from short random button ids to actions."""
import copy
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from . import config


class JsonState:
    """A dict kept in one JSON file (mode 600), safe to use from several threads."""

    def __init__(self, path, default=None):
        self.path = path
        self.lock = threading.RLock()
        self.data = config.read_json(path, default if default is not None else {})
        if not isinstance(self.data, dict):
            self.data = copy.deepcopy(default) if default is not None else {}

    def get(self, key, default=None):
        with self.lock:
            v = self.data.get(key, default)
            return copy.deepcopy(v)

    def set(self, key, value):
        with self.lock:
            self.data[key] = copy.deepcopy(value)
            self.save()

    def update(self, **kw):
        with self.lock:
            for k, v in kw.items():
                self.data[k] = copy.deepcopy(v)
            self.save()

    def pop(self, key, default=None):
        with self.lock:
            v = self.data.pop(key, default)
            self.save()
            return v

    def save(self):
        with self.lock:
            try:
                config.write_json(self.path, self.data, 0o600)
            except OSError:
                pass


class Callbacks:
    """callback_data is a short id (8-12 characters, far below Telegram's 64 bytes) that maps to
    (chat, action, args, expiry). Navigation ids are a keyed hash of (chat, action, args), so the same
    button keeps its id; confirmations get a random single-use id that lives 60 seconds.
    An id that is unknown, expired or from another chat does nothing."""

    MAX = 6000

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.map = {}
        self.dirty = 0
        self.last_save = 0.0
        data = config.read_json(path, {}) or {}
        self.secret = (data.get("secret") or secrets.token_hex(16)).encode()
        now = time.time()
        for k, v in (data.get("ids") or {}).items():
            if isinstance(v, dict) and v.get("exp", 0) > now:
                self.map[k] = v

    def _id(self, chat_id, action, args):
        msg = json.dumps([chat_id, action, args], ensure_ascii=False, sort_keys=True).encode()
        return "n" + hmac.new(self.secret, msg, hashlib.sha256).hexdigest()[:9]

    def nav(self, chat_id, action, args=(), ttl=2 * 86400):
        cid = self._id(chat_id, action, list(args))
        with self.lock:
            self.map[cid] = {"c": chat_id, "a": action, "g": list(args), "exp": time.time() + ttl}
            self.dirty += 1
        return cid

    def once(self, chat_id, action, args=(), ttl=60):
        cid = "o" + secrets.token_urlsafe(7)
        with self.lock:
            self.map[cid] = {"c": chat_id, "a": action, "g": list(args), "exp": time.time() + ttl, "once": 1}
            self.dirty += 1
        return cid

    def take(self, cid, chat_id):
        """(action, args) or None; a single-use id is consumed."""
        with self.lock:
            e = self.map.get(cid or "")
            if not e or e["c"] != chat_id or e["exp"] < time.time():
                return None
            if e.get("once"):
                del self.map[cid]
                self.dirty += 1
            return e["a"], list(e["g"])

    def prune(self):
        now = time.time()
        with self.lock:
            for k in [k for k, v in self.map.items() if v["exp"] < now]:
                del self.map[k]
            if len(self.map) > self.MAX:
                for k, _ in sorted(self.map.items(), key=lambda kv: kv[1]["exp"])[:len(self.map) - self.MAX]:
                    del self.map[k]
            self.dirty += 1

    def save(self, force=False):
        if not self.dirty and not force:
            return
        if not force and time.time() - self.last_save < 5:
            return
        self.prune()
        with self.lock:
            data = {"secret": self.secret.decode(), "ids": {k: v for k, v in self.map.items() if not v.get("once")}}
            self.dirty = 0
            self.last_save = time.time()
        try:
            config.write_json(self.path, data, 0o600)
        except OSError:
            pass


class RateLimit:
    """At most `events` per `seconds` for each chat."""

    def __init__(self, events=20, seconds=10.0):
        self.events, self.seconds = events, seconds
        self.hist = {}
        self.warned = {}
        self.lock = threading.Lock()

    def allow(self, chat_id, now=None):
        now = now or time.monotonic()
        with self.lock:
            h = [t for t in self.hist.get(chat_id, []) if now - t < self.seconds]
            if len(h) >= self.events:
                self.hist[chat_id] = h
                return False
            h.append(now)
            self.hist[chat_id] = h
            return True

    def should_warn(self, chat_id, now=None):
        now = now or time.monotonic()
        with self.lock:
            if now - self.warned.get(chat_id, -1e9) > self.seconds:
                self.warned[chat_id] = now
                return True
            return False


def ensure_dirs():
    for d in (config.STATE, config.RUN, config.TMP):
        try:
            os.makedirs(d, mode=0o700, exist_ok=True)
        except OSError:
            pass
ERISBOT_EOF_store_py
  put_file "$STAGE/erisbot/stress.py" 0644 << 'ERISBOT_EOF_stress_py'
"""Stability test: every core busy with a Python process for 10/30/60 minutes, to check a BIOS overclock
and a weak power supply. Stops by itself when the CPU reaches 85 °C."""
import os
import signal
import subprocess
import sys
import threading
import time

from . import config, hw, system
from .util import duration, fa, spark

STATE = os.path.join(config.STATE, "stress.json")
LIMIT = 85.0


class StressTest:
    def __init__(self, minutes, on_update=None, on_done=None, temp_fn=None, rpm_fn=None, workers=None,
                 seconds_per_minute=60, sample_every=30, check_every=2.0, limit=LIMIT, meta=None):
        self.minutes = minutes
        self.on_update, self.on_done = on_update, on_done
        self.temp_fn = temp_fn or system.cpu_temp
        self.rpm_fn = rpm_fn or (lambda: {f["label"]: hw.rpm(f) for f in hw.fans()})
        self.workers = workers or os.cpu_count() or 1
        self.spm, self.sample_every, self.check_every, self.limit = seconds_per_minute, sample_every, check_every, limit
        self.procs = []
        self.samples = []            # (elapsed s, temp, {fan: rpm}, MHz)
        self.max_temp = None
        self.reason = None
        self.stop_evt = threading.Event()
        self.thread = None
        self.started = None
        self.throttle0 = None
        self.meta = meta or {}
        self.boot = system.boot_id()

    def start(self):
        self.started = time.time()
        self.throttle0 = hw.throttle_count()
        for _ in range(self.workers):
            self.procs.append(subprocess.Popen([sys.executable, "-c", "while True: pass"],
                                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL, start_new_session=True))
        config.write_json(STATE, dict(self.meta, running=True, start=self.started, minutes=self.minutes,
                                      boot_id=self.boot, pids=[p.pid for p in self.procs]), 0o600)
        self.thread = threading.Thread(target=self._run, name="stress", daemon=True)
        self.thread.start()
        return self

    def stop(self, reason="به دستور شما متوقف شد"):
        if not self.reason:
            self.reason = reason
        self.stop_evt.set()

    def _sample(self):
        t = self.temp_fn()
        if t is not None:
            self.max_temp = t if self.max_temp is None else max(self.max_temp, t)
        return t

    def _run(self):
        total = self.minutes * self.spm
        next_sample = 0.0
        try:
            while not self.stop_evt.is_set():
                el = time.time() - self.started
                t = self._sample()
                if t is not None and t >= self.limit:
                    self.reason = "دمای CPU به %s°C رسید؛ تست خودکار قطع شد" % fa("%.0f" % t)
                    break
                if el >= next_sample:
                    self.samples.append((el, t, self.rpm_fn(), hw.cur_mhz()))
                    next_sample += self.sample_every
                    if self.on_update:
                        try:
                            self.on_update(self)
                        except Exception:  # a failed message edit must not stop the test
                            pass
                if el >= total:
                    break
                self.stop_evt.wait(self.check_every)
        finally:
            self.kill()
            if not self.reason:
                self.reason = "تمام شد"
            config.write_json(STATE, dict(self.meta, running=False, end=time.time(), boot_id=self.boot), 0o600)
            if self.on_done:
                self.on_done(self)

    def kill(self):
        for p in self.procs:
            try:
                p.kill()
            except OSError:
                pass
        for p in self.procs:
            try:
                p.wait(5)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def progress_text(self):
        el = (self.samples[-1][0] if self.samples else 0)
        total = self.minutes * self.spm
        temps = [s[1] for s in self.samples]
        lines = ["🔥 <b>تست پایداری</b> — %s دقیقه، %s پروسس" % (fa(self.minutes), fa(self.workers)),
                 "⏱ %s از %s" % (duration(el * 60 / self.spm, 2), duration(total * 60 / self.spm, 2))]
        last = self.samples[-1] if self.samples else None
        if last:
            lines.append("🌡 CPU: %s°C (بیشترین %s°C)" % (fa("%.0f" % last[1]) if last[1] is not None else "؟",
                                                       fa("%.0f" % self.max_temp) if self.max_temp is not None else "؟"))
            for name, r in (last[2] or {}).items():
                lines.append("🌀 %s: %s RPM" % (name, r if r is not None else "؟"))
            if last[3]:
                lines.append("⚙️ فرکانس: %d MHz" % last[3])
        if temps:
            lines.append("<code>%s</code>  دما (هر %s ثانیه)" % (spark(temps, 30, 90), fa(self.sample_every)))
        return "\n".join(lines)

    def result_text(self):
        thr = hw.throttle_count()
        dthr = (thr - self.throttle0) if (thr is not None and self.throttle0 is not None) else None
        lines = ["🔥 <b>نتیجه‌ی تست پایداری</b>", "وضعیت: %s" % self.reason,
                 "⏱ مدت: %s" % duration(time.time() - self.started, 2),
                 "🌡 بیشترین دمای CPU: %s" % ("%s°C" % fa("%.0f" % self.max_temp) if self.max_temp is not None else "؟"),
                 "🐢 throttle: %s" % ("%s بار" % fa(dthr) if dthr is not None else "شمارنده‌ای نیست"),
                 "✅ سرور ری‌استارت نشد" if system.boot_id() == self.boot else "⚠️ سرور ری‌استارت شد!"]
        temps = [s[1] for s in self.samples]
        if temps:
            lines.append("<code>%s</code>" % spark(temps, 30, 90))
        if self.max_temp is not None and self.max_temp >= 80:
            lines.append("⚠️ دما زیاد است؛ خنک‌کاری یا اورکلاک BIOS را بررسی کنید.")
        return "\n".join(lines)


def leftover():
    """A test that was running when eris-bot (or the server) stopped: (state, rebooted) or None."""
    st = config.read_json(STATE, None)
    if not st or not st.get("running"):
        return None
    rebooted = st.get("boot_id") != system.boot_id()
    if not rebooted:
        for pid in st.get("pids") or []:
            cmd = (system.read_raw("/proc/%d/cmdline" % int(pid)) or b"").replace(b"\0", b" ")
            if b"while True: pass" not in cmd:       # the pid is somebody else's by now
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    st["running"] = False
    config.write_json(STATE, st, 0o600)
    return st, rebooted
ERISBOT_EOF_stress_py
  put_file "$STAGE/erisbot/system.py" 0644 << 'ERISBOT_EOF_system_py'
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
ERISBOT_EOF_system_py
  put_file "$STAGE/erisbot/tg.py" 0644 << 'ERISBOT_EOF_tg_py'
"""Telegram Bot API client: JSON calls, retries (429 retry_after, 5xx, network), the way to Telegram
(direct or a proxy, "auto" = the first that answers), streaming multipart uploads and the fallback
to buttons without colour when the API refuses the "style" field (Bot API 9.4)."""
import copy
import http.client
import json
import os
import socket
import ssl
import threading
import time
import uuid

from . import net
from .util import log

NET_ERRORS = (OSError, http.client.HTTPException, ssl.SSLError, socket.timeout)


class TelegramError(Exception):
    def __init__(self, code, description, retry_after=None):
        Exception.__init__(self, "%s: %s" % (code, description))
        self.code, self.description, self.retry_after = code, description or "", retry_after


class NoRoute(OSError):
    pass


def strip_styles(obj):
    """(copy without "style" keys, True if there were any)."""
    found = [False]

    def walk(x):
        if isinstance(x, dict):
            if "style" in x:
                found[0] = True
            return {k: walk(v) for k, v in x.items() if k != "style"}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x
    out = walk(obj)
    return out, found[0]


def style_refusal(desc):
    """Could this 400 be Telegram refusing the buttons' "style" field? Never for an edit that changes
    nothing ("message is not modified", e.g. a button pressed twice): sending the same screen without
    colours would "work" and wrongly turn colours off for good."""
    d = (desc or "").lower()
    if "not modified" in d or "message to edit not found" in d or "query is too old" in d:
        return False
    return "button" in d or "style" in d or "keyboard" in d or "reply markup" in d or "reply_markup" in d


def stale_refusal(reason):
    """A colour refusal saved by an older version that was really an unchanged edit."""
    return not style_refusal(reason)


class Router:
    """The way to Telegram. proxy = off | auto | URL; auto tries direct first, then proxy_list in order."""

    def __init__(self, conf_fn):
        self.conf_fn = conf_fn
        self.current = None
        self.chosen = False
        self.lock = threading.Lock()
        self.last_fail = 0.0
        self.results = {}              # route -> (ok, seconds, error, when)

    def candidates(self):
        c = self.conf_fn()
        mode = c.get("proxy", "auto")
        if mode == "off":
            return [None]
        if mode == "auto":
            return [None] + [p for p in c.get("proxy_list") or [] if p]
        return [mode]

    def reset(self):
        with self.lock:
            self.chosen = False

    def route(self, probe):
        """The route to use now; probe(route) -> (ok, sec, err) picks one when none is chosen yet."""
        with self.lock:
            if self.chosen:
                return self.current
            cands = self.candidates()
            for r in cands:
                ok, sec, err = probe(r)
                self.results[r] = (ok, sec, err, time.time())
                if ok:
                    if r != self.current or not self.chosen:
                        log("telegram: using %s (%.1f s)", net.describe(r), sec)
                    self.current, self.chosen = r, True
                    return r
                log("telegram: %s does not work: %s", net.describe(r), err)
            self.last_fail = time.time()
            raise NoRoute("no way to Telegram works (%s)" % ", ".join(net.describe(r) for r in cands))

    def failed(self, route):
        with self.lock:
            if self.chosen and self.current == route:
                self.chosen = False


class Bot:
    def __init__(self, conf_fn, state=None, sleep=time.sleep):
        self.conf_fn = conf_fn
        self.router = Router(conf_fn)
        self.state = state                 # erisbot.store.JsonState or None
        self.sleep = sleep
        self.local = threading.local()
        self.me = None

    # ------------------------------------------------------------ basics
    @property
    def conf(self):
        return self.conf_fn()

    def base(self):
        return self.conf["api_base"].rstrip("/")

    def url(self, method):
        return "%s/bot%s/%s" % (self.base(), self.conf["token"], method)

    def colors_on(self):
        if self.conf.get("button_colors", "auto") == "off":
            return False
        return not (self.state and self.state.get("styles_refused"))

    def _conn(self, route, timeout):
        cache = getattr(self.local, "conns", None)
        if cache is None:
            cache = self.local.conns = {}
        key = (self.base(), route)
        c = cache.get(key)
        if c is None:
            c = net.Connection(self.base(), route, timeout)
            cache[key] = c
        c.timeout = timeout
        return c

    def close(self):
        for c in (getattr(self.local, "conns", None) or {}).values():
            c.close()
        self.local.conns = {}

    def _drop(self, route):
        cache = getattr(self.local, "conns", None) or {}
        c = cache.pop((self.base(), route), None)
        if c:
            c.close()

    def probe(self, route, timeout=10.0):
        """(ok, seconds, error) of getMe through one route; never raises."""
        t0 = time.monotonic()
        try:
            r = net.request("POST", self.url("getMe"), proxy=route, body=b"{}",
                            headers={"Content-Type": "application/json"}, timeout=timeout, read_timeout=timeout)
            data = json.loads(r.body or b"{}")
            if data.get("ok"):
                return True, time.monotonic() - t0, None
            return False, time.monotonic() - t0, "%s %s" % (data.get("error_code", r.status), data.get("description", ""))
        except NET_ERRORS + (ValueError,) as e:
            return False, time.monotonic() - t0, str(e) or type(e).__name__

    def route(self):
        return self.router.route(self.probe)

    # ------------------------------------------------------------ JSON calls
    def _post(self, route, method, params, read_timeout):
        body = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
        c = self._conn(route, 10.0)
        try:
            if c.sock is not None:
                c.sock.settimeout(read_timeout)
            c.request("POST", "%s/bot%s/%s" % (self._path_prefix(), self.conf["token"], method),
                      body=body, headers={"Content-Type": "application/json", "Connection": "keep-alive"})
            c.sock.settimeout(read_timeout)
            r = c.getresponse()
            data = r.read()
            if r.getheader("Connection", "").lower() == "close":
                self._drop(route)
            return r.status, data
        except BaseException:
            self._drop(route)
            raise

    def _path_prefix(self):
        import urllib.parse
        return urllib.parse.urlsplit(self.base()).path.rstrip("/")

    def call(self, method, params=None, timeout=30.0, attempts=5):
        params = dict(params or {})
        if "reply_markup" in params and not self.colors_on():
            params["reply_markup"] = strip_styles(params["reply_markup"])[0]
        delay = 1.0
        last = None
        for attempt in range(attempts):
            try:
                route = self.route()
            except NoRoute as e:
                last = e
                self.sleep(min(30, delay))
                delay *= 2
                continue
            try:
                status, raw = self._post(route, method, params, timeout)
            except NET_ERRORS as e:
                last = e
                log("telegram %s: %s via %s", method, e or type(e).__name__, net.describe(route))
                if attempt >= 1:
                    self.router.failed(route)
                self.sleep(min(10, delay * (0.2 if attempt == 0 else 1)))
                delay *= 2
                continue
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                data = {"ok": False, "error_code": status, "description": "not JSON (HTTP %d)" % status}
            if data.get("ok"):
                return data.get("result")
            code = data.get("error_code", status)
            desc = data.get("description", "")
            if code == 429:
                ra = int((data.get("parameters") or {}).get("retry_after") or 3)
                log("telegram %s: too many requests, waiting %d s", method, ra)
                self.sleep(min(ra, 60))
                last = TelegramError(code, desc, ra)
                continue
            if code >= 500:
                last = TelegramError(code, desc)
                self.sleep(min(10, delay))
                delay *= 2
                continue
            if code == 400 and "reply_markup" in params and style_refusal(desc):
                plain, had = strip_styles(params["reply_markup"])
                if had:
                    try:
                        res = self.call(method, dict(params, reply_markup=plain), timeout, attempts)
                    except TelegramError:
                        raise TelegramError(code, desc)
                    log("telegram refused coloured buttons (%s); from now on buttons have no colour", desc)
                    if self.state:
                        self.state.set("styles_refused", True)
                        self.state.set("styles_refused_reason", desc[:200])
                    return res
            raise TelegramError(code, desc)
        if isinstance(last, TelegramError):
            raise last
        raise NoRoute(str(last) if last else "telegram did not answer")

    # ------------------------------------------------------------ uploads
    def send_file(self, method, field, chat_id, filename, path=None, data=None, offset=0, length=None,
                  caption=None, progress=None, extra=None):
        """multipart/form-data upload streamed from a file (offset/length = one piece of it) or bytes.
        One attempt (429 waits are retried); network errors raise, the caller retries."""
        up = self.conf.get("upload") or {}
        wto, rto = float(up.get("write_timeout", 60)), float(up.get("read_timeout", 180))
        if path is not None:
            size = os.path.getsize(path) - offset if length is None else length
        else:
            size = len(data)
        fields = {"chat_id": str(chat_id)}
        if caption:
            fields["caption"] = caption
            fields["parse_mode"] = "HTML"
        for k, v in (extra or {}).items():
            fields[k] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        boundary = "eris-bot-" + uuid.uuid4().hex
        pre = b""
        for k, v in fields.items():
            pre += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n" % (boundary, k)).encode()
            pre += v.encode("utf-8") + b"\r\n"
        safe_name = filename.replace('"', "_").replace("\r", "").replace("\n", "")
        pre += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n" % (boundary, field, safe_name)).encode("utf-8")
        post = ("\r\n--%s--\r\n" % boundary).encode()
        total = len(pre) + size + len(post)
        for _ in range(4):
            route = self.route()
            c = net.Connection(self.base(), route, 15.0)
            try:
                c.putrequest("POST", "%s/bot%s/%s" % (self._path_prefix(), self.conf["token"], method))
                c.putheader("Content-Type", "multipart/form-data; boundary=%s" % boundary)
                c.putheader("Content-Length", str(total))
                c.endheaders()
                c.sock.settimeout(wto)
                c.send(pre)
                sent = 0
                if path is not None:
                    with open(path, "rb") as f:
                        f.seek(offset)
                        left = size
                        while left > 0:
                            chunk = f.read(min(65536, left))
                            if not chunk:
                                raise OSError("%s ended early" % path)
                            c.sock.sendall(chunk)
                            left -= len(chunk)
                            sent += len(chunk)
                            if progress:
                                progress(sent, size)
                else:
                    view = memoryview(data)
                    for i in range(0, len(data), 65536):
                        c.sock.sendall(view[i:i + 65536])
                        sent = min(len(data), i + 65536)
                        if progress:
                            progress(sent, size)
                c.send(post)
                c.sock.settimeout(rto)
                r = c.getresponse()
                raw = r.read()
            except NET_ERRORS:
                self.router.failed(route)
                raise
            finally:
                c.close()
            try:
                res = json.loads(raw or b"{}")
            except ValueError:
                res = {"ok": False, "error_code": r.status, "description": "not JSON"}
            if res.get("ok"):
                return res.get("result")
            code = res.get("error_code", r.status)
            if code == 429:
                self.sleep(min(int((res.get("parameters") or {}).get("retry_after") or 5), 60))
                continue
            raise TelegramError(code, res.get("description", ""))
        raise TelegramError(429, "too many requests")

    # ------------------------------------------------------------ downloads (a file someone sent)
    def download(self, file_id, dest, limit=20 * 1024 * 1024, progress=None):
        """Save a file sent to the bot (getFile + /file/bot<token>/<path>) to `dest`; returns its size.
        The Bot API serves files up to 20 MB."""
        info = self.call("getFile", {"file_id": file_id}, timeout=30)
        path = (info or {}).get("file_path")
        if not path:
            raise TelegramError(400, "Telegram gave no file path (files over 20 MB cannot be downloaded by bots)")
        size = int((info or {}).get("file_size") or 0)
        if size and size > limit:
            raise TelegramError(400, "file too big")
        last = None
        for _ in range(4):
            route = self.route()
            c = net.Connection(self.base(), route, 15.0)
            try:
                c.request("GET", "%s/file/bot%s/%s" % (self._path_prefix(), self.conf["token"], path))
                c.sock.settimeout(120)
                r = c.getresponse()
                if r.status != 200:
                    raise TelegramError(r.status, "download failed (HTTP %d)" % r.status)
                done = 0
                tmp = dest + ".part"
                with open(tmp, "wb") as f:
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        done += len(chunk)
                        if done > limit:
                            raise TelegramError(400, "file too big")
                        f.write(chunk)
                        if progress:
                            progress(done, size)
                os.replace(tmp, dest)
                return done
            except NET_ERRORS as e:
                last = e
                self.router.failed(route)
            finally:
                c.close()
                if os.path.exists(dest + ".part"):
                    os.remove(dest + ".part")
        raise last or OSError("download failed")

    # ------------------------------------------------------------ helpers
    def get_me(self):
        self.me = self.call("getMe")
        return self.me

    def send(self, chat_id, text, markup=None, **kw):
        p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if markup is not None:
            p["reply_markup"] = markup
        p.update(kw)
        return self.call("sendMessage", p)

    def edit(self, chat_id, message_id, text, markup=None):
        p = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
             "disable_web_page_preview": True}
        if markup is not None:
            p["reply_markup"] = markup
        try:
            return self.call("editMessageText", p)
        except TelegramError as e:
            if "not modified" in e.description:
                return None
            raise

    def answer(self, callback_id, text=None, alert=False):
        p = {"callback_query_id": callback_id}
        if text:
            p["text"] = text[:190]
            p["show_alert"] = alert
        try:
            return self.call("answerCallbackQuery", p, attempts=2)
        except (TelegramError, OSError):
            return None

    def delete(self, chat_id, message_id):
        try:
            return self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id}, attempts=2)
        except (TelegramError, OSError):
            return None


def deep(obj):
    return copy.deepcopy(obj)
ERISBOT_EOF_tg_py
  put_file "$STAGE/erisbot/ui.py" 0644 << 'ERISBOT_EOF_ui_py'
"""Screens and buttons. A screen is one message (text + inline keyboard) that is edited in place.

Button colours (Bot API 9.4, field "style" of InlineKeyboardButton / KeyboardButton):
  success = green  safe and positive actions (back up, start, create, normal confirmations)
  primary = blue   menus and details
  danger  = red    anything dangerous or irreversible
"""
SUCCESS, PRIMARY, DANGER = "success", "primary", "danger"

MENU_TEXT = "🏠 منو"
DASH_TEXT = "📊 داشبورد"


class Btn:
    """An inline button. `action`/`args` run a registered action; `confirm` (text) asks first with a
    red button, `steps=2` asks twice. `url` makes a link button."""

    def __init__(self, text, action=None, *args, style=None, url=None, confirm=None, steps=1, ttl=None, once=False):
        self.text, self.action, self.args = text, action, list(args)
        self.style, self.url, self.confirm, self.steps, self.ttl = style, url, confirm, steps, ttl
        self.once = once                 # a random id that works one time within `ttl` (60 s) seconds
        if confirm and style is None:
            self.style = DANGER


class Screen:
    def __init__(self, text, rows=None, back=None, refresh=None, notice=None):
        self.text = text
        self.rows = [r for r in (rows or []) if r]
        self.back = back              # (action, args) or None
        self.refresh = refresh        # (action, args) or None
        self.notice = notice          # short text for answerCallbackQuery


def nav_row(refresh=None, back=None):
    row = []
    if refresh:
        row.append(Btn("🔄 به‌روزرسانی", refresh[0], *refresh[1], style=PRIMARY))
    if back:
        row.append(Btn("🔙 برگشت", back[0], *back[1], style=PRIMARY))
    return row


def grid(buttons, per_row=2):
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def pager(action, args, page, pages):
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(Btn("◀️ قبلی", action, *(list(args) + [page - 1]), style=PRIMARY))
    row.append(Btn("%s / %s" % (_fa(page + 1), _fa(pages)), "noop", style=None))
    if page < pages - 1:
        row.append(Btn("بعدی ▶️", action, *(list(args) + [page + 1]), style=PRIMARY))
    return row


def _fa(x):
    from .util import fa
    return fa(x)


def reply_keyboard():
    return {"keyboard": [[{"text": MENU_TEXT, "style": PRIMARY}, {"text": DASH_TEXT, "style": PRIMARY}]],
            "resize_keyboard": True, "is_persistent": True}


def markup(rows, make_id):
    """Inline keyboard JSON; make_id(btn) gives the callback id of a button."""
    out = []
    for row in rows:
        r = []
        for b in row:
            d = {"text": b.text[:64]}
            if b.url:
                d["url"] = b.url
            else:
                d["callback_data"] = make_id(b)
            if b.style:
                d["style"] = b.style
            r.append(d)
        if r:
            out.append(r)
    return {"inline_keyboard": out}
ERISBOT_EOF_ui_py
  put_file "$STAGE/erisbot/util.py" 0644 << 'ERISBOT_EOF_util_py'
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
ERISBOT_EOF_util_py
  put_file "$STAGE/erisbot/weather.py" 0644 << 'ERISBOT_EOF_weather_py'
"""Weather from Open-Meteo (free, no key): city search and a forecast, as Persian text."""
import json
import os
import time
import urllib.parse

from . import net
from .util import esc, fa

GEO_URL = os.environ.get("ERISBOT_GEO_URL", "https://geocoding-api.open-meteo.com/v1/search")
FORECAST_URL = os.environ.get("ERISBOT_FORECAST_URL", "https://api.open-meteo.com/v1/forecast")

# WMO weather codes
CODES = {0: ("☀️", "صاف"), 1: ("🌤", "تقریباً صاف"), 2: ("⛅️", "نیمه‌ابری"), 3: ("☁️", "ابری"),
         45: ("🌫", "مه"), 48: ("🌫", "مه یخ‌زده"), 51: ("🌦", "نم‌نم باران"), 53: ("🌦", "نم‌نم باران"),
         55: ("🌦", "نم‌نم باران شدید"), 56: ("🌧", "نم‌نم یخ‌زده"), 57: ("🌧", "نم‌نم یخ‌زده"),
         61: ("🌧", "باران کم"), 63: ("🌧", "باران"), 65: ("🌧", "باران شدید"), 66: ("🌧", "باران یخ‌زده"),
         67: ("🌧", "باران یخ‌زده‌ی شدید"), 71: ("🌨", "برف کم"), 73: ("🌨", "برف"), 75: ("❄️", "برف شدید"),
         77: ("🌨", "دانه‌ی برف"), 80: ("🌦", "رگبار"), 81: ("🌧", "رگبار"), 82: ("⛈", "رگبار شدید"),
         85: ("🌨", "رگبار برف"), 86: ("❄️", "رگبار برف شدید"), 95: ("⛈", "رعد و برق"),
         96: ("⛈", "رعد و برق با تگرگ"), 99: ("⛈", "رعد و برق با تگرگ شدید")}
DAYS_FA = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه", "شنبه", "یکشنبه"]


class WeatherError(Exception):
    pass


def code(c):
    return CODES.get(int(c or 0), ("🌡", "نامشخص"))


def get_json(url, routes):
    """GET through the first route that answers (direct first, then the bot's proxies)."""
    last = None
    for r in routes:
        try:
            res = net.request("GET", url, proxy=r, timeout=10, read_timeout=15)
            if res.status == 200:
                return json.loads(res.body.decode("utf-8"))
            last = "HTTP %d" % res.status
        except (OSError, ValueError) as e:
            last = str(e) or type(e).__name__
    raise WeatherError("سرویس هواشناسی جواب نداد (%s)" % last)


def search(name, routes):
    """[(display name, lat, lon, timezone)] for a city name (Persian or English)."""
    q = urllib.parse.urlencode({"name": name.strip()[:60], "count": 6, "language": "fa", "format": "json"})
    d = get_json("%s?%s" % (GEO_URL, q), routes)
    out = []
    for r in d.get("results") or []:
        parts = [r.get("name"), r.get("admin1"), r.get("country")]
        label = "، ".join(p for i, p in enumerate(parts) if p and p not in parts[:i])
        out.append((label, float(r["latitude"]), float(r["longitude"]), r.get("timezone") or "auto"))
    return out


def forecast(lat, lon, routes):
    q = urllib.parse.urlencode({
        "latitude": "%.4f" % lat, "longitude": "%.4f" % lon, "timezone": "auto", "forecast_days": 5,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset,uv_index_max",
        "hourly": "temperature_2m,weather_code,precipitation_probability"})
    return get_json("%s?%s" % (FORECAST_URL, q), routes)


def n(x, digits=0):
    if x is None:
        return "؟"
    return fa(("%." + str(digits) + "f") % x).replace("-", "−")


def hhmm(iso):
    return fa((iso or "")[11:16]) if iso else "؟"


def render(city, d):
    cur = d.get("current") or {}
    day = d.get("daily") or {}
    em, txt = code(cur.get("weather_code"))
    lines = ["%s <b>آب و هوای %s</b>" % (em, esc(city)),
             "🌡 الان: <b>%s°</b> (حس می‌شود %s°) · %s" % (n(cur.get("temperature_2m")), n(cur.get("apparent_temperature")), txt),
             "💧 رطوبت %s٪ · 💨 باد %s km/h" % (n(cur.get("relative_humidity_2m")), n(cur.get("wind_speed_10m")))]
    if cur.get("precipitation"):
        lines.append("☔️ بارش الان: %s mm" % n(cur["precipitation"], 1))
    if day.get("time"):
        lines.append("🌅 طلوع %s · 🌇 غروب %s · ☀️ UV %s" % (hhmm(day["sunrise"][0]), hhmm(day["sunset"][0]),
                                                         n((day.get("uv_index_max") or [None])[0])))
        hourly = d.get("hourly") or {}
        now = cur.get("time") or ""
        nxt = []
        for t, temp, c in zip(hourly.get("time") or [], hourly.get("temperature_2m") or [], hourly.get("weather_code") or []):
            if t > now and t[11:13] in ("00", "03", "06", "09", "12", "15", "18", "21"):
                nxt.append("%s %s %s°" % (hhmm(t), code(c)[0], n(temp)))
            if len(nxt) >= 6:
                break
        if nxt:
            lines += ["", "🕒 <b>ساعت‌های بعد</b>", " · ".join(nxt)]
        lines += ["", "📅 <b>روزهای بعد</b>"]
        for i, t in enumerate(day["time"]):
            name = "امروز" if i == 0 else ("فردا" if i == 1 else DAYS_FA[time.strptime(t, "%Y-%m-%d").tm_wday])
            e, w = code(day["weather_code"][i])
            pop = (day.get("precipitation_probability_max") or [None] * 5)[i]
            lines.append("%s %s: %s° تا %s° · %s%s" % (e, name, n(day["temperature_2m_min"][i]), n(day["temperature_2m_max"][i]),
                                                     w, " · ☔️ %s٪" % n(pop) if pop else ""))
    return "\n".join(lines)
ERISBOT_EOF_weather_py
  put_file /usr/local/bin/eris-bot 0755 << 'ERISBOT_EOF_eris_bot'
#!/usr/bin/python3
"""eris-bot: a Telegram bot for this server (see eris-bot help)."""
import os
import sys

sys.path.insert(0, os.environ.get("ERISBOT_LIB", "/usr/local/lib/eris-bot"))
from erisbot.cli import run  # noqa: E402

run()
ERISBOT_EOF_eris_bot
  put_file /etc/systemd/system/eris-bot.service 0644 << 'ERISBOT_EOF_eris_bot_service'
[Unit]
Description=eris-bot, the Telegram bot of this server
Documentation=https://github.com/eris4444/eris-bot
After=network-online.target
Wants=network-online.target eris-fan.service

[Service]
Type=simple
ExecStart=/usr/local/bin/eris-bot run
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
UMask=0077
Nice=5
# root: it reads /opt/marzban/.env, runs eris, systemctl and docker, and writes /sys for the fans

[Install]
WantedBy=multi-user.target
ERISBOT_EOF_eris_bot_service
  put_file /etc/systemd/system/eris-fan.service 0644 << 'ERISBOT_EOF_eris_fan_service'
[Unit]
Description=eris-bot fan control (fans go back to the BIOS when it stops)
Documentation=https://github.com/eris4444/eris-bot
After=systemd-modules-load.service eris-bot.service
# stops (and gives the fans back to the BIOS) whenever eris-bot stops or crashes
BindsTo=eris-bot.service

[Service]
Type=notify
NotifyAccess=main
ExecStart=/usr/local/bin/eris-bot fan-daemon
# whatever happened to the daemon (even kill -9): the original pwm_enable of every fan comes back
ExecStopPost=/usr/local/bin/eris-bot fan-restore
Restart=always
RestartSec=3
WatchdogSec=20
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=eris-bot.service
ERISBOT_EOF_eris_fan_service
  write_uninstaller /usr/local/sbin/eris-bot-uninstall
}

[ "$(id -u)" -eq 0 ] || die "run as root"
START=1
SELFTEST=1
for a in "$@"; do
  case $a in
    --uninstall)
      if [ -x /usr/local/sbin/eris-bot-uninstall ]; then exec /usr/local/sbin/eris-bot-uninstall; fi
      tmp=$(mktemp)
      write_uninstaller "$tmp"
      bash "$tmp"; rc=$?; rm -f "$tmp"; exit $rc ;;
    --no-start) START=0 ;;
    --no-selftest) SELFTEST=0 ;;
    -h | --help) sed -n '2,9p' "$0"; exit 0 ;;
    *) die "unknown option $a" ;;
  esac
done

[ -r /etc/debian_version ] || die "this installer is for Debian"
say "eris-bot $VERSION installer, log: $LOG"

# 1. packages: only what is missing, never an upgrade (python3, openssl for the backups, CA certificates)
need=()
for p in python3 openssl ca-certificates; do
  dpkg-query -W -f='${Status}' "$p" 2> /dev/null | grep -q "install ok installed" || need+=("$p")
done
if [ ${#need[@]} -gt 0 ]; then
  say "installing missing packages: ${need[*]}"
  if ! DEBIAN_FRONTEND=noninteractive timeout 900 apt-get install -y -q --no-install-recommends --no-upgrade "${need[@]}" >> "$LOG" 2>&1; then
    DEBIAN_FRONTEND=noninteractive timeout 600 apt-get update -q >> "$LOG" 2>&1 || true
    DEBIAN_FRONTEND=noninteractive timeout 900 apt-get install -y -q --no-install-recommends --no-upgrade "${need[@]}" >> "$LOG" 2>&1 \
      || die "apt-get install ${need[*]} failed (see $LOG)"
  fi
else
  say "packages: all present (python3 openssl ca-certificates)"
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die "python3 3.9 or newer is needed"

# 2. a copy of everything this installer may change (the settings too)
mkdir -p "$BK"
chmod 700 "$BK" /var/backups/eris-bot
for p in /etc/eris-bot /usr/local/lib/eris-bot /usr/local/bin/eris-bot /usr/local/sbin/eris-bot-uninstall \
         /etc/systemd/system/eris-bot.service /etc/systemd/system/eris-fan.service; do
  if [ -e "$p" ]; then
    mkdir -p "$BK$(dirname "$p")"
    cp -a "$p" "$BK$p"
  fi
done
say "backup of the old files: $BK"

# 3. an upgrade stops the bot (eris-fan stops with it: the fans go back to the BIOS meanwhile)
was_active=0
if systemctl is-active --quiet eris-bot.service 2> /dev/null; then
  was_active=1
  systemctl stop eris-bot.service eris-fan.service || true
fi

# 4. files: the program into a new folder first, then swapped in
rm -rf "$STAGE"
write_files
rm -rf "$LIB.old"
[ -d "$LIB" ] && mv "$LIB" "$LIB.old"
mv "$STAGE" "$LIB"
rm -rf "$LIB.old"
find "$LIB" -name __pycache__ -prune -exec rm -rf {} + 2> /dev/null || true
install -d -m 0700 /etc/eris-bot /var/lib/eris-bot
say "files: /usr/local/bin/eris-bot, $LIB/, eris-bot.service, eris-fan.service, /usr/local/sbin/eris-bot-uninstall"

# 5. settings: kept on an upgrade (only new keys are added), mode 600
if [ ! -e /etc/eris-bot/config.json ]; then
  python3 -c 'import sys; sys.path.insert(0, "/usr/local/lib/eris-bot"); from erisbot import config
c = dict(config.DEFAULTS); c["allowed_chat_ids"] = []; config.write_json(config.CONFIG, c, 0o600)'
  say "settings: /etc/eris-bot/config.json (no token yet)"
else
  python3 - << 'PY'
import sys
sys.path.insert(0, "/usr/local/lib/eris-bot")
from erisbot import config
data = config.read_json(config.CONFIG, {}) or {}
missing = {k: v for k, v in config.DEFAULTS.items() if k not in data}
if missing:
    data.update(missing)
config.write_json(config.CONFIG, data, 0o600)
PY
  say "settings: kept /etc/eris-bot/config.json"
fi
chmod 600 /etc/eris-bot/config.json
[ -e /etc/eris-bot/backup.pass ] && chmod 600 /etc/eris-bot/backup.pass

# 6. services
systemctl daemon-reload
systemctl enable eris-bot.service eris-fan.service > /dev/null 2>&1 || say "warning: could not enable the services"
configured=$(python3 -c 'import sys; sys.path.insert(0, "/usr/local/lib/eris-bot"); from erisbot import config
c = config.load(); print("yes" if c["token"] and c["allowed_chat_ids"] else "no")')
if [ "$START" = 1 ]; then
  systemctl restart eris-bot.service || say "warning: eris-bot.service did not start (journalctl -u eris-bot)"
  systemctl start eris-fan.service || say "warning: eris-fan.service did not start (journalctl -u eris-fan)"
fi
if [ "$configured" = yes ]; then
  say "eris-bot runs (proxy and chats from /etc/eris-bot/config.json)"
else
  say "next: eris-bot setup   (the bot token and your chat ID; it waits until then)"
fi
[ "$was_active" = 1 ] && say "upgraded from the running version; the bot was restarted"

# 7. self-test
rc=0
if [ "$SELFTEST" = 1 ]; then
  /usr/local/bin/eris-bot selftest || rc=$?
fi
say "done. Commands: eris-bot setup | status | test | proxy | password | logs | update"
say "remove: eris-bot-uninstall   (or: bash $0 --uninstall)"
exit $rc
