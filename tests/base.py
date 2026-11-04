"""Shared test setup: one fake server tree (ERISBOT_ROOT), a fake /sys and /proc, the mocks.

Imported first by every test module, so the environment is set before erisbot is imported.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
ROOT = tempfile.mkdtemp(prefix="erisbot-test-")
os.environ["ERISBOT_ROOT"] = ROOT
os.environ["ERISBOT_SYS"] = ROOT + "/sys"
os.environ["ERISBOT_PROC"] = ROOT + "/proc"
os.environ["ERISBOT_ERIS"] = os.path.join(HERE, "fake_eris.py")
os.environ["ERISBOT_ERIS_LIB"] = ROOT + "/no-eris-lib"
os.environ["FAKE_ERIS_DIR"] = ROOT + "/fake-eris"
os.environ["ERISBOT_SENSORS_DETECT"] = ROOT + "/sensors-detect"
os.chmod(os.path.join(HERE, "fake_eris.py"), 0o755)
sys.path.insert(0, SRC)
sys.path.insert(0, HERE)

from erisbot import config  # noqa: E402
import mocks  # noqa: E402

OWNER, VIEWER, STRANGER = 1001, 1002, 666


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(text))


def build_proc(boot_id="11111111-2222-3333-4444-555555555555"):
    p = ROOT + "/proc"
    put(p + "/uptime", "93784.12 300000.00\n")
    put(p + "/stat", "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 50 0 50 400 0 0 0 0 0 0\n")
    put(p + "/loadavg", "0.52 0.40 0.31 1/200 1234\n")
    put(p + "/meminfo", "MemTotal:  4000000 kB\nMemFree:  1000000 kB\nMemAvailable:  2000000 kB\nSwapTotal: 1000000 kB\nSwapFree: 1000000 kB\n")
    put(p + "/sys/kernel/random/boot_id", boot_id + "\n")
    put(p + "/sys/kernel/hostname", "testhost\n")
    put(p + "/sys/vm/swappiness", "60\n")
    put(p + "/mounts", "/dev/sda1 / ext4 rw 0 0\nproc /proc proc rw 0 0\n")
    put(p + "/cpuinfo", "processor\t: 0\ncpu MHz\t\t: 2826.000\nprocessor\t: 1\ncpu MHz\t\t: 2826.000\n")


def build_sys(cpufreq=False, gpu_fan=True, cpu_fan=False, ncpu=2):
    s = ROOT + "/sys"
    shutil.rmtree(s, ignore_errors=True)
    hw = s + "/class/hwmon"
    put(hw + "/hwmon0/name", "acpitz\n")
    put(hw + "/hwmon0/temp1_input", "40000\n")
    put(hw + "/hwmon1/name", "coretemp\n")
    for i in range(ncpu):
        put(hw + "/hwmon1/temp%d_input" % (i + 2), "50000\n")
        put(hw + "/hwmon1/temp%d_label" % (i + 2), "Core %d\n" % i)
    put(hw + "/hwmon2/name", "drivetemp\n")
    put(hw + "/hwmon2/temp1_input", "36000\n")
    os.makedirs(hw + "/hwmon2/device/block/sda", exist_ok=True)
    if gpu_fan:
        dev = s + "/devices/pci0000:00/0000:01:00.0"
        os.makedirs(dev, exist_ok=True)
        put(hw + "/hwmon3/name", "nouveau\n")
        os.symlink(dev, hw + "/hwmon3/device")
        for f, v in (("pwm1", 40), ("pwm1_enable", 2), ("pwm1_min", 0), ("pwm1_max", 100), ("fan1_input", 1500),
                     ("temp1_input", 45000)):
            put(hw + "/hwmon3/" + f, "%s\n" % v)
    if cpu_fan:
        put(hw + "/hwmon4/name", "it87\n")
        for f, v in (("pwm1", 128), ("pwm1_enable", 2), ("fan1_input", 2000)):
            put(hw + "/hwmon4/" + f, "%s\n" % v)
    for i in range(ncpu):
        c = s + "/devices/system/cpu/cpu%d" % i
        put(c + "/topology/core_id", "%d\n" % i)
        put(c + "/thermal_throttle/core_throttle_count", "0\n")
        if cpufreq:
            put(c + "/cpufreq/scaling_governor", "ondemand\n")
            put(c + "/cpufreq/scaling_available_governors", "conservative ondemand userspace powersave performance\n")
            put(c + "/cpufreq/scaling_min_freq", "2000000\n")
            put(c + "/cpufreq/scaling_max_freq", "2833000\n")
            put(c + "/cpufreq/cpuinfo_min_freq", "2000000\n")
            put(c + "/cpufreq/cpuinfo_max_freq", "2833000\n")
            put(c + "/cpufreq/scaling_cur_freq", "2000000\n")


def set_temp(path_rel, celsius):
    put(ROOT + "/sys/class/hwmon/" + path_rel, "%d\n" % int(celsius * 1000))


def reset_tree():
    for d in ("/etc", "/var", "/run", "/opt", "/fake-eris", "/usr"):
        shutil.rmtree(ROOT + d, ignore_errors=True)
    for d in (config.ETC, config.STATE, config.RUN, config.TMP, os.environ["FAKE_ERIS_DIR"]):
        os.makedirs(d, exist_ok=True)
    build_proc()
    build_sys()
    config.ERIS_BIN = os.environ["ERISBOT_ERIS"]


def write_config(**over):
    c = {"token": mocks.TOKEN, "allowed_chat_ids": [OWNER, VIEWER], "roles": {str(VIEWER): "viewer"},
         "api_base": "", "proxy": "off", "proxy_list": []}
    c.update(over)
    config.write_json(config.CONFIG, c, 0o600)


class BotCase(unittest.TestCase):
    """A fresh App talking to a MockTelegram (no threads: updates are handled one by one)."""

    marzban = False

    def setUp(self):
        reset_tree()
        self.tg = mocks.MockTelegram()
        write_config(api_base=self.tg.url())
        self.mz = None
        if self.marzban:
            self.mz = mocks.MockMarzban(ROOT, tls=True)
            put(config.MARZBAN_ENV, self.mz.env_text())
        from erisbot.app import App
        self.app = App(sleep=lambda s: None, workers=0)

    def tearDown(self):
        self.app.bot.close()
        self.tg.close()
        if self.mz:
            self.mz.close()

    def poll(self):
        return self.app.poll_once(timeout=0, sync=True)

    def say(self, text, chat=OWNER):
        self.tg.text(chat, text)
        self.poll()
        return self.tg.sent[-1] if self.tg.sent else None

    def last_screen(self, chat=OWNER):
        """(text, markup, message id) of the newest sendMessage/editMessageText to this chat."""
        for m, p in reversed(self.tg.sent):
            if m in ("sendMessage", "editMessageText") and int(p["chat_id"]) == chat and "reply_markup" in p \
                    and "inline_keyboard" in p["reply_markup"]:
                mid = p.get("message_id") or self.find_mid(chat, p["text"])
                return p["text"], p["reply_markup"], mid
        return None, None, None

    def find_mid(self, chat, text):
        for (c, mid), t in sorted(self.tg.messages.items(), key=lambda kv: -kv[0][1]):
            if c == chat and t == text:
                return mid
        return None

    def press(self, needle, chat=OWNER):
        text, markup, mid = self.last_screen(chat)
        data = mocks.find_button(markup, needle)
        self.tg.press(chat, mid, data)
        self.poll()
        return self.last_screen(chat)

    def press_data(self, data, chat=OWNER, mid=None):
        self.tg.press(chat, mid or 1, data)
        self.poll()

    def answers(self):
        return [a.get("text") for a in self.tg.answers]

    def json(self, obj):
        return json.dumps(obj, ensure_ascii=False)
