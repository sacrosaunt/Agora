"""The read-only code snapshot: listing, excluding and copying the source."""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time  # noqa: F401
from .compat import *  # noqa: F401,F403
from .ws import *  # noqa: F401,F403
from .store import *  # noqa: F401,F403


def sync():
    """Refresh the code snapshot from SRC (serialized: concurrent one-shot runs would race on the directory swap)."""
    if SRC and not os.path.isdir(SRC):
        sys.exit(f"agora: the source folder {SRC} is gone; run agora from the project folder, or --src DIR")
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, "sync.lock"), "w") as lk:
        lock(lk)
        return _sync()


def excluded(rel, pats):
    """rel (a /-separated path) is left out by pats. A glob (*, ?, [) matches the whole path or any one component
    (a leading **/ means at any depth: **/build/); a SECRETS name matches any component; other patterns are rooted
    paths matched by whole components (data drops data and data/..., not database.py). A trailing / means a
    directory only."""
    from fnmatch import fnmatch
    parts = rel.split("/")
    for p in pats:
        name, d = p.rstrip("/"), p.endswith("/")
        comps = parts[:-1] if d else parts
        if any(c in p for c in "*?["):
            bare = name[3:] if name.startswith("**/") else name
            if fnmatch(rel, name) or any(fnmatch(c, bare) for c in comps) \
                    or "/" in bare and any(fnmatch("/".join(parts[i:i + bare.count("/") + 1]), bare)
                                           for i in range(len(comps) - bare.count("/"))):
                return True
        elif p in SECRETS and name in comps:
            return True
        elif rel.startswith(name.lstrip("/") + "/") or not d and rel == name.lstrip("/"):
            return True
    return False


class TooBig(Exception):
    pass


def list_files(src):
    """src's files as /-separated relative paths: git-tracked plus untracked-not-ignored, for a plain folder (no
    git) every file .gitignore files don't exclude."""
    if git_top(src):
        cmd = ["git", "-C", src, "ls-files", "-z", "-co", "--exclude-standard"]
    else:   # a throwaway git dir lets git walk the folder and apply any .gitignore files in it
        g = os.path.join(STATE, "plain.git")
        if not os.path.isdir(g):
            subprocess.run(["git", "init", "-q", "--bare", g], capture_output=True, check=True)
        cmd = ["git", "--git-dir", g, "--work-tree", src, "-c", "core.bare=false", "ls-files", "-z", "-o",
               "--exclude-standard"]
    return [f for f in subprocess.run(cmd, capture_output=True, check=True, cwd=src).stdout.decode(
        errors="surrogateescape").split("\0") if f]


def _snap(src, dest, exclude=()):
    """Copy src's files (see list_files) minus exclude and SECRETS into dest, swapped in whole; src None makes
    an empty snapshot. Over the size limits it raises TooBig and the old snapshot stays.
    Returns (file count, content digest, HEAD line)."""
    pats = tuple(exclude) + SECRETS
    files = list_files(src) if src else []
    root = os.path.realpath(src) + os.sep if src else ""
    big = float(CFG.get("max_file_mb", 20)) * 2**20
    skipped = []

    def ok(f):   # a regular file, not excluded, and (symlinks resolved) still inside the source, not excluded
        p = os.path.join(src, f)
        real = os.path.realpath(p)
        if excluded(f, pats) or not os.path.isfile(p) or not real.startswith(root) \
                or excluded(real[len(root):].replace(os.sep, "/"), pats) \
                or real.startswith(DATA + os.sep) or real.startswith(WS + os.sep):
            return False
        if os.path.getsize(p) > big:
            skipped.append(f)
            return False
        return True
    files = [f for f in files if ok(f)]
    max_files, max_mb = int(CFG.get("max_files", 20000)), float(CFG.get("max_mb", 500))
    if len(files) > max_files:
        raise TooBig(f"{len(files)} files (limit {max_files}, config max_files)")
    dig, total = __import__("hashlib").sha1(), 0
    for f in files:
        st = os.stat(os.path.join(src, f))
        total += st.st_size
        dig.update(f"{f}\0{st.st_size}\0{st.st_mtime_ns}\n".encode(errors="surrogateescape"))
    if total > max_mb * 2**20:
        raise TooBig(f"{total / 2**20:.0f} MB (limit {max_mb:.0f} MB, config max_mb)")
    new, old = dest + ".new", dest + ".old"
    rmtree(new)
    for f in files:
        d = os.path.join(new, f)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy2(os.path.join(src, f), d)
    os.makedirs(new, exist_ok=True)
    git = bool(src and git_top(src))
    head = subprocess.run(["git", "-C", src, "log", "-1", "--format=%h %s"], capture_output=True,
                          text=True).stdout if git else "(not a git repo)\n"
    hist = git and HISTORY and subprocess.run(["git", "clone", "--bare", "--quiet", src, os.path.join(new, ".git")],
                                              capture_output=True).returncode == 0
    if hist:   # the history, read-only: git log / show / diff / blame work in the snapshot
        subprocess.run(["git", "-C", new, "config", "core.bare", "false"], capture_output=True)
        subprocess.run(["git", "-C", new, "reset", "-q"], capture_output=True)   # index from HEAD; files untouched
    with open(os.path.join(new, "SNAPSHOT.txt"), "w") as fh:
        fh.write(f"Snapshot of {src} at {time.strftime('%Y-%m-%d %H:%M:%S')}, HEAD {head}"
                 f"{len(files)} files; excluded: {', '.join(exclude) or 'nothing'} plus "
                 + ("gitignored files" if git else "files .gitignore files exclude")
                 + ", secrets-like files (.env*, *.pem, *.key, ...)"
                 + (f" and {len(skipped)} files over {big / 2**20:.0f} MB" if skipped else "") + ".\n"
                 + ("Git history is in .git (git log/show/diff/blame); `git status` shows the excluded files as "
                    "deleted.\n" if hist else "") if src else
                 f"No source folder: this discussion has no code ({time.strftime('%Y-%m-%d %H:%M:%S')}).\n")
    rmtree(old)
    if os.path.exists(dest):
        rename(dest, old)
    rename(new, dest)
    rmtree(old)
    return len(files), dig.hexdigest(), head


def _sync():
    try:
        n, dig, head = _snap(SRC, CODE, EXCLUDE)
    except TooBig as e:
        n = None
        print(f"agora: not refreshing the snapshot of {SRC}: {e}. Exclude big folders (`agora setup`, or "
              f"\"exclude\" in {CONFIG}); the previous snapshot stays.", file=sys.stderr)
        if not os.path.isdir(CODE):
            sys.exit(1)
        return None
    for name, path in ALSO.items():
        if not os.path.isdir(os.path.join(path, ".git")) or os.path.realpath(path) == SRC:
            print(f"agora: skipping also.{name}: {path} is not a separate git repo", file=sys.stderr)
            continue
        try:
            dig += _snap(path, os.path.join(ALSO_DIR, name))[1]
        except TooBig as e:
            print(f"agora: not refreshing also.{name}: {e}", file=sys.stderr)
    if os.path.isdir(ALSO_DIR):  # drop snapshots of repos no longer configured
        for d in os.listdir(ALSO_DIR):
            if d not in ALSO:
                rmtree(os.path.join(ALSO_DIR, d))
    with open(os.path.join(STATE, "snapshot.json"), "w") as fh:  # content digest: sessions note real changes only
        json.dump({"digest": __import__("hashlib").sha1(dig.encode()).hexdigest()[:12], "head": head.strip()}, fh)
    return n


__all__ = [k for k in list(globals()) if not k.startswith("__")]
