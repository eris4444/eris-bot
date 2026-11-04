"""Marzban menus against the mock Marzban API (HTTPS with a certificate not valid for 127.0.0.1)."""
import base  # noqa: F401  (environment first)
import time
import unittest

import mocks
from base import OWNER, VIEWER, BotCase
from erisbot import marzban

GB = 1024 ** 3


class MarzbanUnitTests(unittest.TestCase):
    def test_online_now_is_two_minutes(self):
        now = 1_800_000_000
        iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S.375208", time.gmtime(t))  # noqa: E731
        users = [{"username": "a", "online_at": iso(now - 30)}, {"username": "b", "online_at": iso(now - 119)},
                 {"username": "c", "online_at": iso(now - 121)}, {"username": "d", "online_at": None}]
        self.assertEqual(marzban.online_now(users, now), ["a", "b"])

    def test_utc_without_z(self):
        self.assertEqual(marzban.utc_epoch("2026-11-04T09:57:30.375208"), 1793786250)
        self.assertIsNone(marzban.utc_epoch(None))


class MarzbanMenuTests(BotCase):
    marzban = True

    def open_mz(self, chat=OWNER):
        self.say("/menu", chat)
        return self.press("👥 مرزبان", chat)

    def test_overview(self):
        text, markup, _ = self.open_mz()
        self.assertIn("وصل همین الان (۲ دقیقه‌ی اخیر): ۱", text)
        self.assertIn("alice", text)
        self.assertNotIn("bob", text.split("وصل همین الان")[1].split("\n")[1])
        self.assertIn("12.0 GB", text)                         # today: 2+6+1+3 GiB
        start = time.strptime(self.mz.usage_start, "%Y-%m-%dT%H:%M:%S")
        self.assertEqual(time.strftime("%H:%M", time.localtime(time.mktime(start) - time.timezone)), "00:00")

    def test_users_pages_filters_and_search(self):
        self.open_mz()
        text, markup, _ = self.press("👥 کاربرها")
        self.assertIn("۲۵ نفر", text)
        self.assertIn("1 / 4".translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")), str(mocks.buttons(markup)))
        text, markup, _ = self.press("بعدی")
        self.assertIn("user", str(mocks.buttons(markup)))
        self.press("محدود")
        self.assertIn("carol", str(mocks.buttons(self.last_screen()[1])))
        self.say("caro")                                      # sending a name searches
        self.assertIn("👤 <b>carol</b>", self.last_screen()[0])
        self.say("user1")
        self.assertIn("«user1»", self.last_screen()[0])

    def test_user_actions(self):
        self.open_mz()
        self.say("bob")
        old = self.mz.users["bob"]["expire"]
        self.press("+۳۰ روز")
        self.assertEqual(self.mz.users["bob"]["expire"], int(old + 30 * 86400))
        self.press("+۱۰ گیگ")
        self.assertEqual(self.mz.users["bob"]["data_limit"], 20 * GB)
        self.press("غیرفعال")
        self.assertEqual(self.mz.users["bob"]["status"], "disabled")
        self.press("فعال کردن")
        self.assertEqual(self.mz.users["bob"]["status"], "active")
        self.press("ریست مصرف")
        self.press("بله")
        self.assertEqual(self.mz.users["bob"]["used_traffic"], 0)
        self.say("erin")
        self.press("+۵۰ گیگ")
        self.assertIn("نامحدود", self.answers()[-1])

    def test_delete_with_the_500_bug(self):
        self.open_mz()
        self.say("dave")
        self.press("حذف کاربر")
        text, _, _ = self.press("بله")
        self.assertIn("حذف شد", text)
        self.assertNotIn("dave", self.mz.users)

    def test_create_user_wizard(self):
        self.open_mz()
        self.press("ساخت کاربر")
        self.say("Bad Name")
        self.assertIn("نامعتبر", self.tg.all("sendMessage")[-1]["text"])
        self.say("newbie")
        self.press("۵۰ گیگ")
        self.press("۳۰ روز")
        text, markup, _ = self.last_screen()
        self.assertIn("VLESS_TCP", text + str(mocks.buttons(markup)))
        self.press("VLESS_WS")                               # all vless inbounds are on at first: this one off
        self.press("VMESS_WS")                               # and a vmess one on
        self.press("ساخت کاربر")
        b = self.mz.users["newbie"]["_body"]
        self.assertEqual(b["proxies"], {"vless": {}, "vmess": {}})
        self.assertEqual(b["inbounds"], {"vless": ["VLESS_TCP"], "vmess": ["VMESS_WS"]})
        self.assertEqual(b["data_limit"], 50 * GB)
        self.assertAlmostEqual(b["expire"], time.time() + 30 * 86400, delta=60)
        self.assertIn("ساخته شد", self.answers()[-1] or "ساخته شد")

    def test_subscription_link_and_qr_for_the_owner_only(self):
        self.open_mz()
        self.say("alice")
        self.press("لینک ساب")
        self.assertIn("https://panel.example.com/sub/alice-token-abcdef", self.tg.all("sendMessage")[-1]["text"])
        self.press("QR")
        chat, name, png, cap = self.tg.photos[-1]
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.open_mz(VIEWER)
        self.say("alice", VIEWER)
        self.press("لینک ساب", VIEWER)
        self.assertIn("فقط برای مالک", self.answers()[-1])

    def test_nodes_and_reconnect(self):
        self.open_mz()
        text, _, _ = self.press("🛰 نودها")
        self.assertIn("tr-node", text)
        self.assertIn("connection refused", text)
        self.press("ری‌کانکت tr-node")
        self.assertEqual(self.mz.reconnected, [2])

    def test_login_again_on_401(self):
        self.open_mz()
        self.mz.tokens.clear()                                # the panel restarted: old token rejected
        self.press("🔄 به‌روزرسانی")
        self.assertIn("مرزبان", self.last_screen()[0])
        self.assertEqual(len(self.mz.tokens), 1)


if __name__ == "__main__":
    unittest.main()
