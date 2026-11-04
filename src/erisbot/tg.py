"""Telegram Bot API client: JSON calls, retries (429 retry_after, 5xx, network), the way to Telegram
(direct or a proxy, "auto" = the first that answers), streaming multipart uploads and the fallback
to buttons without colour when the API refuses the "style" field (Bot API 9.4)."""
import copy
import http.client
import json
import os
import socket
import ssl
import threading
import time
import uuid

from . import net
from .util import log

NET_ERRORS = (OSError, http.client.HTTPException, ssl.SSLError, socket.timeout)


class TelegramError(Exception):
    def __init__(self, code, description, retry_after=None):
        Exception.__init__(self, "%s: %s" % (code, description))
        self.code, self.description, self.retry_after = code, description or "", retry_after


class NoRoute(OSError):
    pass


def strip_styles(obj):
    """(copy without "style" keys, True if there were any)."""
    found = [False]

    def walk(x):
        if isinstance(x, dict):
            if "style" in x:
                found[0] = True
            return {k: walk(v) for k, v in x.items() if k != "style"}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x
    out = walk(obj)
    return out, found[0]


class Router:
    """The way to Telegram. proxy = off | auto | URL; auto tries direct first, then proxy_list in order."""

    def __init__(self, conf_fn):
        self.conf_fn = conf_fn
        self.current = None
        self.chosen = False
        self.lock = threading.Lock()
        self.last_fail = 0.0
        self.results = {}              # route -> (ok, seconds, error, when)

    def candidates(self):
        c = self.conf_fn()
        mode = c.get("proxy", "auto")
        if mode == "off":
            return [None]
        if mode == "auto":
            return [None] + [p for p in c.get("proxy_list") or [] if p]
        return [mode]

    def reset(self):
        with self.lock:
            self.chosen = False

    def route(self, probe):
        """The route to use now; probe(route) -> (ok, sec, err) picks one when none is chosen yet."""
        with self.lock:
            if self.chosen:
                return self.current
            cands = self.candidates()
            for r in cands:
                ok, sec, err = probe(r)
                self.results[r] = (ok, sec, err, time.time())
                if ok:
                    if r != self.current or not self.chosen:
                        log("telegram: using %s (%.1f s)", net.describe(r), sec)
                    self.current, self.chosen = r, True
                    return r
                log("telegram: %s does not work: %s", net.describe(r), err)
            self.last_fail = time.time()
            raise NoRoute("no way to Telegram works (%s)" % ", ".join(net.describe(r) for r in cands))

    def failed(self, route):
        with self.lock:
            if self.chosen and self.current == route:
                self.chosen = False


