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
