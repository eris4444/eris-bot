"""1.2.0: typed amounts for Marzban users, the public IP fix, the colour fix, Claude/Codex, NAS files, weather."""
import base  # noqa: F401  (environment first)
import http.server
import json
import os
import shutil
import threading
import time
import unittest

import mocks
from base import OWNER, ROOT, VIEWER, BotCase, put
from erisbot import ai, config, nas, tg, util, weather

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_AI = os.path.join(HERE, "fake_ai.py")
os.chmod(FAKE_AI, 0o755)


def wait_for(fn, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        r = fn()
        if r:
            return r
        time.sleep(0.05)
    raise AssertionError("timed out waiting")


class AmountTests(unittest.TestCase):
    def test_gb(self):
        p = lambda t: util.parse_amount(t, "gb")  # noqa: E731
        self.assertEqual(p("15"), 15)
        self.assertEqual(p("۱۵"), 15)
        self.assertEqual(p("2.5"), 2.5)
        self.assertEqual(p("۲٫۵ گیگ"), 2.5)
        self.assertEqual(p("512 مگ"), 0.5)
        self.assertEqual(p("1 ترا"), 1024)
        self.assertEqual(p("0"), 0)
        self.assertEqual(p("نامحدود"), 0)
        self.assertIsNone(p("ده"))
        self.assertIsNone(p("-5"))
        self.assertEqual(util.parse_amount("-5", "gb", allow_negative=True), -5)
        self.assertIsNone(p("5 kg"))

    def test_days(self):
        p = lambda t: util.parse_amount(t, "days")  # noqa: E731
        self.assertEqual(p("45"), 45)
        self.assertEqual(p("2 ماه"), 60)
        self.assertEqual(p("12 ساعت"), 0.5)
        self.assertEqual(p("1 سال"), 365)
        self.assertEqual(p("بی‌نهایت"), 0)
        self.assertIsNone(p("99999999"))

    def test_fmt(self):
        self.assertEqual(util.fmt_num(15.0), "۱۵")
        self.assertEqual(util.fmt_num(2.5), "۲.۵")


class StyleTests(BotCase):
    def test_pressing_twice_keeps_colours(self):
        self.say("/menu")
        self.press("به‌روزرسانی")
        self.press("به‌روزرسانی")
        text, markup, _ = self.press("به‌روزرسانی")
        self.assertFalse(self.app.state.get("styles_refused"))
        self.assertIn("style", json.dumps(self.tg.all("editMessageText")[-1]["reply_markup"]))

    def test_not_modified_is_never_a_colour_refusal(self):
        self.assertFalse(tg.style_refusal("Bad Request: message is not modified: specified new message content and reply markup are exactly the same"))
        self.assertTrue(tg.style_refusal("Bad Request: can't parse inline keyboard button: invalid button style"))

    def test_old_wrong_refusal_is_undone(self):
        self.app.state.update(styles_refused=True, styles_refused_reason="Bad Request: message is not modified: x")
        from erisbot.app import App
        a = App(sleep=lambda s: None, workers=0)
        self.assertFalse(a.state.get("styles_refused"))
        a.bot.close()

    def test_real_refusal_is_kept(self):
        self.app.state.update(styles_refused=True, styles_refused_reason="Bad Request: can't parse inline keyboard button")
        from erisbot.app import App
        a = App(sleep=lambda s: None, workers=0)
        self.assertTrue(a.state.get("styles_refused"))
        a.bot.close()


class MarzbanAmountTests(BotCase):
    marzban = True

    def open_new(self):
        self.say("/menu")
        self.press("مرزبان")
        self.press("ساخت کاربر")
        self.say("typed_user")

    def test_typed_data_and_days(self):
        self.open_new()
        text, _, _ = self.last_screen()
        self.assertIn("قدم ۲", text)
        self.say("15")
        self.assertIn("حجم: ۱۵ گیگ", self.last_screen()[0])
        self.say("2 ماه")
        self.assertIn("قدم ۴", self.last_screen()[0])
        self.press("✅ ساخت کاربر")
        u = self.mz.users["typed_user"]
        self.assertEqual(u["data_limit"], 15 * 2 ** 30)
        self.assertAlmostEqual(u["expire"], time.time() + 60 * 86400, delta=120)

    def test_typed_fraction_and_unlimited(self):
        self.open_new()
        self.say("۲٫۵")
        self.say("0")
        self.press("✅ ساخت کاربر")
        u = self.mz.users["typed_user"]
        self.assertEqual(u["data_limit"], int(2.5 * 2 ** 30))
        self.assertIsNone(u["expire"])

    def test_bad_number_asks_again(self):
        self.open_new()
        self.say("ده گیگ")
        text = self.last_screen()[0]
        self.assertIn("عدد حجم نیست", text)
        self.say("10")
        self.assertIn("قدم ۳", self.last_screen()[0])

    def test_presets_still_work(self):
        self.open_new()
        self.press("۲۰ گیگ")
        self.press("۳۰ روز")
        self.press("✅ ساخت کاربر")
        self.assertEqual(self.mz.users["typed_user"]["data_limit"], 20 * 2 ** 30)

    def test_custom_days_and_data_on_a_user(self):
        self.say("/menu")
        self.press("مرزبان")
        self.say("alice")
        before = dict(self.mz.users["alice"])
        self.press("روز دلخواه")
        self.say("-2")
        self.assertAlmostEqual(self.mz.users["alice"]["expire"], before["expire"] - 2 * 86400, delta=5)
        self.press("حجم دلخواه")
        self.say("512 مگ")
        self.assertEqual(self.mz.users["alice"]["data_limit"], before["data_limit"] + 2 ** 29)

    def test_custom_on_unlimited_user_starts_now(self):
        self.say("/menu")
        self.press("مرزبان")
        self.say("erin")
        self.press("روز دلخواه")
        self.say("10")
        self.assertAlmostEqual(self.mz.users["erin"]["expire"], time.time() + 10 * 86400, delta=60)


class AITests(BotCase):
    def setUp(self):
        super().setUp()
        self.log = os.path.join(ROOT, "ai-args.log")
        if os.path.exists(self.log):
            os.remove(self.log)
        os.environ.update(ERISBOT_CLAUDE=FAKE_AI, ERISBOT_CODEX=FAKE_AI, FAKE_AI_LOG=self.log)
        self.orig = (ai.AI_ENV, ai.CLAUDE_DIR, ai.CODEX_HOME)
        ai.AI_ENV = os.path.join(ROOT, "no-ai-env.sh")
        ai.CLAUDE_DIR = os.path.join(ROOT, "claude")
        ai.CODEX_HOME = os.path.join(ROOT, "codex")
        os.makedirs(ai.CODEX_HOME, exist_ok=True)
        put(os.path.join(ai.CODEX_HOME, "models_cache.json"), json.dumps({"models": [
            {"slug": "gpt-test", "display_name": "GPT-Test", "visibility": "list", "default_reasoning_level": "medium",
             "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}, {"effort": "high"}]},
            {"slug": "hidden", "visibility": "hide"}]}))
        put(os.path.join(ai.CODEX_HOME, "config.toml"), 'model = "gpt-test"\n')

    def tearDown(self):
        ai.AI_ENV, ai.CLAUDE_DIR, ai.CODEX_HOME = self.orig
        for k in ("ERISBOT_CLAUDE", "ERISBOT_CODEX", "FAKE_AI_LOG"):
            os.environ.pop(k, None)
        super().tearDown()

    def runs(self):
        with open(self.log) as f:
            return [json.loads(l) for l in f]

    def reply(self, needle):
        return wait_for(lambda: [p["text"] for p in self.tg.all("sendMessage") if needle in p["text"]])

    def test_claude_chat_continues(self):
        self.say("/ai")
        self.press("چت با Claude")
        self.say("hello")
        self.assertIn("echo: hello", self.reply("echo: hello")[0])
        self.say("again")
        self.reply("echo: again")
        a1, a2 = self.runs()
        self.assertIn("--tools", a1)
        self.assertEqual(a1[a1.index("--tools") + 1], "")
        self.assertNotIn("--resume", a1)
        self.assertEqual(a2[a2.index("--resume") + 1], "sess-1")
        self.assertEqual(a2[a2.index("--model") + 1], "sonnet")

    def test_model_and_effort(self):
        self.say("/ai")
        self.press("مدل Claude")
        self.press("Opus")
        self.press("افورت Claude")
        self.press("حداکثر")
        self.press("چت با Claude")
        self.say("hi")
        self.reply("echo: hi")
        a = self.runs()[-1]
        self.assertEqual(a[a.index("--model") + 1], "opus")
        self.assertEqual(a[a.index("--effort") + 1], "max")

    def test_codex_chat_and_new(self):
        self.say("/ai")
        self.press("چت با Codex")
        self.say("q1")
        self.reply("codex: q1")
        self.say("q2")
        self.reply("codex: q2")
        self.say("/ai")                                  # a command leaves the chat...
        self.press("چت با Codex")                        # ...and the chat comes back where it was
        self.press("گفتگوی تازه")
        self.say("q3")
        self.reply("codex: q3")
        r1, r2, r3 = self.runs()
        self.assertNotIn("resume", r1)
        self.assertEqual(r2[:2], ["exec", "resume"])
        self.assertIn("th-1", r2)
        self.assertNotIn("resume", r3)
        self.assertIn('sandbox_mode="read-only"', r1)
        self.assertEqual(r1[r1.index("-m") + 1], "gpt-test")

    def test_errors_are_shown(self):
        self.say("/ai")
        self.press("چت با Claude")
        self.say("fail")
        self.assertTrue(self.reply("overloaded")[0].startswith("❌"))

    def test_long_answer_is_split_and_formatted(self):
        self.say("/ai")
        self.press("چت با Claude")
        self.say("long")
        end = self.reply("the end")
        msgs = [p["text"] for p in self.tg.all("sendMessage") if "<pre>" in p["text"] or "the end" in p["text"] or "<b>bold</b>" in p["text"]]
        self.assertGreater(len(msgs), 1)
        self.assertTrue(all(len(m) <= 4096 for m in msgs))
        self.assertIn("x = 1 &lt; 2", "".join(msgs))
        self.assertIn("<code>code</code>", msgs[0])

    def test_viewer_cannot_chat(self):
        self.say("/ai", chat=VIEWER)
        self.press("چت با Claude", chat=VIEWER)
        self.assertTrue(any("مالک" in (a or "") for a in self.answers()))

    def test_codex_usage_from_logs(self):
        d = os.path.join(ai.CODEX_HOME, "sessions", "2026", "11", "04")
        os.makedirs(d, exist_ok=True)
        put(os.path.join(ai.CODEX_HOME, "auth.json"), "{}")
        put(os.path.join(d, "rollout-1.jsonl"), json.dumps({"timestamp": "2026-11-04T10:00:00Z", "payload": {
            "rate_limits": {"primary": {"used_percent": 42.0, "window_minutes": 300, "resets_at": 1893456000},
                            "secondary": {"used_percent": 7.5, "window_minutes": 10080}}}}) + "\n")
        u = ai.Usage()._codex()
        self.assertEqual([round(w["pct"]) for w in u["windows"]], [42, 8])
        self.assertEqual(u["windows"][0]["label"], "۵ ساعته".replace("۵", "5"))

    def test_split_counts_the_escaped_length(self):
        for md in ("```\n" + "<&>" * 5000 + "\n```", "<" * 9000, "```\n" + "a < b\n" * 3000 + "```"):
            msgs = ai.to_messages(md)
            self.assertTrue(all(len(m) <= 3800 for m in msgs), [len(m) for m in msgs])
            self.assertGreater(len(msgs), 1)

    def test_markdown(self):
        m = ai.to_messages("# Title\nsome **b** and [link](https://example.com/a?b=1) <tag>")
        self.assertIn("<b>Title</b>", m[0])
        self.assertIn('<a href="https://example.com/a?b=1">link</a>', m[0])
        self.assertIn("&lt;tag&gt;", m[0])


class NasTests(BotCase):
    def setUp(self):
        super().setUp()
        self.disk = os.path.join(ROOT, "srv", "dev-disk-by-uuid-1111")
        shutil.rmtree(os.path.join(ROOT, "srv"), ignore_errors=True)
        self.media = os.path.join(self.disk, "Media")
        os.makedirs(os.path.join(self.media, "films"))
        put(os.path.join(self.media, "readme.txt"), "hello nas")
        put(config.R("/etc/openmediavault/config.xml"),
            "<config><system><fstab><mntent><uuid>m1</uuid><dir>/srv/dev-disk-by-uuid-1111</dir></mntent></fstab>"
            "<shares><sharedfolder><name>Media</name><mntentref>m1</mntentref><reldirpath>Media/</reldirpath></sharedfolder>"
            "<sharedfolder><name>sys</name><mntentref>root</mntentref><reldirpath>x/</reldirpath></sharedfolder>"
            "</shares></system></config>")
        self.app.nas_auth.verify_fn = lambda u, p: p == "right-pass"
        self.orig_account = nas.account
        nas.account = lambda u: (os.getuid(), os.getgid(), "/nonexistent") if u == "erisrtg" else None

    def tearDown(self):
        nas.account = self.orig_account
        super().tearDown()

    def login(self, pw="right-pass"):
        self.say("/files")
        self.press("ورود")
        self.say("erisrtg")
        self.tg.text(OWNER, pw)
        self.poll()
        return self.last_screen()

    def test_login_wrong_then_right(self):
        text, _, _ = self.login("nope")
        self.assertIn("اشتباه", text)
        self.assertTrue(self.tg.deleted)              # the password message is deleted
        text, markup, _ = self.login()
        self.assertIn("erisrtg", text)
        labels = [t for t, _, _ in mocks.buttons(markup)]
        self.assertIn("📁 Media", labels)
        self.assertNotIn("📁 sys", labels)            # shares outside /srv are not offered

    def test_lock_after_five_wrong(self):
        a = nas.Auth(verify=lambda u, p: p == "ok")
        for _ in range(5):
            with self.assertRaises(nas.NasError):
                a.check(OWNER, "erisrtg", "bad")
        with self.assertRaises(nas.NasError) as e:
            a.check(OWNER, "erisrtg", "ok")           # right, but locked now
        self.assertIn("زیاد بود", str(e.exception))
        self.assertEqual(a.check(VIEWER, "erisrtg", "ok")[0], os.getuid())     # per chat
        a.fails[OWNER] = [time.time() - 1000] * 5
        self.assertEqual(a.check(OWNER, "erisrtg", "ok")[0], os.getuid())      # after 15 minutes

    def test_browse_upload_send_mkdir_delete(self):
        self.login()
        text, _, _ = self.press("Media")
        self.assertIn("readme.txt", json.dumps(self.last_screen()[1], ensure_ascii=False))
        self.press("آپلود اینجا")
        self.tg.document(OWNER, "notes.txt", b"from telegram")
        self.poll()
        wait_for(lambda: os.path.exists(os.path.join(self.media, "notes.txt")))
        with open(os.path.join(self.media, "notes.txt"), "rb") as f:
            self.assertEqual(f.read(), b"from telegram")
        self.tg.document(OWNER, "notes.txt", b"second")      # same name: kept, new one gets a suffix
        self.poll()
        wait_for(lambda: os.path.exists(os.path.join(self.media, "notes (2).txt")))
        self.press("تمام")
        self.press("readme.txt")
        self.press("بفرست به تلگرام")
        wait_for(lambda: [d for d in self.tg.documents if d[1] == "readme.txt"])
        self.assertEqual([d for d in self.tg.documents if d[1] == "readme.txt"][0][2], b"hello nas")
        self.press("برگشت")
        self.press("پوشه‌ی تازه")
        self.say("docs")
        self.assertTrue(os.path.isdir(os.path.join(self.media, "docs")))
        self.press("notes.txt · ")
        self.press("حذف")
        self.press("بله")
        self.assertFalse(os.path.exists(os.path.join(self.media, "notes.txt")))

    def test_file_without_question_asks_where(self):
        self.login()
        self.press("Media")
        self.tg.document(OWNER, "photo.jpg", b"jpegdata")
        self.poll()
        text, _, _ = self.last_screen()
        self.assertIn("کجا", text)
        self.press("💾")
        wait_for(lambda: os.path.exists(os.path.join(self.media, "photo.jpg")))

    def test_not_logged_in_file_waits_for_login(self):
        self.tg.document(OWNER, "later.bin", b"123")
        self.poll()
        self.assertIn("اول وارد شوید", self.last_screen()[0])
        self.press("ورود")
        self.say("erisrtg")
        self.tg.text(OWNER, "right-pass")
        self.poll()
        self.press("انتخاب پوشه")
        self.press("Media")
        self.press("ذخیره‌ی «later.bin» اینجا")
        wait_for(lambda: os.path.exists(os.path.join(self.media, "later.bin")))

    def test_paths_stay_inside_the_share(self):
        sess = {"uid": os.getuid(), "gid": os.getgid()}
        with self.assertRaises(nas.NasError):
            nas.run_as(sess, self.media, "list", "/etc")
        os.symlink("/etc", os.path.join(self.media, "escape"))
        with self.assertRaises(nas.NasError):
            nas.run_as(sess, self.media, "list", os.path.join(self.media, "escape"))
        with self.assertRaises(nas.NasError):
            nas.run_as(sess, self.media, "mkdir", self.media, "../x")
        listed = nas.run_as(sess, self.media, "list", self.media)
        self.assertNotIn("escape", [e["name"] for e in listed["items"]])

    def test_viewer_has_no_files(self):
        self.say("/files", chat=VIEWER)
        self.assertIn("فقط برای مالک", self.last_screen(VIEWER)[0])

    def test_real_password_check(self):
        h = nas.crypt("secret-1", "$6$saltsalt$")
        self.assertTrue(h and h.startswith("$6$saltsalt$"))
        self.assertEqual(nas.crypt("secret-1", h), h)
        self.assertNotEqual(nas.crypt("secret-2", h), h)


class _Wx(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/geo"):
            body = {"results": [{"name": "تهران", "admin1": "استان تهران", "country": "ایران", "latitude": 35.69,
                                 "longitude": 51.39, "timezone": "Asia/Tehran"}]}
        else:
            days = ["2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08"]
            body = {"current": {"time": "2026-11-04T14:00", "temperature_2m": 18.4, "apparent_temperature": 16.9,
                                "relative_humidity_2m": 30, "wind_speed_10m": 11, "weather_code": 2, "precipitation": 0},
                    "hourly": {"time": ["2026-11-04T15:00", "2026-11-04T18:00", "2026-11-04T21:00"],
                               "temperature_2m": [19, 15, 12], "weather_code": [1, 3, 61], "precipitation_probability": [0, 5, 40]},
                    "daily": {"time": days, "weather_code": [2, 61, 0, 3, 71], "temperature_2m_max": [20, 17, 19, 15, 9],
                              "temperature_2m_min": [9, 8, 7, 5, -2], "precipitation_probability_max": [0, 70, 0, 10, 80],
                              "sunrise": ["2026-11-04T06:20"] * 5, "sunset": ["2026-11-04T16:58"] * 5, "uv_index_max": [3] * 5}}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class WeatherTests(BotCase):
    def setUp(self):
        super().setUp()
        self.srv = mocks._Server(("127.0.0.1", 0), _Wx)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        base_url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.orig = (weather.GEO_URL, weather.FORECAST_URL)
        weather.GEO_URL, weather.FORECAST_URL = base_url + "/geo", base_url + "/forecast"

    def tearDown(self):
        weather.GEO_URL, weather.FORECAST_URL = self.orig
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def test_city_then_forecast(self):
        self.say("/weather")
        self.assertIn("شهرتان", self.last_screen()[0])
        self.say("تهران")
        text = self.last_screen()[0]
        self.assertIn("آب و هوای تهران", text)
        self.assertIn("۱۸°", text)
        self.assertIn("فردا", text)
        self.assertIn("−۲°", text)
        self.assertEqual(self.app.conf["weather"]["lat"], 35.69)

    def test_location_message(self):
        self.tg.location(OWNER, 32.65, 51.67)
        self.poll()
        self.assertIn("موقعیت من", [p["text"] for p in self.tg.all("sendMessage")][-1])

    def test_daily_message(self):
        self.app.save_conf(weather={"city": "تهران", "lat": 35.69, "lon": 51.39, "daily": True, "at": "07:30"})
        lt = time.localtime()
        at = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 7, 40, 0, 0, 0, -1))
        self.assertTrue(self.app.weather_tick(at))
        self.assertFalse(self.app.weather_tick(at + 60))            # once a day
        self.assertIn("آب و هوای تهران", self.tg.all("sendMessage")[-1]["text"])


if __name__ == "__main__":
    unittest.main()
