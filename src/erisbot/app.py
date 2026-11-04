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
        """Wait for the next text message of this chat (search, a port, a name, ...)."""
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
