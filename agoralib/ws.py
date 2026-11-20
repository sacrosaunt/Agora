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
FAMILIES = ("claude", "codex", "gemini")   # the CLIs; each serves one seat named after it, plus any "seats" in the config
NAMES, AGENTS = ("user",) + FAMILIES, FAMILIES   # set from the config's seats once it is loaded (below)
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


def find_ws(x):
    """A workspace by its name (as `agora ls` lists it), its dir, or the folder it was made for (where that folder is
    now, or where it was before it moved); None if there is none."""
    if re.fullmatch(r"[\w.-]+-[0-9a-f]{8}", x) and os.path.isfile(os.path.join(DATA, x, "workspace.json")):
        return os.path.join(DATA, x)
    p = os.path.realpath(os.path.expanduser(x))
    if p.startswith(DATA + os.sep) and os.path.isfile(os.path.join(p, "workspace.json")):
        return p
    keys = {os.path.normcase(k) for k in (p, git_top(p) if os.path.isdir(p) else None) if k}
    for d in sorted(os.listdir(DATA)) if os.path.isdir(DATA) else ():
        if os.path.normcase(_json(os.path.join(DATA, d, "workspace.json")).get("src") or "") in keys:
            return os.path.join(DATA, d)
    return None


def resolve_ws():
    """(workspace dir, source dir or None, how). The workspace holds the thread, state, snapshots and work dirs;
    each source folder gets its own. Legacy: an agora.json with "src" next to the script keeps the old layout
    (everything in the install dir) for that source."""
    o = _ws_args()
    rest = [x for x in sys.argv[1:] if not x.startswith("-")]
    if rest[:1] == ["ls"] or not rest and {"-h", "--help"} & set(sys.argv[1:]) or rest[:1] == ["skill"]:
        return os.path.join(DATA, ".none"), None, "none"   # needs no workspace: don't create one
    if rest[:1] == ["move"]:   # `agora move FROM [TO]`: the workspace is FROM's, wherever this runs
        w = find_ws(rest[1]) if len(rest) > 1 else None
        return (w or os.path.join(DATA, ".none")), (_json(os.path.join(w, "workspace.json")).get("src") if w else None), "move"
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
RESERVED = {"user", "all", "via", "chat", "post", "read", "wait", "watch", "skill", "idle", "doing", "sleep", "wake",
            "hook", "protocol", "intro", "decide", "decisions", "sync", "status", "update", "setup", "where", "ls",
            "move", "add", "remove", "stop", "restart", "reveal", "reset", "new", "serve"}


ALIAS_POOL = ("arlo", "bexa", "cyra", "dovin", "elko", "fenn", "gilo", "hask", "ivra", "jorn", "kael", "lunt",
              "mirel", "nesk", "orvo", "pell", "quorin", "rusk", "sorel", "tavi", "ulmo", "vesk", "wren", "yarro")


def seat_name_error(n, taken=()):
    """Why n can't name a seat ("" when it can)."""
    if not re.fullmatch(r"[a-z][a-z0-9.-]{0,31}", n or "") or n.endswith((".", "-")):
        return "a seat name is 1-32 of a-z, 0-9, . and -, starting with a letter"
    if n in RESERVED or n in ALIAS_POOL or n in FAMILIES:
        return f"{n!r} is taken (a command, family or alias name)"
    return f"{n!r} is already a seat" if n in taken else ""


def load_seats(cfg):
    """{seat: {"family", "model"?, "effort"?}}: one seat per family (named after it), then the config's "seats":
    {"claude-sonnet": {"family": "claude", "model": "sonnet"}, ...}, several models of one family side by side.
    A malformed seat is left out with a warning."""
    out = {f: {"family": f} for f in FAMILIES}
    for n, v in (cfg.get("seats") or {}).items():
        n = str(n).lower()
        fam = (v or {}).get("family") if isinstance(v, dict) else None
        bad = seat_name_error(n) or ("" if fam in FAMILIES else f"family must be one of {', '.join(FAMILIES)}")
        if bad:
            print(f"agora: ignoring seat {n!r}: {bad}", file=sys.stderr)
            continue
        out[n] = {k: v[k] for k in ("family", "model", "effort") if v.get(k)}
    return out


SEATS = load_seats(CFG)
AGENTS = tuple(SEATS)
NAMES = ("user",) + AGENTS


def family(name):
    """The CLI behind a seat: claude, codex or gemini."""
    return SEATS.get(name, {}).get("family", name)


