"""📁 NAS files: log in with a user of this server, browse the shared folders, upload files sent to the
bot into any folder, send NAS files to Telegram, new folder / rename / delete. Owner chats only."""
import os
import threading
import time

from . import config, nas, tg
from .ui import PRIMARY, SUCCESS, Btn, Screen, pager
from .util import esc, fa, human, log

PAGE = 10
UP_TTL = 1800


def incoming_file(msg):
    """(file_id, name, size) of a file in a message (document, photo, video, audio, voice...), else None."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(msg.get("date") or time.time()))
    for key, ext in (("document", ""), ("video", ".mp4"), ("audio", ".mp3"), ("voice", ".ogg"),
                     ("animation", ".mp4"), ("video_note", ".mp4")):
        f = msg.get(key)
        if isinstance(f, dict) and f.get("file_id"):
            return f["file_id"], f.get("file_name") or "%s-%s%s" % (key, stamp, ext), int(f.get("file_size") or 0)
    photos = msg.get("photo")
    if isinstance(photos, list) and photos:
        big = max(photos, key=lambda p: p.get("file_size") or p.get("width", 0) * p.get("height", 0))
        return big["file_id"], "photo-%s.jpg" % stamp, int(big.get("file_size") or 0)
    return None


def safe_name(name):
    name = (name or "file").replace("/", "_").replace("\0", "").strip() or "file"
    return name if name not in (".", "..") else "file"


def register(app):
    auth = nas.Auth()
    app.nas_auth = auth

    def nconf():
        return app.conf.get("nas") or config.DEFAULTS["nas"]

    def sess(ctx):
        s = ctx.session().get("nas")
        if not s or s["exp"] < time.time():
            ctx.session()["nas"] = None
            return None
        s["exp"] = time.time() + int(nconf().get("session_minutes", 30)) * 60
        return s

    def login_screen(ctx, note=None):
        pend = ctx.session().get("nas_pending")
        lines = ["📁 <b>فایل‌های NAS</b>", "",
                 "با یکی از کاربرهای همین سرور (همان که در SMB/OMV دارید) وارد شوید.",
                 "هر کاری که می‌کنید با دسترسی همان کاربر انجام می‌شود.",
                 "🔒 رمز هیچ‌جا ذخیره نمی‌شود و پیامش بلافاصله پاک می‌شود."]
        if pend:
            lines += ["", "📥 فایل منتظر: <b>%s</b>" % esc(pend["name"])]
        if note:
            lines = [note, ""] + lines
        return Screen("\n".join(lines), [[Btn("🔐 ورود", "nas_login", style=SUCCESS)]], back=("menu", []))

    def guard(ctx):
        """The NAS session, or a Screen to show instead (login / owner only)."""
        if not ctx.owner:
            return None, Screen("🔒 فایل‌های NAS فقط برای مالک است.", back=("menu", []))
        s = sess(ctx)
        if not s:
            return None, login_screen(ctx, "⌛ برای ادامه دوباره وارد شوید." if ctx.session().get("nas_was") else None)
        return s, None

    def where(path, lst=None):
        sh = nas.share_of(path, lst)
        if not sh:
            return None, None, None
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(sh[1]))
        return sh[0], sh[1], "" if rel == "." else rel

    def title(path):
        name, root, rel = where(path)
        return "📁 <b>%s</b>%s" % (esc(name or "?"), (" / " + esc(rel.replace("/", " / "))) if rel else "")

    # -------------------------------------------------------- log in / out
    @app.action("nas")
    def main(ctx, notice=None):
        s, other = guard(ctx)
        if other:
            return other
        lst = nas.shares()
        lines = ["📁 <b>فایل‌های NAS</b> — 👤 %s" % esc(s["user"]), "", "پوشه‌های اشتراکی:"]
        rows = [[Btn("📁 %s" % n, "nas_ls", p, 0, style=PRIMARY)] for n, p in lst]
        if not lst:
            lines.append("هیچ پوشه‌ی اشتراکی پیدا نشد.")
        if s.get("home") and os.path.isdir(s["home"]) and nas.share_of(s["home"], lst):
            rows.append([Btn("🏠 پوشه‌ی خانه‌ی من", "nas_ls", s["home"], 0, style=PRIMARY)])
        if s.get("last") and os.path.isdir(s["last"]):
            rows.append([Btn("🕘 آخرین پوشه: %s" % os.path.basename(s["last"].rstrip("/")), "nas_ls", s["last"], 0, style=PRIMARY)])
        rows.append([Btn("🚪 خروج از حساب", "nas_logout", style=PRIMARY)])
        sc = Screen("\n".join(lines), rows, refresh=("nas", []), back=("menu", []))
        sc.notice = notice
        return sc

    @app.action("nas_login", write=True, view=False)
    def login(ctx):
        return ctx.ask_text("nas_user", "👤 نام کاربری NAS را بفرستید:", back=("nas", []))

    @app.text_input("nas_user")
    def user_input(ctx, text, msg):
        user = text.strip()
        ctx.msg_id = None
        if not user or len(user) > 32:
            return login_screen(ctx, "❌ نام کاربری نامعتبر است.")
        return ctx.ask_text("nas_pass", "🔑 رمز کاربر <b>%s</b> را بفرستید (پیام بلافاصله پاک می‌شود):" % esc(user), user,
                            back=("nas", []))

    @app.text_input("nas_pass")
    def pass_input(ctx, text, msg, user):
        app.bot.delete(ctx.chat_id, msg.get("message_id"))
        ctx.msg_id = None
        try:
            uid, gid, home = auth.check(ctx.chat_id, user, text)
        except nas.NasError as e:
            return login_screen(ctx, "❌ %s" % esc(e))
        ctx.session()["nas"] = {"user": user, "uid": uid, "gid": gid, "home": home, "last": None,
                                "exp": time.time() + int(nconf().get("session_minutes", 30)) * 60}
        ctx.session()["nas_was"] = True
        if ctx.session().get("nas_pending"):
            return incoming_screen(ctx)
        return main(ctx, "✅ وارد شدید")

    @app.action("nas_logout", view=False)
    def logout(ctx):
        ctx.session()["nas"] = None
        ctx.session()["nas_was"] = False
        return login_screen(ctx, "🚪 از حساب NAS خارج شدید.")

    # -------------------------------------------------------- browse
    @app.action("nas_ls")
    def ls(ctx, path, page=0, notice=None):
        s, other = guard(ctx)
        if other:
            return other
        name, root, rel = where(path)
        if not root:
            return main(ctx, "این مسیر جزو پوشه‌های اشتراکی نیست")
        try:
            r = nas.run_as(s, root, "list", path)
        except nas.NasError as e:
            return Screen("❌ %s: %s" % (title(path), esc(e)), back=("nas", []))
        s["last"] = r["path"]
        items = sorted(r["items"], key=lambda e: (not e["dir"], e["name"].lower()))
        items = [e for e in items if not e["name"].startswith(".") or e["name"] in (".", "..")]
        pages = max(1, (len(items) + PAGE - 1) // PAGE)
        page = max(0, min(int(page), pages - 1))
        dirs = sum(1 for e in items if e["dir"])
        lines = [title(r["path"]), "%s پوشه · %s فایل%s" % (fa(dirs), fa(len(items) - dirs),
                                                         "" if r["writable"] else " · 🔒 فقط خواندنی")]
        rows = []
        for e in items[page * PAGE:(page + 1) * PAGE]:
            child = os.path.join(r["path"], e["name"])
            if e["dir"]:
                rows.append([Btn("📁 %s" % e["name"], "nas_ls", child, 0, style=PRIMARY)])
            else:
                rows.append([Btn("📄 %s · %s" % (e["name"], human(e["size"])), "nas_file", child, style=PRIMARY)])
        if not items:
            lines.append("این پوشه خالی است.")
        rows.append(pager("nas_ls", [path], page, pages))
        pend = ctx.session().get("nas_pending")
        if pend and r["writable"]:
            rows.append([Btn("📥 ذخیره‌ی «%s» اینجا" % pend["name"][:30], "nas_save_here", r["path"], style=SUCCESS)])
        tools = []
        if r["writable"]:
            tools += [Btn("📤 آپلود اینجا", "nas_up", r["path"], style=SUCCESS), Btn("➕ پوشه‌ی تازه", "nas_mkdir", r["path"], style=SUCCESS)]
        rows.append(tools)
        up = os.path.dirname(r["path"])
        nav = [Btn("🏠 پوشه‌ها", "nas", style=PRIMARY)]
        if rel:
            nav.insert(0, Btn("⬆️ بالا", "nas_ls", up, 0, style=PRIMARY))
        rows.append(nav)
        sc = Screen("\n".join(lines), rows, refresh=("nas_ls", [path, page]))
        sc.notice = notice
        return sc

    @app.action("nas_file")
    def file_page(ctx, path, notice=None):
        s, other = guard(ctx)
        if other:
            return other
        name, root, rel = where(path)
        if not root:
            return main(ctx)
        try:
            st = nas.run_as(s, root, "stat", path)
        except nas.NasError as e:
            return Screen("❌ %s" % esc(e), back=("nas_ls", [os.path.dirname(path), 0]))
        lines = ["📄 <b>%s</b>" % esc(os.path.basename(path)), title(os.path.dirname(path)),
                 "📦 حجم: %s" % human(st["size"]),
                 "🕒 تغییر: %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(st["mtime"]))]
        rows = []
        if st.get("readable"):
            rows.append([Btn("📥 بفرست به تلگرام", "nas_send", path, style=SUCCESS)])
        if st.get("writable"):
            rows.append([Btn("✏️ تغییر نام", "nas_rename", path, style=PRIMARY),
                         Btn("🗑 حذف", "nas_del", path, confirm="فایل <b>%s</b> برای همیشه حذف شود؟" % esc(os.path.basename(path)))])
        sc = Screen("\n".join(lines), rows, back=("nas_ls", [os.path.dirname(path), 0]))
        sc.notice = notice
        return sc

    # -------------------------------------------------------- change things
    @app.action("nas_mkdir", write=True, view=False)
    def mkdir(ctx, d):
        return ctx.ask_text("nas_mkdir", "➕ اسم پوشه‌ی تازه را بفرستید:", d, back=("nas_ls", [d, 0]))

    @app.text_input("nas_mkdir")
    def mkdir_input(ctx, text, msg, d):
        s, other = guard(ctx)
        ctx.msg_id = None
        if other:
            return other
        try:
            nas.run_as(s, where(d)[1], "mkdir", d, text.strip())
        except nas.NasError as e:
            return ls(ctx, d, 0, "❌ %s" % e)
        log("nas: %s made folder %r in %s", s["user"], text.strip(), d)
        return ls(ctx, d, 0, "✅ پوشه ساخته شد")

    @app.action("nas_rename", write=True, view=False)
    def rename(ctx, path):
        return ctx.ask_text("nas_rename", "✏️ اسم تازه‌ی <b>%s</b> را بفرستید:" % esc(os.path.basename(path)), path,
                            back=("nas_file", [path]))

    @app.text_input("nas_rename")
    def rename_input(ctx, text, msg, path):
        s, other = guard(ctx)
        ctx.msg_id = None
        if other:
            return other
        try:
            r = nas.run_as(s, where(path)[1], "rename", path, text.strip())
        except nas.NasError as e:
            return file_page(ctx, path, "❌ %s" % e)
        log("nas: %s renamed %s", s["user"], path)
        return file_page(ctx, r["path"], "✅ اسم عوض شد")

    @app.action("nas_del", write=True, steps=1, view=False)
    def delete(ctx, path):
        s, other = guard(ctx)
        if other:
            return other
        try:
            nas.run_as(s, where(path)[1], "delete", path)
        except nas.NasError as e:
            return file_page(ctx, path, "❌ %s" % e)
        log("nas: %s deleted %s", s["user"], path)
        return ls(ctx, os.path.dirname(path), 0, "🗑 حذف شد")

    # -------------------------------------------------------- NAS -> Telegram
    @app.action("nas_send", write=True, view=False)
    def send(ctx, path):
        s, other = guard(ctx)
        if other:
            return other
        root = where(path)[1]
        try:
            st = nas.run_as(s, root, "stat", path)
        except nas.NasError as e:
            return file_page(ctx, path, "❌ %s" % e)
        if st["size"] > 2 * 1024 ** 3:
            return file_page(ctx, path, "❌ بیشتر از ۲ گیگ است؛ از SMB بردارید")
        ctx.answer("📤 در حال فرستادن…")
        threading.Thread(target=send_job, args=(ctx.chat_id, dict(s), root, path, st["size"]), daemon=True).start()
        return None

    def send_job(chat_id, s, root, path, size):
        name = os.path.basename(path)
        os.makedirs(config.TMP, exist_ok=True)
        tmp = os.path.join(config.TMP, "nas-%d-%d" % (chat_id, int(time.time() * 1000)))
        status = None
        try:
            status = app.bot.send(chat_id, "📤 <b>%s</b> (%s) — آماده‌سازی…" % (esc(name), human(size)))
            with open(os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600), "w+b") as out:
                nas.run_as(s, root, "get", path, stdout=out, timeout=1800)
            piece = int(nconf().get("send_piece_mb", 45)) * 1024 * 1024
            total = os.path.getsize(tmp)
            parts = max(1, (total + piece - 1) // piece)
            for i in range(parts):
                off = i * piece
                fname = name if parts == 1 else "%s.part%02d" % (name, i + 1)
                cap = "📁 %s" % esc(name) + ("" if parts == 1 else " — تکه‌ی %s از %s" % (fa(i + 1), fa(parts)))
                for attempt in range(4):
                    try:
                        app.bot.send_file("sendDocument", "document", chat_id, fname, path=tmp, offset=off,
                                          length=min(piece, total - off), caption=cap)
                        break
                    except (OSError, tg.TelegramError) as e:
                        if attempt == 3:
                            raise
                        log("nas send %s: %s (again)", fname, e)
                        time.sleep(3 * (attempt + 1))
                if status and parts > 1:
                    app.bot.edit(chat_id, status["message_id"], "📤 <b>%s</b>: %s از %s تکه فرستاده شد" % (esc(name), fa(i + 1), fa(parts)))
            if parts > 1:
                app.bot.send(chat_id, "🧩 «%s» در %s تکه آمد. برای یکی کردن: <code>cat \"%s\".part* &gt; \"%s\"</code> "
                             "(ویندوز: <code>copy /b \"%s.part01\"+\"%s.part02\"… \"%s\"</code>)" % (
                                 esc(name), fa(parts), esc(name), esc(name), esc(name), esc(name), esc(name)))
            if status:
                app.bot.delete(chat_id, status["message_id"])
            log("nas: %s sent %s (%d bytes) to chat %s", s["user"], path, total, chat_id)
        except (nas.NasError, OSError, tg.TelegramError) as e:
            app.bot.send(chat_id, "❌ فرستادن «%s» نشد: %s" % (esc(name), esc(e)))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    # -------------------------------------------------------- Telegram -> NAS
    @app.action("nas_up", write=True, view=False)
    def upload(ctx, d, notice=None):
        s, other = guard(ctx)
        if other:
            return other
        ctx.session()["input"] = {"kind": "nas_up", "args": [d], "exp": time.time() + UP_TTL}
        lim = int(nconf().get("max_upload_mb", 20))
        sc = Screen("📤 <b>آپلود به</b> %s\n\nهر چند فایل که می‌خواهید بفرستید (سند، عکس، ویدیو، صدا). هر کدام همین‌جا "
                    "ذخیره می‌شود.\n⚠️ تلگرام به ربات‌ها فقط فایل تا %s مگ می‌دهد؛ بزرگ‌تر را با SMB بریزید.\n"
                    "💡 عکس را «به صورت فایل» بفرستید تا کیفیتش کم نشود." % (title(d), fa(lim)),
                    [[Btn("✅ تمام", "nas_ls", d, 0, style=SUCCESS)]])
        sc.notice = notice
        return sc

    @app.text_input("nas_up")
    def up_input(ctx, text, msg, d):
        s, other = guard(ctx)
        if other:
            ctx.msg_id = None
            return other
        ctx.session()["input"] = {"kind": "nas_up", "args": [d], "exp": time.time() + UP_TTL}
        f = incoming_file(msg)
        if not f:
            app.bot.send(ctx.chat_id, "📎 یک فایل بفرستید (یا «✅ تمام» را بزنید).")
            return None
        start_save(ctx.chat_id, dict(s), d, f)
        return None

    def start_save(chat_id, s, d, f):
        threading.Thread(target=save_job, args=(chat_id, s, d, f), daemon=True).start()

    def save_job(chat_id, s, d, f):
        file_id, name, size = f
        name = safe_name(name)
        lim = int(nconf().get("max_upload_mb", 20)) * 1024 * 1024
        if size and size > lim:
            app.bot.send(chat_id, "❌ «%s» %s است؛ تلگرام فایل بیشتر از %s مگ را به ربات نمی‌دهد. با SMB بریزید." % (
                esc(name), human(size), fa(lim // 1048576)))
            return
        os.makedirs(config.TMP, exist_ok=True)
        tmp = os.path.join(config.TMP, "up-%d-%d" % (chat_id, int(time.time() * 1000)))
        status = None
        try:
            status = app.bot.send(chat_id, "⏳ ذخیره‌ی «%s»…" % esc(name))
            app.bot.download(file_id, tmp, limit=lim)
            os.chmod(tmp, 0o600)
            with open(tmp, "rb") as src:
                r = nas.run_as(s, nas.share_of(d)[1], "put", d, name, stdin=src, timeout=1800)
            text = "✅ <b>%s</b> ذخیره شد (%s)\n%s" % (esc(r["name"]), human(r["size"]), title(d))
            if r["name"] != name:
                text += "\n(اسم «%s» بود، پس با اسم تازه ذخیره شد)" % esc(name)
            app.bot.edit(chat_id, status["message_id"], text)
            log("nas: %s uploaded %s (%d bytes) into %s", s["user"], r["name"], r["size"], d)
        except (nas.NasError, OSError, tg.TelegramError) as e:
            msg = "❌ «%s» ذخیره نشد: %s" % (esc(name), esc(e))
            if status:
                app.bot.edit(chat_id, status["message_id"], msg)
            else:
                app.bot.send(chat_id, msg)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def incoming_screen(ctx):
        pend = ctx.session().get("nas_pending")
        s = sess(ctx)
        lines = ["📥 فایل <b>%s</b> (%s) رسید. کجا در NAS ذخیره شود؟" % (esc(pend["name"]), human(pend["size"]))]
        rows = []
        if s and s.get("last"):
            rows.append([Btn("💾 در «%s»" % os.path.basename(s["last"].rstrip("/")), "nas_save_here", s["last"], style=SUCCESS)])
        rows.append([Btn("📁 انتخاب پوشه", "nas", style=PRIMARY), Btn("✖️ ذخیره نکن", "nas_forget", style=PRIMARY)])
        return Screen("\n".join(lines), rows)

    @app.action("nas_incoming", view=False)
    def incoming(ctx, file_id, name, size):
        if not ctx.owner:
            return None
        ctx.session()["nas_pending"] = {"file_id": file_id, "name": safe_name(name), "size": int(size or 0)}
        if not sess(ctx):
            return login_screen(ctx, "📥 فایل <b>%s</b> رسید. برای ذخیره در NAS اول وارد شوید." % esc(name))
        return incoming_screen(ctx)

    @app.action("nas_save_here", write=True, view=False)
    def save_here(ctx, d):
        s, other = guard(ctx)
        if other:
            return other
        pend = ctx.session().pop("nas_pending", None)
        if not pend:
            return ls(ctx, d, 0, "فایلی منتظر نیست")
        start_save(ctx.chat_id, dict(s), d, (pend["file_id"], pend["name"], pend["size"]))
        return ls(ctx, d, 0, "⏳ در حال ذخیره…")

    @app.action("nas_forget", view=False)
    def forget(ctx):
        ctx.session().pop("nas_pending", None)
        return Screen("باشه، ذخیره نشد.", back=("menu", []))
