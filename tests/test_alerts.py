"""Alerts: only on change, the 10-minute rule, mute, types, and every source."""
import base  # noqa: F401  (environment first)
import json
import os
import subprocess
import time
import unittest

from base import OWNER, VIEWER, BotCase
from erisbot import alerts, config, fand, system

T0 = 1_800_000_000


class EngineTests(unittest.TestCase):
    def setUp(self):
        base.reset_tree()
        self.e = alerts.Engine(os.path.join(config.STATE, "a.json"))

    def up(self, cond, t, events=()):
        return self.e.update(cond, events, now=T0 + t)

    def test_only_on_change(self):
        c = {"k": ("eris", 2, "hot")}
        self.assertEqual(self.up(c, 0), ["🔴 hot"])
        self.assertEqual(self.up(c, 60), [])
        self.assertEqual(self.up(c, 120), [])
        self.assertEqual(self.up({}, 700), ["✅ رفع شد: hot"])
        self.assertEqual(self.up({}, 760), [])

    def test_ten_minute_rule(self):
        c = {"k": ("node", 2, "node down", "✅ node back")}
        self.assertEqual(len(self.up(c, 0)), 1)
        self.assertEqual(self.up({}, 60), [])          # resolved after 1 minute: held back
        self.assertEqual(self.up(c, 120), [])          # and again: still the state that was reported
        self.assertEqual(self.up(c, 700), [])          # nothing changed since the message
        self.assertEqual(self.up({}, 800), ["✅ node back"])
        self.assertEqual(self.up(c, 900), [])          # back again within 10 minutes of the last message
        self.assertEqual(self.up(c, 1401), ["🔴 node down"])

    def test_events_once_per_ten_minutes(self):
        ev = [("reboot", "reboot:x", 1, "rebooted")]
        self.assertEqual(len(self.up({}, 0, ev)), 1)
        self.assertEqual(self.up({}, 300, ev), [])
        self.assertEqual(len(self.up({}, 700, ev)), 1)

    def test_mute_and_types(self):
        self.e.mute(T0 + 3600)
        self.assertEqual(self.up({"a": ("eris", 1, "x")}, 0), [])
        self.assertEqual(self.up({"a": ("eris", 1, "x")}, 3700), ["🟠 x"])     # after the silence
        self.e.set_enabled("user", False)
        self.assertEqual(self.up({"a": ("eris", 1, "x"), "u": ("user", 1, "user y")}, 3800), [])
        self.assertIn("u", self.e.active())                                   # still listed in the menu
        self.assertEqual(len(self.e.history()), 1)


