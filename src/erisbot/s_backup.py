"""Backups: the state of each part, back up and send now, the copies on the flash disk, restore."""
from . import config, eris
from .ui import PRIMARY, SUCCESS, Btn, Screen
from .util import ago, clip, esc, fa, human, log


def part_line(name, p, sent):
    if not p.get("present"):
        icon, what = "⏭", "روی این سرور نیست"
    elif not p.get("enabled"):
        icon, what = "⚪️", "در BACKUP_PARTS نیست"
    elif p.get("failed"):
        icon, what = "❌", "ناموفق: %s" % esc((p.get("last_error") or "")[:80])
    elif p.get("last_ok_epoch"):
        icon, what = "✅", "%s · %s · %s نسخه" % (ago(p["last_ok_epoch"]), human(p.get("last_size")), fa(p.get("count", 0)))
    else:
        icon, what = "⏳", "هنوز بکاپی نیست"
    s = "%s <b>%s</b> — %s" % (icon, name, what)
    if sent:
        s += "\n     📤 %s" % ("ارسال %s" % ago(sent.get("last_sent")) if sent.get("ok") else
                             "ارسال ناموفق: %s" % esc((sent.get("error") or "")[:60]))
    return s


def register(app):
    @app.action("bk")
    def main(ctx, notice=None):
        s = eris.backup_status()
        res = app.sender.results()
        bs = app.conf.get("backup_send") or {}
        full = s.get("full") or {}
        lines = ["💾 <b>بکاپ</b>",
                 "📁 پوشه: <code>%s</code> %s · شبانه ساعت %s" % (esc(s.get("backup_dir") or "تنظیم نشده"),
                                                                "✅" if s.get("dir_ok") else "❌", esc(s.get("at"))),
                 "🗄 بکاپ کلی (فقط روی فلش، هرگز ارسال نمی‌شود): %s" % (
                     ("❌ ناموفق" if full.get("failed") else ("✅ %s" % ago(full.get("last_ok_epoch")) if full.get("last_ok_epoch") else "—"))
                     if full.get("enabled") else "خاموش"), "", "<b>بخش‌ها</b>"]
        for name, p in (s.get("parts") or {}).items():
            lines.append(part_line(name, p, res.get(name)))
        lines += ["", "📤 ارسال خودکار: %s" % {"after_nightly": "بعد از بکاپ شبانه", "at": "ساعت %s" % bs.get("at"),
                                               "off": "خاموش"}.get(bs.get("mode"), "؟")]
        if app.sender.busy:
            lines.append("⏳ در حال کار: %s" % "، ".join(app.sender.busy.get("parts", [])))
        rows = [[Btn("📤 بکاپ و ارسال همین الان (همه)", "bk_now", "all", style=SUCCESS)],
                [Btn("📤 یک بخش…", "bk_pick", "now", style=PRIMARY), Btn("📂 بکاپ‌های روی فلش", "bk_pick", "flash", style=PRIMARY)],
                [Btn("⚙️ زمان و بخش‌های ارسال", "set_send", style=PRIMARY), Btn("🔑 رمز بکاپ", "set_pw", style=PRIMARY)]]
        sc = Screen("\n".join(lines), rows, refresh=("bk", []), back=("menu", []))
        sc.notice = notice
        return sc

    def present_parts():
        s = eris.backup_status()
        return [n for n, p in (s.get("parts") or {}).items() if p.get("present")]

    @app.action("bk_pick")
    def pick(ctx, what):
        names = present_parts()
        btns = [Btn(n, "bk_now" if what == "now" else "bk_flash", n, style=SUCCESS if what == "now" else PRIMARY) for n in names]
        rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
        title = "📤 <b>کدام بخش؟</b> (بکاپ تازه + ارسال)" if what == "now" else "📂 <b>بکاپ‌های کدام بخش؟</b>"
        return Screen(title, rows, back=("bk", []))

    @app.action("bk_now", write=True, view=False)
    def now(ctx, which):
        names = present_parts()
        parts = [p for p in config.send_parts(app.conf) if p in names] if which == "all" else [which] if which in names else []
        if not parts:
            return main(ctx, "بخشی برای ارسال نیست")
        app.sender.submit(parts, make=True, chats=[ctx.chat_id], trigger="manual")
        log("backup + send %s asked by chat %s", ",".join(parts), ctx.chat_id)
        return main(ctx, "در صف قرار گرفت: %s" % "، ".join(parts))

    @app.action("bk_flash")
    def flash(ctx, part):
        if part not in config.PARTS:
            return None
        items = (eris.backup_list(part).get("groups") or {}).get(part) or []
        lines = ["📂 <b>بکاپ‌های %s روی فلش</b> (جدید به قدیم)" % part]
        rows = []
        for it in items[:15]:
            label = it["time"] + " · " + human(it.get("size")) + (" · قبل از بازگردانی" if it.get("label") == "before-restore" else "")
            rows.append([Btn("📄 " + label, "bk_file", part, it["path"], style=PRIMARY)])
        if not items:
            lines.append("هنوز نسخه‌ای نیست.")
        return Screen("\n".join(lines), rows, refresh=("bk_flash", [part]), back=("bk_pick", ["flash"]))

    def find(part, path):
        items = (eris.backup_list(part).get("groups") or {}).get(part) or []
        return next((it for it in items if it["path"] == path), None)

    @app.action("bk_file")
    def file(ctx, part, path):
        it = find(part, path)
        if not it:
            return Screen("این نسخه دیگر نیست.", back=("bk_flash", [part]))
        lines = ["📄 <b>%s</b>" % esc(it["name"]), "🕒 %s · %s" % (it["time"], human(it.get("size"))),
                 "🔐 sha256: <code>%s</code>" % esc(it.get("sha256") or "؟")]
        rows = [[Btn("📤 ارسال این نسخه", "bk_send_file", part, path, style=SUCCESS)],
                [Btn("♻️ بازگردانی این نسخه", "bk_restore", part, path, steps=2,
                     confirm="بخش <b>%s</b> از نسخه‌ی %s بازگردانی شود؟\nفایل‌های فعلی این بخش رونویسی می‌شوند؛ ERIS اول از وضعیت فعلی بکاپ می‌گیرد و سرویس‌های مربوط را لحظه‌ای متوقف می‌کند." % (part, it["time"]))]]
        return Screen("\n".join(lines), rows, back=("bk_flash", [part]))

    @app.action("bk_send_file", write=True, view=False)
    def send_file(ctx, part, path):
        if not find(part, path):
            return Screen("این نسخه دیگر نیست.", back=("bk_flash", [part]))
        app.sender.submit([part], make=False, files={part: path}, chats=[ctx.chat_id], trigger="manual")
        return main(ctx, "در صف ارسال قرار گرفت")

    @app.action("bk_restore", write=True, steps=2, view=False)
    def restore(ctx, part, path):
        if part not in config.PARTS or not find(part, path):
            return Screen("این نسخه دیگر نیست.", back=("bk", []))
        log("restore %s from %s asked by chat %s", part, path, ctx.chat_id)
        ctx.answer("در حال بازگردانی…")
        ctx.show(Screen("♻️ در حال بازگردانی <b>%s</b>… (ممکن است چند دقیقه طول بکشد)" % part))
        r = eris.restore(part, path)
        ok = r.returncode == 0
        return Screen("%s بازگردانی %s\n<pre>%s</pre>" % ("✅" if ok else "❌", part, esc(clip((r.stdout + r.stderr).strip(), 3000))),
                      back=("bk", []))
