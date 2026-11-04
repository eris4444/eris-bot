"""The bot as a whole against the mock Bot API: who may use it, menus, colours, confirmations, callbacks."""
import base  # noqa: F401  (environment first)
import json
import os
import time
import unittest

import mocks
from base import OWNER, ROOT, STRANGER, VIEWER, BotCase
from erisbot import config, system


class AccessTests(BotCase):
    def test_strangers_get_nothing(self):
        self.tg.text(STRANGER, "/start")
        self.tg.press(STRANGER, 5, "nanything")
        self.poll()
        self.assertEqual([m for m, p in self.tg.sent if m not in ("getUpdates", "getMe")], [])

    def test_start_menu_and_reply_keyboard(self):
        self.say("/start")
        sends = self.tg.all("sendMessage")
        kb = sends[0]["reply_markup"]
        self.assertEqual(kb["keyboard"][0][0]["text"], "🏠 منو")
        self.assertTrue(kb["is_persistent"])
        text, markup, mid = self.last_screen()
        self.assertIn("منوی ERIS", text)
        labels = [t for t, _, _ in mocks.buttons(markup)]
        self.assertIn("📊 داشبورد", labels)
        self.assertIn("💾 بکاپ", labels)            # ERIS is here (the fake eris)
        self.assertNotIn("👥 مرزبان", labels)       # no /opt/marzban/.env
        for t, data, style in mocks.buttons(markup):
            self.assertLessEqual(len(data.encode()), 64)
            self.assertEqual(style, "primary", t)    # menus are blue

    def test_menus_hidden_without_eris(self):
        config.ERIS_BIN = ROOT + "/nope/eris"
        self.say("/menu")
        labels = [t for t, _, _ in mocks.buttons(self.last_screen()[1])]
        self.assertNotIn("💾 بکاپ", labels)
        self.assertNotIn("🛡 فایروال", labels)
        self.assertIn("🔁 سرور", labels)
        self.press("📊 داشبورد")                     # the dashboard still works without ERIS and Marzban
        self.assertIn("داشبورد", self.last_screen()[0])

    def test_every_screen_has_refresh_and_back(self):
        self.say("/menu")
        for needle in ("📊 داشبورد", "🔔 هشدارها", "⚙️ تنظیمات", "🔁 سرور"):
            self.say("/menu")
            text, markup, _ = self.press(needle)
            labels = [t for t, _, _ in mocks.buttons(markup)]
            self.assertIn("🔙 برگشت", labels, needle)
            self.assertTrue(any("به‌روزرسانی" in l for l in labels), needle)

    def test_menu_edits_one_message(self):
        self.say("/menu")
        _, _, mid = self.last_screen()
        self.press("📊 داشبورد")
        self.assertEqual(self.tg.last("editMessageText")["message_id"], mid)
        self.press("🔙 برگشت")
        self.assertEqual(self.tg.last("editMessageText")["message_id"], mid)

    def test_forged_and_expired_callbacks(self):
        self.say("/menu")
        before = len(self.tg.all("editMessageText"))
        self.press_data("nforged0000")
        self.assertIn("منقضی", self.answers()[-1])
        self.press_data("x" * 64)
        self.assertEqual(len(self.tg.all("editMessageText")), before)

    def test_callback_of_another_chat_is_refused(self):
        self.say("/menu")
        _, markup, mid = self.last_screen()
        data = mocks.find_button(markup, "🔁 سرور")
        self.tg.press(VIEWER, mid, data)               # an allowed chat, but the id belongs to OWNER
        self.poll()
        self.assertIn("منقضی", self.answers()[-1])

    def test_viewer_can_look_but_not_change(self):
        self.say("/menu", chat=VIEWER)
        self.assertIn("فقط دیدن", self.last_screen(VIEWER)[0])
        self.press("🔁 سرور", chat=VIEWER)
        calls = []
        system.reboot = lambda: calls.append(1)
        self.press("ری‌استارت سرور", chat=VIEWER)
        self.assertIn("فقط برای مالک", self.answers()[-1])
        self.assertEqual(calls, [])

    def test_rate_limit(self):
        self.app.rate.events = 3
        for _ in range(6):
            self.tg.text(OWNER, "/menu")
        self.poll()
        menus = [p for p in self.tg.all("sendMessage") if "منوی ERIS" in p["text"]]
        self.assertEqual(len(menus), 3)
        self.assertTrue(any("صبر کنید" in p["text"] for p in self.tg.all("sendMessage")))

    def test_colours_fall_back_when_refused(self):
        self.tg.reject_style = True
        self.say("/menu")
        text, markup, _ = self.last_screen()
        self.assertIn("منوی ERIS", text)
        self.assertNotIn("style", json.dumps(markup))
        self.assertTrue(self.app.state.get("styles_refused"))

    def test_colour_rules(self):
        self.say("/menu")
        self.press("🔁 سرور")
        styles = {t: s for t, _, s in mocks.buttons(self.last_screen()[1])}
        self.assertEqual(styles["🔁 ری‌استارت سرور"], "danger")
        self.assertEqual(styles["⏻ خاموش کردن سرور"], "danger")
        self.assertEqual(styles["🔙 برگشت"], "primary")
        self.say("/menu")
        self.press("💾 بکاپ")
        styles = {t: s for t, _, s in mocks.buttons(self.last_screen()[1])}
        self.assertEqual(styles["📤 بکاپ و ارسال همین الان (همه)"], "success")


