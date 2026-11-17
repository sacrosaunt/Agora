"""Workspaces and configuration: where the thread, state and snapshot live, and what the source is."""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time  # noqa: F401
from .compat import *  # noqa: F401,F403


INSTALL = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))   # the program: venv, templates, Codex home, default config
SCRIPT = os.path.join(INSTALL, "agora")   # the launcher
# agora run as a child of this process: already in the right interpreter, and on Windows straight into UTF-8 mode, so
# the launcher doesn't start a second process (whose output a redirect, e.g. serve's log, wouldn't reach)
SELF = [sys.executable] + (["-X", "utf8"] if WINDOWS else []) + [SCRIPT]
COMMAND = os.path.join(INSTALL, "agora.cmd") if WINDOWS else SCRIPT   # what a shell (or an agent) runs
VENV = os.path.join(INSTALL, ".venv")  # optional: python -m venv .venv && .venv/bin/pip install -r requirements.txt
NAMES = ("user", "claude", "codex", "gemini")
AGENTS = ("claude", "codex", "gemini")
WS_KEYS = ("src", "exclude", "project", "also", "history")   # workspace-only config: never inherited from the install
DATA = os.path.realpath(os.path.expanduser(os.environ.get("AGORA_DATA") or os.path.join(
    (os.environ.get("LOCALAPPDATA") if WINDOWS else os.environ.get("XDG_DATA_HOME"))
    or os.path.join("~", ".local", "share"), "agora")))
SECRETS = (".env*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks", "id_rsa*", "id_dsa*", "id_ecdsa*",
           "id_ed25519*", "*credentials*", ".netrc", ".npmrc", ".pypirc", ".pgpass", ".ssh/", ".aws/", ".gnupg/",
           ".codex-home/", ".claude/")   # always left out of snapshots, at any depth


def _json(p):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def git_top(d):
    """The top of the git work tree containing d, or None."""
    try:
        r = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    except OSError:
        return None
    return os.path.realpath(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def _ws_args():
    """Pop the workspace options (--dir D, --src S, --no-src) that come before the command."""
    out, i, argv = {}, 1, sys.argv
    while i < len(argv) and argv[i].startswith("-"):
        k, eq, v = argv[i].partition("=")
        if k in ("--dir", "--src") and (eq or i + 1 < len(argv)):
            out[k[2:]] = v if eq else argv[i + 1]
            del argv[i:i + (1 if eq else 2)]
        elif k == "--no-src":
            out["no_src"] = True
            del argv[i]
        else:
            i += 2 if k == "--last" and not eq else 1
    return out


def _has_ws(d):
    return os.path.isfile(os.path.join(ws_path(d, d), "workspace.json"))


def _known_parent(d):
    """Outside git, for a folder without its own workspace: the nearest parent folder that has one, so a subfolder
    (e.g. dist/) joins the project's thread instead of starting its own; None if there is none."""
    p = os.path.dirname(d)
    while p and p != os.path.dirname(p):
        if _has_ws(p):
            return p
        p = os.path.dirname(p)
    return None


def ws_path(key, label):
    """The workspace dir for a source (or source-less folder) key."""
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(label.rstrip(os.sep)) or "root")[:40]
    return os.path.join(DATA, f"{base}-{__import__('hashlib').sha1(key.encode()).hexdigest()[:8]}")


