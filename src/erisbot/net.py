"""HTTP(S) over a direct connection, an HTTP proxy (CONNECT) or a SOCKS5 proxy (DNS on the proxy side).

urllib has no SOCKS, so the tunnels are made here with plain sockets and handed to http.client.
"""
import base64
import http.client
import ipaddress
import socket
import ssl
import struct
import time
import urllib.parse


class ProxyError(OSError):
    pass


def parse_proxy(url):
    """None for a direct connection, else {scheme, host, port, user, password}."""
    if not url or url in ("off", "direct", "none"):
        return None
    u = urllib.parse.urlsplit(url)
    scheme = u.scheme.lower()
    if scheme not in ("http", "socks5", "socks5h"):
        raise ValueError("unknown proxy type %s (http:// or socks5://)" % scheme)
    if not u.hostname or not u.port:
        raise ValueError("a proxy needs host:port")
    return {"scheme": "socks5" if scheme.startswith("socks5") else "http", "host": u.hostname, "port": u.port,
            "user": urllib.parse.unquote(u.username) if u.username else None,
            "password": urllib.parse.unquote(u.password) if u.password else None}


def describe(url):
    """The proxy without its password, for logs and messages."""
    if not url or url in ("off", "direct"):
        return "direct"
    p = parse_proxy(url)
    auth = (p["user"] + ":***@") if p["user"] else ""
    return "%s://%s%s:%d" % (p["scheme"], auth, p["host"], p["port"])


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ProxyError("the proxy closed the connection")
        buf += chunk
    return buf


def _http_connect(sock, host, port, p):
    target = "%s:%d" % (("[%s]" % host) if ":" in host else host, port)
    req = "CONNECT %s HTTP/1.1\r\nHost: %s\r\n" % (target, target)
    if p["user"] is not None:
        cred = base64.b64encode(("%s:%s" % (p["user"], p["password"] or "")).encode()).decode()
        req += "Proxy-Authorization: Basic %s\r\n" % cred
    sock.sendall((req + "\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            raise ProxyError("the HTTP proxy closed the connection")
        head += chunk
        if len(head) > 65536:
            raise ProxyError("the HTTP proxy answer is too long")
    status = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = status.split()
    if len(parts) < 2 or parts[1] != "200":
        raise ProxyError("the HTTP proxy refused CONNECT: %s" % status)


def _socks5_connect(sock, host, port, p):
    methods = [0x00] + ([0x02] if p["user"] is not None else [])
    sock.sendall(bytes([5, len(methods)] + methods))
    ver, method = _recv_exact(sock, 2)
    if ver != 5 or method == 0xFF:
        raise ProxyError("the SOCKS5 proxy accepts none of our login methods")
    if method == 0x02:
        u, pw = (p["user"] or "").encode(), (p["password"] or "").encode()
        sock.sendall(bytes([1, len(u)]) + u + bytes([len(pw)]) + pw)
        if _recv_exact(sock, 2)[1] != 0:
            raise ProxyError("the SOCKS5 proxy refused the user name or password")
    elif method != 0x00:
        raise ProxyError("unknown SOCKS5 method %d" % method)
    try:
        ip = ipaddress.ip_address(host)
        addr = (b"\x01" + ip.packed) if ip.version == 4 else (b"\x04" + ip.packed)
    except ValueError:                      # a name: the proxy resolves it (no DNS leak, no filtered DNS)
        name = host.encode("idna")
        addr = b"\x03" + bytes([len(name)]) + name
    sock.sendall(b"\x05\x01\x00" + addr + struct.pack(">H", port))
    ver, rep, _, atyp = _recv_exact(sock, 4)
    if rep != 0:
        reasons = {1: "general failure", 2: "not allowed", 3: "network unreachable", 4: "host unreachable",
                   5: "connection refused", 6: "TTL expired", 7: "command not supported", 8: "address type not supported"}
        raise ProxyError("the SOCKS5 proxy could not connect: %s" % reasons.get(rep, rep))
    if atyp == 1:
        _recv_exact(sock, 4 + 2)
    elif atyp == 4:
        _recv_exact(sock, 16 + 2)
    elif atyp == 3:
        _recv_exact(sock, _recv_exact(sock, 1)[0] + 2)


def open_socket(host, port, proxy=None, timeout=15.0):
    """A TCP socket to host:port, through the proxy URL (or direct)."""
    p = parse_proxy(proxy) if isinstance(proxy, str) or proxy is None else proxy
    if p is None:
        return socket.create_connection((host, port), timeout=timeout)
    sock = socket.create_connection((p["host"], p["port"]), timeout=timeout)
    try:
        sock.settimeout(timeout)
        if p["scheme"] == "http":
            _http_connect(sock, host, port, p)
        else:
            _socks5_connect(sock, host, port, p)
    except BaseException:
        sock.close()
        raise
    return sock


_SSL = None


def ssl_context():
    global _SSL
    if _SSL is None:
        _SSL = ssl.create_default_context()
    return _SSL


class Connection(http.client.HTTPConnection):
    """http.client connection whose socket comes from open_socket() (and TLS for https)."""

    def __init__(self, url, proxy=None, timeout=15.0, context=None):
        u = urllib.parse.urlsplit(url)
        self.tls = u.scheme == "https"
        port = u.port or (443 if self.tls else 80)
        http.client.HTTPConnection.__init__(self, u.hostname, port, timeout=timeout)
        self.proxy = proxy
        self.context = context

    def connect(self):
        sock = open_socket(self.host, self.port, self.proxy, self.timeout)
        if self.tls:
            ctx = self.context or ssl_context()
            sock = ctx.wrap_socket(sock, server_hostname=self.host)
        self.sock = sock


class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body


def request(method, url, proxy=None, body=None, headers=None, timeout=15.0, read_timeout=None,
            conn=None, context=None):
    """One HTTP request; returns Response. `conn` (a Connection) is reused when given."""
    u = urllib.parse.urlsplit(url)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    own = conn is None
    c = conn or Connection(url, proxy, timeout, context)
    try:
        c.request(method, path, body=body, headers=headers or {})
        if c.sock is not None and read_timeout:
            c.sock.settimeout(read_timeout)
        r = c.getresponse()
        data = r.read()
        return Response(r.status, dict(r.getheaders()), data)
    finally:
        if own:
            c.close()


def probe(url, proxy, timeout=10.0):
    """(ok, seconds, error) for one GET through the proxy."""
    t0 = time.monotonic()
    try:
        r = request("GET", url, proxy=proxy, timeout=timeout, read_timeout=timeout)
        return r.status < 500, time.monotonic() - t0, None if r.status < 500 else "HTTP %d" % r.status
    except (OSError, http.client.HTTPException, ValueError) as e:
        return False, time.monotonic() - t0, str(e) or type(e).__name__