class SourceTests(BotCase):
    marzban = True

    def round(self, now=None):
        return self.app.alert_round(now or time.time())

    def test_marzban_users_and_nodes(self):
        cond, ev = self.app.sources.c_marzban(time.time())
        self.assertIn("user_exp:bob", cond)               # 2 days left
        self.assertIn("user_data:bob", cond)              # 5 % left
        self.assertNotIn("user_exp:alice", cond)
        self.assertIn("node:tr-node", cond)
        self.mz.users["alice"]["status"] = "limited"
        self.app.sources.next.clear()
        cond, ev = self.app.sources.c_marzban(time.time())
        self.assertTrue(any("alice" in e[3] and "limited" in e[3] for e in ev), ev)

    def test_node_back_message(self):
        lines = self.round(T0)
        self.assertTrue(any("tr-node" in l for l in lines))
        self.mz.nodes[1]["status"] = "connected"
        self.app.sources.next.clear()
        lines = self.round(T0 + 700)
        self.assertIn("✅ نود <b>tr-node</b> دوباره وصل شد", lines)
        sent = [p for p in self.tg.all("sendMessage") if "هشدار" in p["text"]]
        self.assertEqual({int(p["chat_id"]) for p in sent}, {OWNER, VIEWER})

    def test_reboot_event(self):
        self.round(T0)
        base.build_proc(boot_id="99999999-0000-0000-0000-000000000000")
        lines = self.round(T0 + 60)
        self.assertTrue(any("ری‌استارت" in l for l in lines))

    def test_ssh_new_ip(self):
        out = {"text": "Accepted publickey for root from 192.0.2.10 port 5000 ssh2\n"}
        orig = system.sh
        system.sh = lambda cmd, timeout=30, input=None: subprocess.CompletedProcess(cmd, 0, out["text"], "") \
            if cmd[0] == "journalctl" else orig(cmd, timeout, input)
        try:
            _, ev = self.app.sources.c_ssh(T0)            # the first look only learns the old addresses
            self.assertEqual(ev, [])
            out["text"] = "Accepted publickey for root from 192.0.2.10 port 5001 ssh2\nAccepted password for eris from 198.51.100.7 port 6000 ssh2\n"
            self.app.sources.next.clear()
            _, ev = self.app.sources.c_ssh(T0 + 60)
            self.assertEqual(len(ev), 1)
            self.assertIn("198.51.100.7", ev[0][3])
        finally:
            system.sh = orig

    def test_certificates(self):
        for name, days in (("soon.example.com", 5), ("panel.example.com", 60)):
            d = os.path.join(config.CERTS, name)
            os.makedirs(d, exist_ok=True)
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", d + "/privkey.pem",
                            "-out", d + "/fullchain.pem", "-days", str(days), "-subj", "/CN=" + name], check=True, capture_output=True)
        cond, _ = self.app.sources.c_certs(time.time())
        self.assertEqual(list(cond), ["cert:soon.example.com"])

    def ip_rounds(self, ips):
        """c_ip() once per agreed IP (None = the services did not agree); the events of each round."""
        seq = iter(ips)
        orig = alerts.consensus_ip
        alerts.consensus_ip = lambda urls, ask=None: (next(seq), [])
        try:
            out = []
            for i in range(len(ips)):
                self.app.sources.next.clear()
                out.append(self.app.sources.c_ip(T0 + i))
            return out
        finally:
            alerts.consensus_ip = orig

    def test_public_ip_change_needs_two_rounds(self):
        r = self.ip_rounds(["192.0.2.10", "192.0.2.10", "198.51.100.20", "198.51.100.20"])
        self.assertEqual([e for _, e in r[:3]], [[], [], []])           # one odd answer is not a change
        self.assertIn("198.51.100.20", r[3][1][0][3])

    def test_public_ip_flapping_is_quiet(self):
        r = self.ip_rounds(["192.0.2.10", "198.51.100.20", "192.0.2.10", "198.51.100.21", None, "192.0.2.10"])
        self.assertEqual([e for _, e in r], [[]] * 6)

    def test_public_ip_static(self):
        self.app.save_conf(public_ip={"static": "192.0.2.10"})
        r = self.ip_rounds(["192.0.2.10", "198.51.100.20", "198.51.100.20", "198.51.100.20", "192.0.2.10"])
        self.assertEqual(r[0], ({}, []))
        self.assertEqual(r[1][0], {})                                    # first odd round: nothing yet
        self.assertIn("ip_static", r[2][0])
        self.assertIn("198.51.100.20", r[2][0]["ip_static"][2])
        self.assertIn("ip_static", r[3][0])
        self.assertEqual(r[4][0], {})                                    # back to the static IP: resolved

    def test_consensus(self):
        answers = {"a": "192.0.2.1", "b": "192.0.2.1", "c": "203.0.113.9"}
        self.assertEqual(alerts.consensus_ip(list(answers), ask=answers.get)[0], "192.0.2.1")
        split = {"a": "192.0.2.1", "b": "203.0.113.9", "c": None}
        self.assertIsNone(alerts.consensus_ip(list(split), ask=split.get)[0])
        self.assertEqual(alerts.consensus_ip(["a"], ask=answers.get)[0], "192.0.2.1")
        self.assertNotIn("https://ifconfig.me/ip", alerts.public_ip_urls({"public_ip_urls": ["https://ifconfig.me/ip", "x"]}))

    def test_part_backup_failed(self):
        with open(os.path.join(os.environ["FAKE_ERIS_DIR"], "failed.json"), "w") as f:
            json.dump({"x-ui": "disk full"}, f)
        cond, _ = self.app.sources.c_backup(time.time())
        self.assertIn("backup:x-ui", cond)
        self.assertIn("disk full", cond["backup:x-ui"][2])

    def test_fan_alerts_and_dead_fan_service(self):
        cfg = fand.load_cfg()
        cfg["fans"]["nouveau@0000:01:00.0/pwm1"] = {"mode": "manual", "percent": 40}
        fand.save_cfg(cfg)
        cond, _ = self.app.sources.c_fans(time.time())
        self.assertIn("fan_service", cond)                # manual fans but no eris-fan status
        fand.write_status({"ts": time.time(), "fans": {}, "alerts": [{"key": "fan_hot:x", "level": 2, "text": "hot"}]})
        cond, _ = self.app.sources.c_fans(time.time())
        self.assertEqual(list(cond), ["fan_hot:x"])

    def test_alerts_screen(self):
        self.round(T0)
        self.say("/menu")
        text, _, _ = self.press("🔔 هشدارها")
        self.assertIn("tr-node", text)
        self.press("۱ ساعت")
        self.assertGreater(self.app.alerts.st.get("mute_until"), time.time() + 3000)
        self.press("انواع هشدار")
        self.press("نودها")
        self.assertFalse(self.app.alerts.enabled("node"))


if __name__ == "__main__":
    unittest.main()


