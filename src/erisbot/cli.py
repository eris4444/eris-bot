"""eris-bot - a Telegram bot for this server.

  eris-bot setup                  token (hidden while you type) and chat IDs
  eris-bot status                 what is set up and running
  eris-bot test                   connect to Telegram directly and through each proxy (getMe, with latency)
  eris-bot proxy [off|auto|URL|test]   the way to Telegram (URL: http://host:port or socks5://[user:pass@]host:port)
  eris-bot password               set the password that encrypts the backups sent to Telegram
  eris-bot logs [-f] [-n N]       the log (journalctl -u eris-bot -u eris-fan)
  eris-bot update [--check|--force]   install the latest release (through the bot's proxy)
  eris-bot version
  eris-bot run | fan-daemon | fan-restore | selftest   (used by the services and the installer)
"""
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
import time

from . import VERSION, REPO, config, net, store, tg
from .util import log

GREEN, RED, AMBER, GREY = "\033[1;32m", "\033[1;31m", "\033[33m", "\033[1;30m"
TTY = sys.stdout.isatty()


def c(code, s):
    return "%s%s\033[0m" % (code, s) if TTY else s


def need_root():
    if os.geteuid() != 0 and not config.ROOT:
        sys.exit("eris-bot: run as root")


def bot_for(conf):
    return tg.Bot(lambda: conf, store.JsonState(os.path.join(config.STATE, "bot.json"), {}))


def routes(conf):
    mode = conf.get("proxy", "auto")
    lst = list(conf.get("proxy_list") or [])
    if mode == "off":
        return [None]
    if mode == "auto":
        return [None] + lst
    return [mode]


def cmd_test(conf, every=True):
    if not conf.get("token"):
        print("no token yet: eris-bot setup")
        return 1
    b = bot_for(conf)
    cands = [None] + list(conf.get("proxy_list") or [])
    if conf["proxy"] not in ("auto", "off") and conf["proxy"] not in cands:
        cands.append(conf["proxy"])
    if not every:
        cands = routes(conf)
    ok_any = False
    print("Telegram API: %s   (setting proxy=%s)" % (conf["api_base"], conf["proxy"] if conf["proxy"] in ("auto", "off") else net.describe(conf["proxy"])))
    for r in cands:
        ok, sec, err = b.probe(r, timeout=10)
        ok_any |= ok
        print("  %-38s %s" % (net.describe(r) if r else "direct", c(GREEN, "ok  %4d ms" % (sec * 1000)) if ok else c(RED, "no  %s" % (err or "")[:70])))
    return 0 if ok_any else 1


def cmd_status(conf):
    print("eris-bot %s" % VERSION)
    print("  settings : %s (%s)" % (config.CONFIG, oct(os.stat(config.CONFIG).st_mode & 0o777) if os.path.exists(config.CONFIG) else "missing"))
    print("  token    : %s" % ("set" if conf.get("token") else c(RED, "not set (eris-bot setup)")))
    print("  chats    : %s" % (", ".join("%s (%s)" % (i, config.role(conf, i)) for i in conf["allowed_chat_ids"]) or c(RED, "none")))
    print("  proxy    : %s; list: %s" % (conf["proxy"] if conf["proxy"] in ("auto", "off") else net.describe(conf["proxy"]),
                                         ", ".join(net.describe(p) for p in conf.get("proxy_list") or [])))
    for u in ("eris-bot", "eris-fan"):
        st = subprocess.run(["systemctl", "is-active", u], capture_output=True, text=True).stdout.strip() if os.path.exists("/usr/bin/systemctl") or os.path.exists("/bin/systemctl") else "?"
        print("  %-8s : %s" % (u, c(GREEN, st) if st == "active" else c(AMBER, st or "?")))
    pw = conf.get("backup_password_file")
    print("  password : %s" % ("set" if pw and os.path.isfile(pw) and os.path.getsize(pw) > 0 else c(AMBER, "not set (eris-bot password)")))
    from . import eris, hw, marzban
    print("  ERIS     : %s" % (eris.version() or "?") if eris.available() else "  ERIS     : not installed (backup and firewall menus are hidden)")
    print("  Marzban  : %s" % ("found (%s)" % config.MARZBAN_ENV if marzban.Marzban().available() else "not found (its menu is hidden)"))
    print("  fans     : %s; cpufreq: %s" % (", ".join(f["id"] for f in hw.fans()) or "none", "yes" if hw.has_cpufreq() else "no"))
    send = config.read_json(os.path.join(config.STATE, "send.json"), {}) or {}
    for part, r in sorted((send.get("results") or {}).items()):
        print("  sent %-12s %s %s" % (part, "ok" if r.get("ok") else "FAILED", time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("at", 0)))))
    return 0


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def write_password(path, pw):
    from .s_main import write_password as wp
    wp(path, pw)


