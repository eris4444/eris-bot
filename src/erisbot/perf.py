"""Performance profiles: fan curves, cpufreq governor, min frequency and vm.swappiness.

A profile only touches what this server really has; the rest is reported as "not available here".
Raising the FSB or the voltage (real overclocking) is only possible from the BIOS and is never done here.
"""
import os

from . import config, fand, hw

PROFILES = {
    "quiet": {"title": "🤫 بی‌صدا", "fan": "quiet", "governor": ["powersave"], "swappiness": None, "minmax": False},
    "balanced": {"title": "⚖️ متعادل", "fan": "normal", "governor": ["schedutil", "ondemand"], "swappiness": None,
                 "minmax": False},
    "performance": {"title": "🚀 عملکرد", "fan": "steep", "governor": ["performance"], "swappiness": 10,
                    "minmax": False},
    "max": {"title": "🔥 حداکثر", "fan": "full", "governor": ["performance"], "swappiness": 10, "minmax": True},
}
DEFAULT = "balanced"
FAN_CHOICES = {"auto": "🔁 خودکار (BIOS)", "quiet": "منحنی آرام", "normal": "منحنی معمولی", "steep": "منحنی تند",
               "full": "۱۰۰٪"}
ORIG = os.path.join(config.STATE, "perf-orig.json")


def load():
    d = config.read_json(config.PROFILE, {}) or {}
    d.setdefault("active", None)
    d.setdefault("custom", {})
    return d


def save(d):
    config.write_json(config.PROFILE, d, 0o600)


def all_profiles():
    out = dict(PROFILES)
    for name, p in (load().get("custom") or {}).items():
        out["c:" + name] = {"title": "⭐ " + name, "fan": p.get("fan", "normal"),
                            "governor": [p["governor"]] if p.get("governor") else [],
                            "swappiness": p.get("swappiness"), "minmax": bool(p.get("minmax")), "custom": True}
    return out


def record_orig():
    o = config.read_json(ORIG, None)
    if o:
        return o
    o = {"governor": hw.governor(), "swappiness": hw.swappiness(),
         "min_freq": {d: lim[0] for d, lim in hw.freq_limits().items()}}
    config.write_json(ORIG, o, 0o600)
    return o


def fan_settings(choice):
    if choice == "full":
        return {"mode": "manual", "percent": 100}
    if choice in fand.CURVES:
        return {"mode": "curve", "curve": fand.CURVES[choice], "curve_name": choice}
    return {"mode": "auto"}


def availability(p):
    """[(what, available, note)] for the profile page."""
    fans = hw.fans()
    out = [("فن‌ها (%s)" % FAN_CHOICES.get(p["fan"], p["fan"]), bool(fans),
            "%d فن" % len(fans) if fans else "هیچ pwm پیدا نشد")]
    if p["governor"]:
        g = [x for x in p["governor"] if x in hw.governors()]
        out.append(("governor %s" % "/".join(p["governor"]), bool(g),
                    g[0] if g else ("cpufreq نیست" if not hw.has_cpufreq() else "این governor نیست")))
    if p.get("minmax"):
        out.append(("حداقل فرکانس = حداکثر", hw.has_cpufreq(), "" if hw.has_cpufreq() else "cpufreq نیست"))
    if p.get("swappiness") is not None:
        out.append(("vm.swappiness=%d" % p["swappiness"], hw.swappiness() is not None, ""))
    return out


