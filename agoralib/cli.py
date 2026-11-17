"""agora: a tiny shared discussion thread for several coding agents reading one codebase.

Participants: user, claude, codex, gemini (Antigravity). Messages live in thread.jsonl (source of truth) and are
rendered to thread.md. Everyone reads the code from a read-only snapshot (code/) of the source folder: for a git
repo its tracked + untracked-not-ignored files plus its history, for a plain folder the files no .gitignore
excludes; never secrets-like files (.env*, *.pem, *.key, id_rsa*, *credentials*, ...).

Workspaces: run agora in a project folder and it uses that folder's workspace (thread, state, snapshot), kept in
$AGORA_DATA, else $XDG_DATA_HOME/agora, else ~/.local/share/agora: one per git repo (or plain folder). Options
before the command: --src DIR (discuss DIR), --no-src (a discussion without code), --dir WS (that workspace;
env AGORA_DIR). Legacy: an agora.json with "src" next to this script keeps that source's workspace here.

Config: agora.json next to this script holds install-wide defaults (agents, models, binaries, limits); the
workspace's own agora.json overrides them and alone holds src-related keys (exclude, project, also, history).
`agora setup` writes both. Env AGORA_SRC and AGORA_EXCLUDE (comma-separated) override.
  exclude  left out of the snapshot: path prefixes (data/, runs/; a trailing / means a directory) or globs
           matching the path or any component (*.log, *.egg-info/); gitignored files are always left out
  project  markdown file with project notes appended to the protocol (default PROJECT.md in the workspace)
  agy      Antigravity CLI binary (default: agy on PATH)
  claude   Claude Code CLI binary (default: claude on PATH, else ~/.local/bin/claude)
  also     {"name": "/path/to/repo", ...}: extra repos the agents may read, snapshotted to also/<name>
  history  put the git history (.git) into the snapshots (default true)
  max_files, max_mb, max_file_mb   snapshot limits (20000 files, 500 MB; files over 20 MB are skipped): over a
           limit the snapshot isn't refreshed
  independent_timeout   seconds before an independent round is revealed with whoever answered (default 1200)
  serve    agents the serve loop runs headlessly (default: all three; drop one whose seat a person runs)
  clock    "12h" or "24h" (default): how the chat shows times; /clock in the chat switches and saves it
  autoserve  joining the chat (or posting to a served agent) starts `agora serve` in the background if none is
           running (default true; env AGORA_AUTOSERVE=0 turns it off); it exits after idle_exit seconds
           (default 1800) with nothing to do and no chat, wait or post --via attached
  max_turns, max_tokens   session rotation limits (defaults 40 turns, 300000 context tokens)
  claude_model, claude_effort, codex_model, codex_effort, gemini_model   per-agent model and reasoning effort
           (default: each CLI's own default; codex effort medium); env AGORA_CLAUDE_MODEL etc. override

Each headless agent keeps one persistent CLI session (codex thread / Antigravity conversation / Claude Code
session, all on the local logins, no API keys) and is resumed with only the messages it hasn't seen. A fresh
session (full protocol + thread) starts deterministically when there is none, after max_turns turns, past
max_tokens of context, when the protocol or project notes change, after a failed turn, or on `agora reset`.
Context means the latest model call's input, not cumulative usage. On a turn/token cap the old session first writes a
private handoff note (open questions, hypotheses, path:line facts) that goes into the fresh session's prompt.

  agora post -a NAME [-t to1,to2|all] [--re N] [TEXT... | -f FILE | - (stdin)]
                                         as user in a terminal: posts, then opens the live chat (-q: don't)
  agora [--last N]  (or: agora chat [-t to] [--last N])   join the live chat as user: shows the last N messages, follows
                                         the thread and posts what you type
  agora read [-a NAME] [--new] [--last N] [--json]   --new: only unseen messages for NAME, then mark them seen
  agora wait -a NAME [--timeout S]       block until a message for NAME arrives, print it, mark seen
  agora post --via AGENT [-t to] [--vote a,b [--rounds N]] [--timeout S] [--mode M] [TEXT | -f FILE | -]
                                         an agent speaking for the user: posts as the user, prints the discussion
                                         as it happens and returns once the question is answered (prints the
                                         outcomes and why it stopped); --vote polls every agent
  --mode audit|design|independent        (user posts) a longer structured answer, or an independent round: every
                                         agent answers blind, the answers are revealed together, then cross-checked
  agora decide [--via AGENT] TEXT        record a decision; `agora decisions` lists them (also atop THREAD.md)
  agora skill [--install DIR]            print the agent skill for `post --via` (or write DIR/agora/SKILL.md)
  agora doing -a NAME [--id ID [--bg] [--done]] TEXT   show what NAME is doing in the chat (phase, or one of
                                         several concurrent actions); wait sets it, posting clears it
  agora idle -a NAME                     clear NAME's activity
  agora sync                             refresh the code snapshot from the source folder
  agora setup                            choose excludes, the agents to seat, their models and efforts
  agora where | ls                       this folder's workspace | all workspaces
  agora claude | codex | gemini          run that agent once on the thread and post its reply
  agora serve [--budget N] [--only X]    loop: answer as the serve agents whenever a message is addressed to them
  agora reset [NAME|all]                 drop agent sessions: their next turn starts fresh
  agora new                              start an empty thread and open the chat (the old one moves to archive/<time>/)
  agora status
  agora protocol                         print the protocol (PROTOCOL.md + the project notes) with real paths
  agora intro NAME                       print the starter prompt for a seat you run by hand (SEAT.md)
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time  # noqa: F401
from .compat import *  # noqa: F401,F403
from .ws import *  # noqa: F401,F403
from .store import *  # noqa: F401,F403
from .snapshot import *  # noqa: F401,F403
from .agents import *  # noqa: F401,F403
from .serve import *  # noqa: F401,F403
from .chat import *  # noqa: F401,F403
from .converse import *  # noqa: F401,F403
from .setup import *  # noqa: F401,F403


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", help="the workspace dir to use (default: the one for this folder; env AGORA_DIR)")
    ap.add_argument("--src", help="the folder to discuss (default: this git repo, else this folder)")
    ap.add_argument("--no-src", action="store_true", help="a discussion without code")
    ap.add_argument("--last", type=int, help="with no command: join the chat showing this many earlier messages (default 20)")
    sp = ap.add_subparsers(dest="cmd")
    p = sp.add_parser("post"); p.add_argument("-a", "--as", dest="who")
    p.add_argument("--via", metavar="AGENT", help="an agent posting as the user: follows the discussion it starts "
                   "and prints it, returning once the agents have settled")
    p.add_argument("--vote", metavar="A,B", help="with --via: every agent votes between these options")
    p.add_argument("--rounds", type=int, default=1, help="with --vote: re-ask up to N rounds until unanimous")
    p.add_argument("--timeout", type=float, default=1800, help="with --via: give up after this many seconds")
    p.add_argument("-t", "--to", default="all"); p.add_argument("-f", "--file"); p.add_argument("text", nargs="*")
    p.add_argument("-q", "--no-follow", action="store_true", help="just post; don't open the live chat")
    p.add_argument("--mode", choices=MODES, help="reply shape: short (default), audit or design (longer, structured "
                   "findings), independent (every agent answers blind; the answers are revealed together, then "
                   "cross-checked)")
    p.add_argument("--independent", dest="mode", action="store_const", const="independent", help="= --mode independent")
    p.add_argument("--re", metavar="N[,N]", help="the messages this one answers (shown as ↩ #N in the header)")
    p = sp.add_parser("chat"); p.add_argument("-t", "--to", default="all")
    p.add_argument("--last", type=int, default=20, help="show this many earlier messages on joining")
    p = sp.add_parser("read"); p.add_argument("-a", "--as", dest="who"); p.add_argument("--new", action="store_true")
    p.add_argument("--last", type=int); p.add_argument("--json", action="store_true")
    p = sp.add_parser("wait"); p.add_argument("-a", "--as", dest="who", required=True)
    p.add_argument("--timeout", type=float, default=0)
    p.add_argument("--until", help="block until this participant (e.g. codex) posts, or you are pinged")
    p = sp.add_parser("skill", help="print the agent skill (SKILL.md) with this agora's paths, or install it")
    p.add_argument("--install", metavar="DIR", help="write DIR/agora/SKILL.md (e.g. ~/.claude/skills)")
    p = sp.add_parser("idle"); p.add_argument("-a", "--as", dest="who", required=True)
    p = sp.add_parser("doing"); p.add_argument("-a", "--as", dest="who", required=True)
    p.add_argument("--id", help="a concurrent action (several may run at once); without --id TEXT sets the phase")
    p.add_argument("--bg", action="store_true", help="the action runs in the background")
    p.add_argument("--done", action="store_true", help="the action with --id finished")
    p.add_argument("text", nargs="*")
    p = sp.add_parser("sleep", help="stop answering messages to all until pinged (@name or -t name)")
    p.add_argument("-a", "--as", dest="who", required=True); p.add_argument("--for", dest="dur", help="e.g. 30m, 2h")
    p.add_argument("--until", help="also wake when this participant (e.g. codex) posts next")
    p.add_argument("--via", metavar="AGENT", help="put another agent to sleep for the user, like /sleep in the chat")
    p.add_argument("why", nargs="*")
    p = sp.add_parser("wake"); p.add_argument("-a", "--as", dest="who", required=True)
    p = sp.add_parser("hook", help="Claude Code tool hook (reads hook JSON on stdin)"); p.add_argument("phase", choices=["pre", "post"])
    sp.add_parser("protocol"); p = sp.add_parser("intro", help="the starter prompt for a seat you run by hand")
    p.add_argument("name", choices=AGENTS)
    p = sp.add_parser("decide", help="record a decision (shown at the top of the thread; nobody is asked to reply)")
    p.add_argument("--via", metavar="AGENT", help="an agent recording it for the user"); p.add_argument("text", nargs="+")
    sp.add_parser("decisions", help="list the recorded decisions")
    sp.add_parser("sync"); sp.add_parser("status")
    sp.add_parser("setup", help="choose excludes, the agents to seat, and their models and efforts")
    sp.add_parser("where", help="this folder's workspace, source and config")
    sp.add_parser("ls", help="list the workspaces")
    for n in AGENTS:
        sp.add_parser(n, help=f"run {n} once headlessly on the thread")
    p = sp.add_parser("reset", help="drop agent sessions; their next turn starts fresh"); p.add_argument("name", nargs="?", default="all")
    sp.add_parser("new", help="start an empty thread; the old one is archived")
    p = sp.add_parser("serve"); p.add_argument("--budget", type=int, default=BUDGET)
    p.add_argument("--only", help="comma list of agents to serve (default: config serve, else all)")
    p.add_argument("--auto", action="store_true", help=argparse.SUPPRESS)   # started by a client: exits when idle
    a = ap.parse_args()

    if a.cmd == "setup":
        return setup()
    if a.cmd == "where":
        return where()
    if a.cmd == "ls":
        return list_ws()
    if a.cmd == "new":   # then straight into the chat on the empty thread (in a terminal)
        new_thread()
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return
        a.cmd, a.last = None, 0
    if WS_HOW == "new" and a.cmd in (None, "chat") and sys.stdin.isatty():
        print(f"New agora workspace for {SRC or 'a discussion without code'} (data in {WS}).")
        if ask("Run setup now (excludes, agents, models)? [Y/n]", "y").lower().startswith("y"):
            setup()
    if a.cmd in (None, "chat"):
        msgs = load()
        if not msgs and getattr(a, "dir", None) is None and sys.stdout.isatty():
            hint_other_threads()
        last = a.last if a.last is not None else 20
        return chat(getattr(a, "to", "all"), after=msgs[-min(last, len(msgs))]["id"] - 1 if msgs and last else None)
    if a.cmd == "post" and a.via:
        if a.who and a.who.lower() != "user":
            sys.exit("--via posts as the user; drop -a")
        text = (open(a.file).read() if a.file else sys.stdin.read() if a.text == ["-"] or not a.text
                else " ".join(a.text))
        if not text.strip():
            sys.exit("empty message")
        sys.exit(converse(a.via.lower(), a.to, text, [o.strip() for o in a.vote.split(",")] if a.vote else None,
                          max(1, a.rounds), a.timeout, a.mode))
    if a.cmd == "post":
        if a.vote:
            sys.exit("--vote needs --via AGENT")
        if not a.who:
            sys.exit("post needs -a NAME (or --via AGENT to post as the user)")
        live = a.who.lower() == "user" and not a.no_follow and sys.stdin.isatty() and sys.stdout.isatty()
        if live and not a.text and not a.file:
            return chat(a.to)
        text = (open(a.file).read() if a.file else sys.stdin.read() if a.text == ["-"] or not a.text
                else " ".join(a.text))
        if a.mode and a.who.lower() != "user":
            sys.exit("--mode is for the user's messages")
        client()   # an idle serve can't exit between this post and the check below
        who, msgs = a.who.lower(), load()
        asked = [q for q in open_rounds() if any(m["id"] == q and pinged(m, who) for m in msgs)]
        try:
            re_ = [int(x.strip().lstrip("#")) for x in a.re.split(",") if x.strip()] if a.re else []
        except ValueError:
            sys.exit("--re takes message numbers, e.g. --re 12,14")
        m = post(a.who, a.to, text, mode=a.mode, hold=asked, re_=re_)
        if not m.get("held") and any(for_me(m, x) for x in AUTO):
            ensure_serve()
        if m.get("held"):
            print(f"held: independent round #{m['held']} is open; your answer is revealed with the others'")
            return
        if a.who.lower() in NAMES:
            seen(a.who.lower(), max(seen(a.who.lower()), m["id"]))
        print(f"posted #{m['id']}")
        if live:
            chat(a.to, after=m["id"] - 1)
    elif a.cmd == "read":
        msgs = load()
        if a.new:
            if not a.who:
                sys.exit("--new needs -a NAME")
            msgs = [m for m in msgs if for_me(m, a.who) and m["id"] > seen(a.who)]
            if msgs:
                seen(a.who, msgs[-1]["id"])
        if a.last:
            msgs = msgs[-a.last:]
        print("\n".join(json.dumps(m, ensure_ascii=False) for m in msgs) if a.json else "\n".join(fmt(m) for m in msgs))
    elif a.cmd == "wait":
        t0 = time.time()
        ensure_serve()   # a waiting seat is a client: the serve stays up while it waits
        if a.until:
            sleep_set(a.who, True, why="waiting for " + a.until, until_from=a.until.lower().split(","))
        while True:
            msgs, last = load(), seen(a.who)
            if any(for_me(m, a.who) and m["id"] > last for m in msgs):
                new = [m for m in msgs if m["id"] > last and m["from"] != a.who]  # everything, not just yours
                seen(a.who, msgs[-1]["id"])
                sleep_set(a.who, False)
                if a.who != "user":
                    busy(a.who, True, "reading the new messages")
                print("\n".join(fmt(m) for m in new))
                return
            if a.timeout and time.time() - t0 > a.timeout:
                sys.exit(3)
            time.sleep(2)
    elif a.cmd == "skill":
        s = template(os.path.join("skills", "agora", "SKILL.md"), agora=COMMAND)
        if not a.install:
            print(s, end="")
        else:
            d = os.path.join(os.path.expanduser(a.install), "agora")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "SKILL.md"), "w") as f:
                f.write(s)
            print(f"wrote {os.path.join(d, 'SKILL.md')}")
    elif a.cmd == "idle":
        busy(a.who.lower(), False)
    elif a.cmd == "doing":
        if a.id:
            act(a.who.lower(), a.id, None if a.done else (" ".join(a.text) or "working"), bg=a.bg)
        else:
            busy(a.who.lower(), True, " ".join(a.text) or "thinking")
    elif a.cmd == "sleep":
        wf = a.until.lower().split(",") if a.until else []
        sleep_set(a.who.lower(), True, parse_dur(a.dur), "user" if a.via else " ".join(a.why), wf)
        print(f"{a.who} sleeps until pinged" + (f" or {a.until} posts" if wf else "") + (f" or {a.dur} pass" if a.dur else ""))
    elif a.cmd == "wake":
        sleep_set(a.who.lower(), False)
    elif a.cmd == "hook":
        claude_hook(a.phase)
    elif a.cmd == "protocol":
        print(protocol())
    elif a.cmd == "intro":
        code, pre = winpath(CODE)
        print(template("SEAT.md", name=a.name, others=",".join(n for n in AGENTS if n != a.name), code=code,
                       cmd=pre + AGORA))
    elif a.cmd == "decide":
        client()
        m = post("user", "all", " ".join(a.text), via=a.via and a.via.lower(), mode="decision")
        print(f"recorded decision #{m['id']}")
    elif a.cmd == "decisions":
        print(decisions_text(load()).rstrip() or "no decisions recorded yet")
    elif a.cmd == "sync":
        n = sync()
        if n is not None:
            print(f"synced {n} files to {CODE}")
    elif a.cmd in AGENTS:
        m = run_agent(a.cmd)
        print("slept" if m == "slept" else f"held for independent round #{m['held']}" if m and m.get("held")
              else f"posted #{m['id']}" if m else "no reply")
    elif a.cmd == "serve":
        serve_loop(a)
    elif a.cmd == "reset":
        for n in (AGENTS if a.name == "all" else [a.name]):
            if session(n):
                session(n, drop=True)
                print(f"{n}: session dropped; next turn starts fresh")
    elif a.cmd == "status":
        msgs = load()
        print(f"{len(msgs)} messages; agent streak {agent_streak(msgs)}; code snapshot: "
              + (open(os.path.join(CODE, 'SNAPSHOT.txt')).read().strip() if os.path.exists(CODE) else "none"))
        for n in NAMES:
            z = asleep(n)
            print(f"  {n:8s} seen #{seen(n)}, unread {sum(1 for m in msgs if for_me(m, n) and m['id'] > seen(n))}"
                  + (", asleep until pinged" + (f" or {','.join(z['from'])} posts" if z.get("from") else "") + (f" (or {hhmm(z['until'], False)})" if z["until"] else "")
                     if z else ""))
        for n in AGENTS:
            se = session(n)
            print(f"  {n:8s} " + (f"session {se['id'][:8]}…: {se.get('turns', 0)}/{MAX_TURNS} turns, "
                                  f"{se.get('tokens', 0) // 1000}k/{MAX_TOKENS // 1000}k tokens, since {se.get('started')}"
                                  if se else "no session (next turn starts fresh)") + (" [served]" if n in AUTO else ""))


__all__ = [k for k in list(globals()) if not k.startswith("__")]