class Bot:
    def __init__(self, conf_fn, state=None, sleep=time.sleep):
        self.conf_fn = conf_fn
        self.router = Router(conf_fn)
        self.state = state                 # erisbot.store.JsonState or None
        self.sleep = sleep
        self.local = threading.local()
        self.me = None

    # ------------------------------------------------------------ basics
    @property
    def conf(self):
        return self.conf_fn()

    def base(self):
        return self.conf["api_base"].rstrip("/")

    def url(self, method):
        return "%s/bot%s/%s" % (self.base(), self.conf["token"], method)

    def colors_on(self):
        if self.conf.get("button_colors", "auto") == "off":
            return False
        return not (self.state and self.state.get("styles_refused"))

    def _conn(self, route, timeout):
        cache = getattr(self.local, "conns", None)
        if cache is None:
            cache = self.local.conns = {}
        key = (self.base(), route)
        c = cache.get(key)
        if c is None:
            c = net.Connection(self.base(), route, timeout)
            cache[key] = c
        c.timeout = timeout
        return c

    def close(self):
        for c in (getattr(self.local, "conns", None) or {}).values():
            c.close()
        self.local.conns = {}

    def _drop(self, route):
        cache = getattr(self.local, "conns", None) or {}
        c = cache.pop((self.base(), route), None)
        if c:
            c.close()

    def probe(self, route, timeout=10.0):
        """(ok, seconds, error) of getMe through one route; never raises."""
        t0 = time.monotonic()
        try:
            r = net.request("POST", self.url("getMe"), proxy=route, body=b"{}",
                            headers={"Content-Type": "application/json"}, timeout=timeout, read_timeout=timeout)
            data = json.loads(r.body or b"{}")
            if data.get("ok"):
                return True, time.monotonic() - t0, None
            return False, time.monotonic() - t0, "%s %s" % (data.get("error_code", r.status), data.get("description", ""))
        except NET_ERRORS + (ValueError,) as e:
            return False, time.monotonic() - t0, str(e) or type(e).__name__

    def route(self):
        return self.router.route(self.probe)

    # ------------------------------------------------------------ JSON calls
    def _post(self, route, method, params, read_timeout):
        body = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
        c = self._conn(route, 10.0)
        try:
            if c.sock is not None:
                c.sock.settimeout(read_timeout)
            c.request("POST", "%s/bot%s/%s" % (self._path_prefix(), self.conf["token"], method),
                      body=body, headers={"Content-Type": "application/json", "Connection": "keep-alive"})
            c.sock.settimeout(read_timeout)
            r = c.getresponse()
            data = r.read()
            if r.getheader("Connection", "").lower() == "close":
                self._drop(route)
            return r.status, data
        except BaseException:
            self._drop(route)
            raise

    def _path_prefix(self):
        import urllib.parse
        return urllib.parse.urlsplit(self.base()).path.rstrip("/")

    def call(self, method, params=None, timeout=30.0, attempts=5):
        params = dict(params or {})
        if "reply_markup" in params and not self.colors_on():
            params["reply_markup"] = strip_styles(params["reply_markup"])[0]
        delay = 1.0
        last = None
        for attempt in range(attempts):
            try:
                route = self.route()
            except NoRoute as e:
                last = e
                self.sleep(min(30, delay))
                delay *= 2
                continue
            try:
                status, raw = self._post(route, method, params, timeout)
            except NET_ERRORS as e:
                last = e
                log("telegram %s: %s via %s", method, e or type(e).__name__, net.describe(route))
                if attempt >= 1:
                    self.router.failed(route)
                self.sleep(min(10, delay * (0.2 if attempt == 0 else 1)))
                delay *= 2
                continue
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                data = {"ok": False, "error_code": status, "description": "not JSON (HTTP %d)" % status}
            if data.get("ok"):
                return data.get("result")
            code = data.get("error_code", status)
            desc = data.get("description", "")
            if code == 429:
                ra = int((data.get("parameters") or {}).get("retry_after") or 3)
                log("telegram %s: too many requests, waiting %d s", method, ra)
                self.sleep(min(ra, 60))
                last = TelegramError(code, desc, ra)
                continue
            if code >= 500:
                last = TelegramError(code, desc)
                self.sleep(min(10, delay))
                delay *= 2
                continue
            if code == 400 and "reply_markup" in params:
                plain, had = strip_styles(params["reply_markup"])
                if had:
                    try:
                        res = self.call(method, dict(params, reply_markup=plain), timeout, attempts)
                    except TelegramError:
                        raise TelegramError(code, desc)
                    log("telegram refused coloured buttons (%s); from now on buttons have no colour", desc)
                    if self.state:
                        self.state.set("styles_refused", True)
                        self.state.set("styles_refused_reason", desc[:200])
                    return res
            raise TelegramError(code, desc)
        if isinstance(last, TelegramError):
            raise last
        raise NoRoute(str(last) if last else "telegram did not answer")

    # ------------------------------------------------------------ uploads
    def send_file(self, method, field, chat_id, filename, path=None, data=None, offset=0, length=None,
                  caption=None, progress=None, extra=None):
        """multipart/form-data upload streamed from a file (offset/length = one piece of it) or bytes.
        One attempt (429 waits are retried); network errors raise, the caller retries."""
        up = self.conf.get("upload") or {}
        wto, rto = float(up.get("write_timeout", 60)), float(up.get("read_timeout", 180))
        if path is not None:
            size = os.path.getsize(path) - offset if length is None else length
        else:
            size = len(data)
        fields = {"chat_id": str(chat_id)}
        if caption:
            fields["caption"] = caption
            fields["parse_mode"] = "HTML"
        for k, v in (extra or {}).items():
            fields[k] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        boundary = "eris-bot-" + uuid.uuid4().hex
        pre = b""
        for k, v in fields.items():
            pre += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n" % (boundary, k)).encode()
            pre += v.encode("utf-8") + b"\r\n"
        safe_name = filename.replace('"', "_").replace("\r", "").replace("\n", "")
        pre += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n" % (boundary, field, safe_name)).encode("utf-8")
        post = ("\r\n--%s--\r\n" % boundary).encode()
        total = len(pre) + size + len(post)
        for _ in range(4):
            route = self.route()
            c = net.Connection(self.base(), route, 15.0)
            try:
                c.putrequest("POST", "%s/bot%s/%s" % (self._path_prefix(), self.conf["token"], method))
                c.putheader("Content-Type", "multipart/form-data; boundary=%s" % boundary)
                c.putheader("Content-Length", str(total))
                c.endheaders()
                c.sock.settimeout(wto)
                c.send(pre)
                sent = 0
                if path is not None:
                    with open(path, "rb") as f:
                        f.seek(offset)
                        left = size
                        while left > 0:
                            chunk = f.read(min(65536, left))
                            if not chunk:
                                raise OSError("%s ended early" % path)
                            c.sock.sendall(chunk)
                            left -= len(chunk)
                            sent += len(chunk)
                            if progress:
                                progress(sent, size)
                else:
                    view = memoryview(data)
                    for i in range(0, len(data), 65536):
                        c.sock.sendall(view[i:i + 65536])
                        sent = min(len(data), i + 65536)
                        if progress:
                            progress(sent, size)
                c.send(post)
                c.sock.settimeout(rto)
                r = c.getresponse()
                raw = r.read()
            except NET_ERRORS:
                self.router.failed(route)
                raise
            finally:
                c.close()
            try:
                res = json.loads(raw or b"{}")
            except ValueError:
                res = {"ok": False, "error_code": r.status, "description": "not JSON"}
            if res.get("ok"):
                return res.get("result")
            code = res.get("error_code", r.status)
            if code == 429:
                self.sleep(min(int((res.get("parameters") or {}).get("retry_after") or 5), 60))
                continue
            raise TelegramError(code, res.get("description", ""))
        raise TelegramError(429, "too many requests")

    # ------------------------------------------------------------ helpers
    def get_me(self):
        self.me = self.call("getMe")
        return self.me

    def send(self, chat_id, text, markup=None, **kw):
        p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if markup is not None:
            p["reply_markup"] = markup
        p.update(kw)
        return self.call("sendMessage", p)

    def edit(self, chat_id, message_id, text, markup=None):
        p = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
             "disable_web_page_preview": True}
        if markup is not None:
            p["reply_markup"] = markup
        try:
            return self.call("editMessageText", p)
        except TelegramError as e:
            if "not modified" in e.description:
                return None
            raise

    def answer(self, callback_id, text=None, alert=False):
        p = {"callback_query_id": callback_id}
        if text:
            p["text"] = text[:190]
            p["show_alert"] = alert
        try:
            return self.call("answerCallbackQuery", p, attempts=2)
        except (TelegramError, OSError):
            return None

    def delete(self, chat_id, message_id):
        try:
            return self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id}, attempts=2)
        except (TelegramError, OSError):
            return None


def deep(obj):
    return copy.deepcopy(obj)
