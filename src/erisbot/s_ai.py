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