class ConfirmTests(BotCase):
    def setUp(self):
        BotCase.setUp(self)
        self.calls = []
        self._orig = system.reboot, system.poweroff
        system.reboot = lambda: self.calls.append("reboot") or system.subprocess.CompletedProcess([], 0, "", "")
        system.poweroff = lambda: self.calls.append("off") or system.subprocess.CompletedProcess([], 0, "", "")

    def tearDown(self):
        system.reboot, system.poweroff = self._orig
        BotCase.tearDown(self)

    def test_reboot_needs_two_red_confirmations(self):
        self.say("/menu")
        self.press("🔁 سرور")
        text, markup, _ = self.press("ری‌استارت سرور")
        self.assertIn("تأیید لازم است", text)
        self.assertIn("۶۰ ثانیه", text)
        yes = [b for b in mocks.buttons(markup) if "بله" in b[0]][0]
        self.assertEqual(yes[2], "danger")
        self.assertTrue(yes[1].startswith("o"))          # single use
        text, markup, _ = self.press("بله")
        self.assertIn("تأیید نهایی", text)
        self.assertEqual(self.calls, [])
        self.press("بله")
        self.assertEqual(self.calls, ["reboot"])

    def test_confirmation_is_single_use_and_expires(self):
        self.say("/menu")
        self.press("🔁 سرور")
        _, markup, mid = self.press("خاموش کردن")
        data = mocks.find_button(markup, "بله")
        self.app.cbs.map[data]["exp"] = time.time() - 1   # 60 s later
        self.tg.press(OWNER, mid, data)
        self.poll()
        self.assertIn("منقضی", self.answers()[-1])
        self.assertEqual(self.calls, [])

    def test_the_action_itself_refuses_without_confirmation(self):
        cid = self.app.cbs.nav(OWNER, "server_reboot", [])    # even a valid id straight to the action
        self.press_data(cid)
        self.assertEqual(self.calls, [])
        self.assertIn("تأیید", self.answers()[-1])

    def test_cancel(self):
        self.say("/menu")
        self.press("🔁 سرور")
        self.press("ری‌استارت سرور")
        text, _, _ = self.press("لغو")
        self.assertIn("سرور", text)
        self.assertEqual(self.calls, [])


class SettingsTests(BotCase):
    def test_password_is_set_and_message_deleted(self):
        self.say("/menu")
        self.press("⚙️ تنظیمات")
        self.press("🔑 رمز بکاپ")
        self.tg.text(OWNER, "correct horse battery staple", mid=777)
        self.poll()
        self.assertIn((OWNER, 777), self.tg.deleted)
        pw = self.app.sender.password_file()
        with open(pw) as f:
            self.assertEqual(f.read().strip(), "correct horse battery staple")
        self.assertEqual(os.stat(pw).st_mode & 0o777, 0o600)
        for m, p in self.tg.sent:
            self.assertNotIn("battery", json.dumps(p, ensure_ascii=False))

    def test_config_stays_600_and_proxy_changes(self):
        self.say("/menu")
        self.press("⚙️ تنظیمات")
        self.press("🌐 پروکسی")
        self.press("خودکار")
        self.assertEqual(config.load()["proxy"], "auto")
        self.assertEqual(os.stat(config.CONFIG).st_mode & 0o777, 0o600)

    def test_add_chat_and_keep_one_owner(self):
        self.say("/menu")
        self.press("⚙️ تنظیمات")
        self.press("👥 chat IDها")
        self.press("افزودن")
        self.say("5555")
        c = config.load()
        self.assertIn(5555, c["allowed_chat_ids"])
        self.assertEqual(config.role(c, 5555), "viewer")
        self.app.save_conf(allowed_chat_ids=[OWNER], roles={})
        self.app.run(self.app_ctx(), "chat_role", [OWNER])
        self.assertEqual(config.role(config.load(), OWNER), "owner")

    def app_ctx(self):
        from erisbot.app import Ctx
        return Ctx(self.app, OWNER, "owner")

    def test_no_token_waits(self):
        write = base.write_config
        write(token="", api_base=self.tg.url())
        self.app.stop_evt.set()
        self.assertEqual(self.app.run_forever(), 0)


class DashboardTests(BotCase):
    def test_dashboard_numbers(self):
        self.say(base.ROOT and "📊 داشبورد")
        text = self.tg.all("sendMessage")[-1]["text"]
        for needle in ("آپتایم", "CPU ۵۰°C", "sda ۳۶°C", "رم", "💽 /", "هشدار", "💾 آخرین بکاپ"):
            self.assertIn(needle, text)
        self.assertIn("۱ روز و ۲ ساعت", text)


if __name__ == "__main__":
    unittest.main()