def resolve_ws():
    """(workspace dir, source dir or None, how). The workspace holds the thread, state, snapshots and work dirs;
    each source folder gets its own. Legacy: an agora.json with "src" next to the script keeps the old layout
    (everything in the install dir) for that source."""
    o = _ws_args()
    rest = [x for x in sys.argv[1:] if not x.startswith("-")]
    if rest[:1] == ["ls"] or not rest and {"-h", "--help"} & set(sys.argv[1:]) or rest[:1] == ["skill"]:
        return os.path.join(DATA, ".none"), None, "none"   # needs no workspace: don't create one
    legacy = _json(os.path.join(INSTALL, "agora.json")).get("src")
    legacy = os.path.realpath(os.path.expanduser(legacy)) if legacy else None
    src = os.path.realpath(os.path.expanduser(o["src"])) if o.get("src") else None
    if o.get("src") and o.get("no_src"):
        sys.exit("agora: --src and --no-src exclude each other")
    d = o.get("dir") or os.environ.get("AGORA_DIR")
    cwd = os.path.realpath(os.getcwd())
    if d:
        ws = os.path.realpath(os.path.expanduser(d))
    elif legacy and (src == legacy or not src and not o.get("no_src") and (
            cwd == INSTALL or cwd.startswith(INSTALL + os.sep) or (git_top(cwd) or cwd) == legacy)):
        return INSTALL, legacy, "legacy"
    else:
        ws, p = None, cwd   # inside a workspace already (e.g. an agent's work dir)?
        while not src and not o.get("no_src") and p.startswith(DATA + os.sep):
            if os.path.isfile(os.path.join(p, "workspace.json")):
                ws = p
                break
            p = os.path.dirname(p)
        if not ws:
            if o.get("no_src"):
                ws = ws_path("nosrc:" + cwd, cwd)
            else:
                src = src or git_top(cwd) or (cwd if _has_ws(cwd) else _known_parent(cwd)) or cwd
                ws = ws_path(src, src)
    if ws == INSTALL:
        return INSTALL, legacy, "legacy"
    meta_p = os.path.join(ws, "workspace.json")
    meta = _json(meta_p)
    if not meta or src or o.get("no_src"):   # new workspace, or the source given again: record it
        if not meta and not src and not o.get("no_src"):
            src = git_top(cwd) or cwd
        new = {"src": None if o.get("no_src") else src, "created": meta.get("created") or time.strftime("%Y-%m-%d %H:%M:%S")}
        if new["src"] != meta.get("src") or not meta:
            check_src(new["src"], ws)
            os.makedirs(ws, exist_ok=True)
            with open(tmpname(meta_p), "w") as f:
                json.dump(new, f, indent=1)
            replace(tmpname(meta_p), meta_p)
            return ws, new["src"], "new" if not meta else "changed"
    return ws, meta.get("src"), "found"


def check_src(src, ws):
    """Refuse sources that would snapshot agora's own storage or a whole system/home dir."""
    if not src:
        return
    home = os.path.realpath(os.path.expanduser("~"))
    def inside(a, b):
        return a == b or a.startswith(b.rstrip(os.sep) + os.sep)
    if inside(src, DATA) or inside(DATA, src) or inside(ws, src):
        sys.exit(f"agora: {src} overlaps agora's workspace storage ({DATA}); pick another folder")
    roots = {os.path.abspath(os.sep), "/usr", "/etc", "/var", "/bin", "/sbin", "/lib", "/opt", "/proc", "/sys", "/dev",
             "/boot", "/tmp", "/mnt", "/home", "/Users", "/Volumes", "/media", "/srv", "/root"}
    plain = not git_top(src)
    if src in roots or os.path.splitdrive(src)[1] in ("", os.sep) or plain and src in (home, INSTALL):
        sys.exit(f"agora: refusing to snapshot {src}: too broad. Run agora from a project folder, or use --src DIR "
                 "(or --no-src for a discussion without code)")


WS, SRC0, WS_HOW = resolve_ws()
THREAD, MD, STATE = (os.path.join(WS, x) for x in ("thread.jsonl", "thread.md", "state"))
CODE, CONFIG = os.path.join(WS, "code"), os.path.join(WS, "agora.json")
ICONFIG = os.path.join(INSTALL, "agora.json")   # install-wide defaults (models, binaries, serve, limits)
AGORA = COMMAND + ("" if WS == INSTALL else " --dir " + (WS if re.fullmatch(r"[\w./~:\\-]+", WS) else json.dumps(WS)))


