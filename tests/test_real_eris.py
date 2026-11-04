"""The bot with the real ERIS Console 2.5 (`eris backup ... --json`, encrypt, alerts()), on a fake server tree.

Runs when ERIS_CONSOLE_SRC points at eris-console/src (CI checks it out); skipped otherwise.
"""
import base  # noqa: F401  (environment first)
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from base import OWNER, BotCase
from erisbot import config, eris, s_main

SRC = os.environ.get("ERIS_CONSOLE_SRC")


@unittest.skipUnless(SRC and os.path.isfile(os.path.join(SRC or "", "erisbackup.py")) and os.geteuid() == 0,
                     "set ERIS_CONSOLE_SRC=<eris-console>/src and run as root")
class RealErisTests(BotCase):
    def setUp(self):
        BotCase.setUp(self)
        self.root = tempfile.mkdtemp(prefix="eris-root-")
        self.bdir = os.path.join(self.root, "srv", "backups")
        os.makedirs(self.bdir)
        os.makedirs(self.root + "/var/tmp")
        for p, rows in (("/var/lib/marzban/db.sqlite3", 3), ("/etc/x-ui/x-ui.db", 2)):
            os.makedirs(os.path.dirname(self.root + p), exist_ok=True)
            db = sqlite3.connect(self.root + p)
            db.execute("create table t (v)")
            db.executemany("insert into t values (?)", [(i,) for i in range(rows)])
            db.commit()
            db.close()
        os.makedirs(self.root + "/opt/marzban")
        with open(self.root + "/opt/marzban/.env", "w") as f:
            f.write('UVICORN_PORT="8000"\n')
        os.makedirs(self.root + "/etc/eris")
        with open(self.root + "/etc/eris/eris.conf", "w") as f:
            f.write('BACKUP_DIR="%s"\nBACKUP_PARTS="marzban,x-ui,eris"\n' % self.bdir)
        wrapper = os.path.join(self.root, "eris")
        with open(wrapper, "w") as f:
            f.write("#!/bin/sh\nERIS_ROOT=%s ERIS_LIB=%s exec %s %s/eris \"$@\"\n" % (self.root, SRC, sys.executable, SRC))
        os.chmod(wrapper, 0o755)
        config.ERIS_BIN = wrapper
        self.pw = self.app.sender.password_file()
        s_main.write_password(self.pw, "integration password 1")
        self.app.sender.sleep = lambda s: None

    def test_status_list_and_send_with_real_eris(self):
        st = eris.backup_status()
        self.assertTrue(st["parts"]["marzban"]["present"])
        self.assertFalse(st["parts"]["jellyfin"]["present"])
        self.app.sender.submit(["marzban", "x-ui", "jellyfin"], chats=[OWNER])
        self.app.sender.process(self.app.sender.q.get_nowait())
        docs = [d for d in self.tg.documents if d[0] == OWNER]
        self.assertEqual(len(docs), 2)
        groups = eris.backup_list()["groups"]
        for chat, name, data, cap in docs:
            part = "marzban" if "-marzban-" in name else "x-ui"
            arch = groups[part][0]
            self.assertEqual(name, arch["name"] + ".enc")
            self.assertIn(arch["sha256"], cap)
            self.assertIn("دیتابیس: ۱", cap)
            out = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
                                  "-pass", "file:" + self.pw], input=data, capture_output=True, check=True).stdout
            with open(arch["path"], "rb") as f:
                self.assertEqual(out, f.read())
        self.assertFalse(os.path.exists(os.path.join(self.bdir, "full")))      # only the parts were made
        self.assertTrue(self.app.sender.results()["x-ui"]["ok"])
        # the backup menu reads the same status
        self.say("/menu")
        text, _, _ = self.press("💾 بکاپ")
        self.assertIn("✅ <b>marzban</b>", text)
        self.assertIn("⏭ <b>jellyfin</b>", text)

    def test_failed_part_alert(self):
        os.rename(self.bdir, self.bdir + ".gone")                                # the flash disk is gone
        eris.run(["backup", "now", "--part", "x-ui", "--json"])
        cond, _ = self.app.sources.c_backup(0)
        self.assertIn("backup:x-ui", cond)

    def test_dashboard_alerts_from_erisd(self):
        config.ERIS_LIB = SRC
        a = eris.Alerts()
        res = a.current()
        self.assertIsInstance(res, list)
        self.assertIsNot(a.mod, False, a.error)

    def test_firewall_not_set_up(self):
        text = eris.fw_status()
        self.assertFalse(eris.fw_set_up(text), text)

    def tearDown(self):
        config.ERIS_LIB = os.environ["ERISBOT_ERIS_LIB"]
        BotCase.tearDown(self)


if __name__ == "__main__":
    unittest.main()
