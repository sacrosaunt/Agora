"""where, ls and the interactive setup."""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time  # noqa: F401
from .compat import *  # noqa: F401,F403
from .ws import *  # noqa: F401,F403
from .store import *  # noqa: F401,F403
from .snapshot import *  # noqa: F401,F403
from .agents import *  # noqa: F401,F403
from .serve import *  # noqa: F401,F403
from .chat import *  # noqa: F401,F403
from .converse import *  # noqa: F401,F403


def where():
    print(f"workspace  {WS}" + ("  (legacy: in the install dir)" if WS == INSTALL else ""))
    print(f"source     {SRC or '(none: a discussion without code)'}"
          + ("" if not SRC else "  (git repo)" if git_top(SRC) else "  (plain folder)"))
    print(f"install    {INSTALL}")
    print(f"config     {CONFIG}" + ("" if WS == INSTALL else f" over {ICONFIG}"))
    print(f"thread     {MD}")


def list_ws():
    rows = [(INSTALL, _json(ICONFIG).get("src"))] if _json(ICONFIG).get("src") else []
    if os.path.isdir(DATA):
        rows += [(os.path.join(DATA, d), _json(os.path.join(DATA, d, "workspace.json")).get("src"))
                 for d in sorted(os.listdir(DATA)) if os.path.isfile(os.path.join(DATA, d, "workspace.json"))]
    for d, src in rows:
        t = os.path.join(d, "thread.jsonl")
        n = sum(1 for _ in open(t)) if os.path.exists(t) else 0
        mt = os.path.getmtime(t) if n else 0
        when = time.strftime("%Y-%m-%d ", time.localtime(mt)) + hhmm(mt, False) if n else "-"
        print(f"{'*' if d == WS else ' '} {os.path.basename(d):40s} {n:5d} msgs  last {when}  {src or '(no source)'}")


def hint_other_threads(hours=24):
    """This folder's thread is empty: point at threads active lately, since an agent working on this project from
    another folder (a sibling copy, a differently named checkout) talks there, not here."""
    if not os.path.isdir(DATA):
        return
    rows = []
    for d in os.listdir(DATA):
        w, t = os.path.join(DATA, d), os.path.join(DATA, d, "thread.jsonl")
        if w != WS and os.path.isfile(os.path.join(w, "workspace.json")) and os.path.exists(t) \
                and time.time() - os.path.getmtime(t) < hours * 3600:
            rows.append((os.path.getmtime(t), w, _json(os.path.join(w, "workspace.json")).get("src")))
    if not rows:
        return
    print(f"This folder's thread ({SRC or WS}) is empty. Active in the last {hours} h:")
    for mt, w, src in sorted(rows, reverse=True)[:5]:
        print(f"  {hhmm(mt, False)}  {src or w}\n           open it: run agora in that folder, or agora --dir {w}")


def ask(q, default=""):
    try:
        r = input(f"{q} " + (f"[{default}] " if default and not q.endswith("]") else "")).strip()
    except EOFError:
        print()
        return default
    return r or default


def pick(title, options, current):
    """Numbered choice from options [(value, note)]; Enter keeps current, or type any value."""
    print(f"\n{title}" + (f" (now: {current})" if current else ""))
    for i, (v, note) in enumerate(options, 1):
        print(f"  {i:2d}. {v}" + (f"  {note}" if note else ""))
    r = ask("  number or name, Enter keeps it:")
    if r.isdigit() and 1 <= int(r) <= len(options):
        return options[int(r) - 1][0]
    return r or current


UTF8 = {"encoding": "utf-8", "errors": "replace"}   # Windows would decode as cp1252 and drop the output


def cli_models(name):
    """[(model, note)] the CLI offers, best effort ([] when it can't say), and {model: [efforts]}."""
    try:
        if name == "codex":
            r = subprocess.run(["codex", "debug", "models"], capture_output=True, timeout=60, **UTF8,
                               env=agent_env("codex"))
            ms = [m for m in json.loads(r.stdout or "{}").get("models", []) if m.get("visibility") == "list"]
            return ([(m["slug"], m.get("description", "")) for m in ms],
                    {m["slug"]: [e["effort"] for e in m.get("supported_reasoning_levels", [])] for m in ms})
        if name == "gemini":
            r = subprocess.run([AGY, "models"], capture_output=True, timeout=60, **UTF8)
            return [tuple((l.split("\t") + [""])[:2]) for l in r.stdout.splitlines() if "\t" in l], {}
        h = subprocess.run([CLAUDE, "--help"], capture_output=True, timeout=60, **UTF8).stdout or ""
        m, e = re.search(r"--model <model>(.*?)\n  -", h, re.S), re.search(r"--effort <level>.*?\((.*?)\)", h, re.S)
        return ([(x, "") for x in re.findall(r"'([\w.-]+)'", m.group(1))] if m else [],
                {"*": [x.strip() for x in e.group(1).split(",")]} if e else {})
    except (OSError, ValueError, subprocess.SubprocessError):
        return [], {}


