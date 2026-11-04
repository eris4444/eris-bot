"""Marzban 0.8.4 API on this server: https://127.0.0.1:<UVICORN_PORT> as the .env admin.

The certificate is not valid for 127.0.0.1, so it is not checked for this address only; no proxy is used.
"""
import calendar
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

STATUSES = ("active", "disabled", "limited", "expired", "on_hold")
NOW_WINDOW = 120         # "connected right now" = traffic (online_at) in the last 2 minutes


class MarzbanError(Exception):
    def __init__(self, msg, code=None):
        Exception.__init__(self, msg)
        self.code = code


def read_env(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def utc_epoch(s):
    """'2026-11-04T09:57:30.375208' (UTC without Z) -> epoch."""
    if not s:
        return None
    s = s.rstrip("Z").split("+")[0]
    base = s.split(".")[0]
    try:
        t = time.strptime(base, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return calendar.timegm(t)


def online_now(users, now=None):
    now = now or time.time()
    out = []
    for u in users:
        t = utc_epoch(u.get("online_at"))
        if t is not None and now - t <= NOW_WINDOW:
            out.append(u.get("username") or "?")
    return sorted(out)


class Marzban:
    def __init__(self, env_path=None):
        self.env_path = env_path or config.MARZBAN_ENV
        self.token = None
        self.lock = threading.Lock()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))

    def available(self):
        return os.path.isfile(self.env_path)

    def env(self):
        return read_env(self.env_path)

    def base(self):
        e = self.env()
        scheme = "https" if e.get("UVICORN_SSL_CERTFILE") else "http"
        return "%s://127.0.0.1:%s" % (scheme, e.get("UVICORN_PORT", "8000"))

    def _open(self, method, path, body=None, form=None, timeout=20):
        headers = {"Accept": "application/json"}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.token and form is None:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.base() + path, data=data, headers=headers, method=method)
        with self.opener.open(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def login(self):
        e = self.env()
        r = self._open("POST", "/api/admin/token", form={"username": e.get("SUDO_USERNAME", ""),
                                                          "password": e.get("SUDO_PASSWORD", "")})
        self.token = (r or {}).get("access_token")
        if not self.token:
            raise MarzbanError("login failed")

    def req(self, method, path, body=None, query=None):
        if query:
            q = {k: v for k, v in query.items() if v not in (None, "")}
            if q:
                path += "?" + urllib.parse.urlencode(q, doseq=True)
        with self.lock:
            for attempt in (1, 2):
                try:
                    if not self.token:
                        self.login()
                    return self._open(method, path, body=body)
                except urllib.error.HTTPError as e:
                    if e.code == 401 and attempt == 1:
                        self.token = None
                        continue
                    try:
                        detail = json.loads(e.read() or b"{}").get("detail")
                    except (ValueError, AttributeError, OSError):
                        detail = None
                    raise MarzbanError("HTTP %d%s" % (e.code, ": %s" % detail if detail else ""), e.code)
                except (OSError, ValueError) as e:
                    self.token = None
                    raise MarzbanError("no answer from %s (%s)" % (self.base(), getattr(e, "reason", e)))
        raise MarzbanError("login failed")

    # ------------------------------------------------------------ API
    def system(self):
        return self.req("GET", "/api/system") or {}

    def users(self, offset=None, limit=None, search=None, status=None):
        return self.req("GET", "/api/users", query={"offset": offset, "limit": limit, "search": search,
                                                    "status": status}) or {"users": [], "total": 0}

    def all_users(self):
        return (self.users() or {}).get("users", [])

    def user(self, name):
        return self.req("GET", "/api/user/%s" % urllib.parse.quote(name, safe=""))

    def modify(self, name, **fields):
        return self.req("PUT", "/api/user/%s" % urllib.parse.quote(name, safe=""), body=fields)

    def reset(self, name):
        return self.req("POST", "/api/user/%s/reset" % urllib.parse.quote(name, safe=""))

    def delete(self, name):
        """DELETE; Marzban 0.8.4 answers 500 for users made by the .env admin but deletes them anyway,
        so the answer that counts is a GET afterwards (404 = gone)."""
        err = None
        try:
            self.req("DELETE", "/api/user/%s" % urllib.parse.quote(name, safe=""))
        except MarzbanError as e:
            err = e
        try:
            self.user(name)
        except MarzbanError as e:
            if e.code == 404:
                return True
            raise
        raise err or MarzbanError("the user is still there")

    def create(self, username, inbounds, expire=None, data_limit=0, note=""):
        """inbounds: {protocol: [tags]}"""
        body = {"username": username, "proxies": {p: {} for p, tags in inbounds.items() if tags},
                "inbounds": {p: tags for p, tags in inbounds.items() if tags},
                "expire": expire, "data_limit": data_limit or 0, "status": "active", "note": note}
        return self.req("POST", "/api/user", body=body)

    def inbounds(self):
        return self.req("GET", "/api/inbounds") or {}

    def nodes(self):
        return self.req("GET", "/api/nodes") or []

    def reconnect(self, node_id):
        return self.req("POST", "/api/node/%d/reconnect" % int(node_id))

    def core(self):
        return self.req("GET", "/api/core") or {}

    def today_usage(self, now=None):
        """(uplink, downlink) since local midnight (Marzban keeps UTC)."""
        now = now or time.time()
        lt = time.localtime(now)
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        start = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(midnight))
        r = self.req("GET", "/api/nodes/usage", query={"start": start}) or {}
        up = sum(u.get("uplink") or 0 for u in r.get("usages", []))
        down = sum(u.get("downlink") or 0 for u in r.get("usages", []))
        return up, down

    # ------------------------------------------------------------ changes made by the bot
    def extend(self, name, days):
        u = self.user(name)
        if not u.get("expire"):
            return u, False
        base = max(time.time(), u["expire"])
        return self.modify(name, expire=int(base + days * 86400)), True

    def add_data(self, name, gib):
        u = self.user(name)
        if not u.get("data_limit"):
            return u, False
        return self.modify(name, data_limit=int(u["data_limit"] + gib * 1024 ** 3)), True

    def set_status(self, name, status):
        return self.modify(name, status=status)
