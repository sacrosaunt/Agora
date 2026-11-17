"""The serve loop and the locks that keep one per workspace (started and stopped on demand)."""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time  # noqa: F401
from .compat import *  # noqa: F401,F403
from .ws import *  # noqa: F401,F403
from .store import *  # noqa: F401,F403
from .snapshot import *  # noqa: F401,F403
from .agents import *  # noqa: F401,F403


def serve_lock():
    """Take the serve lock (state/serve.lock) without blocking: the open file while held, None if another serve
    holds it. The lock dies with its process, so a crashed serve never blocks the next one."""
    f = lockfile("serve.lock")
    if not lock(f, block=False):
        f.close()
        return None
    f.seek(0); f.truncate(); f.write(str(os.getpid())); f.flush()
    return f


CLIENT = None


def client():
    """Mark this process as a client (chat, post, wait, post --via) until it exits: an idle `serve` started by
    agora only shuts down while no client holds this shared lock. Blocks while a serve is deciding to exit, so
    the serve-lock check that follows is accurate."""
    global CLIENT
    if CLIENT is None:
        CLIENT = lockfile("clients.lock")
        lock(CLIENT, shared=True)
    return CLIENT


def serve_policy(only=None, budget=None, save=False):
    """The launch options (--only, --budget) an auto-started serve uses: the last ones given to `agora serve`."""
    p = os.path.join(STATE, "serve.json")
    if save:
        os.makedirs(STATE, exist_ok=True)
        with open(p, "w") as f:
            json.dump({"only": only, "budget": budget}, f)
    return _json(p)


def ensure_serve(quiet=False):
    """Start `agora serve` in the background (log: state/serve.log) unless one is running or autoserve is off."""
    client()
    if not CFG.get("autoserve", True) or os.environ.get("AGORA_AUTOSERVE", "1") == "0" or not auto():
        return
    with lockfile("spawn.lock") as sp:   # one starter at a time: two clients must not both start a serve
        lock(sp)
        f = serve_lock()
        if f is None:
            return   # already running
        f.close()    # free it for the child
        pol = serve_policy()
        log = open(os.path.join(STATE, "serve.log"), "a")
        subprocess.Popen(SELF + ["--dir", WS, "serve", "--auto"]
                         + (["--only", pol["only"]] if pol.get("only") else [])
                         + (["--budget", str(pol["budget"])] if pol.get("budget") else []),
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, cwd=WS, **detached())
        for _ in range(100):   # until the child holds the serve lock (so the next starter sees it running)
            time.sleep(0.1)
            f = serve_lock()
            if f is None:
                break
            f.close()
    if not quiet:
        print(f"started agora serve for {', '.join(pol.get('only', '').split(',') if pol.get('only') else auto())} "
              f"(log: {os.path.join(STATE, 'serve.log')})", file=sys.stderr)


def serve_loop(a):
    """`agora serve`: answer as the serve agents whenever a message is addressed to them."""
    import threading
    def roster():
        return a.only.split(",") if a.only else list(auto())   # re-read every pass: `agora add` / remove
    who = roster()
    held = serve_lock()   # one serve per workspace (kept open for the life of the process)
    if held is None:
        sys.exit(0 if a.auto else "agora serve is already running (see state/serve.lock)")
    if not a.auto:   # auto-started serves reuse these
        serve_policy(a.only, a.budget if a.budget != BUDGET else None, save=True)
    idle_after = float(CFG.get("idle_exit", 1800)) if a.auto else 0   # seconds; 0: never
    last_busy = time.time()

    def quiet(msgs):
        """Nothing to do now or soon: no agent working or with unread messages (unless over budget), no
        retry due, no open independent round, no sleep that ends by itself."""
        over = agent_streak(msgs) >= a.budget
        return not working and not open_rounds() and not any(
            (not over and any(m["id"] > seen(n) and for_me(m, n) for m in msgs))
            or failure(n) or (asleep(n) or {}).get("until") for n in who)
    print(f"serving {who} in parallel (budget {a.budget} agent msgs per user msg); Ctrl-C to stop", flush=True)
    working, wlock = set(), threading.Lock()

    def turn(name, ids):
        try:
            m = run_agent(name, do_sync=False, served=True)
            print(f"{time.strftime('%H:%M:%S')} {name} -> " + ("asleep" if m == "slept" else
                  "nothing new" if m == "nothing" else
                  f"held for round #{m['held']}" if m and m.get("held") else
                  f"#{m['id']}" if m else "failed"), flush=True)
        except Exception as e:  # keep serving the others; the messages stay unseen and are retried
            f = note_failure(name, ids, None, fatal=fatal_error(repr(e)))
            log_turn(name, ids, "failed", note=f"error {e!r}, " + (
                "not retried: fix it, then post again" if fatal_error(repr(e)) else f"try {f['tries']}/{MAX_TRIES}"))
            print(f"{name} -> error {e!r}", flush=True)
        finally:
            busy(name, False)
            with wlock:
                working.discard(name)

    halted = os.path.join(STATE, "halted")   # present while the budget stops the agents (the chat shows it)
    if os.path.exists(halted):   # left over from an earlier serve: recomputed below
        os.unlink(halted)
    while True:
        if roster() != who:
            print(f"{time.strftime('%H:%M:%S')} now serving {roster()}", flush=True)
            who[:] = roster()   # in place: quiet() reads it too
        reveal_due()
        msgs = load()
        todo = []
        over = agent_streak(msgs) >= a.budget
        if over != os.path.exists(halted):
            if over:
                open(halted, "w").write(str(a.budget))
                print(f"budget reached ({a.budget} agent msgs since the user spoke); waiting for the user",
                      flush=True)
                log_turn("serve", [msgs[-1]["id"]], "budget", note=f"{a.budget} agent messages")
            else:
                os.unlink(halted)
        for name in who:
            if name in working:
                continue
            pend = [m for m in msgs if m["id"] > seen(name) and for_me(m, name)]
            if not pend or over:   # over budget: the messages stay unseen until the user speaks
                continue
            f = failure(name)
            if f and f["tries"] >= MAX_TRIES:   # give up on these messages, visibly
                give_up(name, f)
                print(f"{name}: gave up on {f['ids']}" + ("" if f.get("fatal") else f" after {f['tries']} failed tries"),
                      flush=True)
                continue
            if f and time.time() < f["next"]:
                continue
            todo.append((name, pend))
        if todo:
            with wlock:
                idle = not working
            if idle:  # refresh the code snapshot only when no agent is reading it
                sync()
            for name, pend in todo:
                print(f"{time.strftime('%H:%M:%S')} {name} replying to {[m['id'] for m in pend]}", flush=True)
                busy(name, True)
                with wlock:
                    working.add(name)
                threading.Thread(target=turn, args=(name, [m["id"] for m in pend]), daemon=True).start()
        if idle_after:
            with wlock:
                q = quiet(msgs)
            if not q:
                last_busy = time.time()
            elif time.time() - last_busy > idle_after:
                cl = lockfile("clients.lock")
                if lock(cl, block=False):   # no client is attached, and none can attach until we're gone
                    with wlock:
                        q = quiet(load())
                    if q:
                        print(f"{time.strftime('%H:%M:%S')} idle for {idle_after:.0f}s with no client; exiting",
                              flush=True)
                        held.close()   # serve lock first: a client that gets in next starts a new serve
                        cl.close()
                        return
                cl.close()
        time.sleep(2)