USUAL = {"data", "runs", "logs", "log", "node_modules", "dist", "build", "out", "outputs", "tmp", "checkpoints",
         "models", "wandb", "venv", ".venv", "target", "vendor", "__pycache__"}


def scan_source(src):
    """The files a snapshot could take (the same listing, secrets and per-file size skip as sync), as
    [(path, bytes)]; excludes are not applied, so each folder's full weight shows."""
    big = float(CFG.get("max_file_mb", 20)) * 2**20
    out = []
    SKIPPED.clear()
    for f in list_files(src):
        if excluded(f, SECRETS):
            continue
        try:
            n = os.path.getsize(os.path.join(src, f))
        except OSError:
            continue
        if n > big:
            SKIPPED.append(f)
        elif os.path.isfile(os.path.join(src, f)):
            out.append((f, n))
    return out


SKIPPED = []   # files scan_source left out for size


def folder_rows(files, cur):
    """Folders worth offering as excludes: every top-level folder, plus nested ones that are heavy (>= 25 MB or
    >= 1000 files) or have a usual build/data name; current excludes are kept as rows. [(pattern, idx set)]."""
    groups = {}
    for i, (f, _) in enumerate(files):
        parts = f.split("/")[:-1]
        for d in range(min(len(parts), 3)):
            groups.setdefault("/".join(parts[:d + 1]) + "/", set()).add(i)
    rows = {}
    for g, ix in groups.items():
        depth, name = g.count("/"), g.rstrip("/").split("/")[-1]
        heavy = sum(files[i][1] for i in ix) >= 25 * 2**20 or len(ix) >= 1000
        if depth == 1 or heavy and depth <= 3 or name in USUAL and depth <= 2:
            rows[g] = ix
    for p in cur:
        if p not in rows:
            rows[p] = {i for i, (f, _) in enumerate(files) if excluded(f, [p])}
    return sorted(rows.items(), key=lambda r: (-sum(files[i][1] for i in r[1]), r[0]))


def snapshot_fits(src):
    """(fits, suggestions): the snapshot would be within the limits with the current excludes and no usual
    build/data folder at the top is left in it."""
    files = scan_source(src)
    keep = [(f, n) for f, n in files if not excluded(f, EXCLUDE)]
    sugg = sorted({f.split("/")[0] + "/" for f, _ in keep if "/" in f and f.split("/")[0] in USUAL})
    fits = len(keep) <= int(CFG.get("max_files", 20000)) and sum(n for _, n in keep) <= float(CFG.get("max_mb", 500)) * 2**20
    return fits, sugg


def pick_excludes(src):
    """The excludes: a checklist of folders (suggestions and current excludes ticked) with the snapshot's live
    total against the limits; typed input when not in a terminal. None: unchanged."""
    print("\nScanning the source…")
    files = scan_source(src)
    rows = folder_rows(files, list(EXCLUDE))
    max_files, max_mb = int(CFG.get("max_files", 20000)), float(CFG.get("max_mb", 500))
    total = sum(n for _, n in files)
    sugg = [p for p, ix in rows if p.rstrip("/").split("/")[-1] in USUAL and p not in EXCLUDE
            or sum(files[i][1] for i in ix) > 50 * 2**20 and "/" not in p.rstrip("/")]
    big = f"{len(SKIPPED)} file{'s' if len(SKIPPED) != 1 else ''} over {CFG.get('max_file_mb', 20)} MB, " if SKIPPED else ""
    print(f"{len(files)} files, {total / 2**20:.1f} MB. Gitignored files, {big}and secrets (.env*, *.pem, *.key, ...) "
          "are already left out.")
    idx = dict(rows)

    def status(t):
        out = set().union(*(idx[p] for p in t)) if t else set()
        n, mb = len(files) - len(out), (total - sum(files[i][1] for i in out)) / 2**20
        over = n > max_files or mb > max_mb
        line = f"  snapshot: {n} files, {mb:.1f} MB (limits {max_files} files, {max_mb:.0f} MB)"
        return f"\033[{'31' if over else '32'}m{line}{'  over the limit' if over else ''}\033[0m"
    opts = [(p, f"{sum(files[i][1] for i in ix) / 2**20:8.1f} MB {len(ix):6d} files"
             + ("  (suggested)" if p in sugg else "")) for p, ix in rows]
    got = choose("Leave out of the snapshot:", opts, ticked=list(EXCLUDE) + sugg, empty_ok=True, status=status)
    if got is None and sys.stdin.isatty() and sys.stdout.isatty():
        return None   # cancelled: keep what there is
    if got is None:   # no terminal: typed
        for p, note in opts[:15]:
            print(f"  {p:30s} {note}")
        r = ask("Exclude (space-separated paths or globs; Enter = now + suggested, - = clear):",
                " ".join(list(EXCLUDE) + [p for p in sugg if p not in EXCLUDE]))
        return [] if r == "-" else r.split()
    return got