def _system(p, orig, changes, unavailable):
    if p["governor"]:
        if not hw.has_cpufreq():
            unavailable.append("cpufreq / governor (در دسترس نیست روی این سرور)")
        else:
            g = next((x for x in p["governor"] if x in hw.governors()), None)
            cur = hw.governor()
            if g is None:
                unavailable.append("governor %s (در دسترس نیست روی این سرور)" % "/".join(p["governor"]))
            elif g != cur:
                hw.set_governor(g)
                changes.append("governor: از %s به %s" % (cur, g))
    elif hw.has_cpufreq() and orig.get("governor") and hw.governor() != orig["governor"]:
        cur = hw.governor()
        hw.set_governor(orig["governor"])
        changes.append("governor: از %s به %s" % (cur, orig["governor"]))
    if hw.has_cpufreq():
        for d, (mn, mx, hwmin, hwmax) in hw.freq_limits().items():
            want = hwmax if p.get("minmax") else (orig.get("min_freq") or {}).get(d) or hwmin
            if want and mn != want:
                hw.set_min_freq(d, want)
                changes.append("%s: حداقل فرکانس از %d به %d MHz" % (os.path.basename(os.path.dirname(d)),
                                                                       (mn or 0) // 1000, want // 1000))
    elif p.get("minmax"):
        unavailable.append("حداقل فرکانس = حداکثر (cpufreq نیست)")
    want = p.get("swappiness")
    if want is None:
        want = orig.get("swappiness")
    cur = hw.swappiness()
    if want is not None and cur is not None and cur != want:
        hw.set_swappiness(want)
        changes.append("vm.swappiness: از %d به %d" % (cur, want))


def apply(name):
    """Switch to a profile. Returns {"changes": [...], "unavailable": [...]}."""
    profiles = all_profiles()
    if name not in profiles:
        raise ValueError("unknown profile %s" % name)
    p = profiles[name]
    orig = record_orig()
    changes, unavailable = [], []
    fans = hw.fans()
    if not fans:
        unavailable.append("کنترل فن (هیچ pwm قابل کنترلی پیدا نشد)")
    else:
        cfg = fand.load_cfg()
        for f in fans:
            old = fand.fan_cfg(cfg, f["id"])
            new = fan_settings(p["fan"])
            cfg["fans"][f["id"]] = new
            nd = fand.describe_cfg(fand.fan_cfg(cfg, f["id"]))
            if fand.describe_cfg(old) != nd:
                changes.append("%s: از %s به %s" % (f["label"], fand.describe_cfg(old), nd))
        fand.save_cfg(cfg)
    _system(p, orig, changes, unavailable)
    d = load()
    d["active"] = name
    save(d)
    return {"changes": changes, "unavailable": unavailable}


def apply_saved_system():
    """At boot (eris-fan.service start): the CPU settings of the saved profile (fans.json keeps the fans)."""
    name = load().get("active")
    profiles = all_profiles()
    if not name or name not in profiles:
        return None
    changes, unavailable = [], []
    _system(profiles[name], record_orig(), changes, unavailable)
    return changes


def reset_default():
    """Everything back to how the system was before: governor, min frequency, swappiness, fans auto."""
    orig = config.read_json(ORIG, None) or {}
    changes = []
    if hw.has_cpufreq():
        if orig.get("governor") and hw.governor() != orig["governor"]:
            cur = hw.governor()
            hw.set_governor(orig["governor"])
            changes.append("governor: از %s به %s" % (cur, orig["governor"]))
        for d, (mn, _, hwmin, _) in hw.freq_limits().items():
            want = (orig.get("min_freq") or {}).get(d) or hwmin
            if want and mn != want:
                hw.set_min_freq(d, want)
                changes.append("%s: حداقل فرکانس %d MHz" % (os.path.basename(os.path.dirname(d)), want // 1000))
    if orig.get("swappiness") is not None and hw.swappiness() not in (None, orig["swappiness"]):
        cur = hw.swappiness()
        hw.set_swappiness(orig["swappiness"])
        changes.append("vm.swappiness: از %d به %d" % (cur, orig["swappiness"]))
    cfg = fand.load_cfg()
    for fid, fc in list(cfg["fans"].items()):
        if fc.get("mode") != "auto":
            changes.append("%s: 🔁 خودکار" % fid)
        cfg["fans"][fid] = {"mode": "auto"}
    fand.save_cfg(cfg)
    d = load()
    d["active"] = None
    save(d)
    return changes


def save_custom(name, fan, governor, swappiness):
    if not name or len(name) > 24:
        raise ValueError("bad name")
    d = load()
    d["custom"][name] = {"fan": fan, "governor": governor or None, "swappiness": swappiness}
    save(d)


def delete_custom(name):
    d = load()
    d["custom"].pop(name, None)
    if d.get("active") == "c:" + name:
        d["active"] = None
    save(d)
