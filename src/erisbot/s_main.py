"""Main menu, dashboard, confirmations, server power and settings."""
import os
import re
import time

from . import config, eris, fand, marzban, net, system, tg
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
    btns += [Btn("🔔 هشدارها", "al", style=PRIMARY), Btn("🌀 فن و عملکرد", "fan", style=PRIMARY),
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
                 "🧩 اندازه‌ی هر تکه‌ی ارسال: %s MB" % fa(c.get("part_size_mb", 45))]
        rows = [[Btn("🌐 پروکسی", "set_proxy", style=PRIMARY), Btn("👥 chat IDها", "set_chats", style=PRIMARY)],
                [Btn("💾 ارسال بکاپ", "set_send", style=PRIMARY), Btn("🔑 رمز بکاپ", "set_pw", style=PRIMARY)],
                [Btn("🗓 گزارش روزانه", "set_report", style=PRIMARY), Btn("🎨 رنگ دکمه‌ها", "set_colors", style=PRIMARY)],
                [Btn("🧩 اندازه‌ی تکه", "set_piece", style=PRIMARY)]]
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
