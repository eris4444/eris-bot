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
