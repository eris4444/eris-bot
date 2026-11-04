"""Fan control (eris-fan.service): every 2 seconds read the temperatures and set each pwm fan.

Modes of a fan (fans.json): auto (the BIOS / driver, as before), manual (a percent, at least 25 %)
and curve (temperature -> percent with 3 °C hysteresis and gradual steps).
Protection that no setting can turn off: CPU >= 75 °C or GPU >= 80 °C puts that fan at 100 % at once.
On stop (or a crash: ExecStopPost=eris-bot fan-restore) every fan gets its original pwm_enable back.
"""
import os
import signal
import socket
import time

from . import config, hw
from .util import log, read, write

MIN_PCT = 25
HYST = 3.0
STEP_UP, STEP_DOWN = 10.0, 5.0
CPU_LIMIT, GPU_LIMIT = 75.0, 80.0
STALL_PCT = 30.0
INTERVAL = 2.0
ORIG = os.path.join(config.STATE, "fan-orig.json")
STATUS = os.path.join(config.RUN, "fans-status.json")

CURVES = {
    "quiet": [[45, 25], [55, 35], [65, 55], [72, 100]],
    "normal": [[40, 30], [50, 45], [60, 65], [70, 100]],
    "steep": [[35, 40], [45, 60], [55, 80], [65, 100]],
}
CURVE_TITLES = {"quiet": "منحنی آرام", "normal": "منحنی معمولی", "steep": "منحنی تند"}


def curve_value(points, temp):
    pts = sorted((float(t), float(p)) for t, p in points)
    if not pts:
        return 100.0
    if temp <= pts[0][0]:
        return pts[0][1]
    for (t1, p1), (t2, p2) in zip(pts, pts[1:]):
        if temp <= t2:
            return p1 + (p2 - p1) * (temp - t1) / (t2 - t1) if t2 > t1 else p2
    return pts[-1][1]


def load_cfg():
    c = config.read_json(config.FANS, {}) or {}
    if not isinstance(c.get("fans"), dict):
        c["fans"] = {}
    return c


def save_cfg(c):
    config.write_json(config.FANS, c, 0o600)


def fan_cfg(cfg, fan_id):
    f = (cfg.get("fans") or {}).get(fan_id) or {}
    mode = f.get("mode", "auto")
    if mode not in ("auto", "manual", "curve"):
        mode = "auto"
    return {"mode": mode, "percent": float(f.get("percent", 50)), "curve": f.get("curve") or CURVES["normal"],
            "curve_name": f.get("curve_name")}


def describe_cfg(fc):
    if fc["mode"] == "auto":
        return "🔁 خودکار"
    if fc["mode"] == "manual":
        return "🎚 دستی %d٪" % fc["percent"]
    return "📈 " + CURVE_TITLES.get(fc.get("curve_name") or "", "منحنی سفارشی")


class FanState:
    def __init__(self):
        self.pct = None          # what we set last (None = not ours / auto)
        self.taken = False       # pwm_enable was changed by us
        self.protect = False
        self.stall = 0


