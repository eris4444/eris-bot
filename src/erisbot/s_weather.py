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
