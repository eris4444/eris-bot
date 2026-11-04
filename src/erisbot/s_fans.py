"""Fans, performance profiles, finding the CPU fan controller, the BIOS overclocking guide, the stress test."""
import os
import threading

from . import fand, hw, perf, stress, system
from .ui import PRIMARY, SUCCESS, Btn, Screen
from .util import clip, esc, fa, log

BIOS_GUIDE = """📖 <b>راهنمای اورکلاک از BIOS</b> (Core 2 روی چیپ‌ست G41)

<b>واقعیت:</b> اورکلاک واقعی یعنی بالا بردن FSB یا ولتاژ، و روی Core 2 و G41 فقط از داخل BIOS امن است. ابزارهایی مثل setfsb مستقیم روی چیپ PLL می‌نویسند و ممکن است سرور را هنگ کنند یا داده را خراب کنند؛ این ربات چنین کاری نمی‌کند. «پروفایل‌ها»ی ربات فقط فن، governor و swappiness را عوض می‌کنند.

<b>Core 2 Quad Q9550</b> (ضریب قفل ۸٫۵، FSB 333 MHz = 1333 QDR، یعنی ۲٫۸۳ گیگاهرتز):
۱. اول از همه یک بکاپ بگیرید و دمای فعلی را با «🔥 تست پایداری» ۱۰ دقیقه‌ای بسنجید.
۲. در BIOS، FSB را قدم‌به‌قدم بالا ببرید: 333 ← 350 ← 366 (حدود ۳٫۱ گیگ) ← 380 (حدود ۳٫۲۳ گیگ). هر بار فقط یک قدم.
۳. فرکانس رم را با نسبت DRAM پایین نگه دارید تا رم بیش از حدش نرود. اگر گزینه‌اش هست، PCI-E را روی 100 MHz قفل کنید.
۴. ولتاژ: Vcore را تا جایی که پایدار است پایین نگه دارید و از حدود 1.3625V (حد Intel برای Q9550) بالاتر نروید. هر بار فقط 0.0125 تا 0.025 ولت اضافه کنید.
۵. بعد از هر قدم، «🔥 تست پایداری» ۳۰ دقیقه‌ای بگیرید. دمای CPU زیر بار باید <b>زیر ۷۰°C</b> بماند (TCase کوادکور Q9550 حدود ۷۱°C است). اگر throttle دیدید یا سرور ری‌استارت شد، یک قدم برگردید.
۶. اگر EIST را در BIOS روشن کنید، cpufreq در لینوکس فعال می‌شود و پروفایل‌ها governor را هم تنظیم می‌کنند.

<b>Pentium E5700</b> (ضریب ۱۵، FSB 200 MHz، یعنی ۳٫۰ گیگاهرتز): همین روش، با FSB از 200 به 210 و بعد 220.

⚠️ <b>پاور:</b> پاور ۲۳۰ واتی ارزان است و حاشیه‌ی کمی دارد. Q9550 (95 وات) به‌علاوه‌ی GTX 650 و هاردها نزدیک حد آن است و اورکلاک و ولتاژ بیشتر، مصرف را بالاتر می‌برد. ری‌استارت ناگهانی زیر بار یعنی پاور کم آورده. برای اورکلاک یک پاور معتبر ۴۰۰ تا ۴۵۰ واتی توصیه می‌شود.
⚠️ VRM بردهای ارزان G41 ضعیف است؛ کیس باید هوای خوب داشته باشد.
↩️ اگر سرور بالا نیامد: Clear CMOS (با جامپر یا برداشتن باتری)."""


