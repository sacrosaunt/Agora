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


def setup():
    """Interactive: what to leave out of the snapshot, which agents to seat, their models and efforts."""
    where()
    if SRC:   # 1. excludes, with the biggest top-level entries and the usual suspects suggested
        print("\nScanning the source…")
        sizes, counts = {}, {}
        for f in list_files(SRC):
            if excluded(f, SECRETS):
                continue
            top = f.split("/")[0] + ("/" if "/" in f else "")
            try:
                sizes[top] = sizes.get(top, 0) + os.path.getsize(os.path.join(SRC, f))
                counts[top] = counts.get(top, 0) + 1
            except OSError:
                pass
        usual = {"data/", "runs/", "logs/", "log/", "node_modules/", "dist/", "build/", "out/", "outputs/", "tmp/",
                 "checkpoints/", "models/", "wandb/", "venv/", ".venv/", "target/", "vendor/", "__pycache__/"}
        cur = list(EXCLUDE)
        sugg = [t for t in sizes if t not in cur and (t in usual or sizes[t] > 50 * 2**20)]
        print(f"{sum(counts.values())} files, {sum(sizes.values()) / 2**20:.1f} MB. Largest:")
        for t in sorted(sizes, key=sizes.get, reverse=True)[:12]:
            print(f"  {t:30s} {sizes[t] / 2**20:8.1f} MB {counts[t]:6d} files" + ("  (suggested)" if t in sugg else "")
                  + ("  (excluded)" if t in cur else ""))
        print(f"Excluded now: {', '.join(cur) or 'nothing'} (secrets like .env*, *.pem, *.key always are)")
        r = ask("Exclude (space-separated paths or globs; Enter = now + suggested, - = clear):",
                " ".join(cur + sugg))
        save_cfg(CONFIG, {"exclude": [] if r == "-" else r.split()})
    found = [n for n in AGENTS if shutil.which({"claude": CLAUDE, "codex": "codex", "gemini": AGY}[n])]
    print(f"\nAgent CLIs found: {', '.join(found) or 'none'}")
    seat = ask("Which agents should answer automatically (space-separated)?", " ".join(n for n in auto() if n in found)
               or " ".join(found)).split()
    inst = {"serve": [n for n in seat if n in AGENTS]}
    for n in inst["serve"]:
        models, efforts = cli_models(n)
        cur = opt(n, "model") or ""
        inst[f"{n}_model"] = pick(f"{n} model", models, cur) if models else ask(f"\n{n} model (Enter: CLI default):", cur)
        effs = efforts.get(inst[f"{n}_model"]) or efforts.get("*")
        if effs:
            inst[f"{n}_effort"] = pick(f"{n} effort", [(e, "") for e in effs], opt(n, "effort") or "")
    inst["web_search"] = ask("\nLet the agents search the web (docs, papers, release notes)? [Y/n]",
                             "y" if web_on() else "n").lower().startswith("y")
    if WS == INSTALL or ask("\nUse these agents and models for all workspaces? [Y/n]", "y").lower().startswith("y"):
        save_cfg(ICONFIG, inst)
    else:
        save_cfg(CONFIG, inst)
    home = os.path.join(INSTALL, ".codex-home")   # agora's own Codex home: your ~/.codex stays untouched
    if "codex" in inst["serve"] and not os.path.exists(os.path.join(home, "auth.json")):
        codex = shutil.which("codex")
        if codex and ask("\nCodex isn't logged in for agora yet (it keeps its own Codex home). Log in now? [Y/n]",
                         "y").lower().startswith("y"):
            os.makedirs(home, exist_ok=True)
            subprocess.run([codex, "login"], env={**os.environ, "CODEX_HOME": home})
        if not os.path.exists(os.path.join(home, "auth.json")):
            print(f"\nLog Codex in later: `agora setup` again, or CODEX_HOME={home} codex login")
    try:
        import rich  # noqa: F401
    except ImportError:
        if ask("\nInstall rich (markdown in the chat) into agora's own .venv? [y/N]", "n").lower().startswith("y"):
            subprocess.run([sys.executable, "-m", "venv", VENV], check=True)
            subprocess.run([venv_python(VENV), "-m", "pip", "install", "-q", "-r",
                            os.path.join(INSTALL, "requirements.txt")], check=True)
    print("\nSaved. `agora` joins the chat; `agora sync` refreshes the snapshot.")


__all__ = [k for k in list(globals()) if not k.startswith("__")]
