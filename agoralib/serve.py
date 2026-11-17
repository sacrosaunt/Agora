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
        pol = spawn_serve()
        for _ in range(100):   # until the child holds the serve lock (so the next starter sees it running)
            time.sleep(0.1)
            f = serve_lock()
            if f is None:
                break
            f.close()
    if not quiet:
        print(f"started agora serve for {', '.join(pol.get('only', '').split(',') if pol.get('only') else auto())} "
              f"(log: {os.path.join(STATE, 'serve.log')})", file=sys.stderr)


def spawn_serve():
    """Start a background serve with the saved options (see serve_policy); returns them."""
    pol = serve_policy()
    log = open(os.path.join(STATE, "serve.log"), "a")
    subprocess.Popen(SELF + ["--dir", WS, "serve", "--auto"]
                     + (["--only", pol["only"]] if pol.get("only") else [])
                     + (["--budget", str(pol["budget"])] if pol.get("budget") else []),
                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, cwd=WS, **detached())
    return pol


RESTART = os.path.join(STATE, "restart")   # present while a restart waits for the replies in progress


def serve_running():
    f = serve_lock()
    if f is None:
        return True
    f.close()
    return False


def restart_serve(now=False):
    """/restart, `agora restart`: the serve starts again on the code now on disk, so a `git pull` takes effect.
    It first lets the replies in progress finish (it starts no new ones meanwhile), or with now stops them; the
    messages they were answering stay unread, so the new serve answers them. Returns the agents it waits for."""
    if not serve_running():
        ensure_serve(quiet=True)
        if not serve_running():   # autoserve is off: start one anyway, the user asked for it
            spawn_serve()
        return []
    os.makedirs(STATE, exist_ok=True)
    open(RESTART, "w").close()   # first: a stopped reply must not be started again by this serve
    if now:
        for n in AGENTS:
            stop_turn(n, "retry")
    return list(busy_now())