def register(app):
    def status():
        return fand.read_status()

    @app.action("fan")
    def main(ctx, notice=None):
        st = status()
        fans = hw.fans()
        prof = perf.load().get("active")
        profiles = perf.all_profiles()
        lines = ["🌀 <b>فن و عملکرد</b>",
                 "🎛 پروفایل: %s" % (profiles[prof]["title"] if prof in profiles else "پیش‌فرض سیستم")]
        ct = system.cpu_temp()
        gt = hw.gpu_temp()
        lines.append("🌡 CPU: %s · GPU: %s" % ("%s°C" % fa("%.0f" % ct) if ct is not None else "؟",
                                              "%s°C" % fa("%.0f" % gt) if gt is not None else "—"))
        if not fans:
            lines.append("فن قابل کنترلی پیدا نشد (هیچ pwm در /sys/class/hwmon نیست).")
        for f in fans:
            s = (st or {}).get("fans", {}).get(f["id"]) or {}
            fc = fand.fan_cfg(fand.load_cfg(), f["id"])
            r = hw.rpm(f)
            raw = hw.read_int(f["pwm"])
            pct = s.get("pct") if s else hw.pwm_to_pct(f, raw)
            lines.append("• %s: %s RPM · %s · %s%s" % (esc(f["label"]), fa(r) if r is not None else "؟",
                                                      "%s٪" % fa("%.0f" % pct) if pct is not None else "؟",
                                                      fand.describe_cfg(fc), " 🔥 حفاظت دمایی" if s.get("protect") else ""))
        if not any(f["kind"] == "cpu" for f in fans):
            lines.append("ℹ️ فن CPU و کیس فعلاً از لینوکس کنترل نمی‌شود؛ «🔍 تشخیص کنترلر» را امتحان کنید.")
        if fans and st is None:
            lines.append("⚠️ سرویس eris-fan کار نمی‌کند (<code>systemctl status eris-fan</code>)؛ فن‌ها دست BIOS است.")
        lines.append("⚙️ cpufreq: %s" % ("governor %s" % esc(hw.governor()) if hw.has_cpufreq() else
                                        "ندارد (EIST در BIOS خاموش است یا پشتیبانی نمی‌شود)"))
        rows = [[Btn("🌀 " + f["label"], "fan_one", f["id"], style=PRIMARY)] for f in fans]
        rows += [[Btn("🎛 پروفایل‌ها", "pf", style=PRIMARY), Btn("🔍 تشخیص کنترلر فن CPU", "fan_detect", style=PRIMARY)],
                 [Btn("🔥 تست پایداری", "stress", style=PRIMARY), Btn("📖 راهنمای اورکلاک از BIOS", "bios", style=PRIMARY)]]
        s = Screen("\n".join(lines), rows, refresh=("fan", []), back=("menu", []))
        s.notice = notice
        return s

    def find(fid):
        return next((f for f in hw.fans() if f["id"] == fid), None)

    @app.action("fan_one")
    def one(ctx, fid, notice=None):
        f = find(fid)
        if not f:
            return Screen("این فن دیگر پیدا نمی‌شود.", back=("fan", []))
        st = (status() or {}).get("fans", {}).get(fid) or {}
        fc = fand.fan_cfg(fand.load_cfg(), fid)
        lo, hi = hw.pwm_range(f)
        t = st.get("temp") if st else hw.fan_temp(f)
        lines = ["🌀 <b>%s</b>" % esc(f["label"]),
                 "دور: %s RPM" % (fa(hw.rpm(f)) if hw.rpm(f) is not None else "؟ (fan_input ندارد)"),
                 "قدرت: %s" % ("%s٪" % fa("%.0f" % st["pct"]) if st.get("pct") is not None else "؟"),
                 "دما (%s): %s" % ("خود کارت" if f["kind"] == "gpu" else "داغ‌ترین هسته‌ی CPU",
                                  "%s°C" % fa("%.0f" % t) if t is not None else "؟"),
                 "حالت: %s" % fand.describe_cfg(fc),
                 "محدوده‌ی pwm: %s تا %s" % (lo, hi),
                 "🛡 حفاظت همیشگی: CPU ≥ ۷۵°C یا GPU ≥ ۸۰°C ⇐ ۱۰۰٪. کمترین مقدار دستی ۲۵٪."]
        if st.get("protect"):
            lines.append("🔥 حفاظت دمایی الان فعال است (۱۰۰٪).")
        if st.get("stall"):
            lines.append("🛑 فن نمی‌چرخد!")
        rows = [[Btn("🔁 خودکار (BIOS/درایور)", "fan_set", fid, "auto", 0, style=SUCCESS)],
                [Btn("📈 " + fand.CURVE_TITLES[c], "fan_set", fid, "curve", c, style=PRIMARY) for c in ("quiet", "normal", "steep")],
                [Btn("%s٪" % fa(p), "fan_set", fid, "manual", p, style=PRIMARY) for p in (30, 40, 50, 60)],
                [Btn("%s٪" % fa(p), "fan_set", fid, "manual", p, style=PRIMARY) for p in (70, 80, 100)],
                [Btn("➖ ۵٪", "fan_set", fid, "step", -5, style=PRIMARY), Btn("➕ ۵٪", "fan_set", fid, "step", 5, style=PRIMARY)]]
        s = Screen("\n".join(lines), rows, refresh=("fan_one", [fid]), back=("fan", []))
        s.notice = notice
        return s

    @app.action("fan_set", write=True, view=False)
    def fan_set(ctx, fid, mode, value):
        if not find(fid):
            return main(ctx)
        cfg = fand.load_cfg()
        cur = fand.fan_cfg(cfg, fid)
        if mode == "auto":
            new = {"mode": "auto"}
        elif mode == "curve" and value in fand.CURVES:
            new = {"mode": "curve", "curve": fand.CURVES[value], "curve_name": value}
        elif mode in ("manual", "step"):
            base = cur["percent"] if cur["mode"] == "manual" else 50
            p = float(value) if mode == "manual" else base + float(value)
            new = {"mode": "manual", "percent": max(fand.MIN_PCT, min(100, p))}
        else:
            return one(ctx, fid)
        cfg["fans"][fid] = new
        fand.save_cfg(cfg)
        log("fan %s -> %s by chat %s", fid, new, ctx.chat_id)
        return one(ctx, fid, "✅ %s (تا ۲ ثانیه اعمال می‌شود)" % fand.describe_cfg(fand.fan_cfg(cfg, fid)))

    # -------------------------------------------------------- profiles
    @app.action("pf")
    def profiles(ctx, notice=None):
        d = perf.load()
        lines = ["🎛 <b>پروفایل‌های عملکرد</b>",
                 "هر پروفایل فقط چیزهایی را عوض می‌کند که روی این سرور هست. اورکلاک FSB فقط از BIOS ممکن است (📖 راهنما)."]
        rows = []
        for name, p in perf.all_profiles().items():
            on = d.get("active") == name
            av = perf.availability(p)
            miss = [w for w, ok, _ in av if not ok]
            lines.append("%s %s%s" % ("✅" if on else "▫️", p["title"], " — در دسترس نیست روی این سرور: %s" % "، ".join(miss) if miss else ""))
            rows.append([Btn(("✅ " if on else "") + p["title"], "pf_one", name, style=SUCCESS if on else PRIMARY)])
        rows.append([Btn("⭐ پروفایل سفارشی جدید", "pf_new", style=SUCCESS)])
        rows.append([Btn("↩️ برگشت به پیش‌فرض سیستم", "pf_reset", confirm="همه‌چیز (فن‌ها، governor، حداقل فرکانس، swappiness) به حالت اولیه‌ی سیستم برگردد؟")])
        s = Screen("\n".join(lines), rows, refresh=("pf", []), back=("fan", []))
        s.notice = notice
        return s

    @app.action("pf_one")
    def pf_one(ctx, name):
        ps = perf.all_profiles()
        if name not in ps:
            return profiles(ctx)
        p = ps[name]
        lines = [p["title"]]
        for what, ok, note in perf.availability(p):
            lines.append("%s %s%s" % ("✅" if ok else "🚫", what, (" — " + note) if note and ok else
                                      (" — در دسترس نیست روی این سرور (%s)" % note if not ok else "")))
        rows = [[Btn("✅ اعمال این پروفایل", "pf_apply", name, style=SUCCESS)]]
        if p.get("custom"):
            rows.append([Btn("🗑 حذف این پروفایل", "pf_del", name[2:], confirm="پروفایل %s حذف شود؟" % esc(name[2:]))])
        return Screen("\n".join(lines), rows, back=("pf", []))

    @app.action("pf_apply", write=True, view=False)
    def pf_apply(ctx, name):
        r = perf.apply(name)
        p = perf.all_profiles()[name]
        log("profile %s by chat %s: %s", name, ctx.chat_id, r)
        lines = ["✅ پروفایل %s فعال شد (بعد از ری‌استارت هم می‌ماند)." % p["title"], "", "<b>چه چیزهایی عوض شد:</b>"]
        lines += ["• " + esc(c) for c in r["changes"]] or ["• چیزی لازم نبود عوض شود."]
        if r["unavailable"]:
            lines += ["", "<b>در دسترس نیست روی این سرور:</b>"] + ["• " + esc(u) for u in r["unavailable"]]
        return Screen("\n".join(lines), back=("pf", []))

    @app.action("pf_reset", write=True, steps=1, view=False)
    def pf_reset(ctx):
        ch = perf.reset_default()
        log("profiles reset by chat %s", ctx.chat_id)
        return Screen("↩️ همه‌چیز به پیش‌فرض سیستم برگشت.\n" + ("\n".join("• " + esc(c) for c in ch) or "چیزی تغییر نکرده بود."),
                      back=("pf", []))

    @app.action("pf_del", write=True, steps=1, view=False)
    def pf_del(ctx, name):
        perf.delete_custom(name)
        return profiles(ctx, "حذف شد")

    @app.action("pf_new", write=True, view=False)
    def pf_new(ctx):
        ctx.session()["pf_new"] = {}
        return ctx.ask_text("pf_name", "⭐ اسم پروفایل جدید را بفرستید (حداکثر ۲۴ نویسه):", back=("pf", []))

    @app.text_input("pf_name")
    def pf_name(ctx, text, msg):
        name = text.strip()[:24]
        if not name or any(c in name for c in "<>&"):
            return Screen("❌ اسم نامعتبر.", back=("pf", []))
        ctx.session()["pf_new"] = {"name": name}
        ctx.msg_id = None
        rows = [[Btn(t, "pf_new_fan", k, style=PRIMARY)] for k, t in perf.FAN_CHOICES.items()]
        return Screen("⭐ <b>%s</b> — منحنی فن:" % esc(name), rows, back=("pf", []))

    @app.action("pf_new_fan", write=True, view=False)
    def pf_new_fan(ctx, fan):
        w = ctx.session().get("pf_new") or {}
        if "name" not in w:
            return profiles(ctx)
        w["fan"] = fan
        govs = hw.governors()
        rows = [[Btn(g, "pf_new_gov", g, style=PRIMARY) for g in govs[i:i + 3]] for i in range(0, len(govs), 3)]
        rows.append([Btn("بدون تغییر governor", "pf_new_gov", "", style=PRIMARY)])
        note = "" if govs else "\n(cpufreq روی این سرور نیست؛ governor در دسترس نیست)"
        return Screen("⭐ <b>%s</b> — governor:%s" % (esc(w["name"]), note), rows, back=("pf", []))

    @app.action("pf_new_gov", write=True, view=False)
    def pf_new_gov(ctx, gov):
        w = ctx.session().get("pf_new") or {}
        if "name" not in w:
            return profiles(ctx)
        w["governor"] = gov
        rows = [[Btn("بدون تغییر", "pf_new_save", -1, style=SUCCESS), Btn("۱۰", "pf_new_save", 10, style=SUCCESS),
                 Btn("۶۰", "pf_new_save", 60, style=SUCCESS)]]
        return Screen("⭐ <b>%s</b> — vm.swappiness:" % esc(w["name"]), rows, back=("pf", []))

    @app.action("pf_new_save", write=True, view=False)
    def pf_new_save(ctx, sw):
        w = ctx.session().get("pf_new") or {}
        if "name" not in w:
            return profiles(ctx)
        perf.save_custom(w["name"], w.get("fan", "normal"), w.get("governor") or None, None if sw < 0 else int(sw))
        ctx.session()["pf_new"] = {}
        return pf_one(ctx, "c:" + w["name"])

    # -------------------------------------------------------- finding the CPU fan controller
    @app.action("fan_detect")
    def detect(ctx):
        txt = ("🔍 <b>تشخیص کنترلر فن CPU</b>\n"
               "الان هیچ درایور Super I/O (it87، nct6775، w83627ehf، f71882fg …) لود نیست، پس فن CPU و کیس pwm ندارند.\n"
               "این کار <code>sensors-detect --auto</code> را اجرا می‌کند؛ روی بعضی بردها پرس‌وجوی سخت‌افزار کمی ریسک دارد.\n"
               "بعد از آن، هر درایور پیشنهادی را جدا و با تأیید لود می‌کنیم.")
        if not os.path.exists(hw.config.SENSORS_DETECT):
            txt += "\n\nℹ️ sensors-detect پیدا نشد (<code>apt install lm-sensors</code>)؛ پیدا کردن خودکار بدون آن هم کار می‌کند."
            return Screen(txt, [[Btn("🪄 پیدا کردن خودکار و تست فن CPU", "fan_auto", confirm="درایورهای Super I/O یکی‌یکی امتحان شوند؟")]],
                          back=("fan", []))
        auto = Btn("🪄 پیدا کردن خودکار و تست فن CPU", "fan_auto",
                   confirm="ربات درایورهای Super I/O (it87، w83627ehf، nct6775، f71882fg …) را یکی‌یکی امتحان می‌کند؛ "
                           "هر درایور خودش شناسه‌ی چیپ را چک می‌کند و روی چیپ دیگر لود نمی‌شود.\n"
                           "بعد هر pwm پیدا شده چند ثانیه روی ۱۰۰٪ و ۳۵٪ می‌رود تا معلوم شود کدام فن را می‌چرخاند، "
                           "و به حالت قبل برمی‌گردد. (حدود ۱ تا ۲ دقیقه)")
        return Screen(txt, [[auto], [Btn("🔍 فقط اجرای sensors-detect", "fan_detect_go", confirm="sensors-detect --auto اجرا شود؟")]],
                      back=("fan", []))

    @app.action("fan_detect_go", write=True, steps=1, view=False)
    def detect_go(ctx):
        ctx.answer("در حال اجرا… (تا یک دقیقه)")
        ctx.show(Screen("⏳ sensors-detect در حال اجراست…"))
        out, rc = hw.sensors_detect()
        p = hw.parse_sensors_detect(out)
        log("sensors-detect: drivers %s, fan drivers %s, ids %s", p["drivers"], p["fan_drivers"], p["unknown_ids"])
        tail = "\n".join(out.strip().splitlines()[-30:])
        lines = ["🔍 <b>نتیجه‌ی sensors-detect</b>", "<pre>%s</pre>" % esc(clip(tail, 2200))]
        rows = []
        for d in p["fan_drivers"]:
            rows.append([Btn("📥 لود %s" % d, "fan_modprobe", d, "", confirm="درایور <b>%s</b> لود شود؟ اگر فن جدیدی پیدا شد، در بوت هم لود می‌شود." % d)])
        for i in p["unknown_ids"]:
            rows.append([Btn("📥 it87 force_id=%s" % i, "fan_modprobe", "it87", i,
                             confirm="it87 با <b>force_id=%s</b> لود شود؟\n⚠️ force_id یعنی چیپ را به زور یک مدل فرض می‌کنیم؛ اگر اشتباه باشد مقادیر غلط می‌شوند. اول بدون آن امتحان کنید." % i)])
        if not rows:
            lines.append("😕 هیچ کنترلر فنی پیدا نشد. یعنی فن CPU این برد از لینوکس قابل کنترل نیست و فقط BIOS (مثلاً Smart Fan) آن را کنترل می‌کند.")
        elif "it87" in p["fan_drivers"] and not p["unknown_ids"]:
            lines.append("ℹ️ اگر it87 لود نشد، روی بردهای قدیمی force_id لازم است (ID را از خروجی بالا ببینید).")
        return Screen("\n".join(lines), rows, back=("fan", []))

    @app.action("fan_modprobe", write=True, steps=1, view=False)
    def modprobe(ctx, driver, force_id=""):
        before = {f["id"] for f in hw.fans()}
        r = hw.modprobe(driver, force_id or None)
        if r.returncode != 0:
            return Screen("❌ modprobe %s نشد:\n<pre>%s</pre>" % (esc(driver), esc(clip(r.stderr or r.stdout, 1500))), back=("fan", []))
        new = [f for f in hw.fans() if f["id"] not in before]
        if not new:
            return Screen("ℹ️ درایور %s لود شد ولی pwm تازه‌ای پیدا نشد؛ پس این درایور فن را کنترل نمی‌کند." % esc(driver), back=("fan", []))
        hw.persist_driver(driver, force_id or None)
        log("fan driver %s%s loaded and kept by chat %s", driver, " force_id=" + force_id if force_id else "", ctx.chat_id)
        return Screen("✅ %s لود شد و در بوت هم لود می‌شود.\nفن‌های تازه:\n%s" % (
            esc(driver), "\n".join("• " + esc(f["label"]) for f in new)), back=("fan", []))

    @app.action("fan_auto", write=True, steps=1, view=False)
    def fan_auto(ctx):
        chat, mid = ctx.chat_id, ctx.msg_id
        ctx.answer("شروع شد…")
        if getattr(app, "fan_auto_running", False):
            return Screen("⏳ پیدا کردن خودکار از قبل در حال اجراست.", back=("fan", []))
        app.fan_auto_running = True
        lines = ["🪄 <b>پیدا کردن کنترلر فن CPU</b>"]

        def show(text):
            try:
                t, mk = app.render(chat, Screen("\n".join(lines + ["⏳ " + text])))
                app.bot.edit(chat, mid, t, mk)
            except Exception:  # a failed edit must not stop the search
                pass

        def run():
            try:
                rep = hw.auto_detect(progress=show)
                t, mk = app.render(chat, auto_result(rep))
            except Exception as e:  # noqa: B902 - show it instead of dying in a thread
                log("fan auto-detect: %r", e)
                t, mk = app.render(chat, Screen("❌ خطا: %s" % esc(e), back=("fan", [])))
            finally:
                app.fan_auto_running = False
            try:
                app.bot.edit(chat, mid, t, mk)
            except Exception:  # noqa: B902
                app.notify(t, chats=[chat])
        log("fan auto-detect asked by chat %s", chat)
        show("شروع…")
        threading.Thread(target=run, name="fan-auto", daemon=True).start()
        return None

    def auto_result(rep):
        lines = ["🪄 <b>نتیجه‌ی پیدا کردن کنترلر فن CPU</b>"] + rep["steps"]
        rows = []
        if rep["driver"]:
            lines.append("")
            lines.append("✅ درایور <b>%s</b>%s لود شد و از این به بعد در بوت هم لود می‌شود." % (
                esc(rep["driver"]), " (با %s)" % esc(rep["options"]) if rep["options"] else ""))
            for f, t in rep["fans"]:
                if t["ok"]:
                    lines.append("🌀 %s: کار می‌کند — %s RPM در ۱۰۰٪ و %s RPM در ۳۵٪" % (esc(f["id"]), fa(t["hi"]), fa(t["lo"])))
                    rows.append([Btn("📈 منحنی معمولی برای %s" % t["input"], "fan_set", f["id"], "curve", "normal", style=SUCCESS),
                                 Btn("🌀 تنظیم این فن", "fan_one", f["id"], style=PRIMARY)])
                else:
                    lines.append("▫️ %s: دور هیچ فنی با آن عوض نشد (یا فنی به آن وصل نیست%s)" % (
                        esc(f["id"]), "، یا BIOS آن را قفل کرده" if t.get("hi") else ""))
            if not any(t["ok"] for _, t in rep["fans"]):
                lines.append("⚠️ pwm هست ولی دور فن عوض نمی‌شود. در BIOS «CPU Fan Control / Smart Fan» را روی Manual یا "
                             "Disabled بگذارید تا BIOS با لینوکس سر فن دعوا نکند، و اگر فن ۳ سیم است حالت DC/Voltage را انتخاب کنید؛ بعد دوباره امتحان کنید.")
        else:
            lines.append("")
            if rep["conflict"]:
                lines.append("⚠️ چیپ فن هست ولی BIOS پورت‌هایش را برای ACPI گرفته (resource conflict). "
                             "راه رایج: پارامتر کرنل <code>acpi_enforce_resources=lax</code> و یک ری‌استارت. "
                             "این کار رایج و معمولاً بی‌خطر است، ولی یعنی BIOS و درایور هر دو به یک چیپ دست می‌زنند.")
                rows.append([Btn("🧩 افزودن acpi_enforce_resources=lax به GRUB", "fan_acpi_lax", steps=2,
                                 confirm="پارامتر <code>acpi_enforce_resources=lax</code> به خط فرمان کرنل (GRUB) اضافه شود؟ "
                                         "از /etc/default/grub نسخه‌ی پشتیبان گرفته می‌شود و بعد از ری‌استارت اعمال می‌شود.")])
            elif rep.get("unknown_ids"):
                lines.append("ℹ️ sensors-detect یک چیپ ناشناخته دید (%s)؛ دکمه‌ی it87 با force_id را از «🔍 فقط اجرای sensors-detect» امتحان کنید."
                             % "، ".join(rep["unknown_ids"]))
            else:
                lines.append("😕 هیچ کنترلر فنی پیدا نشد. یعنی فن CPU این برد از لینوکس قابل کنترل نیست و فقط BIOS "
                             "(Smart Fan / CPU Fan Control) آن را کنترل می‌کند. منحنی را در BIOS تنظیم کنید.")
        return Screen("\n".join(lines), rows, back=("fan", []))

    @app.action("fan_acpi_lax", write=True, steps=2, view=False)
    def fan_acpi_lax(ctx):
        ok, msg = hw.acpi_lax()
        log("acpi_enforce_resources=lax by chat %s: %s", ctx.chat_id, msg)
        rows = [[Btn("🔁 ری‌استارت سرور", "server_reboot", confirm="سرور ری‌استارت شود؟", steps=2)]] if ok else []
        return Screen(("✅ " if ok else "ℹ️ ") + esc(msg), rows, back=("fan", []))

    @app.action("bios")
    def bios(ctx):
        return Screen(BIOS_GUIDE, [[Btn("🔥 تست پایداری", "stress", style=PRIMARY)]], back=("fan", []))

    # -------------------------------------------------------- stress test
    @app.action("stress")
    def stress_page(ctx):
        t = app.stress
        if t and t.running():
            return Screen(t.progress_text(), [[Btn("⏹ توقف تست", "stress_stop", style=SUCCESS)]], refresh=("stress", []), back=("fan", []))
        lines = ["🔥 <b>تست پایداری</b>",
                 "همه‌ی هسته‌ها با پروسس‌های پایتون زیر بار کامل می‌روند. هر ۳۰ ثانیه دما، دور فن‌ها و فرکانس گزارش می‌شود.",
                 "اگر دمای CPU به ۸۵°C برسد، تست خودکار قطع می‌شود.",
                 "برای بررسی اورکلاک BIOS و پاور ضعیف."]
        rows = [[Btn("%s دقیقه" % fa(m), "stress_go", m, confirm="سرور %s دقیقه زیر بار کامل برود؟ سرویس‌ها کندتر می‌شوند." % fa(m))
                 for m in (10, 30, 60)]]
        return Screen("\n".join(lines), rows, back=("fan", []))

    @app.action("stress_go", write=True, steps=1, view=False)
    def stress_go(ctx, minutes):
        if app.stress and app.stress.running():
            return stress_page(ctx)
        chat, mid = ctx.chat_id, ctx.msg_id

        def update(t):
            text, mk = app.render(chat, Screen(t.progress_text(), [[Btn("⏹ توقف تست", "stress_stop", style=SUCCESS)]]))
            app.bot.edit(chat, mid, text, mk)

        def done(t):
            try:
                text, mk = app.render(chat, Screen(t.result_text(), back=("fan", [])))
                app.bot.edit(chat, mid, text, mk)
            except Exception:  # the message may be gone; send the result anyway
                app.notify(t.result_text(), chats=[chat])
        log("stress test %d min started by chat %s", minutes, chat)
        app.stress = stress.StressTest(int(minutes), update, done, meta={"chat_id": chat, "msg_id": mid}).start()
        return None

    @app.action("stress_stop", write=True, view=False)
    def stress_stop(ctx):
        if app.stress and app.stress.running():
            app.stress.stop()
        ctx.answer("⏹ متوقف می‌شود…")
        return None


def after_start(app):
    left = stress.leftover()
    if left:
        st, rebooted = left
        chat = st.get("chat_id")
        text = ("⚠️ <b>سرور وسط تست پایداری ری‌استارت شد!</b>\nاحتمالاً پاور یا اورکلاک ناپایدار است." if rebooted
                else "ℹ️ تست پایداری قبلی با ری‌استارت ربات قطع شد.")
        app.notify(text, chats=[chat] if chat else None)