def cmd_password(conf):
    path = config.R(conf["backup_password_file"]) if config.ROOT else conf["backup_password_file"]
    print("The backups sent to Telegram are encrypted with this password (AES-256, openssl).")
    print("Without it nobody (you included) can open them: keep it somewhere safe.")
    a = getpass.getpass("new backup password (at least 12 characters): ")
    if len(a) < 12:
        print(c(RED, "too short, nothing changed"))
        return 1
    if getpass.getpass("again: ") != a:
        print(c(RED, "not the same, nothing changed"))
        return 1
    write_password(path, a)
    print(c(GREEN, "saved in %s (mode 600)" % path))
    return 0


def systemctl(*args):
    try:
        return subprocess.run(["systemctl"] + list(args), capture_output=True, text=True, timeout=60).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1


def cmd_setup(conf):
    need_root()
    print("eris-bot setup. Nothing you type here is shown or printed.")
    token = conf.get("token") or ""
    t = getpass.getpass("bot token from @BotFather%s: " % (" (Enter = keep the current one)" if token else ""))
    if t.strip():
        if not config.TOKEN_RX.match(t.strip()):
            print(c(RED, "that does not look like a bot token (123456:ABC...); nothing changed"))
            return 1
        token = t.strip()
    if not token:
        print(c(RED, "a token is needed"))
        return 1
    conf = dict(conf, token=token)
    p = ask("proxy to Telegram [%s] (auto, off, http://host:port, socks5://host:port): " % conf["proxy"])
    if p:
        if not config.validate_proxy(p):
            print(c(RED, "not a proxy URL; nothing changed"))
            return 1
        conf["proxy"] = p
    print("testing the connection ...")
    b = bot_for(conf)
    try:
        route = b.route()
        me = b.get_me()
    except (tg.TelegramError, OSError) as e:
        print(c(RED, "Telegram does not answer with this token: %s" % e))
        print("check the token and the proxy (eris-bot test), then run eris-bot setup again")
        return 1
    print(c(GREEN, "ok: @%s via %s" % (me.get("username"), net.describe(route) if route else "a direct connection")))
    was_active = systemctl("is-active", "--quiet", "eris-bot") == 0
    if was_active:
        systemctl("stop", "eris-bot")
    try:
        ids = list(conf.get("allowed_chat_ids") or [])
        print("Allowed chat IDs now: %s" % (", ".join(map(str, ids)) or "none"))
        a = ask("chat IDs (numbers, comma separated), or Enter to find yours: send any message to @%s now: " % me.get("username"))
        if a:
            if not re.fullmatch(r"\s*-?\d+(\s*,\s*-?\d+)*\s*", a):
                print(c(RED, "chat IDs are numbers; nothing changed"))
                return 1
            ids = [int(x) for x in a.split(",")]
        elif not ids:
            print("waiting up to 120 s for a message to @%s ..." % me.get("username"))
            found = {}
            off = None
            deadline = time.time() + 120
            while time.time() < deadline and not found:
                upd = b.call("getUpdates", {"timeout": 20, "offset": off} if off else {"timeout": 20}, timeout=35) or []
                for u in upd:
                    off = u["update_id"] + 1
                    m = u.get("message") or {}
                    ch = m.get("chat") or {}
                    if ch.get("id"):
                        found[ch["id"]] = ch.get("username") or ch.get("first_name") or ch.get("title") or ""
            if off:
                b.call("getUpdates", {"offset": off, "timeout": 0})
            if not found:
                print(c(RED, "no message came; run eris-bot setup again (or type the chat ID)"))
                return 1
            for cid, name in found.items():
                if ask("allow chat %s (%s)? [Y/n] " % (cid, name)).lower() in ("", "y", "yes"):
                    ids.append(cid)
        if not ids:
            print(c(RED, "no chat allowed; nothing changed"))
            return 1
        config.save({"token": token, "proxy": conf["proxy"], "allowed_chat_ids": ids})
        print(c(GREEN, "saved %s (mode 600)" % config.CONFIG))
        pw = config.R(conf["backup_password_file"]) if config.ROOT else conf["backup_password_file"]
        if not (os.path.isfile(pw) and os.path.getsize(pw) > 0):
            if ask("set the backup password now? [Y/n] ").lower() in ("", "y", "yes"):
                cmd_password(config.load())
    finally:
        systemctl("enable", "eris-bot")
        systemctl("restart", "eris-bot")
    print("eris-bot runs. Send /start to @%s." % me.get("username"))
    return 0


def cmd_proxy(conf, args):
    if not args:
        print("proxy=%s" % (conf["proxy"] if conf["proxy"] in ("auto", "off") else net.describe(conf["proxy"])))
        print("proxy_list: %s" % ", ".join(net.describe(p) for p in conf.get("proxy_list") or []))
        return 0
    if args[0] == "test":
        return cmd_test(conf)
    need_root()
    if not config.validate_proxy(args[0]):
        print("eris-bot proxy: off, auto, http://host:port or socks5://[user:pass@]host:port")
        return 2
    config.save({"proxy": args[0]})
    print("proxy=%s (eris-bot picks it up at once)" % (args[0] if args[0] in ("auto", "off") else net.describe(args[0])))
    return cmd_test(config.load(), every=False)


