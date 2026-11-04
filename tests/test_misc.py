"""QR codes, the firewall and services menus, the fan menu, and starting without ERIS or Marzban."""
import base  # noqa: F401  (environment first)
import os
import struct
import subprocess
import time
import unittest
import zlib

import mocks
from base import OWNER, ROOT, BotCase
from erisbot import config, fand, qr, system, ui

# made with Nayuki's reference encoder (qrcodegen), ECC M, mask 3
REF_TEXT = "https://panel.example.com:8443/sub/dGVzdC11c2VyLDE3MzAwMDAwMDA5X2FiY2RlZg"
REF = ["1fde854d7f", "1055637541", "174921095d", "17560e3f5d", "1742fad75d", "1045e35441", "1fd555557f", "1bac5100",
       "16e7b3ed4b", "1407f8636a", "157064a4a4", "18a8e67c2c", "55f991ef3", "b0be25ff9", "16facfddd4", "13af8eb5f2",
       "acb5d622e", "8aa1390ef", "1746b2976f", "159f4a013a", "11d195c7b9", "1e9037e408", "dff88a0e0", "12ab85785e",
       "1ce12664c5", "7885d05b7", "b66bf08ee", "1603d39123", "7fe6d89f3", "1a6ca917", "1fd7a9c155", "105c233310",
       "174f6611f8", "1758e7ccd5", "1754d572d0", "1041105a3c", "1fd5fb1cdb"]


class QrTests(unittest.TestCase):
    def test_same_as_the_reference_encoder(self):
        q = qr.QR(REF_TEXT, "M", mask=3)
        self.assertEqual(q.version, 5)
        got = [format(int("".join("1" if v else "0" for v in row), 2), "x") for row in q.modules]
        self.assertEqual(got, REF)

    def test_png(self):
        png = qr.png(REF_TEXT)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        w, h = struct.unpack(">II", png[16:24])
        self.assertEqual(w, (37 + 8) * 8)
        self.assertEqual(w, h)
        idat = png[png.index(b"IDAT") + 4:]
        raw = zlib.decompress(idat[:struct.unpack(">I", png[png.index(b"IDAT") - 4:png.index(b"IDAT")])[0]])
        self.assertEqual(len(raw), h * (w + 1))

    def test_versions_and_limits(self):
        self.assertEqual(qr.QR("x" * 100).version, 6)
        self.assertGreaterEqual(qr.QR("x" * 1000).version, 20)
        with self.assertRaises(ValueError):
            qr.QR("x" * 3000)


