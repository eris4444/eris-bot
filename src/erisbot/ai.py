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
