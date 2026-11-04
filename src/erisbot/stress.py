"""Stability test: every core busy with a Python process for 10/30/60 minutes, to check a BIOS overclock
and a weak power supply. Stops by itself when the CPU reaches 85 °C."""
import os
import signal
import subprocess
import sys
import threading
import time

from . import config, hw, system
from .util import duration, fa, spark

STATE = os.path.join(config.STATE, "stress.json")
LIMIT = 85.0


class StressTest:
    def __init__(self, minutes, on_update=None, on_done=None, temp_fn=None, rpm_fn=None, workers=None,
                 seconds_per_minute=60, sample_every=30, check_every=2.0, limit=LIMIT, meta=None):
        self.minutes = minutes
        self.on_update, self.on_done = on_update, on_done
        self.temp_fn = temp_fn or system.cpu_temp
        self.rpm_fn = rpm_fn or (lambda: {f["label"]: hw.rpm(f) for f in hw.fans()})
        self.workers = workers or os.cpu_count() or 1
        self.spm, self.sample_every, self.check_every, self.limit = seconds_per_minute, sample_every, check_every, limit
        self.procs = []
        self.samples = []            # (elapsed s, temp, {fan: rpm}, MHz)
        self.max_temp = None
        self.reason = None
        self.stop_evt = threading.Event()
        self.thread = None
        self.started = None
        self.throttle0 = None
        self.meta = meta or {}
        self.boot = system.boot_id()

    def start(self):
        self.started = time.time()
        self.throttle0 = hw.throttle_count()
        for _ in range(self.workers):
            self.procs.append(subprocess.Popen([sys.executable, "-c", "while True: pass"],
                                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL, start_new_session=True))
        config.write_json(STATE, dict(self.meta, running=True, start=self.started, minutes=self.minutes,
                                      boot_id=self.boot, pids=[p.pid for p in self.procs]), 0o600)
        self.thread = threading.Thread(target=self._run, name="stress", daemon=True)
        self.thread.start()
        return self

    def stop(self, reason="به دستور شما متوقف شد"):
        if not self.reason:
            self.reason = reason
        self.stop_evt.set()

    def _sample(self):
        t = self.temp_fn()
        if t is not None:
            self.max_temp = t if self.max_temp is None else max(self.max_temp, t)
        return t

    def _run(self):
        total = self.minutes * self.spm
        next_sample = 0.0
        try:
            while not self.stop_evt.is_set():
                el = time.time() - self.started
                t = self._sample()
                if t is not None and t >= self.limit:
                    self.reason = "دمای CPU به %s°C رسید؛ تست خودکار قطع شد" % fa("%.0f" % t)
                    break
                if el >= next_sample:
                    self.samples.append((el, t, self.rpm_fn(), hw.cur_mhz()))
                    next_sample += self.sample_every
                    if self.on_update:
                        try:
                            self.on_update(self)
                        except Exception:  # a failed message edit must not stop the test
                            pass
                if el >= total:
                    break
                self.stop_evt.wait(self.check_every)
        finally:
            self.kill()
            if not self.reason:
                self.reason = "تمام شد"
            config.write_json(STATE, dict(self.meta, running=False, end=time.time(), boot_id=self.boot), 0o600)
            if self.on_done:
                self.on_done(self)

    def kill(self):
        for p in self.procs:
            try:
                p.kill()
            except OSError:
                pass
        for p in self.procs:
            try:
                p.wait(5)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def progress_text(self):
        el = (self.samples[-1][0] if self.samples else 0)
        total = self.minutes * self.spm
        temps = [s[1] for s in self.samples]
        lines = ["🔥 <b>تست پایداری</b> — %s دقیقه، %s پروسس" % (fa(self.minutes), fa(self.workers)),
                 "⏱ %s از %s" % (duration(el * 60 / self.spm, 2), duration(total * 60 / self.spm, 2))]
        last = self.samples[-1] if self.samples else None
        if last:
            lines.append("🌡 CPU: %s°C (بیشترین %s°C)" % (fa("%.0f" % last[1]) if last[1] is not None else "؟",
                                                       fa("%.0f" % self.max_temp) if self.max_temp is not None else "؟"))
            for name, r in (last[2] or {}).items():
                lines.append("🌀 %s: %s RPM" % (name, r if r is not None else "؟"))
            if last[3]:
                lines.append("⚙️ فرکانس: %d MHz" % last[3])
        if temps:
            lines.append("<code>%s</code>  دما (هر %s ثانیه)" % (spark(temps, 30, 90), fa(self.sample_every)))
        return "\n".join(lines)

    def result_text(self):
        thr = hw.throttle_count()
        dthr = (thr - self.throttle0) if (thr is not None and self.throttle0 is not None) else None
        lines = ["🔥 <b>نتیجه‌ی تست پایداری</b>", "وضعیت: %s" % self.reason,
                 "⏱ مدت: %s" % duration(time.time() - self.started, 2),
                 "🌡 بیشترین دمای CPU: %s" % ("%s°C" % fa("%.0f" % self.max_temp) if self.max_temp is not None else "؟"),
                 "🐢 throttle: %s" % ("%s بار" % fa(dthr) if dthr is not None else "شمارنده‌ای نیست"),
                 "✅ سرور ری‌استارت نشد" if system.boot_id() == self.boot else "⚠️ سرور ری‌استارت شد!"]
        temps = [s[1] for s in self.samples]
        if temps:
            lines.append("<code>%s</code>" % spark(temps, 30, 90))
        if self.max_temp is not None and self.max_temp >= 80:
            lines.append("⚠️ دما زیاد است؛ خنک‌کاری یا اورکلاک BIOS را بررسی کنید.")
        return "\n".join(lines)


def leftover():
    """A test that was running when eris-bot (or the server) stopped: (state, rebooted) or None."""
    st = config.read_json(STATE, None)
    if not st or not st.get("running"):
        return None
    rebooted = st.get("boot_id") != system.boot_id()
    if not rebooted:
        for pid in st.get("pids") or []:
            cmd = (system.read_raw("/proc/%d/cmdline" % int(pid)) or b"").replace(b"\0", b" ")
            if b"while True: pass" not in cmd:       # the pid is somebody else's by now
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    st["running"] = False
    config.write_json(STATE, st, 0o600)
    return st, rebooted