def http_get(url, conf, timeout=60, max_redirects=5):
    """GET through the bot's way out (auto: direct first, then each proxy), following redirects."""
    last = None
    for r in routes(conf):
        u = url
        try:
            for _ in range(max_redirects):
                resp = net.request("GET", u, proxy=r, headers={"User-Agent": "eris-bot/%s" % VERSION,
                                                                "Accept": "application/vnd.github+json, */*"},
                                   timeout=20, read_timeout=timeout)
                if resp.status in (301, 302, 303, 307, 308):
                    u = resp.headers.get("Location") or resp.headers.get("location")
                    continue
                if resp.status != 200:
                    raise OSError("HTTP %d from %s" % (resp.status, u.split("?")[0]))
                return resp.body
        except (OSError, ValueError) as e:
            last = e
    raise OSError(str(last) if last else "no route")


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def cmd_update(conf, args):
    need_root()
    try:
        tag = json.loads(http_get("https://api.github.com/repos/%s/releases/latest" % REPO, conf, 30))["tag_name"]
    except (OSError, ValueError, KeyError) as e:
        print(c(RED, "eris-bot update: GitHub did not answer (%s)" % e))
        return 1
    print("installed %s, latest release %s" % (VERSION, tag))
    if "--check" in args:
        return 0 if version_tuple(tag) <= version_tuple(VERSION) else 10
    if version_tuple(tag) <= version_tuple(VERSION) and "--force" not in args:
        print(c(GREEN, "up to date"))
        return 0
    url = "https://github.com/%s/releases/download/%s/install-eris-bot.sh" % (REPO, tag)
    try:
        data = http_get(url, conf, 300)
    except OSError as e:
        print(c(RED, "download failed: %s" % e))
        return 1
    if not data.startswith(b"#!/bin/bash") or b"eris-bot installer" not in data[:2048]:
        print(c(RED, "%s is not the eris-bot installer" % url))
        return 1
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "install-eris-bot.sh")
        with open(path, "wb") as f:
            f.write(data)
        print("installing %s ..." % tag)
        return subprocess.call(["bash", path])


def cmd_selftest():
    ok = True
    lib = os.path.dirname(os.path.abspath(__file__))
    for f in sorted(os.listdir(lib)):
        if f.endswith(".py"):
            try:
                compile(open(os.path.join(lib, f), encoding="utf-8").read(), f, "exec")
            except SyntaxError as e:
                print("FAIL python syntax %s: %s" % (f, e))
                ok = False
    if os.path.exists(config.CONFIG):
        mode = os.stat(config.CONFIG).st_mode & 0o777
        print("%s %s mode %o" % ("PASS" if mode == 0o600 else "FAIL", config.CONFIG, mode))
        ok &= mode == 0o600
    try:
        from . import app  # noqa: F401
        print("PASS modules import")
    except Exception as e:  # noqa: B902
        print("FAIL import: %s" % e)
        ok = False
    print("selftest %s" % ("passed" if ok else "FAILED"))
    return 0 if ok else 1


def main(argv):
    cmd = argv[0] if argv else "status"
    args = argv[1:]
    conf = config.load()
    if cmd in ("version", "--version"):
        print(VERSION)
        return 0
    if cmd == "run":
        need_root()
        from .app import App
        return App().run_forever()
    if cmd == "fan-daemon":
        need_root()
        from . import fand
        return fand.run_daemon()
    if cmd == "fan-restore":
        from . import fand
        return fand.restore_from_file()
    if cmd == "profile-reset":
        need_root()
        from . import perf
        for line in perf.reset_default():
            print(line)
        return 0
    if cmd == "setup":
        return cmd_setup(conf)
    if cmd == "status":
        return cmd_status(conf)
    if cmd == "test":
        return cmd_test(conf)
    if cmd == "proxy":
        return cmd_proxy(conf, args)
    if cmd == "password":
        need_root()
        return cmd_password(conf)
    if cmd == "logs":
        n = args[args.index("-n") + 1] if "-n" in args and args.index("-n") + 1 < len(args) else "100"
        cmd_ = ["journalctl", "-u", "eris-bot", "-u", "eris-fan", "-n", n, "--no-pager"] + (["-f"] if "-f" in args else [])
        os.execvp(cmd_[0], cmd_)
    if cmd in ("update", "upgrade"):
        return cmd_update(conf, args)
    if cmd == "selftest":
        return cmd_selftest()
    print(__doc__.strip())
    return 0 if cmd in ("help", "-h", "--help") else 2


def run():
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print()
        sys.exit(130)
    except Exception as e:  # noqa: B902 - never print a token in a traceback
        log("eris-bot: %r", e)
        sys.exit(1)