class Controller:
    def __init__(self, temp_fns=None):
        self.temp_fns = temp_fns or {"cpu": hw.system.cpu_temp, "gpu": None}
        self.fans = hw.fans()
        self.states = {}
        self.cfg = load_cfg()
        self.cfg_mtime = self._mtime()
        self.orig = config.read_json(ORIG, {}) or {}
        self.last_scan = time.monotonic()
        self.record_orig()

    @staticmethod
    def _mtime():
        try:
            return os.stat(config.FANS).st_mtime_ns
        except OSError:
            return None

    def record_orig(self):
        """The pwm_enable / pwm each fan had before eris-bot touched it (kept for good)."""
        changed = False
        for f in self.fans:
            if f["id"] in self.orig:
                continue
            en = read(f["enable"]) if f["enable"] else None
            if en == "1":            # already manual: probably left by us after a hard crash; auto is "2"
                en = "2"
            self.orig[f["id"]] = {"enable": en, "pwm": read(f["pwm"]), "enable_path": f["enable"], "pwm_path": f["pwm"]}
            changed = True
        if changed:
            config.write_json(ORIG, self.orig, 0o600)

    def temp_for(self, fan):
        if fan["kind"] == "gpu":
            fn = self.temp_fns.get("gpu")
            return fn(fan) if fn else hw.fan_temp(fan)
        return self.temp_fns["cpu"]()

    def target(self, fan, st, temp, fc):
        """Percent to set, or None for the BIOS/driver (auto)."""
        limit = CPU_LIMIT if fan["kind"] == "cpu" else GPU_LIMIT
        if temp is not None:
            if temp >= limit:
                st.protect = True
            elif st.protect and temp <= limit - HYST:
                st.protect = False
        if st.protect:
            return 100.0
        if fc["mode"] == "auto":
            return None
        if fc["mode"] == "manual":
            return max(MIN_PCT, min(100.0, fc["percent"]))
        if temp is None:              # no sensor: leave it to the BIOS
            return None
        up = curve_value(fc["curve"], temp)
        down = curve_value(fc["curve"], temp + HYST)
        cur = st.pct if st.pct is not None else up
        if up > cur:
            new = min(up, cur + STEP_UP)
        elif down < cur:
            new = max(down, cur - STEP_DOWN)
        else:
            new = cur
        return max(MIN_PCT, min(100.0, new))

    def apply(self, fan, st, pct):
        if pct is None:
            if st.taken:
                self.restore_one(fan["id"])
                st.taken = False
            st.pct = None
            return
        if fan["enable"] and read(fan["enable"]) != "1":
            write(fan["enable"], "1")
        st.taken = True
        value = hw.pct_to_pwm(fan, pct)
        if read(fan["pwm"]) != str(value):
            write(fan["pwm"], value)
        st.pct = pct

    def restore_one(self, fan_id):
        o = self.orig.get(fan_id)
        if not o:
            return
        try:
            if o.get("enable_path") and o.get("enable") is not None and os.path.exists(o["enable_path"]):
                write(o["enable_path"], o["enable"])
            elif o.get("pwm_path") and o.get("pwm") is not None and os.path.exists(o["pwm_path"]):
                write(o["pwm_path"], o["pwm"])
        except OSError as e:
            log("fan %s: could not restore: %s", fan_id, e)

    def restore_all(self):
        for fid in list(self.orig):
            self.restore_one(fid)
        for st in self.states.values():
            st.taken, st.pct = False, None

    def step(self, now=None):
        now = now or time.time()
        m = self._mtime()
        if m != self.cfg_mtime:
            self.cfg, self.cfg_mtime = load_cfg(), m
        if time.monotonic() - self.last_scan > 60:
            self.fans = hw.fans()
            self.record_orig()
            self.last_scan = time.monotonic()
        cpu_t = self.temp_fns["cpu"]()
        status = {"ts": now, "cpu_temp": cpu_t, "gpu_temp": None, "fans": {}, "alerts": []}
        for fan in self.fans:
            st = self.states.setdefault(fan["id"], FanState())
            fc = fan_cfg(self.cfg, fan["id"])
            temp = cpu_t if fan["kind"] == "cpu" else self.temp_for(fan)
            if fan["kind"] == "gpu":
                status["gpu_temp"] = temp
            was = st.protect
            try:
                pct = self.target(fan, st, temp, fc)
                self.apply(fan, st, pct)
            except OSError as e:
                log("fan %s: %s", fan["id"], e)
            speed = hw.rpm(fan)
            raw = read(fan["pwm"])
            real = hw.pwm_to_pct(fan, int(raw)) if raw and raw.lstrip("-").isdigit() else None
            if speed == 0 and real is not None and real > STALL_PCT:
                st.stall += 1
            else:
                st.stall = 0
            what = "CPU" if fan["kind"] == "cpu" else "کارت گرافیک"
            if st.protect:
                status["alerts"].append({"key": "fan_hot:" + fan["id"], "level": 2,
                                         "text": "🔥 دمای %s %.0f°C شد؛ فن %s روی ۱۰۰٪ رفت" % (what, temp or 0, fan["label"])})
            if st.stall >= 3:
                status["alerts"].append({"key": "fan_stall:" + fan["id"], "level": 2,
                                         "text": "🛑 فن %s نمی‌چرخد (۰ RPM با %d٪)؛ گیر کرده یا خراب است" % (fan["label"], real)})
            if st.protect and not was:
                log("fan %s: %s at %.0f C, 100%%", fan["id"], what, temp or 0)
            status["fans"][fan["id"]] = {"label": fan["label"], "kind": fan["kind"], "chip": fan["chip"], "rpm": speed,
                                         "pct": real, "set": st.pct, "mode": fc["mode"], "desc": describe_cfg(fc),
                                         "protect": st.protect, "stall": st.stall >= 3, "temp": temp}
        return status


def write_status(status):
    try:
        config.write_json(STATUS, status, 0o644)
    except OSError:
        pass


def read_status(max_age=15):
    s = config.read_json(STATUS, None)
    if not s or time.time() - s.get("ts", 0) > max_age:
        return None
    return s


def sd_notify(msg):
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return
    if path.startswith("@"):
        path = "\0" + path[1:]
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.connect(path)
        s.sendall(msg.encode())
    except OSError:
        pass
    finally:
        s.close()


def run_daemon(interval=INTERVAL, max_cycles=None):
    from . import perf
    stop = {"now": False}

    def on_signal(signum, frame):
        stop["now"] = True
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        perf.apply_saved_system()
    except Exception as e:  # the profile's CPU settings are not worth stopping the fans for
        log("profile at start: %s", e)
    c = Controller()
    log("eris-fan: %d fan(s): %s", len(c.fans), ", ".join(f["id"] for f in c.fans) or "none")
    sd_notify("READY=1")
    n = 0
    try:
        while not stop["now"]:
            write_status(c.step())
            sd_notify("WATCHDOG=1")
            n += 1
            if max_cycles and n >= max_cycles:
                break
            t_end = time.monotonic() + interval
            while not stop["now"] and time.monotonic() < t_end:
                time.sleep(0.2)
    finally:
        c.restore_all()
        log("eris-fan: fans back to their original mode")
    return 0


def restore_from_file():
    """ExecStopPost: whatever happened to the daemon, the fans go back to how they were."""
    orig = config.read_json(ORIG, {}) or {}
    c = Controller.__new__(Controller)
    c.orig, c.states = orig, {}
    c.restore_all()
    try:
        os.remove(STATUS)
    except OSError:
        pass
    return 0
