#!/usr/bin/env python3
"""A stand-in for ERIS Console 2.5's `eris` command (tests): backup --json, encrypt, restore, fw.

State lives in $FAKE_ERIS_DIR: backups/<part>/*.tar, failed.json, fw.txt, calls.log.
"""
import hashlib
import json
import os
import subprocess
import sys
import time

D = os.environ["FAKE_ERIS_DIR"]
B = os.path.join(D, "backups")
PARTS = ("marzban", "x-ui", "xray-site", "jellyfin", "eris", "claude-f-web", "system")
PRESENT = os.environ.get("FAKE_PRESENT", "marzban,x-ui,xray-site,eris,system").split(",")
SIZE = int(os.environ.get("FAKE_SIZE", "4096"))


def log(*a):
    with open(os.path.join(D, "calls.log"), "a") as f:
        f.write(" ".join(a) + "\n")


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def archives(part):
    d = os.path.join(B, part)
    if not os.path.isdir(d):
        return []
    out = [os.path.join(d, n) for n in os.listdir(d) if n.endswith(".tar")]
    return sorted(out, key=lambda p: (os.path.getmtime(p), p), reverse=True)


def failed():
    try:
        return json.load(open(os.path.join(D, "failed.json")))
    except (OSError, ValueError):
        return {}


def describe(p, n):
    return {"n": n, "path": p, "name": os.path.basename(p), "epoch": int(os.path.getmtime(p)),
            "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(p))), "size": os.path.getsize(p),
            "sha256": open(p + ".sha256").read().split()[0] if os.path.exists(p + ".sha256") else None,
            "label": "before-restore" if "before-restore" in p else ""}


def make(part):
    os.makedirs(os.path.join(B, part), exist_ok=True)
    name = "host-%s-backup-%s-%d.tar" % (part, time.strftime("%Y%m%d-%H%M%S"), len(archives(part)))
    p = os.path.join(B, part, name)
    with open(p, "wb") as f:
        f.write(os.urandom(SIZE))
    with open(p + ".sha256", "w") as f:
        f.write("%s  %s\n" % (sha(p), name))
    return p


def main(a):
    log(*a)
    if a == ["version"]:
        print("2.5.0")
        return 0
    if a[:1] == ["backup"]:
        sub = a[1]
        if sub == "status":
            fl = failed()
            parts = {}
            for p in PARTS:
                arcs = archives(p)
                st = {"title": p, "present": p in PRESENT, "enabled": True, "count": len(arcs), "folder": os.path.join(B, p)}
                if arcs:
                    st.update(last_ok=arcs[0], last_ok_epoch=int(os.path.getmtime(arcs[0])), last_size=os.path.getsize(arcs[0]))
                if p in fl:
                    st.update(last_error=fl[p], last_error_epoch=int(time.time()))
                st["failed"] = st.get("last_error_epoch", 0) > (st.get("last_ok_epoch") or 0)
                parts[p] = st
            print(json.dumps({"backup_dir": B, "dir_ok": True, "at": "03:30", "part_keep": 7,
                              "full": {"enabled": True, "last_ok_epoch": int(time.time()) - 3600, "failed": False},
                              "parts": parts, "timer": {"state": "active"}, "eris": "2.5.0"}))
            return 0
        if sub == "list":
            want = a[a.index("--part") + 1] if "--part" in a else None
            groups = {}
            for p in PARTS:
                if want and p != want:
                    continue
                groups[p] = [describe(x, i + 1) for i, x in enumerate(archives(p))]
            print(json.dumps({"backup_dir": B, "keep": 7, "part_keep": 7, "groups": groups}))
            return 0
        if sub == "now":
            parts = a[a.index("--part") + 1].split(",")
            res, fl = [], failed()
            for p in parts:
                if p not in PRESENT:
                    res.append({"part": p, "ok": False, "skipped": True, "error": "not on this server"})
                elif p in fl:
                    res.append({"part": p, "ok": False, "error": fl[p]})
                else:
                    path = make(p)
                    res.append({"part": p, "ok": True, "path": path, "size": os.path.getsize(path), "sha256": sha(path),
                                "files": 12, "databases": 1 if p in ("marzban", "x-ui") else 0, "message": "12 files"})
            ok = all(r["ok"] or r.get("skipped") for r in res)
            print(json.dumps({"ok": ok, "results": res}))
            return 0 if ok else 1
        if sub == "verify":
            p = a[2]
            print(json.dumps({"ok": True, "message": "12 files, 1 databases", "files": 12, "databases": 1, "path": p,
                              "sha256": sha(p), "size": os.path.getsize(p)}))
            return 0
        if sub == "encrypt":
            src = a[2]
            pw = a[a.index("--password-file") + 1]
            out = a[a.index("--out") + 1]
            r = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-in", src, "-out", out,
                                "-pass", "file:" + pw], capture_output=True, text=True)
            if r.returncode:
                print(r.stderr)
                return 1
            os.chmod(out, 0o600)
            print(out)
            return 0
    if a[:1] == ["restore"]:
        print("  the present state is saved first: x\n  done: 3 files, 1 databases")
        return 0
    if a[:1] == ["fw"]:
        st = os.path.join(D, "fw.txt")
        text = open(st).read() if os.path.exists(st) else "ERIS firewall: not set up on this server (optional): eris fw setup"
        if len(a) == 1:
            print(text)
            return 0
        if a[1] == "confirm":
            open(st, "w").write(text.replace("  PENDING x: live, not confirmed; the previous rules come back in 60 s unless: eris fw confirm\n", ""))
            print("  kept: x")
            return 0
        if "22/tcp" in a:
            print("  careful: SSH\n  not changed: to do it anyway without a terminal, add --open-ssh")
            return 1
        if "8080/tcp" in a and "--force" not in a:
            print("  careful: admin panel\n  not changed: to do it anyway without a terminal, add --force")
            return 1
        lines = text.split("\n")
        lines.insert(1, "  PENDING x: live, not confirmed; the previous rules come back in 60 s unless: eris fw confirm")
        open(st, "w").write("\n".join(lines))
        print("  loaded: the new rules are live\n  keep it with: eris fw confirm   (within 60 s, otherwise the previous rules come back)")
        return 0
    print("unknown", a)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
