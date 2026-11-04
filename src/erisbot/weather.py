"""Weather from Open-Meteo (free, no key): city search and a forecast, as Persian text."""
import json
import os
import time
import urllib.parse

from . import net
from .util import esc, fa

GEO_URL = os.environ.get("ERISBOT_GEO_URL", "https://geocoding-api.open-meteo.com/v1/search")
FORECAST_URL = os.environ.get("ERISBOT_FORECAST_URL", "https://api.open-meteo.com/v1/forecast")

# WMO weather codes
CODES = {0: ("☀️", "صاف"), 1: ("🌤", "تقریباً صاف"), 2: ("⛅️", "نیمه‌ابری"), 3: ("☁️", "ابری"),
         45: ("🌫", "مه"), 48: ("🌫", "مه یخ‌زده"), 51: ("🌦", "نم‌نم باران"), 53: ("🌦", "نم‌نم باران"),
         55: ("🌦", "نم‌نم باران شدید"), 56: ("🌧", "نم‌نم یخ‌زده"), 57: ("🌧", "نم‌نم یخ‌زده"),
         61: ("🌧", "باران کم"), 63: ("🌧", "باران"), 65: ("🌧", "باران شدید"), 66: ("🌧", "باران یخ‌زده"),
         67: ("🌧", "باران یخ‌زده‌ی شدید"), 71: ("🌨", "برف کم"), 73: ("🌨", "برف"), 75: ("❄️", "برف شدید"),
         77: ("🌨", "دانه‌ی برف"), 80: ("🌦", "رگبار"), 81: ("🌧", "رگبار"), 82: ("⛈", "رگبار شدید"),
         85: ("🌨", "رگبار برف"), 86: ("❄️", "رگبار برف شدید"), 95: ("⛈", "رعد و برق"),
         96: ("⛈", "رعد و برق با تگرگ"), 99: ("⛈", "رعد و برق با تگرگ شدید")}
DAYS_FA = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه", "شنبه", "یکشنبه"]


class WeatherError(Exception):
    pass


def code(c):
    return CODES.get(int(c or 0), ("🌡", "نامشخص"))


def get_json(url, routes):
    """GET through the first route that answers (direct first, then the bot's proxies)."""
    last = None
    for r in routes:
        try:
            res = net.request("GET", url, proxy=r, timeout=10, read_timeout=15)
            if res.status == 200:
                return json.loads(res.body.decode("utf-8"))
            last = "HTTP %d" % res.status
        except (OSError, ValueError) as e:
            last = str(e) or type(e).__name__
    raise WeatherError("سرویس هواشناسی جواب نداد (%s)" % last)


def search(name, routes):
    """[(display name, lat, lon, timezone)] for a city name (Persian or English)."""
    q = urllib.parse.urlencode({"name": name.strip()[:60], "count": 6, "language": "fa", "format": "json"})
    d = get_json("%s?%s" % (GEO_URL, q), routes)
    out = []
    for r in d.get("results") or []:
        parts = [r.get("name"), r.get("admin1"), r.get("country")]
        label = "، ".join(p for i, p in enumerate(parts) if p and p not in parts[:i])
        out.append((label, float(r["latitude"]), float(r["longitude"]), r.get("timezone") or "auto"))
    return out


def forecast(lat, lon, routes):
    q = urllib.parse.urlencode({
        "latitude": "%.4f" % lat, "longitude": "%.4f" % lon, "timezone": "auto", "forecast_days": 5,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset,uv_index_max",
        "hourly": "temperature_2m,weather_code,precipitation_probability"})
    return get_json("%s?%s" % (FORECAST_URL, q), routes)


def n(x, digits=0):
    if x is None:
        return "؟"
    return fa(("%." + str(digits) + "f") % x).replace("-", "−")


def hhmm(iso):
    return fa((iso or "")[11:16]) if iso else "؟"


def render(city, d):
    cur = d.get("current") or {}
    day = d.get("daily") or {}
    em, txt = code(cur.get("weather_code"))
    lines = ["%s <b>آب و هوای %s</b>" % (em, esc(city)),
             "🌡 الان: <b>%s°</b> (حس می‌شود %s°) · %s" % (n(cur.get("temperature_2m")), n(cur.get("apparent_temperature")), txt),
             "💧 رطوبت %s٪ · 💨 باد %s km/h" % (n(cur.get("relative_humidity_2m")), n(cur.get("wind_speed_10m")))]
    if cur.get("precipitation"):
        lines.append("☔️ بارش الان: %s mm" % n(cur["precipitation"], 1))
    if day.get("time"):
        lines.append("🌅 طلوع %s · 🌇 غروب %s · ☀️ UV %s" % (hhmm(day["sunrise"][0]), hhmm(day["sunset"][0]),
                                                         n((day.get("uv_index_max") or [None])[0])))
        hourly = d.get("hourly") or {}
        now = cur.get("time") or ""
        nxt = []
        for t, temp, c in zip(hourly.get("time") or [], hourly.get("temperature_2m") or [], hourly.get("weather_code") or []):
            if t > now and t[11:13] in ("00", "03", "06", "09", "12", "15", "18", "21"):
                nxt.append("%s %s %s°" % (hhmm(t), code(c)[0], n(temp)))
            if len(nxt) >= 6:
                break
        if nxt:
            lines += ["", "🕒 <b>ساعت‌های بعد</b>", " · ".join(nxt)]
        lines += ["", "📅 <b>روزهای بعد</b>"]
        for i, t in enumerate(day["time"]):
            name = "امروز" if i == 0 else ("فردا" if i == 1 else DAYS_FA[time.strptime(t, "%Y-%m-%d").tm_wday])
            e, w = code(day["weather_code"][i])
            pop = (day.get("precipitation_probability_max") or [None] * 5)[i]
            lines.append("%s %s: %s° تا %s° · %s%s" % (e, name, n(day["temperature_2m_min"][i]), n(day["temperature_2m_max"][i]),
                                                     w, " · ☔️ %s٪" % n(pop) if pop else ""))
    return "\n".join(lines)
