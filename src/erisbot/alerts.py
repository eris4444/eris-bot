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
        if not self.due("ip", 600, now):
            return {}, []
        ip = public_ip(self.app.conf.get("public_ip_urls") or [])
        if not ip:
            return {}, []
        old = self.st.get("public_ip")
        events = []
        if old and old != ip:
            events.append(("ip", "ip:%s" % ip, 1, "🌐 IP عمومی عوض شد: از <code>%s</code> به <code>%s</code>" % (esc(old), esc(ip))))
        if ip != old:
            self.st.set("public_ip", ip)
        return {}, events

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


def public_ip(urls):
    for u in urls:
        try:
            r = net.request("GET", u, proxy=None, timeout=10, read_timeout=10)
            ip = r.body.decode("ascii", "replace").strip()
            ipaddress.ip_address(ip)
            return ip
        except (OSError, ValueError, Exception):  # noqa: B014 - any failure: try the next service
            continue
    return None