class FirewallTests(BotCase):
    STATUS = """ERIS firewall  ON   table inet eris_guard, loaded at boot   settings: /etc/eris/firewall.conf
  Open to the Internet
    1) 443/tcp    limit 300 shared   xray-site
    2) 51820/udp  no limit
  Full access (LAN, Docker)
    3) 192.168.1.0/24                LAN
  Custom rules
    4) allow  192.0.2.10             full access
    5) deny   198.51.100.0/24        blocked     12 packets
  always allowed: localhost, SSH (22) from the LAN
"""

    def setUp(self):
        BotCase.setUp(self)
        base.put(os.path.join(os.environ["FAKE_ERIS_DIR"], "fw.txt"), self.STATUS)

    def calls(self):
        p = os.path.join(os.environ["FAKE_ERIS_DIR"], "calls.log")
        with open(p) as f:
            return [l.strip() for l in f if l.split()[:1] == ["fw"]]

    def test_open_port_with_confirmation_and_keep(self):
        self.say("/menu")
        text, markup, _ = self.press("🛡 فایروال")
        self.assertIn("443/tcp", text)
        self.press("باز کردن پورت")
        self.say("8443/tcp limit 300")
        text, markup, _ = self.last_screen()
        self.assertIn("8443/tcp", text)
        self.assertEqual(self.calls(), ["fw"])                  # nothing changed before the red button
        text, markup, _ = self.press("بله")
        self.assertIn("fw open 8443/tcp limit 300", self.calls())
        self.assertIn("ثانیه", text)
        self.assertIn("✅ نگه دار", str(mocks.buttons(markup)))
        self.press("نگه دار")
        self.assertIn("fw confirm", self.calls())
        self.app.fw_countdowns.clear()

    def test_never_opens_ssh(self):
        self.say("/menu")
        self.press("🛡 فایروال")
        self.press("باز کردن پورت")
        self.say("22")
        self.assertIn("SSH", self.last_screen()[0] or self.tg.all("sendMessage")[-1]["text"])
        self.assertFalse(any("open 22" in c for c in self.calls()))

    def test_force_needs_a_second_red_button(self):
        self.say("/menu")
        self.press("🛡 فایروال")
        self.press("باز کردن پورت")
        self.say("8080")
        self.press("بله")
        text, markup, _ = self.last_screen()
        self.assertIn("خطرناک", text)
        self.assertNotIn("fw open 8080/tcp --force", self.calls())
        self.press("--force")
        self.press("بله")
        self.assertIn("fw open 8080/tcp --force", self.calls())
        self.app.fw_countdowns = {}

    def test_deny_and_remove(self):
        self.say("/menu")
        self.press("🛡 فایروال")
        self.press("deny IP")
        self.say("203.0.113.0/24")
        self.press("بله")
        self.assertIn("fw deny 203.0.113.0/24", self.calls())
        self.app.fw_countdowns = {}
        self.say("/menu")
        self.press("🛡 فایروال")
        self.press("حذف قانون")
        self.press("5) deny")
        self.press("بله")
        self.assertIn("fw remove 5", self.calls())
        self.app.fw_countdowns = {}


class ServiceTests(BotCase):
    def setUp(self):
        BotCase.setUp(self)
        self.orig = system.services, system.logs, system.restart, system.unit_info
        system.services = lambda conf: [{"name": "x-ui", "kind": "unit", "state": "failed", "sub": "failed", "since": "", "desc": ""},
                                        {"name": "jellyfin", "kind": "container", "state": "active", "sub": "running",
                                         "since": "Up 3 days", "desc": "Docker"}]
        system.unit_info = lambda n: {"name": n, "kind": "unit", "load": "loaded", "state": "failed", "sub": "failed",
                                      "since": "", "desc": "3x-ui"}
        system.logs = lambda kind, name, n=30: "line <1>\nline 2"
        self.restarted = []
        system.restart = lambda kind, name: self.restarted.append((kind, name)) or subprocess.CompletedProcess([], 0, "", "")

    def tearDown(self):
        system.services, system.logs, system.restart, system.unit_info = self.orig
        BotCase.tearDown(self)

    def test_logs_and_restart(self):
        self.say("/menu")
        text, _, _ = self.press("🖥 سرویس‌ها")
        self.assertIn("🔴 x-ui", text)
        self.press("x-ui")
        self.press("لاگ")
        self.assertIn("line &lt;1&gt;", self.tg.all("sendMessage")[-1]["text"])   # escaped HTML
        self.press("ری‌استارت")
        self.assertEqual(self.restarted, [])
        self.press("بله")
        self.assertEqual(self.restarted, [("unit", "x-ui")])


