"""Small JSON state files and the map from short random button ids to actions."""
import copy
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from . import config


class JsonState:
    """A dict kept in one JSON file (mode 600), safe to use from several threads."""

    def __init__(self, path, default=None):
        self.path = path
        self.lock = threading.RLock()
        self.data = config.read_json(path, default if default is not None else {})
        if not isinstance(self.data, dict):
            self.data = copy.deepcopy(default) if default is not None else {}

    def get(self, key, default=None):
        with self.lock:
            v = self.data.get(key, default)
            return copy.deepcopy(v)

    def set(self, key, value):
        with self.lock:
            self.data[key] = copy.deepcopy(value)
            self.save()

    def update(self, **kw):
        with self.lock:
            for k, v in kw.items():
                self.data[k] = copy.deepcopy(v)
            self.save()

    def pop(self, key, default=None):
        with self.lock:
            v = self.data.pop(key, default)
            self.save()
            return v

    def save(self):
        with self.lock:
            try:
                config.write_json(self.path, self.data, 0o600)
            except OSError:
                pass


class Callbacks:
    """callback_data is a short id (8-12 characters, far below Telegram's 64 bytes) that maps to
    (chat, action, args, expiry). Navigation ids are a keyed hash of (chat, action, args), so the same
    button keeps its id; confirmations get a random single-use id that lives 60 seconds.
    An id that is unknown, expired or from another chat does nothing."""

    MAX = 6000

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.map = {}
        self.dirty = 0
        self.last_save = 0.0
        data = config.read_json(path, {}) or {}
        self.secret = (data.get("secret") or secrets.token_hex(16)).encode()
        now = time.time()
        for k, v in (data.get("ids") or {}).items():
            if isinstance(v, dict) and v.get("exp", 0) > now:
                self.map[k] = v

    def _id(self, chat_id, action, args):
        msg = json.dumps([chat_id, action, args], ensure_ascii=False, sort_keys=True).encode()
        return "n" + hmac.new(self.secret, msg, hashlib.sha256).hexdigest()[:9]

    def nav(self, chat_id, action, args=(), ttl=2 * 86400):
        cid = self._id(chat_id, action, list(args))
        with self.lock:
            self.map[cid] = {"c": chat_id, "a": action, "g": list(args), "exp": time.time() + ttl}
            self.dirty += 1
        return cid

    def once(self, chat_id, action, args=(), ttl=60):
        cid = "o" + secrets.token_urlsafe(7)
        with self.lock:
            self.map[cid] = {"c": chat_id, "a": action, "g": list(args), "exp": time.time() + ttl, "once": 1}
            self.dirty += 1
        return cid

    def take(self, cid, chat_id):
        """(action, args) or None; a single-use id is consumed."""
        with self.lock:
            e = self.map.get(cid or "")
            if not e or e["c"] != chat_id or e["exp"] < time.time():
                return None
            if e.get("once"):
                del self.map[cid]
                self.dirty += 1
            return e["a"], list(e["g"])

    def prune(self):
        now = time.time()
        with self.lock:
            for k in [k for k, v in self.map.items() if v["exp"] < now]:
                del self.map[k]
            if len(self.map) > self.MAX:
                for k, _ in sorted(self.map.items(), key=lambda kv: kv[1]["exp"])[:len(self.map) - self.MAX]:
                    del self.map[k]
            self.dirty += 1

    def save(self, force=False):
        if not self.dirty and not force:
            return
        if not force and time.time() - self.last_save < 5:
            return
        self.prune()
        with self.lock:
            data = {"secret": self.secret.decode(), "ids": {k: v for k, v in self.map.items() if not v.get("once")}}
            self.dirty = 0
            self.last_save = time.time()
        try:
            config.write_json(self.path, data, 0o600)
        except OSError:
            pass


class RateLimit:
    """At most `events` per `seconds` for each chat."""

    def __init__(self, events=20, seconds=10.0):
        self.events, self.seconds = events, seconds
        self.hist = {}
        self.warned = {}
        self.lock = threading.Lock()

    def allow(self, chat_id, now=None):
        now = now or time.monotonic()
        with self.lock:
            h = [t for t in self.hist.get(chat_id, []) if now - t < self.seconds]
            if len(h) >= self.events:
                self.hist[chat_id] = h
                return False
            h.append(now)
            self.hist[chat_id] = h
            return True

    def should_warn(self, chat_id, now=None):
        now = now or time.monotonic()
        with self.lock:
            if now - self.warned.get(chat_id, -1e9) > self.seconds:
                self.warned[chat_id] = now
                return True
            return False


def ensure_dirs():
    for d in (config.STATE, config.RUN, config.TMP):
        try:
            os.makedirs(d, mode=0o700, exist_ok=True)
        except OSError:
            pass
