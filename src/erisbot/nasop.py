"""One file operation on the NAS, run AS the logged-in user (setpriv drops root before this starts),
so the system's own permissions decide what is allowed. Standard library only; run with python3 -I.

    nasop.py ROOT OP ARGS...      ROOT: the shared folder every path must stay inside
    list PATH | stat PATH | mkdir DIR NAME | put DIR NAME (data on stdin) | get PATH (data to stdout)
    delete PATH | rename PATH NEWNAME
Answers one JSON object on stdout (except get), {"error": "..."} on failure (exit code 1).
"""
import json
import os
import stat
import sys

BAD_NAMES = ("", ".", "..")


def fail(msg):
    sys.stdout.write(json.dumps({"error": msg}, ensure_ascii=False))
    sys.exit(1)


def is_inside(root, path):
    real_root = os.path.realpath(root)
    real = os.path.realpath(path)
    return real == real_root or real.startswith(real_root.rstrip("/") + "/")


def inside(root, path):
    """The real path of `path` when it is root or under it (symlinks resolved), else fail."""
    if not is_inside(root, path):
        fail("outside the shared folder")
    return os.path.realpath(path)


def check_name(name):
    if name in BAD_NAMES or "/" in name or "\0" in name or len(name.encode("utf-8")) > 255:
        fail("bad name")
    return name


def entry(path, name):
    st = os.lstat(os.path.join(path, name))
    return {"name": name, "size": st.st_size, "mtime": int(st.st_mtime),
            "dir": stat.S_ISDIR(st.st_mode), "link": stat.S_ISLNK(st.st_mode)}


def op_list(root, path):
    p = inside(root, path)
    try:
        names = os.listdir(p)
    except PermissionError:
        fail("permission denied")
    except FileNotFoundError:
        fail("not found")
    items = []
    for n in names:
        try:
            e = entry(p, n)
            if e["link"]:
                if not is_inside(root, os.path.join(p, n)):
                    continue                       # a link out of the shared folder is not shown
                try:
                    t = os.stat(os.path.join(p, n))
                except OSError:
                    continue
                e.update(dir=stat.S_ISDIR(t.st_mode), size=t.st_size)
            items.append(e)
        except OSError:
            continue
    return {"path": p, "items": items, "writable": os.access(p, os.W_OK | os.X_OK)}


def op_stat(root, path):
    p = inside(root, path)
    try:
        st = os.stat(p)
    except PermissionError:
        fail("permission denied")
    except FileNotFoundError:
        fail("not found")
    return {"path": p, "size": st.st_size, "mtime": int(st.st_mtime), "dir": stat.S_ISDIR(st.st_mode),
            "readable": os.access(p, os.R_OK), "writable": os.access(os.path.dirname(p), os.W_OK | os.X_OK)}


def op_mkdir(root, d, name):
    p = os.path.join(inside(root, d), check_name(name))
    try:
        os.mkdir(p, 0o775)
    except FileExistsError:
        fail("already exists")
    except PermissionError:
        fail("permission denied")
    return {"path": p}


def free_name(d, name):
    base, ext = os.path.splitext(name)
    cand, i = name, 2
    while os.path.lexists(os.path.join(d, cand)):
        cand = "%s (%d)%s" % (base, i, ext)
        i += 1
    return cand


def op_put(root, d, name):
    d = inside(root, d)
    name = free_name(d, check_name(name))
    final = os.path.join(d, name)
    tmp = os.path.join(d, ".%s.eris-bot-upload" % name)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o664)
    except PermissionError:
        fail("permission denied")
    except FileExistsError:
        fail("an upload with this name is already running")
    size = 0
    try:
        with os.fdopen(fd, "wb") as f:
            while True:
                chunk = sys.stdin.buffer.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                size += len(chunk)
        os.rename(tmp, final)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return {"path": final, "name": name, "size": size}


def op_get(root, path):
    p = inside(root, path)
    try:
        f = open(p, "rb")
    except PermissionError:
        fail("permission denied")
    except (FileNotFoundError, IsADirectoryError):
        fail("not a file")
    with f:
        out = sys.stdout.buffer
        while True:
            chunk = f.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
        out.flush()
    sys.exit(0)


def op_delete(root, path):
    p = inside(root, path)
    if p == os.path.realpath(root):
        fail("the shared folder itself cannot be deleted")
    try:
        if os.path.isdir(p) and not os.path.islink(p):
            os.rmdir(p)
        else:
            os.remove(p)
    except OSError as e:
        fail("not empty" if e.errno == 39 else ("permission denied" if isinstance(e, PermissionError) else str(e)))
    return {"deleted": p}


def op_rename(root, path, new):
    p = inside(root, path)
    if p == os.path.realpath(root):
        fail("the shared folder itself cannot be renamed")
    dest = os.path.join(os.path.dirname(p), check_name(new))
    if os.path.lexists(dest):
        fail("already exists")
    try:
        os.rename(p, dest)
    except PermissionError:
        fail("permission denied")
    return {"path": dest}


OPS = {"list": (op_list, 1), "stat": (op_stat, 1), "mkdir": (op_mkdir, 2), "put": (op_put, 2), "get": (op_get, 1),
       "delete": (op_delete, 1), "rename": (op_rename, 2)}


def main(argv):
    if len(argv) < 3 or argv[2] not in OPS:
        fail("usage: nasop.py ROOT OP ARGS")
    fn, n = OPS[argv[2]]
    args = argv[3:]
    if len(args) != n:
        fail("wrong arguments")
    sys.stdout.write(json.dumps(fn(argv[1], *args), ensure_ascii=False))


if __name__ == "__main__":
    os.umask(0o002)
    main(sys.argv)
