"""Alerts: the active ones, history, each type on/off, silence for a while."""
import time

from .alerts import TYPES
from .ui import PRIMARY, SUCCESS, Btn, Screen
from .util import fa


def register(app):
    @app.action("al")
    def main(ctx, notice=None):
        eng = app.alerts
        act = eng.active()
        lines = ["🔔 <b>هشدارها</b>"]
        if act:
            lines.append("فعال (%s):" % fa(len(act)))
            for a in sorted(act.values(), key=lambda a: (-a["level"], a["since"])):
                lines.append("%s %s%s" % ("🔴" if a["level"] >= 2 else "🟠", a["text"],
                                          "" if eng.enabled(a["type"]) else " <i>(خاموش)</i>"))
        else:
            lines.append("✅ هیچ هشدار فعالی نیست.")
        mu = app.alerts.st.get("mute_until") or 0
        lines.append("")
        lines.append("🔕 سکوت تا %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(mu)) if mu > time.time() else "🔔 سکوت: ندارد")
        rows = [[Btn("🔕 ۱ ساعت", "al_mute", 3600, style=PRIMARY), Btn("🔕 ۸ ساعت", "al_mute", 8 * 3600, style=PRIMARY),
                 Btn("🔕 تا صبح", "al_mute", -1, style=PRIMARY)],
                [Btn("🔔 لغو سکوت", "al_mute", 0, style=SUCCESS)],
                [Btn("⚙️ انواع هشدار", "al_types", style=PRIMARY), Btn("📜 تاریخچه", "al_hist", style=PRIMARY)]]
        s = Screen("\n".join(lines), rows, refresh=("al", []), back=("menu", []))
        s.notice = notice
        return s

    @app.action("al_mute", write=True, view=False)
    def mute(ctx, seconds):
        now = time.time()
        if seconds == 0:
            until = 0
        elif seconds < 0:          # until 08:00 tomorrow (or today, before 08:00)
            lt = time.localtime(now)
            until = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 8, 0, 0, 0, 0, -1))
            if until <= now:
                until += 86400
        else:
            until = now + seconds
        app.alerts.mute(until)
        return main(ctx, "🔕 سکوت شد" if until else "🔔 سکوت لغو شد")

    @app.action("al_types")
    def types(ctx):
        rows = [[Btn(("✅ " if app.alerts.enabled(t) else "🚫 ") + title, "al_toggle", t,
                     style=SUCCESS if app.alerts.enabled(t) else PRIMARY)] for t, title in TYPES.items()]
        return Screen("⚙️ <b>انواع هشدار</b>\nبا زدن هر کدام روشن یا خاموش می‌شود.", rows, back=("al", []))

    @app.action("al_toggle", write=True, view=False)
    def toggle(ctx, t):
        if t in TYPES:
            app.alerts.set_enabled(t, not app.alerts.enabled(t))
        return types(ctx)

    @app.action("al_hist")
    def hist(ctx):
        h = app.alerts.history(25)
        lines = ["📜 <b>تاریخچه‌ی هشدارها</b>"]
        for e in reversed(h):
            icon = {"on": "🔴" if e.get("level", 1) >= 2 else "🟠", "off": "✅", "event": "🔵"}.get(e["kind"], "•")
            lines.append("%s %s %s" % (time.strftime("%m-%d %H:%M", time.localtime(e["ts"])), icon, e["text"]))
        if not h:
            lines.append("خالی.")
        return Screen("\n".join(lines), refresh=("al_hist", []), back=("al", []))
