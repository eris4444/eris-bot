"""Proxies (HTTP CONNECT, SOCKS5 with and without a password, DNS on the proxy) and the Bot API client."""
import base  # noqa: F401  (environment first)
import json
import os
import time
import unittest

import mocks
from base import ROOT
from erisbot import config, net, store, tg


class ProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tg = mocks.MockTelegram()

    @classmethod
    def tearDownClass(cls):
        cls.tg.close()

    def get_me(self, proxy, host="127.0.0.1"):
        url = "http://%s:%d/bot%s/getMe" % (host, self.tg.port, mocks.TOKEN)
        r = net.request("POST", url, proxy=proxy, body=b"{}", headers={"Content-Type": "application/json"}, timeout=5)
        return json.loads(r.body)

    def test_direct(self):
        self.assertTrue(self.get_me(None)["ok"])

    def test_http_connect(self):
        p = mocks.MockProxy()
        try:
            self.assertTrue(self.get_me("http://127.0.0.1:%d" % p.port)["ok"])
            self.assertEqual(p.seen[-1][0], "http")
        finally:
            p.close()

    def test_http_connect_with_login(self):
        p = mocks.MockProxy(user="u", password="p@ss")
        try:
            self.assertTrue(self.get_me("http://u:p%%40ss@127.0.0.1:%d" % p.port)["ok"])
            with self.assertRaises(net.ProxyError):
                self.get_me("http://u:wrong@127.0.0.1:%d" % p.port)
        finally:
            p.close()

    def test_socks5_dns_on_the_proxy(self):
        # telegram.invalid only resolves on the proxy (like api.telegram.org behind a filter)
        p = mocks.MockProxy(hosts={"telegram.invalid": "127.0.0.1"})
        try:
            self.assertTrue(self.get_me("socks5://127.0.0.1:%d" % p.port, host="telegram.invalid")["ok"])
            self.assertEqual(p.seen[-1], ("socks5-dns", "telegram.invalid", self.tg.port))
            with self.assertRaises(OSError):
                self.get_me(None, host="telegram.invalid")
        finally:
            p.close()

    def test_socks5_login(self):
        p = mocks.MockProxy(user="eris", password="s3cret")
        try:
            self.assertTrue(self.get_me("socks5://eris:s3cret@127.0.0.1:%d" % p.port)["ok"])
            with self.assertRaises(net.ProxyError):
                self.get_me("socks5://eris:nope@127.0.0.1:%d" % p.port)
            with self.assertRaises(net.ProxyError):
                self.get_me("socks5://127.0.0.1:%d" % p.port)
        finally:
            p.close()

    def test_describe_hides_password(self):
        self.assertEqual(net.describe("socks5://eris:s3cret@127.0.0.1:1080"), "socks5://eris:***@127.0.0.1:1080")
        self.assertEqual(net.describe(None), "direct")
        self.assertTrue(config.validate_proxy("socks5://127.0.0.1:1090"))
        self.assertTrue(config.validate_proxy("http://u:p@127.0.0.1:1091"))
        self.assertFalse(config.validate_proxy("ftp://127.0.0.1:21"))
        self.assertFalse(config.validate_proxy("127.0.0.1:1090"))