def serve_loop(a):
    """`agora serve`: answer as the serve agents whenever a message is addressed to them."""
    import threading
    def roster():
        return list(auto())   # re-read every pass: `agora add` / remove (and --only, saved in serve.json)
    held = serve_lock()   # one serve per workspace (kept open for the life of the process)
    if held is None:
        sys.exit(0 if a.auto else "agora serve is already running (see state/serve.lock)")
    if not a.auto:   # auto-started serves reuse these
        serve_policy(a.only, a.budget if a.budget != BUDGET else None, save=True)
    who = roster()
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
    working, wlock, calm = set(), threading.Lock(), {}

    def turn(name, ids):
        try:
            m = run_agent(name, do_sync=False, served=True)
            print(f"{time.strftime('%H:%M:%S')} {name} -> " + ("asleep" if m == "slept" else "stopped" if m == "stopped" else
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
    if os.path.exists(RESTART):   # asked of an earlier serve that is gone: this one already runs the new code
        os.unlink(RESTART)
    while True:
        if roster() != who:
            print(f"{time.strftime('%H:%M:%S')} now serving {roster()}", flush=True)
            who[:] = roster()   # in place: quiet() reads it too
        if os.path.exists(RESTART):   # start no new replies; once the running ones are done, hand over
            with wlock:
                idle = not working
            if idle:
                try:
                    os.unlink(RESTART)
                except OSError:
                    pass
                log_turn("serve", [], "restart", note="serve restarted on the current code")
                print(f"{time.strftime('%H:%M:%S')} restarting (log: {os.path.join(STATE, 'serve.log')})", flush=True)
                held.close()   # the new serve takes the lock
                spawn_serve()
                return
            time.sleep(1)
            continue
        reveal_due()
        msgs = load()
        with wlock:
            idle = not working
        for q in revealed_rounds():   # a revealed round's discussion that went quiet gets its conclusion drafted
            if conclude_pending(q) and idle and not os.path.exists(halted) and settled(q, msgs):
                calm[q] = calm.get(q, 0) + 1
                if calm[q] >= 2:   # quiet on two passes: a reply about to start isn't missed
                    c = ask_conclusion(q, msgs)
                    print(f"{time.strftime('%H:%M:%S')} round #{q} settled; "
                          + (f"conclusion asked of {c['to'][0]} (#{c['id']})" if c else "too few answers to conclude"),
                          flush=True)
                    msgs = load()
            elif conclusion_state(q, "draft") and not conclusion_state(q, "final") and idle \
                    and not os.path.exists(halted) and settled(int(conclusion_state(q, "draft")), msgs):
                calm[q] = calm.get(q, 0) + 1   # the draft's checks are in: post it, or ask for the final version
                if calm[q] >= 2:
                    c = finish_conclusion(q, msgs)
                    print(f"{time.strftime('%H:%M:%S')} round #{q} conclusion checked; "
                          + ("posted" if c and c.get("conclusion") else "final version asked" if c else "draft lost"),
                          flush=True)
                    calm.pop(q, None)
                    msgs = load()
            else:
                calm.pop(q, None)
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
                busy_task(name, "drafting the conclusion" if any(m.get("conclude") for m in pend)
                          else "finalizing the conclusion" if any(m.get("finalize") for m in pend)
                          else "checking the conclusion" if any(m.get("conclusion_draft") for m in pend) else None)
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
    workspace's agora.json). An added agent answers from your latest message if nobody has answered it yet, else
    from the next one (its fresh session still reads the thread); a removed one finishes a turn it is in, then answers no more. Logged so every open chat shows it.
    Returns the names that changed."""
    cur, raw = list(auto()), serve_cfg()
    changed = [n for n in AGENTS if n in names and (n in cur) != add]
    if not changed:
        return []
    save_cfg(CONFIG, {"serve": [n for n in AGENTS if (n in raw or n in changed) and not (n in changed and not add)]})
    pol = serve_policy()
    if add and pol.get("only"):   # a serve started with --only serves the added agent too
        serve_policy(",".join(n for n in AGENTS if n in pol["only"].split(",") or n in changed), pol.get("budget"), save=True)
    msgs = load()
    for n in changed:
        if add and msgs:
            # not the backlog (context, not questions), but a question you just asked and nobody answered yet
            start = msgs[-1]["id"] - (1 if msgs[-1]["from"] == "user" else 0)
            seen(n, max(seen(n), start))
        elif not add:
            for q in open_rounds():   # an open independent round stops waiting for it
                mem = members(q)
                if n in mem:
                    wjson(os.path.join(INDEP, str(q), "members.json"), [x for x in mem if x != n])
            for o in AGENTS:   # whoever waits on its answer stops waiting
                if n in ((asleep(o) or {}).get("from") or ()) and (asleep(o) or {}).get("why") != "user":
                    sleep_set(o, False)
            sleep_set(n, False)
        if not add:   # out of the roll call now, not when its last read ages out (an agent run by hand that
            p = os.path.join(STATE, n + ".seen")   # reads again is back in by itself)
            if os.path.exists(p):
                t = time.time() - 7300
                os.utime(p, (t, t))
    log_turn(by, [], "roster", note=f"{', '.join(changed)} {'added to' if add else 'removed from'} this chat by {by}")
    if add:
        ensure_serve(quiet=True)
    return changed


def _kill_serve(pid):
    if WINDOWS:   # the whole tree: the serve and the agent CLIs it started
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    else:
        import signal
        try:   # an auto-started serve leads its own process group, which its agent runs share
            os.killpg(pid, signal.SIGTERM) if os.getpgid(pid) == pid else os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


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
    for n in AGENTS:   # agent runs lead their own process groups: stop them first (their messages stay unread)
        stop_turn(n, "retry")
    _kill_serve(pid)
    for _ in range(100):
        time.sleep(0.1)
        f = serve_lock()
        if f is not None:
            f.close()
            return True
    return False


def move_ws(frm, to=None):
    """`agora move FROM [TO]`: FROM's workspace (thread, state, setup, snapshot) follows its project to the folder TO
    (default: this one), so agora run there opens it. Its serve stops first; agents start fresh sessions on their
    next turn, since their source folder changed."""
    if WS_HOW != "move" or not os.path.isfile(os.path.join(WS, "workspace.json")):
        sys.exit(f"agora: no workspace for {frm}; `agora ls` lists them (give a name from there, or the folder it was "
                 "made for)")
    if not SRC0:
        sys.exit("agora: that workspace is a discussion without code: there is no folder to move")
    to = os.path.realpath(os.path.expanduser(to or os.getcwd()))
    if not os.path.isdir(to):
        sys.exit(f"agora: {to} is not a folder; move the project there first, then run agora move")
    src = git_top(to) or to
    if src == SRC0:
        sys.exit(f"agora: this workspace already belongs to {src}")
    dest = ws_path(src, src)
    check_src(src, dest)
    cl = lockfile("clients.lock")
    if not lock(cl, block=False):
        cl.close()
        log_turn("agora", [], "notice", note=f"agora move to {src} is waiting for this chat to close: /quit here, then run "
                                              "agora move again")
        sys.exit(f"agora: an agora chat or wait is open on {SRC0}; close it (/quit) and run agora move again "
                 "(the chat was told)")
    if os.path.exists(dest):   # agora already ran in the new folder: fine while that thread is still empty
        t = os.path.join(dest, "thread.jsonl")
        n = sum(1 for ln in open(t, encoding="utf-8") if ln.strip()) if os.path.exists(t) else 0
        if n:
            sys.exit(f"agora: {src} already has its own thread ({n} messages, in {dest}); "
                     f"archive it there first (agora new) or pick another folder")
        def held(lk):
            p = os.path.join(dest, "state", lk)
            if not os.path.exists(p):
                return False
            with open(p, "a+") as f:
                return not lock(f, block=False)
        if held("clients.lock"):
            with open(os.path.join(dest, "state", "turns.jsonl"), "a") as f:
                f.write(json.dumps({"t": time.time(), "name": "agora", "ids": [], "outcome": "notice", "msg": None,
                                    "note": f"agora move of {SRC0} into this folder is waiting for this (empty) "
                                            "chat to close: /quit here, then run agora move again"}) + "\n")
            sys.exit(f"agora: an agora chat is open on {src}; close it (/quit) and run agora move again "
                     "(the chat was told)")
        if held("serve.lock"):   # an idle serve a chat started there: an empty thread gives it nothing to answer
            try:
                _kill_serve(int(open(os.path.join(dest, "state", "serve.lock")).read().strip()))
            except (OSError, ValueError):
                pass
            for _ in range(100):
                if not held("serve.lock"):
                    break
                time.sleep(0.1)
            else:
                sys.exit(f"agora: couldn't stop the serve in {dest} (pid in state/serve.lock); stop it and try again")
        shutil.rmtree(dest)
        print(f"replaced the empty workspace agora had started for {src}")
    try:
        if not stop_serve():
            sys.exit("agora: couldn't stop this workspace's serve (pid in state/serve.lock); stop it and try again")
    finally:
        cl.close()
    meta = _json(os.path.join(WS, "workspace.json"))
    try:
        os.rename(WS, dest)
    except OSError as e:
        sys.exit(f"agora: couldn't move {WS} to {dest} ({e.strerror}); close anything using it and try again")
    meta.update(src=src, moved=meta.get("moved", []) + [{"from": SRC0, "at": time.strftime("%Y-%m-%d %H:%M:%S")}])
    p = os.path.join(dest, "workspace.json")
    with open(tmpname(p), "w") as f:
        json.dump(meta, f, indent=1)
    replace(tmpname(p), p)
    print(f"moved the agora workspace from {SRC0} to {src}\n  data now in {dest}\n"
          f"  run agora in {src} to continue the thread; agents start fresh sessions on their next turn")


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
