"""Marzban: overview, users (pages, filters, search), one user, create a user, nodes."""
import re
import time

from . import marzban, qr
from .ui import DANGER, PRIMARY, SUCCESS, Btn, Screen, pager
from .util import ago, esc, fa, human, log, until

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

    def step_data(ctx):
        w = wiz(ctx)
        rows = [[Btn("%s گیگ" % fa(g), "mz_new_gb", g, style=SUCCESS) for g in (10, 20, 50)],
                [Btn("%s گیگ" % fa(g), "mz_new_gb", g, style=SUCCESS) for g in (100, 200)] + [Btn("♾ نامحدود", "mz_new_gb", 0, style=SUCCESS)],
                [Btn("✖️ لغو", "mz", style=PRIMARY)]]
        return Screen("➕ <b>ساخت کاربر %s</b> — قدم ۲ از ۴\nحجم:" % esc(w.get("name")), rows)

    @app.action("mz_new_gb", write=True, view=False)
    def new_gb(ctx, gb):
        wiz(ctx)["gb"] = int(gb)
        w = wiz(ctx)
        rows = [[Btn("%s روز" % fa(d), "mz_new_days", d, style=SUCCESS) for d in (7, 30, 60)],
                [Btn("%s روز" % fa(d), "mz_new_days", d, style=SUCCESS) for d in (90, 180)] + [Btn("♾ بی‌نهایت", "mz_new_days", 0, style=SUCCESS)],
                [Btn("✖️ لغو", "mz", style=PRIMARY)]]
        return Screen("➕ <b>ساخت کاربر %s</b> — قدم ۳ از ۴\nحجم: %s\nمدت:" % (esc(w.get("name")), "%s گیگ" % fa(gb) if gb else "نامحدود"), rows)

    @app.action("mz_new_days", write=True, view=False)
    def new_days(ctx, days):
        w = wiz(ctx)
        w["days"] = int(days)
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
            esc(w["name"]), "%s گیگ" % fa(w["gb"]) if w.get("gb") else "نامحدود",
            "%s روز" % fa(w["days"]) if w.get("days") else "بی‌نهایت"), rows)

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
        mz.create(w["name"], sel, expire=expire, data_limit=w["gb"] * GB if w.get("gb") else 0, note="eris-bot")
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
