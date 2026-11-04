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