def seat(names, add, by="user"):
    """`agora add` / `agora remove`: put names into this workspace's served agents, or take them out (saved in the
    workspace's agora.json). An added agent starts from the next message (its fresh session still reads the
    thread); a removed one finishes a turn it is in, then answers no more. Logged so every open chat shows it.
    Returns the names that changed."""
    cur = list(auto())
    changed = [n for n in AGENTS if n in names and (n in cur) != add]
    if not changed:
        return []
    save_cfg(CONFIG, {"serve": [n for n in AGENTS if (n in cur or n in changed) and not (n in changed and not add)]})
    msgs = load()
    for n in changed:
        if add and msgs:
            seen(n, max(seen(n), msgs[-1]["id"]))   # not the backlog: what came before is context, not questions
        elif not add:   # out of the roll call now, not when its last read ages out (an agent run by hand that
            p = os.path.join(STATE, n + ".seen")   # reads again is back in by itself)
            if os.path.exists(p):
                t = time.time() - 7300
                os.utime(p, (t, t))
    log_turn(by, [], "roster", note=f"{', '.join(changed)} {'added to' if add else 'removed from'} this chat by {by}")
    if add:
        ensure_serve(quiet=True)
    return changed


def stop_serve():
    """Stop this workspace's serve (and the agent turns it is running). True once none holds the serve lock."""
    f = serve_lock()
    if f is not None:
        f.close()
        return True
    try:
        pid = int(open(os.path.join(STATE, "serve.lock")).read().strip())
    except (OSError, ValueError):
        return False
    if WINDOWS:   # the whole tree: the serve and the agent CLIs it started
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    else:
        import signal
        try:   # an auto-started serve leads its own process group, which its agent runs share
            os.killpg(pid, signal.SIGTERM) if os.getpgid(pid) == pid else os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(100):
        time.sleep(0.1)
        f = serve_lock()
        if f is not None:
            f.close()
            return True
    return False


def new_thread():
    """`agora new`: archive the thread with its per-thread state (seen marks, sessions, rounds, sleeps, failures,
    logs, agents' work dirs) under archive/<time>/ and start an empty thread. Setup, project notes and the code
    snapshot stay."""
    cl = lockfile("clients.lock")
    if not lock(cl, block=False):
        cl.close()
        sys.exit("an agora chat or wait is open on this workspace; close it (/quit) and run `agora new` again")
    try:
        if not stop_serve():
            sys.exit("couldn't stop this workspace's serve (pid in state/serve.lock); stop it and try again")
        if not os.path.exists(THREAD) or not load():
            print("the thread is already empty", file=sys.stderr)
            return
        dest = os.path.join(WS, "archive", time.strftime("%Y%m%d-%H%M%S"))
        keep = {"plain.git", "snapshot.json", "serve.json"}   # the snapshot's history and the serve options
        moves = [(p, os.path.join(dest, os.path.basename(p))) for p in (THREAD, MD) if os.path.exists(p)]
        if os.path.isdir(os.path.join(WS, "work")):
            moves.append((os.path.join(WS, "work"), os.path.join(dest, "work")))
        for root, dirs, files in os.walk(STATE):
            rel = os.path.relpath(root, STATE)
            if rel == ".":
                dirs[:] = [d for d in dirs if d not in keep]
            moves += [(os.path.join(root, n), os.path.join(dest, "state", rel, n)) for n in files
                      if not n.endswith(".lock") and not (rel == "." and n in keep)]
        for src, dst in moves:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            rename(src, dst)
        for root, dirs, files in os.walk(STATE, topdown=False):   # drop the emptied dirs (lock files stay)
            if root != STATE and not os.listdir(root):
                os.rmdir(root)
        print(f"new thread; the old one is in {dest}")
    finally:
        cl.close()


__all__ = [k for k in list(globals()) if not k.startswith("__")]