class FanMenuTests(BotCase):
    def test_fan_page_and_modes(self):
        self.say("/menu")
        text, _, _ = self.press("فن و عملکرد")
        self.assertIn("فن کارت گرافیک", text)
        self.assertIn("cpufreq: ندارد", text)
        self.assertIn("تشخیص کنترلر", text)
        self.press("🌀 فن کارت گرافیک")
        self.press("۶۰٪")
        self.assertEqual(fand.load_cfg()["fans"]["nouveau@0000:01:00.0/pwm1"], {"mode": "manual", "percent": 60.0})
        self.press("➖ ۵٪")
        self.assertEqual(fand.load_cfg()["fans"]["nouveau@0000:01:00.0/pwm1"]["percent"], 55.0)
        for _ in range(8):
            self.press("➖ ۵٪")
        self.assertEqual(fand.load_cfg()["fans"]["nouveau@0000:01:00.0/pwm1"]["percent"], 25)
        self.press("منحنی تند")
        self.assertEqual(fand.load_cfg()["fans"]["nouveau@0000:01:00.0/pwm1"]["curve_name"], "steep")
        self.press("خودکار")
        self.assertEqual(fand.load_cfg()["fans"]["nouveau@0000:01:00.0/pwm1"], {"mode": "auto"})

    def test_profiles_show_what_changed(self):
        self.say("/menu")
        self.press("فن و عملکرد")
        text, _, _ = self.press("پروفایل‌ها")
        self.assertIn("در دسترس نیست روی این سرور", text)
        self.press("🚀 عملکرد")
        text, _, _ = self.press("اعمال")
        self.assertIn("vm.swappiness: از 60 به 10", text)
        self.assertIn("در دسترس نیست روی این سرور", text)

    def test_sensors_detect_nothing_found(self):
        script = config.SENSORS_DETECT
        base.put(script, "#!/bin/sh\necho 'Sorry, no sensors were detected.'\n")
        os.chmod(script, 0o755)
        self.say("/menu")
        self.press("فن و عملکرد")
        self.press("تشخیص کنترلر")
        self.press("اجرای sensors-detect")
        text, _, _ = self.press("بله")
        self.assertIn("فقط BIOS", text)

    def test_bios_guide(self):
        self.say("/menu")
        self.press("فن و عملکرد")
        text, _, _ = self.press("راهنمای اورکلاک")
        for needle in ("Q9550", "366", "380", "۷۰°C", "۲۳۰ وات", "setfsb"):
            self.assertIn(needle, text)


class WithoutErisOrMarzban(BotCase):
    def test_starts_and_works(self):
        config.ERIS_BIN = ROOT + "/missing/eris"
        self.assertFalse(self.app.marzban.available())
        self.app.start_background()
        try:
            self.say("/start")
            labels = [t for t, _, _ in mocks.buttons(self.last_screen()[1])]
            self.assertNotIn("👥 مرزبان", labels)
            self.assertNotIn("💾 بکاپ", labels)
            self.app.alert_round(time.time())
            self.press("📊 داشبورد")
            self.assertIn("آپتایم", self.last_screen()[0])
        finally:
            self.app.stop_evt.set()


class UiTests(unittest.TestCase):
    def test_every_button_fits_64_bytes(self):
        base.reset_tree()
        from erisbot.store import Callbacks
        cbs = Callbacks(os.path.join(config.STATE, "cb.json"))
        long_args = ["x" * 500, {"a": list(range(100))}]
        self.assertLessEqual(len(cbs.nav(OWNER, "some_action", long_args)), 64)
        self.assertLessEqual(len(cbs.once(OWNER, "_yes", long_args)), 64)
        self.assertEqual(cbs.nav(OWNER, "a", [1]), cbs.nav(OWNER, "a", [1]))
        self.assertNotEqual(cbs.nav(OWNER, "a", [1]), cbs.nav(OWNER + 1, "a", [1]))
        cid = cbs.once(OWNER, "x", [])
        self.assertEqual(cbs.take(cid, OWNER), ("x", []))
        self.assertIsNone(cbs.take(cid, OWNER))
        cbs.save(force=True)
        self.assertEqual(os.stat(os.path.join(config.STATE, "cb.json")).st_mode & 0o777, 0o600)

    def test_reply_keyboard(self):
        kb = ui.reply_keyboard()
        self.assertEqual(kb["keyboard"][0][0]["text"], ui.MENU_TEXT)


if __name__ == "__main__":
    unittest.main()