def setup(models=False):
    """Interactive review: what to leave out of the snapshot, which agents answer, web search; models and efforts
    only when asked for (models=True, or yes to Customize). Settings go to this workspace, and to the install's
    defaults for new workspaces when you say so."""
    print(f"Setting up agora for {SRC or 'a discussion without code'}")
    if SRC:   # 1. excludes
        ex = pick_excludes(SRC)
        if ex is not None:
            save_cfg(CONFIG, {"exclude": ex})
            print(f"Excluded: {', '.join(ex) or 'nothing'}")
    found = [n for n in AGENTS if shutil.which({"claude": CLAUDE, "codex": "codex", "gemini": AGY}[n])]
    if not found:
        print("\nNo agent CLI found (claude, codex, agy): install one, then run `agora setup` again.")
    cur = [n for n in auto() if n in found] or found
    seat = choose("\nAgents that answer automatically:", [(n, "") for n in found], ticked=cur, empty_ok=True) \
        if found else []
    if seat is None:
        seat = cur if sys.stdin.isatty() and sys.stdout.isatty() else \
            ask(f"\nAgents that answer automatically (found: {', '.join(found)}; space-separated):", " ".join(cur)).split()
    inst = {"serve": [n for n in seat if n in AGENTS]}
    print(f"Answering automatically: {', '.join(inst['serve']) or 'nobody'}")
    if inst["serve"] and (models or ask("\nCustomize models and effort (Enter keeps each CLI's default)? [y/N]",
                                        "n").lower().startswith("y")):
        for n in inst["serve"]:
            ms, efforts = cli_models(n)
            cm = opt(n, "model") or ""
            inst[f"{n}_model"] = pick(f"{n} model", ms, cm) if ms else ask(f"\n{n} model (Enter: CLI default):", cm)
            effs = efforts.get(inst[f"{n}_model"]) or efforts.get("*")
            if effs:
                inst[f"{n}_effort"] = pick(f"{n} effort", [(e, "") for e in effs], opt(n, "effort") or "")
    inst["web_search"] = ask("\nLet the agents search the web (docs, papers, release notes)? Queries go to the "
                             f"agents' web services. [{'Y/n' if web_on() else 'y/N'}]",
                             "y" if web_on() else "n").lower().startswith("y")
    if WS == INSTALL:
        save_cfg(ICONFIG, inst)
        where_to = f"saved in {ICONFIG}"
    else:
        first = not _json(ICONFIG).get("serve")   # no install-wide agents yet: these become the defaults
        if ask(f"\nAlso make these the defaults for new workspaces? [{'Y/n' if first else 'y/N'}]",
               "y" if first else "n").lower().startswith("y"):
            save_cfg(ICONFIG, inst)
            save_cfg(CONFIG, {k: None for k in inst})   # this workspace follows the defaults
            where_to = f"saved as the defaults in {ICONFIG}"
        else:
            save_cfg(CONFIG, inst)
            where_to = f"saved for this workspace only ({CONFIG})"
    home = os.path.join(INSTALL, ".codex-home")   # agora's own Codex home: your ~/.codex stays untouched
    if "codex" in inst["serve"] and not os.path.exists(os.path.join(home, "auth.json")):
        codex = shutil.which("codex")
        if codex and ask("\nCodex isn't logged in for agora yet (it keeps its own Codex home). Log in now? [Y/n]",
                         "y").lower().startswith("y"):
            os.makedirs(home, exist_ok=True)
            subprocess.run([codex, "login"], env={**os.environ, "CODEX_HOME": home})
        if not os.path.exists(os.path.join(home, "auth.json")):
            print(f"\nLog Codex in later: `agora setup` again, or CODEX_HOME={home} codex login")
    print(f"\nSettings {where_to}. `agora` joins the chat; `agora setup --models` picks models and efforts.")
    try:
        import rich  # noqa: F401
    except ImportError:
        print(f"For markdown in the chat: {venv_python(VENV) if os.path.isdir(VENV) else 'python'} -m pip install rich")


__all__ = [k for k in list(globals()) if not k.startswith("__")]