class BotApiTests(unittest.TestCase):
    def setUp(self):
        base.reset_tree()
        self.tg = mocks.MockTelegram()
        self.conf = config.merge(config.DEFAULTS, {"token": mocks.TOKEN, "api_base": self.tg.url(), "proxy": "off"})
        self.state = store.JsonState(os.path.join(config.STATE, "bot.json"), {})
        self.waits = []
        self.bot = tg.Bot(lambda: self.conf, self.state, sleep=self.waits.append)

    def tearDown(self):
        self.tg.close()

    def test_429_retry_after(self):
        self.tg.fail["sendMessage"] = ["429"]
        r = self.bot.send(1, "hi")
        self.assertEqual(r["text"], "hi")
        self.assertIn(1, self.waits)                   # retry_after from the answer

    def test_502_and_cut_connection_retry(self):
        self.tg.fail["sendMessage"] = ["502", "cut"]
        self.assertEqual(self.bot.send(1, "x")["text"], "x")

    def test_style_refused_once_then_remembered(self):
        self.tg.reject_style = True
        mk = {"inline_keyboard": [[{"text": "a", "callback_data": "x", "style": "success"}]]}
        r = self.bot.send(1, "colours", mk)
        self.assertEqual(r["text"], "colours")
        self.assertTrue(self.state.get("styles_refused"))
        self.tg.clear()
        self.bot.send(1, "again", mk)
        self.assertNotIn("style", json.dumps(self.tg.last("sendMessage")["reply_markup"]))
        self.assertEqual(len(self.tg.all("sendMessage")), 1)     # no failed try first

    def test_style_kept_when_accepted(self):
        mk = {"inline_keyboard": [[{"text": "a", "callback_data": "x", "style": "danger"}]]}
        self.bot.send(1, "c", mk)
        self.assertEqual(self.tg.last("sendMessage")["reply_markup"]["inline_keyboard"][0][0]["style"], "danger")

    def test_other_400_is_not_a_style_problem(self):
        with self.assertRaises(tg.TelegramError):
            self.bot.call("editMessageText", {"chat_id": 1, "message_id": 999, "text": "x",
                                              "reply_markup": {"inline_keyboard": [[{"text": "a", "callback_data": "b", "style": "primary"}]]}})
        self.assertFalse(self.state.get("styles_refused"))

    def test_upload_streams_a_piece_of_a_file(self):
        path = os.path.join(ROOT, "big.bin")
        data = os.urandom(300000)
        with open(path, "wb") as f:
            f.write(data)
        seen = []
        r = self.bot.send_file("sendDocument", "document", 5, "x.part02", path=path, offset=100000, length=100000,
                               caption="<b>c</b>", progress=lambda s, t: seen.append((s, t)))
        self.assertEqual(r["document"]["file_name"], "x.part02")
        chat, name, got, cap = self.tg.documents[-1]
        self.assertEqual(got, data[100000:200000])
        self.assertEqual(cap, "<b>c</b>")
        self.assertEqual(seen[-1], (100000, 100000))

    def test_upload_cut_raises_for_the_caller(self):
        path = os.path.join(ROOT, "big2.bin")
        with open(path, "wb") as f:
            f.write(os.urandom(200000))
        self.tg.fail["sendDocument"] = ["cut"]
        with self.assertRaises((OSError, tg.NoRoute)):
            self.bot.send_file("sendDocument", "document", 5, "y", path=path)
        r = self.bot.send_file("sendDocument", "document", 5, "y", path=path)
        self.assertEqual(r["document"]["file_name"], "y")

    def test_token_never_logged(self):
        from erisbot.util import redact
        s = redact("POST https://api.telegram.org/bot%s/getMe failed; token=%s" % (mocks.TOKEN, mocks.TOKEN))
        self.assertNotIn(mocks.TOKEN, s)
        self.assertNotIn(mocks.TOKEN.split(":")[1], s)
        self.assertNotIn("abc", redact("https://panel.example.com/sub/abcdef123"))
        self.assertNotIn("hunter2", redact('{"password": "hunter2"}'))
        self.assertNotIn("pw", redact("socks5://user:pw@127.0.0.1:1080"))


class AutoRouteTests(unittest.TestCase):
    """proxy=auto: direct first, then proxy_list in order; the first that answers getMe is used."""

    def setUp(self):
        base.reset_tree()
        self.tg = mocks.MockTelegram()
        self.socks = mocks.MockProxy(hosts={"telegram.invalid": "127.0.0.1"})
        self.http = mocks.MockProxy(hosts={"telegram.invalid": "127.0.0.1"})

    def tearDown(self):
        self.tg.close()
        self.socks.close()
        self.http.close()

    def bot(self, conf):
        return tg.Bot(lambda: conf, None, sleep=lambda s: None)

    def test_auto_skips_dead_routes(self):
        dead = mocks.dead_port()
        conf = config.merge(config.DEFAULTS, {
            "token": mocks.TOKEN, "api_base": self.tg.url("telegram.invalid"), "proxy": "auto",
            "proxy_list": ["http://127.0.0.1:%d" % dead, "socks5://127.0.0.1:%d" % self.socks.port,
                           "http://127.0.0.1:%d" % self.http.port]})
        b = self.bot(conf)
        self.assertEqual(b.get_me()["username"], "eris_test_bot")
        self.assertEqual(b.router.current, "socks5://127.0.0.1:%d" % self.socks.port)
        self.assertEqual(self.http.seen, [])            # never needed

    def test_auto_prefers_direct(self):
        conf = config.merge(config.DEFAULTS, {"token": mocks.TOKEN, "api_base": self.tg.url(), "proxy": "auto",
                                              "proxy_list": ["socks5://127.0.0.1:%d" % self.socks.port]})
        b = self.bot(conf)
        b.get_me()
        self.assertIsNone(b.router.current)
        self.assertTrue(b.router.chosen)

    def test_switches_when_the_route_dies(self):
        conf = config.merge(config.DEFAULTS, {"token": mocks.TOKEN, "api_base": self.tg.url("telegram.invalid"), "proxy": "auto",
                                              "proxy_list": ["socks5://127.0.0.1:%d" % self.socks.port,
                                                             "http://127.0.0.1:%d" % self.http.port]})
        b = self.bot(conf)
        b.get_me()
        self.assertIn(str(self.socks.port), b.router.current)
        self.socks.close()
        time.sleep(0.1)
        b.local.conns = {}
        b.get_me()
        self.assertIn(str(self.http.port), b.router.current)

    def test_nothing_works(self):
        conf = config.merge(config.DEFAULTS, {"token": mocks.TOKEN, "api_base": "http://telegram.invalid:1",
                                              "proxy": "auto", "proxy_list": ["socks5://127.0.0.1:%d" % mocks.dead_port()]})
        with self.assertRaises(tg.NoRoute):
            self.bot(conf).call("getMe", attempts=2)


if __name__ == "__main__":
    unittest.main()
