"""Sending part backups: encryption round trip, pieces, retries, going on after a stop, the schedule."""
import base  # noqa: F401  (environment first)
import hashlib
import json
import os
import subprocess
import time
import unittest

from base import OWNER, BotCase
from erisbot import backup, config, s_main

FAKE = os.environ["FAKE_ERIS_DIR"]
PW = "correct horse battery staple"


class Crash(BaseException):
    """Stands for a power cut / kill -9 in the middle of an upload."""


def slurp(path):
    with open(path) as f:
        return f.read()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def decrypt(data, pw_file):
    p = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-pass", "file:" + pw_file],
                       input=data, capture_output=True, check=True)
    return p.stdout


class SendTests(BotCase):
    def setUp(self):
        BotCase.setUp(self)
        os.environ["FAKE_SIZE"] = "4096"
        self.pw = self.app.sender.password_file()
        s_main.write_password(self.pw, PW)
        self.app.sender.sleep = lambda s: self.waits.append(s)
        self.waits = []

    def run_job(self, parts, **kw):
        s = self.app.sender
        s.submit(parts, chats=[OWNER], **kw)
        job = s.q.get_nowait()
        s.process(job)
        return job

    def docs(self):
        return [d for d in self.tg.documents if d[0] == OWNER]

    def original(self, part):
        d = os.path.join(FAKE, "backups", part)
        newest = max((os.path.join(d, n) for n in os.listdir(d) if n.endswith(".tar")), key=os.path.getmtime)
        with open(newest, "rb") as f:
            return newest, f.read()

    def test_no_password_no_send(self):
        os.remove(self.pw)
        self.run_job(["x-ui"])
        self.assertEqual(self.docs(), [])
        self.assertTrue(any("رمز بکاپ" in t for t in self.tg.messages.values()))
        self.assertFalse(self.app.sender.results()["x-ui"]["ok"])

    def test_make_encrypt_send_round_trip(self):
        self.run_job(["marzban", "x-ui", "jellyfin"])
        summary = [p["text"] for p in self.tg.all("sendMessage") if "بکاپ‌های" in p["text"]][0]
        self.assertIn("✅ marzban", summary)
        self.assertIn("⏭ jellyfin", summary)
        docs = self.docs()
        self.assertEqual([d[1].split("-backup-")[0] for d in docs], ["host-marzban", "host-x-ui"])
        for chat, name, data, cap in docs:
            part = "marzban" if "marzban" in name else "x-ui"
            path, orig = self.original(part)
            self.assertTrue(name.endswith(".tar.enc"))
            self.assertTrue(data.startswith(b"Salted__"))
            self.assertEqual(decrypt(data, self.pw), orig)
            self.assertIn(sha(orig), cap)                              # sha256 of the original archive
            self.assertIn("openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000", cap)
            self.assertIn("فایل‌ها: ۱۲", cap)
        self.assertEqual(os.listdir(config.TMP), [])                   # the .enc files are gone
        self.assertTrue(self.app.sender.results()["x-ui"]["ok"])

    def test_big_file_in_pieces(self):
        os.environ["FAKE_SIZE"] = str(int(2.5 * 1024 * 1024))
        self.app.save_conf(part_size_mb=1)
        self.run_job(["x-ui"])
        docs = self.docs()
        names = [d[1] for d in docs]
        self.assertEqual([n.rsplit(".", 1)[1] for n in names], ["part01", "part02", "part03"])
        self.assertIn("cat ", docs[0][3])
        self.assertIn(".part*", docs[0][3])
        self.assertIn("تکه ۱ از ۳", docs[0][3])
        self.assertLessEqual(max(len(d[2]) for d in docs), 1024 * 1024)
        _, orig = self.original("x-ui")
        self.assertEqual(decrypt(b"".join(d[2] for d in docs), self.pw), orig)

    def test_retry_after_a_cut_upload(self):
        os.environ["FAKE_SIZE"] = str(2 * 1024 * 1024 + 10)
        self.app.save_conf(part_size_mb=1)
        self.tg.fail["sendDocument"] = ["cut", "502"]
        self.run_job(["eris"])
        docs = self.docs()
        self.assertEqual(len(docs), 3)
        self.assertGreaterEqual(len(self.waits), 1)                    # backoff before trying again
        _, orig = self.original("eris")
        self.assertEqual(decrypt(b"".join(d[2] for d in docs), self.pw), orig)

    def test_goes_on_from_the_next_piece_after_a_stop(self):
        os.environ["FAKE_SIZE"] = str(int(2.5 * 1024 * 1024))
        self.app.save_conf(part_size_mb=1)
        s = self.app.sender
        real = s._send_piece
        calls = []

        def flaky(job, cur, i, n, *a):
            calls.append(i)
            if i == 1:
                raise Crash()
            return real(job, cur, i, n, *a)
        s._send_piece = flaky
        s.submit(["system"], chats=[OWNER])
        job = s.q.get_nowait()
        with self.assertRaises(Crash):
            s.process(job)
        self.assertEqual(len(self.docs()), 1)
        saved = s.st.get("job")
        self.assertEqual(saved["cur"]["sent"], [0])
        # eris-bot starts again: a new Sender finds the job and goes on with piece 2
        fresh = backup.Sender(self.app)
        fresh.sleep = lambda x: None
        job = fresh.st.get("job")
        fresh.process(job)
        docs = self.docs()
        self.assertEqual([d[1].rsplit(".", 1)[1] for d in docs], ["part01", "part02", "part03"])
        _, orig = self.original("system")
        self.assertEqual(decrypt(b"".join(d[2] for d in docs), self.pw), orig)
        self.assertIsNone(fresh.st.get("job"))

    def test_full_backup_is_never_sent(self):
        self.app.sender.submit(["full", "x-ui"], chats=[OWNER])
        job = self.app.sender.q.get_nowait()
        self.assertEqual(job["parts"], ["x-ui"])
        full = os.path.join(FAKE, "backups", "full")
        os.makedirs(full)
        with open(os.path.join(full, "host-backup-1.tar"), "w") as f:
            f.write("x")
        res = self.app.sender.existing({"parts": ["x-ui"], "files": {"x-ui": os.path.join(full, "host-backup-1.tar")}})
        self.assertFalse(res["x-ui"]["ok"])

    def test_after_the_nightly_backup(self):
        s = self.app.sender
        self.assertIsNone(s.tick())                     # the first look only remembers where we are
        time.sleep(1.1)                                 # the nightly backup comes later
        subprocess.run([config.ERIS_BIN, "backup", "now", "--part", "marzban,x-ui"], capture_output=True)
        with open(os.path.join(FAKE, "failed.json"), "w") as f:
            json.dump({"eris": "disk full"}, f)
        later = time.time() + 120
        job_id = s.tick(later)
        self.assertIsNotNone(job_id)
        job = s.q.get_nowait()
        self.assertFalse(job["make"])
        self.assertEqual(sorted(job["parts"]), ["eris", "marzban", "x-ui"])
        s.process(job)
        summary = [p["text"] for p in self.tg.all("sendMessage") if "بکاپ‌های شبانه" in p["text"]][-1]
        self.assertIn("❌ eris — disk full", summary)
        self.assertEqual(len([d for d in self.tg.documents]), 2 * 2)   # two parts, to both chats
        self.assertIsNone(s.tick(later + 60))           # nothing new

    def test_at_a_set_time(self):
        self.app.save_conf(backup_send={"mode": "at", "at": "04:30", "parts": ["x-ui"]})
        lt = time.localtime()
        at = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 4, 31, 0, 0, 0, -1))
        self.assertIsNotNone(self.app.sender.tick(at))
        self.assertIsNone(self.app.sender.tick(at + 60))   # once a day
        job = self.app.sender.q.get_nowait()
        self.assertTrue(job["make"])
        self.assertEqual(job["parts"], ["x-ui"])

    def test_menu_buttons(self):
        self.say("/menu")
        text, _, _ = self.press("💾 بکاپ")
        self.assertIn("بکاپ کلی (فقط روی فلش، هرگز ارسال نمی‌شود)", text)
        self.press("بکاپ و ارسال همین الان")
        job = self.app.sender.q.get_nowait()
        self.assertEqual(job["parts"], ["marzban", "x-ui", "xray-site", "eris", "system"])

    def test_restore_from_the_flash_needs_two_confirmations(self):
        subprocess.run([config.ERIS_BIN, "backup", "now", "--part", "x-ui"], capture_output=True)
        self.say("/menu")
        self.press("💾 بکاپ")
        self.press("بکاپ‌های روی فلش")
        self.press("x-ui")
        self.press("📄")
        self.press("بازگردانی")
        self.press("بله")
        log = slurp(os.path.join(FAKE, "calls.log"))
        self.assertNotIn("restore", log)
        text, _, _ = self.press("بله")
        log = slurp(os.path.join(FAKE, "calls.log"))
        self.assertIn("restore --part x-ui %s/backups/x-ui/" % FAKE, log)
        self.assertIn("--yes", log)

    def test_resend_an_old_copy(self):
        subprocess.run([config.ERIS_BIN, "backup", "now", "--part", "x-ui"], capture_output=True)
        self.say("/menu")
        self.press("💾 بکاپ")
        self.press("بکاپ‌های روی فلش")
        self.press("x-ui")
        self.press("📄")
        self.press("ارسال این نسخه")
        job = self.app.sender.q.get_nowait()
        self.assertFalse(job["make"])
        self.assertTrue(job["files"]["x-ui"].startswith(FAKE))


if __name__ == "__main__":
    unittest.main()


