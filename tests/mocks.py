"""Local stand-ins for the Telegram Bot API, the Marzban API and HTTP/SOCKS5 proxies (tests only)."""
import base64
import http.server
import json
import os
import select
import socket
import socketserver
import ssl
import struct
import subprocess
import threading
import time
import urllib.parse

TOKEN = "123456789:AAEhBP0av18OEMZ-test-token-for-mocks_x"


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass                     # cut connections are part of the tests


def parse_multipart(body, ctype):
    boundary = ctype.split("boundary=", 1)[1].strip().strip('"').encode()
    fields, files = {}, {}
    for part in body.split(b"--" + boundary):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        head, _, data = part.partition(b"\r\n\r\n")
        h = head.decode("utf-8", "replace")
        name = h.split('name="', 1)[1].split('"', 1)[0]
        if 'filename="' in h:
            files[name] = (h.split('filename="', 1)[1].split('"', 1)[0], data)
        else:
            fields[name] = data.decode("utf-8")
    return fields, files


class MockTelegram:
    """The Bot API methods the bot uses, with failures on demand:
    fail[method] = ["429", "502", "cut", "400"...] is used up one request at a time;
    reject_style = True answers 400 to any keyboard with a "style" field."""

    def __init__(self, token=TOKEN, port=0):
        self.token = token
        self.lock = threading.Condition()
        self.updates = []
        self.next_update = 1
        self.next_msg = 100
        self.sent = []          # (method, params)
        self.messages = {}      # (chat, id) -> text
        self.documents = []     # (chat, filename, bytes, caption)
        self.photos = []
        self.answers = []
        self.deleted = []
        self.fail = {}
        self.reject_style = False
        self.calls = []
        mock = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                self.do_POST()

            def do_POST(self):
                path = urllib.parse.urlsplit(self.path).path
                parts = path.strip("/").split("/")
                if len(parts) < 2 or parts[-2] != "bot" + mock.token:
                    return self.reply(404, {"ok": False, "error_code": 404, "description": "Not Found"})
                method = parts[-1]
                n = int(self.headers.get("Content-Length") or 0)
                ctype = self.headers.get("Content-Type", "")
                behaviour = None
                with mock.lock:
                    q = mock.fail.get(method)
                    if q:
                        behaviour = q.pop(0)
                if behaviour == "cut":
                    self.rfile.read(min(n, 4096))
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                body = self.rfile.read(n) if n else b""
                mock.calls.append(method)
                if behaviour == "429":
                    return self.reply(429, {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 1",
                                            "parameters": {"retry_after": 1}})
                if behaviour == "502":
                    return self.reply(502, {"ok": False, "error_code": 502, "description": "Bad Gateway"})
                if ctype.startswith("multipart/form-data"):
                    params, files = parse_multipart(body, ctype)
                else:
                    params, files = (json.loads(body) if body else {}), {}
                if mock.reject_style and "style" in json.dumps(params.get("reply_markup", "")):
                    return self.reply(400, {"ok": False, "error_code": 400,
                                            "description": "Bad Request: can't parse inline keyboard button: invalid button style"})
                try:
                    result = mock.handle(method, params, files)
                except KeyError as e:
                    return self.reply(400, {"ok": False, "error_code": 400, "description": "Bad Request: %s" % e})
                if isinstance(result, tuple):
                    return self.reply(result[0], result[1])
                self.reply(200, {"ok": True, "result": result})

            def reply(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = _Server(("127.0.0.1", port), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, host="127.0.0.1"):
        return "http://%s:%d" % (host, self.port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # ------------------------------------------------------------ the API
    def handle(self, method, p, files):
        with self.lock:
            self.sent.append((method, p))
        if method == "getMe":
            return {"id": 123456789, "is_bot": True, "first_name": "ERIS", "username": "eris_test_bot"}
        if method == "getUpdates":
            off = int(p.get("offset") or 0)
            deadline = time.time() + min(float(p.get("timeout") or 0), 1.0)
            with self.lock:
                while True:
                    ups = [u for u in self.updates if u["update_id"] >= off]
                    if ups or time.time() >= deadline:
                        self.updates = [u for u in self.updates if u["update_id"] >= off]
                        return ups
                    self.lock.wait(0.05)
        if method in ("sendMessage", "sendDocument", "sendPhoto"):
            with self.lock:
                self.next_msg += 1
                mid = self.next_msg
                chat = int(p["chat_id"])
                if method == "sendMessage":
                    self.messages[(chat, mid)] = p["text"]
                    return {"message_id": mid, "chat": {"id": chat}, "text": p["text"]}
                if method == "sendDocument":
                    if "document" in files:
                        name, data = files["document"]
                        self.documents.append((chat, name, data, p.get("caption", "")))
                        fid = "FILE%d" % mid
                        self.file_data = getattr(self, "file_data", {})
                        self.file_data[fid] = (name, data)
                    else:
                        fid = p["document"]
                        name, data = self.file_data[fid]
                        self.documents.append((chat, name, data, p.get("caption", "")))
                    return {"message_id": mid, "chat": {"id": chat}, "document": {"file_id": fid, "file_name": name}}
                name, data = files["photo"]
                self.photos.append((chat, name, data, p.get("caption", "")))
                return {"message_id": mid, "chat": {"id": chat}, "photo": [{"file_id": "P%d" % mid}]}
        if method == "editMessageText":
            key = (int(p["chat_id"]), int(p["message_id"]))
            with self.lock:
                if key not in self.messages:
                    return 400, {"ok": False, "error_code": 400, "description": "Bad Request: message to edit not found"}
                if self.messages[key] == p["text"] and not p.get("reply_markup"):
                    return 400, {"ok": False, "error_code": 400, "description": "Bad Request: message is not modified"}
                self.messages[key] = p["text"]
            return {"message_id": key[1], "chat": {"id": key[0]}, "text": p["text"]}
        if method == "answerCallbackQuery":
            with self.lock:
                self.answers.append(p)
            return True
        if method == "deleteMessage":
            with self.lock:
                self.deleted.append((int(p["chat_id"]), int(p["message_id"])))
                self.messages.pop((int(p["chat_id"]), int(p["message_id"])), None)
            return True
        if method == "setMyCommands":
            return True
        raise KeyError("unknown method %s" % method)

    # ------------------------------------------------------------ helpers for tests
    def push(self, update):
        with self.lock:
            update["update_id"] = self.next_update
            self.next_update += 1
            self.updates.append(update)
            self.lock.notify_all()
        return update

    def text(self, chat, text, user=None, mid=None):
        self.next_msg += 1
        return self.push({"message": {"message_id": mid or self.next_msg, "chat": {"id": chat, "type": "private"},
                                      "from": {"id": user or chat, "first_name": "T"}, "text": text}})

    def press(self, chat, msg_id, data):
        return self.push({"callback_query": {"id": "cq%d" % self.next_update, "from": {"id": chat},
                                             "message": {"message_id": msg_id, "chat": {"id": chat}}, "data": data}})

    def last(self, method):
        for m, p in reversed(self.sent):
            if m == method:
                return p
        return None

    def all(self, method):
        return [p for m, p in self.sent if m == method]

    def clear(self):
        with self.lock:
            self.sent.clear()
            self.answers.clear()


def buttons(markup):
    """[(text, callback_data, style)] of an inline keyboard."""
    out = []
    for row in (markup or {}).get("inline_keyboard", []):
        for b in row:
            out.append((b["text"], b.get("callback_data"), b.get("style")))
    return out


def find_button(markup, needle):
    for t, d, s in buttons(markup):
        if needle in t:
            return d
    raise AssertionError("no button with %r in %s" % (needle, [t for t, _, _ in buttons(markup)]))


# ---------------------------------------------------------------- Marzban

def make_cert(d):
    key, crt = os.path.join(d, "key.pem"), os.path.join(d, "cert.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt, "-days", "2",
                    "-subj", "/CN=panel.example.com"], check=True, capture_output=True)
    return crt, key


class MockMarzban:
    """Marzban 0.8.4 API with users, nodes and inbounds in memory, served over HTTPS (a certificate that is
    not valid for 127.0.0.1) or HTTP."""

    def __init__(self, tmpdir, tls=True, username="admin", password="secret"):
        self.username, self.password = username, password
        self.tokens = set()
        self.calls = []
        self.lock = threading.Lock()
        now = time.time()
        iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S.123456", time.gmtime(t))  # noqa: E731
        self.users = {}
        for name, status, used, limit, expire, online, admin in (
                ("alice", "active", 5 * 2 ** 30, 50 * 2 ** 30, now + 20 * 86400, iso(now - 30), "panel"),
                ("bob", "active", 9.5 * 2 ** 30, 10 * 2 ** 30, now + 2 * 86400, iso(now - 600), "panel"),
                ("carol", "limited", 10 * 2 ** 30, 10 * 2 ** 30, now + 9 * 86400, None, "panel"),
                ("dave", "expired", 1 * 2 ** 30, 0, now - 86400, iso(now - 90000), None),
                ("erin", "disabled", 0, 0, None, None, None)):
            self.users[name] = {"username": name, "status": status, "used_traffic": int(used), "data_limit": limit or None,
                                "expire": int(expire) if expire else None, "online_at": online,
                                "subscription_url": "/sub/%s-token-abcdef" % name, "links": [], "proxies": {"vless": {}},
                                "inbounds": {"vless": ["VLESS_TCP"]}, "note": "", "lifetime_used_traffic": int(used) * 3,
                                "created_at": "2026-01-02T03:04:05", "_admin": admin}
        for i in range(20):
            n = "user%02d" % i
            self.users[n] = dict(self.users["erin"], username=n, status="active", _admin="panel",
                                 subscription_url="/sub/%s" % n)
        self.nodes = [{"id": 1, "name": "home-node", "address": "127.0.0.1", "port": 62050, "api_port": 62051,
                       "status": "connected", "message": None, "xray_version": "26.6.27", "allowed_inbounds": []},
                      {"id": 2, "name": "tr-node", "address": "192.0.2.10", "port": 62050, "api_port": 62051,
                       "status": "error", "message": "connection refused", "xray_version": None, "allowed_inbounds": []}]
        self.reconnected = []
        mock = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def send_json(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def auth(self):
                h = self.headers.get("Authorization", "")
                return h.startswith("Bearer ") and h[7:] in mock.tokens

            def route(self, method):
                u = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                path = u.path
                mock.calls.append((method, path))
                if path == "/api/admin/token" and method == "POST":
                    f = dict(urllib.parse.parse_qsl(self.body().decode()))
                    if f.get("username") == mock.username and f.get("password") == mock.password:
                        tok = "tok%d" % len(mock.tokens)
                        mock.tokens.add(tok)
                        return self.send_json(200, {"access_token": tok, "token_type": "bearer"})
                    return self.send_json(401, {"detail": "Incorrect username or password"})
                if not self.auth():
                    return self.send_json(401, {"detail": "Could not validate credentials"})
                with mock.lock:
                    return mock.api(self, method, path, q)

            def do_GET(self):
                self.route("GET")

            def do_POST(self):
                self.route("POST")

            def do_PUT(self):
                self.route("PUT")

            def do_DELETE(self):
                self.route("DELETE")

        self.server = _Server(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.tls = tls
        if tls:
            crt, key = make_cert(tmpdir)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(crt, key)
            self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
            self.cert = crt
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def env_text(self):
        return ('UVICORN_PORT="%d"\nSUDO_USERNAME="%s"\nSUDO_PASSWORD="%s"\n%sXRAY_SUBSCRIPTION_URL_PREFIX="https://panel.example.com"\n'
                'MAIN_CORE_INBOUNDS="none"\n' % (self.port, self.username, self.password,
                                                 'UVICORN_SSL_CERTFILE="%s"\n' % self.cert if self.tls else ""))

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def public(self, u):
        return {k: v for k, v in u.items() if not k.startswith("_")}

    def api(self, h, method, path, q):
        parts = path.strip("/").split("/")
        if path == "/api/system":
            st = [u["status"] for u in self.users.values()]
            return h.send_json(200, {"version": "0.8.4", "total_user": len(st), "online_users": 3,
                                     "users_active": st.count("active"), "users_limited": st.count("limited"),
                                     "users_expired": st.count("expired"), "users_disabled": st.count("disabled"),
                                     "users_on_hold": st.count("on_hold"), "incoming_bandwidth": 10 ** 12,
                                     "outgoing_bandwidth": 10 ** 11, "incoming_bandwidth_speed": 123456,
                                     "outgoing_bandwidth_speed": 23456})
        if path == "/api/users":
            lst = sorted(self.users.values(), key=lambda u: u["username"])
            if q.get("status"):
                lst = [u for u in lst if u["status"] == q["status"]]
            if q.get("search"):
                lst = [u for u in lst if q["search"] in u["username"]]
            total = len(lst)
            off = int(q.get("offset") or 0)
            lim = int(q["limit"]) if q.get("limit") else None
            lst = lst[off:off + lim] if lim else lst[off:]
            return h.send_json(200, {"users": [self.public(u) for u in lst], "total": total})
        if path == "/api/user" and method == "POST":
            b = json.loads(h.body())
            if b["username"] in self.users:
                return h.send_json(409, {"detail": "User already exists"})
            self.users[b["username"]] = {"username": b["username"], "status": b.get("status", "active"), "used_traffic": 0,
                                         "data_limit": b.get("data_limit") or None, "expire": b.get("expire"), "online_at": None,
                                         "subscription_url": "/sub/%s-new" % b["username"], "links": [],
                                         "proxies": b["proxies"], "inbounds": b["inbounds"], "note": b.get("note", ""),
                                         "lifetime_used_traffic": 0, "created_at": "2026-11-04T10:00:00", "_admin": "panel",
                                         "_body": b}
            return h.send_json(200, self.public(self.users[b["username"]]))
        if len(parts) >= 3 and parts[1] == "user":
            name = urllib.parse.unquote(parts[2])
            u = self.users.get(name)
            if u is None:
                return h.send_json(404, {"detail": "User not found"})
            if len(parts) == 4 and parts[3] == "reset" and method == "POST":
                u["used_traffic"] = 0
                if u["status"] == "limited":
                    u["status"] = "active"
                return h.send_json(200, self.public(u))
            if method == "GET":
                return h.send_json(200, self.public(u))
            if method == "PUT":
                b = json.loads(h.body())
                for k in ("expire", "data_limit", "status", "note"):
                    if k in b:
                        u[k] = b[k]
                return h.send_json(200, self.public(u))
            if method == "DELETE":
                del self.users[name]
                if u.get("_admin") is None:        # the bug of 0.8.4: deleted, but 500
                    return h.send_json(500, {"detail": "Internal Server Error"})
                return h.send_json(200, {})
        if path == "/api/inbounds":
            return h.send_json(200, {"vless": [{"tag": "VLESS_TCP", "protocol": "vless", "network": "tcp", "tls": "reality", "port": 443},
                                               {"tag": "VLESS_WS", "protocol": "vless", "network": "ws", "tls": "tls", "port": 8443}],
                                     "vmess": [{"tag": "VMESS_WS", "protocol": "vmess", "network": "ws", "tls": "none", "port": 8080}]})
        if path == "/api/nodes":
            return h.send_json(200, self.nodes)
        if len(parts) == 4 and parts[1] == "node" and parts[3] == "reconnect":
            self.reconnected.append(int(parts[2]))
            return h.send_json(200, {})
        if path == "/api/core":
            return h.send_json(200, {"version": "26.6.27", "started": True, "logs_websocket": "/api/core/logs"})
        if path == "/api/nodes/usage":
            self.usage_start = q.get("start")
            return h.send_json(200, {"usages": [{"node_id": None, "node_name": "Master", "uplink": 2 * 2 ** 30, "downlink": 6 * 2 ** 30},
                                                {"node_id": 1, "node_name": "home-node", "uplink": 2 ** 30, "downlink": 3 * 2 ** 30}]})
        return h.send_json(404, {"detail": "Not Found"})


# ---------------------------------------------------------------- proxies

class MockProxy:
    """HTTP CONNECT and SOCKS5 on one port (told apart by the first byte), optionally with a login.
    hosts maps names the proxy can resolve (like a proxy abroad that reaches Telegram)."""

    def __init__(self, user=None, password=None, hosts=None):
        self.user, self.password = user, password
        self.hosts = hosts or {}
        self.seen = []            # (kind, target host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(50)
        self.port = self.sock.getsockname()[1]
        self.running = True
        threading.Thread(target=self.accept, daemon=True).start()

    def close(self):
        self.running = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def accept(self):
        while self.running:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(c,), daemon=True).start()

    def resolve(self, host):
        return self.hosts.get(host, host)

    def handle(self, c):
        try:
            first = c.recv(1, socket.MSG_PEEK)
            if not first:
                return c.close()
            if first == b"\x05":
                target = self.socks(c)
            else:
                target = self.http(c)
            if target is None:
                return c.close()
            self.pipe(c, target)
        except OSError:
            c.close()

    def http(self, c):
        head = b""
        while b"\r\n\r\n" not in head:
            d = c.recv(4096)
            if not d:
                return None
            head += d
        lines = head.decode("latin-1").split("\r\n")
        method, target, _ = lines[0].split(" ", 2)
        if method != "CONNECT":
            c.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return None
        if self.user is not None:
            want = "Basic " + base64.b64encode(("%s:%s" % (self.user, self.password)).encode()).decode()
            if not any(l.lower().startswith("proxy-authorization:") and l.split(":", 1)[1].strip() == want for l in lines):
                c.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
                return None
        host, port = target.rsplit(":", 1)
        self.seen.append(("http", host, int(port)))
        try:
            up = socket.create_connection((self.resolve(host), int(port)), timeout=5)
        except OSError:
            c.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return None
        c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        return up

    def socks(self, c):
        ver, n = c.recv(2)
        methods = c.recv(n)
        if self.user is not None:
            if 2 not in methods:
                c.sendall(b"\x05\xff")
                return None
            c.sendall(b"\x05\x02")
            v, ul = c.recv(2)
            u = c.recv(ul).decode()
            pl = c.recv(1)[0]
            p = c.recv(pl).decode()
            if u != self.user or p != self.password:
                c.sendall(b"\x01\x01")
                return None
            c.sendall(b"\x01\x00")
        else:
            c.sendall(b"\x05\x00")
        ver, cmd, _, atyp = c.recv(4)
        if atyp == 1:
            host = socket.inet_ntoa(c.recv(4))
        elif atyp == 3:
            host = c.recv(c.recv(1)[0]).decode()
        else:
            host = socket.inet_ntop(socket.AF_INET6, c.recv(16))
        port = struct.unpack(">H", c.recv(2))[0]
        self.seen.append(("socks5" if atyp != 3 else "socks5-dns", host, port))
        try:
            up = socket.create_connection((self.resolve(host), port), timeout=5)
        except OSError:
            c.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)
            return None
        c.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack(">H", 0))
        return up

    @staticmethod
    def pipe(a, b):
        socks = [a, b]
        try:
            while True:
                r, _, _ = select.select(socks, [], [], 30)
                if not r:
                    break
                for s in r:
                    d = s.recv(65536)
                    if not d:
                        return
                    (b if s is a else a).sendall(d)
        except OSError:
            pass
        finally:
            a.close()
            b.close()


def dead_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p