def save_cfg(path, upd):
    cfg = _json(path)
    cfg.update(upd)
    for k in [k for k, v in cfg.items() if v in ("", None)]:
        del cfg[k]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmpname(path), "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    replace(tmpname(path), path)



def load_config():
    """CFG = the install defaults minus workspace-only keys, overlaid with the workspace's own agora.json."""
    global CFG
    inst = _json(ICONFIG)
    CFG = inst if WS == INSTALL else {**{k: v for k, v in inst.items() if k not in WS_KEYS}, **_json(CONFIG)}
    src = os.environ.get("AGORA_SRC") or (CFG.get("src") if WS == INSTALL else SRC0)
    ex = os.environ.get("AGORA_EXCLUDE")
    ex = [x.strip() for x in ex.split(",")] if ex is not None else CFG.get("exclude", [])
    proj = os.path.join(WS, os.path.expanduser(os.environ.get("AGORA_PROJECT") or CFG.get("project", "PROJECT.md")))
    agy = os.environ.get("AGORA_AGY") or CFG.get("agy") or shutil.which("agy") or "agy"
    return (os.path.realpath(os.path.expanduser(src)) if src else None), tuple(x for x in ex if x), proj, os.path.expanduser(agy)


SRC, EXCLUDE, PROJECT, AGY = load_config()
ALSO_DIR = os.path.join(WS, "also")  # extra repos, each snapshotted read-only to also/<name>
ALSO = {k: os.path.expanduser(v) for k, v in (CFG.get("also") or {}).items()}


def also_note():
    """Prompt text naming the extra read-only snapshots, or ''."""
    return ("Also readable (read-only snapshots): " + ", ".join(f"{k} at {os.path.join(ALSO_DIR, k)}" for k in ALSO)
            + ". ") if ALSO else ""
CLAUDE = os.path.expanduser(os.environ.get("AGORA_CLAUDE") or CFG.get("claude") or shutil.which("claude")
                            or "~/.local/bin/claude")
MAX_TURNS, MAX_TOKENS = int(CFG.get("max_turns", 40)), int(CFG.get("max_tokens", 300_000))
HISTORY = bool(CFG.get("history", True))   # put the repo's git history (.git) in the snapshots
SRC_ID = (("git:" if git_top(SRC) else "dir:") + SRC) if SRC else "none"   # sessions are tied to their source
CLOCK = {"12h": str(CFG.get("clock", "24h")).lower() in ("12h", "12")}   # how the chat shows times; /clock toggles


def hhmm(t, secs=True):
    """A time for people: t is epoch seconds or a stored "YYYY-mm-dd HH:MM:SS" stamp; 12- or 24-hour per CLOCK."""
    st = time.strptime(t, "%Y-%m-%d %H:%M:%S") if isinstance(t, str) else time.localtime(t)
    if not CLOCK["12h"]:
        return time.strftime("%H:%M:%S" if secs else "%H:%M", st)
    return time.strftime("%I:%M:%S %p" if secs else "%I:%M %p", st).lstrip("0")


def winpath(path):
    """Under WSL, (the Windows path of a Linux path, the command prefix to run this script from Windows)."""
    try:
        w = subprocess.run(["wslpath", "-w", path], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return path, ""
    m = re.match(r"\\\\wsl(?:\.localhost|\$)\\([^\\]+)", w)
    return w, (f"wsl -d {m.group(1)} " if m else "")


def template(fname, **kw):
    with open(os.path.join(INSTALL, fname)) as f:
        s = f.read()
    for k, v in kw.items():
        s = s.replace("{" + k + "}", v)
    return s


def protocol():
    s = template("PROTOCOL.md", agora=AGORA, code=CODE, thread=MD)
    if os.path.exists(PROJECT):
        with open(PROJECT) as f:
            s = s.rstrip() + "\n\n" + f.read()
    return s

AUTO = tuple(CFG.get("serve") or AGENTS)  # agents the serve loop runs headlessly


__all__ = [k for k in list(globals()) if not k.startswith("__")]