ANON = str(os.environ.get("AGORA_ANONYMIZE", CFG.get("anonymize", True))).lower() not in ("false", "0", "no", "off")
ALIASES = os.path.join(STATE, "aliases.json")   # {seat: alias}: what the agents call each other in this workspace


def aliases():
    """{seat: alias} for every seat (an identity map when anonymize is off). Assigned at random the first time a
    seat is seen and kept for the workspace, so sessions see stable names that say nothing about the models."""
    if not ANON:
        return {n: n for n in AGENTS}
    have = _json(ALIASES)
    if all(n in have for n in AGENTS):
        return {n: have[n] for n in AGENTS}
    os.makedirs(STATE, exist_ok=True)
    with open(ALIASES + ".lock", "a") as lk:
        lock(lk)
        have = _json(ALIASES)
        import random
        free = [a for a in ALIAS_POOL if a not in have.values() and a not in SEATS]
        random.SystemRandom().shuffle(free)
        for n in AGENTS:
            if n not in have:
                have[n] = free.pop() if free else f"agent{len(have) + 1}"
        with open(tmpname(ALIASES), "w") as f:
            json.dump(have, f)
        replace(tmpname(ALIASES), ALIASES)
    return {n: have[n] for n in AGENTS}


def _names_re(names):
    """A whole participant name: not inside an identifier, path or longer seat name (claude vs claude-sonnet,
    .claude/, claude_x); longest first."""
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(r"(?<![\w./-])(" + alt + r")(?![\w/]|[.-]\w)", re.I)


def enc(text):
    """Text for the models: every seat name becomes its alias (the thread keeps the real names)."""
    al = aliases()
    if not ANON or not text:
        return text
    return _names_re(al).sub(lambda g: al[g.group(1).lower()], text)


def dec(text):
    """Text from the models: aliases back to the seat names."""
    back = {a: n for n, a in aliases().items()}
    if not ANON or not text:
        return text
    return _names_re(back).sub(lambda g: back[g.group(1).lower()], text)


def dec_name(n):
    """A participant name as given by anyone (seat name or alias) as the seat name; others unchanged."""
    n = (n or "").strip().lstrip("@").lower()
    return {a: s for s, a in aliases().items()}.get(n, n)


def seat_label(name):
    """A seat for people: its name, and its model when set (the agents never see this)."""
    m = os.environ.get(f"AGORA_{name.upper().replace('-', '_').replace('.', '_')}_MODEL") or SEATS.get(name, {}).get(
        "model") or CFG.get(f"{name}_model")
    return f"{name} ({m})" if m else name


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


DESCRIBE = {"claude": "Claude Code (Anthropic)", "codex": "Codex CLI (OpenAI)",
            "gemini": "the Antigravity CLI (`agy`)"}


def protocol():
    """The protocol for the agents. Anonymized (the default): the agents appear under their aliases, with
    nothing about the CLI or model behind each one, so they weigh claims by evidence, not by who made them."""
    al = aliases()
    names = [al[n] for n in AGENTS]
    if ANON:
        roster = ("- " + ", ".join(f"**{x}**" for x in names) + ": the coding agents. Which model is behind each "
                  "name is not disclosed; don't guess at it, and judge every claim by its evidence alone.")
    else:
        roster = "\n".join(f"- **{n}**: {DESCRIBE[family(n)]}" + (f", model {SEATS[n]['model']}"
                            if SEATS[n].get("model") else "") + "." for n in AGENTS)
    s = template("PROTOCOL.md", agora=AGORA, code=CODE, roster=roster,
                 agents=", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0],
                 rotation=" → ".join(names))
    if os.path.exists(PROJECT):
        with open(PROJECT) as f:
            s = s.rstrip() + "\n\n" + enc(f.read())
    return s

def auto():
    """The agents the serve loop runs headlessly here, read fresh from the config (so `agora add` / `remove` and
    /add, /remove in a chat take effect at once). No "serve" key: all of them; an empty list: none."""
    s = serve_cfg()
    only = _json(os.path.join(STATE, "serve.json")).get("only")   # `agora serve --only` narrows it further
    return tuple(n for n in s if not only or n in only.split(","))


def serve_cfg():
    """The config's "serve" list alone (all agents when absent), before any `serve --only`."""
    cfg = _json(ICONFIG) if WS == INSTALL else {**_json(ICONFIG), **_json(CONFIG)}
    s = cfg.get("serve")
    return tuple(n for n in AGENTS if n in s) if isinstance(s, list) else AGENTS


__all__ = [k for k in list(globals()) if not k.startswith("__")]
