"""The thread and its state: messages, sleep, rounds, turns, failures, activity, lock files."""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time  # noqa: F401
from .compat import *  # noqa: F401,F403
from .ws import *  # noqa: F401,F403


BUDGET = 400  # max agent messages since the last user message before Codex stops auto-replying


def author(m):
    return m["from"] + (f" (via {m['via']})" if m.get("via") else "")


def load():
    if not os.path.exists(THREAD):
        return []
    with open(THREAD) as f:
        return [json.loads(l) for l in f if l.strip()]


def tag(m):
    """Header tag: what the message answers, and its round or reply mode ('' for ordinary messages)."""
    re_ = (" ↩ " + ", ".join(f"#{i}" for i in m["re"][-4:])) if m.get("re") else ""
    if m.get("independent"):
        return re_ + f" ◆ independent answer to #{m['independent']}"
    if m.get("conclusion"):
        return re_ + f" ◆ conclusion of #{m['conclusion']}"
    if m.get("conclusion_draft"):
        return re_ + f" ◆ draft conclusion of #{m['conclusion_draft']} (agents only)"
    if m.get("objection"):
        return re_ + f" ◆ check of the draft conclusion of #{m['objection']} (agents only)"
    return re_ + {"independent": " ◆ independent", "audit": " ◆ audit", "design": " ◆ design",
                  "decision": " ◆ decision", "conclude": " ◆ conclude"}.get(m.get("mode"), "")


def wjson(path, obj):
    """Write JSON atomically (a crash leaves the old file or the new one, never half of one)."""
    with open(tmpname(path), "w") as f:
        json.dump(obj, f)
    replace(tmpname(path), path)


def decisions(msgs):
    """The decisions recorded in the thread (the user's /decide, vote results), oldest first."""
    return [m for m in msgs if m.get("mode") == "decision"]


def decisions_text(msgs):
    ds = decisions(msgs)
    if not ds:
        return ""
    return "## Decisions so far\n\n" + "".join(
        f"- #{m['id']} ({m['ts'][:16]}, {author(m)}): {' '.join(m['text'].split())[:400]}\n" for m in ds) + "\n"


def render(msgs):
    out = ["# Agora thread\n", "Read-only codebase snapshot: `" + CODE + "`. Protocol: `agora protocol`.\n"]
    if decisions(msgs):
        out.append("\n" + decisions_text(msgs))
    for m in msgs:
        if m.get("hidden"):   # agent-only bookkeeping (a conclusion's trigger, draft and checks): not for the user
            continue
        out.append(f"\n---\n### #{m['id']} {author(m)} → {', '.join(m['to'])}{tag(m)} · {m['ts']}\n\n{m['text'].rstrip()}\n")
    tmp = tmpname(MD)
    with open(tmp, "w") as f:
        f.writelines(out)
    replace(tmp, MD)


PING = re.compile(r"(?<![\w@])@(" + "|".join(AGENTS) + r"|all)\b", re.I)
MODES = ("short", "audit", "design", "independent", "decision", "conclude")
INDEP = os.path.join(STATE, "independent")  # <qid>/<agent>.json: answers held until the round is revealed


def post(frm, to, text, via=None, mode=None, extra=None, hold=(), re_=()):
    """via: an agent posting as the user on the user's behalf (agora post --via NAME). mode (user messages):
    the reply shape asked for (audit, design), an independent round, or a decision. extra: more fields for the
    record. re_: ids of the messages this one answers. hold: ids of open independent rounds this agent message
    answers: it is held in each round (not posted) until the round is revealed; returns {"held": qid, ...}."""
    frm = frm.lower()
    if frm not in NAMES:
        sys.exit(f"unknown author {frm!r}; use one of {NAMES}")
    to = [t.strip().lower() for t in (to or "all").split(",") if t.strip()]
    bad = [t for t in to if t not in NAMES + ("all",)]
    if bad:
        sys.exit(f"unknown recipient(s) {bad}")
    text = text.strip()
    if not text:
        sys.exit("empty message")
    if mode and mode not in MODES:
        sys.exit(f"unknown mode {mode!r}; use one of {MODES}")
    hold = [q for q in hold if frm in AGENTS and q in open_rounds()
            and not os.path.exists(os.path.join(INDEP, str(q), frm + ".json"))]
    if hold:
        held = {"from": frm, "to": to, "text": text, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "held": hold[0],
                "rounds": hold}
        for q in hold:
            wjson(os.path.join(INDEP, str(q), frm + ".json"), held)
        busy(frm, False)
        return held
    with open(THREAD, "a+") as f:
        lock(f)
        f.seek(0)
        n = sum(1 for l in f if l.strip())
        m = {"id": n + 1, "from": frm, "to": to, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "text": text}
        if via:
            m["via"] = via
        if mode and mode != "short":
            m["mode"] = mode
        if re_:
            m["re"] = sorted(set(re_))
        m.update(extra or {})
        f.write(json.dumps(m, ensure_ascii=False) + "\n")
        f.flush()
        render(load())   # under the lock, so a slower writer never replaces thread.md with an older thread
        unlock(f)
    if mode == "independent":   # the round waits for exactly the agents asked when it opened
        d = os.path.join(INDEP, str(m["id"]))
        os.makedirs(d, exist_ok=True)
        wjson(os.path.join(d, "members.json"),
              round_members() if "all" in to else [n for n in AGENTS if n in to or mentioned(text, n)])
    elif mode == "decision":
        pass   # a record, not a question: nobody is asked to answer it
    elif frm == "user" and to == ["all"] and not PING.search(uncode(text)):
        take_turns(m["id"])   # a plain question to all: one lead answer (an @agent or @all ping asks everyone)
    if frm != "user":
        busy(frm, False)
        if frm in AGENTS and to and all(t in AGENTS and t != frm for t in to):
            sleep_set(frm, True, why="auto", until_from=to)   # asked another agent: wait for its answer
    return m


def PING_ONE(name):
    return re.compile(r"(?<![\w@])@" + name + r"\b", re.I)


def uncode(text):
    """text without its code (fenced blocks, `inline` spans): an @name there is an example, not a ping."""
    return re.sub(r"`[^`\n]*`", " ", re.sub(r"(?ms)^\s*(```|~~~).*?(^\s*\1|\Z)", " ", text))


def mentioned(text, name):
    """name is pinged in text (@name outside code)."""
    return bool(PING_ONE(name).search(uncode(text)))


SLEEP = os.path.join(STATE, "sleep")  # <name>.json while an agent sleeps: it skips messages to "all" until pinged


def pinged(m, name):
    """Addressed to name by name (-t name), mentioning @name anywhere in the text, the user mentioning @all, or
    a user question opening an independent round (everyone answers it)."""
    if m["from"] == name or m.get("objection") or m.get("conclusion"):
        return False   # an objection waits for finish_conclusion; the final conclusion is for the user, not a prompt
    if name in m["to"] or mentioned(m["text"], name):
        return True
    return m["from"] == "user" and name in AGENTS and m.get("mode") != "decision" and (
        m.get("mode") == "independent" and "all" in m["to"] or mentioned(m["text"], "all"))


def asleep(name):
    """The sleep record {since, until, why} of a sleeping agent, else None (expired sleeps end by themselves)."""
    p = os.path.join(SLEEP, name + ".json")
    try:
        z = json.load(open(p))
    except (OSError, ValueError):
        return None
    if z.get("until") and time.time() > z["until"]:
        try:
            os.unlink(p)
        except OSError:
            pass
        return None
    return z


def sleep_set(name, on=True, secs=None, why="", until_from=()):
    os.makedirs(SLEEP, exist_ok=True)
    p = os.path.join(SLEEP, name + ".json")
    if not on:
        try:
            os.unlink(p)
        except OSError:
            pass
        return
    with open(p, "w") as f:
        json.dump({"since": time.time(), "until": time.time() + secs if secs else None, "why": why,
                   "from": [x for x in until_from if x != name], "after": len(load())}, f)


def take_turns(mid):
    """A question to all gets one lead answer (agents rotate); the others wait for it, then add only what's new."""
    os.makedirs(STATE, exist_ok=True)
    p = os.path.join(STATE, "lead")
    try:
        i = int(open(p).read())
    except (OSError, ValueError):
        i = -1
    # the lead comes from who will actually answer: in this chat, not put to sleep by you, not failing (an absent
    # lead would leave the rest asleep for the full 15 minutes)
    here = [n for n in round_members() if (asleep(n) or {}).get("why") != "user"]
    able = [n for n in here if not failure(n)] or here
    if not able:
        return
    lead = next(AGENTS[(i + k) % len(AGENTS)] for k in range(1, len(AGENTS) + 1) if AGENTS[(i + k) % len(AGENTS)] in able)
    with open(p, "w") as f:
        f.write(str(AGENTS.index(lead)))
    if (asleep(lead) or {}).get("why") in ("turn", "auto"):
        sleep_set(lead, False)   # it may still wait on an earlier lead that waits on it: two quick posts deadlocked
    for n in here:
        if n != lead:
            sleep_set(n, True, 900, why="turn", until_from=[lead])   # wakes on the lead's answer, a ping, or after 15 min


def for_me(m, name):
    """Pings always count. A sleeper waiting for someone (WAIT: codex) wakes on that one's next message,
    whoever it is addressed to; otherwise sleepers skip messages to all."""
    if m.get("objection") or m.get("conclusion") or m.get("hidden") and name not in m["to"]:
        return False   # bookkeeping reaches only its addressees; objections wait; nobody replies to a conclusion
    if pinged(m, name):
        return True
    z = asleep(name)
    if z and z.get("from") and m["from"] in z["from"] and m["id"] > z.get("after", 0):
        return True
    if z and m["from"] == "user" and "all" in m["to"] and z.get("why") not in ("user", "turn") \
            and m["id"] > z.get("after", 0):
        return True   # the user speaking to everyone wakes agents that went to sleep on their own
    return m["from"] != name and "all" in m["to"] and not z and m.get("mode") != "decision"


def seen(name, val=None):
    os.makedirs(STATE, exist_ok=True)
    p = os.path.join(STATE, name + ".seen")
    if val is not None:
        with open(p, "w") as f:
            f.write(str(val))
        return val
    try:
        return int(open(p).read())
    except (OSError, ValueError):
        return 0


def fmt(m):
    return f"### #{m['id']} {author(m)} → {', '.join(m['to'])}{tag(m)} · {m['ts']}\n\n{m['text'].rstrip()}\n"


ROUND_TIMEOUT = float(CFG.get("independent_timeout", 1200))  # an independent round is revealed after this


def open_rounds():
    """Ids of independent rounds whose answers are still held (oldest first)."""
    try:
        return sorted(int(d) for d in os.listdir(INDEP) if d.isdigit()
                      and not os.path.exists(os.path.join(INDEP, d, "revealed")))
    except OSError:
        return []


def round_members():
    """Agents an independent round waits for: the served ones, plus any agent run by hand that was active in the
    last two hours (it updates its seen marker when it reads the thread)."""
    now = time.time()
    def active(n):
        try:
            return now - os.path.getmtime(os.path.join(STATE, n + ".seen")) < 7200
        except OSError:
            return False
    return [n for n in AGENTS if n in auto() or active(n)]


def members(q):
    """The agents round q waits for, fixed when it opened."""
    try:
        return json.load(open(os.path.join(INDEP, str(q), "members.json")))
    except (OSError, ValueError):
        return round_members()


def round_status(q):
    """{agent: held record} for round q."""
    d, out = os.path.join(INDEP, str(q)), {}
    for n in AGENTS:
        try:
            out[n] = json.load(open(os.path.join(d, n + ".json")))
        except (OSError, ValueError):
            pass
    return out


def hold_outcome(q, name, **kw):
    """Record that name won't answer round q (it slept or failed), so the round doesn't wait for it."""
    p = os.path.join(INDEP, str(q), name + ".json")
    if os.path.isdir(os.path.dirname(p)) and not os.path.exists(p):
        wjson(p, {"from": name, **kw})


def reveal_due(force=False, only=None):
    """Post the held answers of every round that is complete (all members answered, slept or failed) or past
    ROUND_TIMEOUT, together, each addressed to the user and the other agents so they cross-check one another."""
    if not open_rounds():
        return
    os.makedirs(INDEP, exist_ok=True)
    with open(os.path.join(INDEP, ".lock"), "w") as lk:   # one revealer at a time
        lock(lk)
        for q in open_rounds():
            if only is None or q in only:   # /reveal: these rounds now, with the answers in so far
                _reveal(q, force)


def _reveal(q, force):
    """Idempotent: an answer already in the thread is not posted again, so a crash midway is finished on the next
    call; the round is marked revealed only after every answer is out."""
    held = round_status(q)
    msgs = load()
    qm = next((m for m in msgs if m["id"] == q), None)
    t0 = time.mktime(time.strptime(qm["ts"], "%Y-%m-%d %H:%M:%S")) if qm else 0
    if not (force or set(members(q)) <= set(held) or time.time() - t0 > ROUND_TIMEOUT):
        return
    out = {m["from"] for m in msgs if m.get("independent") == q}
    answers = [h for n, h in held.items() if h.get("text")]
    for h in answers:
        if h["from"] in out:
            continue
        others = [n for n in AGENTS if n != h["from"] and n in held and held[n].get("text")]
        post(h["from"], ",".join(["user"] + others), h["text"], extra={"independent": q}, re_=[q])
    open(os.path.join(INDEP, str(q), "revealed"), "w").close()
    missing = [n for n in members(q) if n not in held]
    failed = [n for n, h in held.items() if h.get("failed")]
    halted = [n for n, h in held.items() if h.get("stopped")]
    quiet = [n for n, h in held.items() if not h.get("text") and not h.get("failed") and not h.get("stopped")]
    log_turn("reveal", [q], "revealed", note=f"{len(answers)} answers"
             + (f"; no answer from {', '.join(missing)}" if missing else "")
             + (f"; failed: {', '.join(failed)}" if failed else "")
             + (f"; stopped: {', '.join(halted)}" if halted else "")
             + (f"; nothing to add: {', '.join(quiet)}" if quiet else ""))


def discussion(q, msgs):
    """Ids of q and every message answering it, directly or down a reply chain (the `re` links)."""
    ids = {q["id"] if isinstance(q, dict) else q}
    for m in msgs:
        if m["id"] > min(ids) and set(m.get("re") or ()) & ids:
            ids.add(m["id"])
    return ids


CONCLUDE = CFG.get("conclude", True)   # once a revealed round's discussion settles, one agent drafts its conclusion
CONCLUDE_WINDOW = 3 * 3600             # rounds revealed longer ago than this are left alone (e.g. after an upgrade)


def conclude_pending(q, msgs=None):
    """Round q was revealed recently and its conclusion hasn't been asked for (or skipped) yet."""
    d = os.path.join(INDEP, str(q))
    if not CONCLUDE or not os.path.exists(os.path.join(d, "revealed")) or os.path.exists(os.path.join(d, "conclude")):
        return False
    # age from the revealed answers in the thread, not the marker file: a copied or re-marked workspace (an
    # upgrade, `agora move`, a sync) gets fresh file times and would conclude every old round at once
    msgs = msgs if msgs is not None else load()
    if any(m.get("conclude") == q or m.get("conclusion") == q for m in msgs):
        return False   # already concluded (its marker file lost)
    ts = [m["ts"] for m in msgs if m.get("independent") == q]
    try:
        return bool(ts) and time.time() - time.mktime(time.strptime(max(ts), "%Y-%m-%d %H:%M:%S")) < CONCLUDE_WINDOW
    except ValueError:
        return False


def revealed_rounds():
    try:
        return sorted(int(d) for d in os.listdir(INDEP) if d.isdigit()
                      and os.path.exists(os.path.join(INDEP, d, "revealed")))
    except OSError:
        return []


def settled(q, msgs, working=()):
    """Round q's discussion has gone quiet: no agent at work and no served agent with an unread message in it."""
    disc = discussion(q, msgs)
    return not working and not any(m["id"] in disc and m["id"] > seen(n) and for_me(m, n) for n in auto() for m in msgs)


def conclusion_state(q, key):
    try:
        return open(os.path.join(INDEP, str(q), key)).read().strip()
    except OSError:
        return None


def conclusion_open(q):
    """Round q's conclusion is still to come: not asked for yet, or drafted / being checked / being finalized."""
    if conclude_pending(q):
        return True
    who = conclusion_state(q, "conclude")
    return bool(who) and who in AGENTS and not conclusion_state(q, "final")


def ask_conclusion(q, msgs):
    """Ask one agent of round q to draft its conclusion (agreed, still open, next step). The ask, the draft and the
    others' checks are hidden from the user (agent bookkeeping); only the final conclusion is shown. Rotates over
    the round's served members by round id. Skips rounds with fewer than two answers."""
    d = os.path.join(INDEP, str(q))
    answered = [m["from"] for m in msgs if m.get("independent") == q]
    able = [n for n in members(q) if n in auto() and (asleep(n) or {}).get("why") != "user" and not failure(n)]
    if len(answered) < 2 or not able:
        open(os.path.join(d, "conclude"), "w").write("skipped")
        return None
    drafter = able[q % len(able)]
    open(os.path.join(d, "conclude"), "w").write(drafter)   # first: a crash never asks twice
    return post("user", drafter, f"@{drafter}: the discussion of #{q} has settled. Draft its conclusion; the others "
                "will check it before the user sees it.", via="agora", mode="conclude",
                extra={"conclude": q, "hidden": True}, re_=[q])


def finish_conclusion(q, msgs):
    """Round q's draft conclusion has been checked (its discussion went quiet). No objections: the draft is posted
    to the user as the conclusion. Objections: the drafter is asked (hidden) for the final version."""
    d = os.path.join(INDEP, str(q))
    drafter, draft = conclusion_state(q, "conclude"), int(conclusion_state(q, "draft") or 0)
    dm = next((m for m in msgs if m["id"] == draft), None)
    if not dm:
        open(os.path.join(d, "final"), "w").write("lost")
        return None
    obj = [m for m in msgs if m.get("objection") == q and m["from"] != drafter]
    if not obj:
        m = post(drafter, "user", dm["text"], extra={"conclusion": q}, re_=[q])
        open(os.path.join(d, "final"), "w").write(str(m["id"]))
        return m
    open(os.path.join(d, "final"), "w").write("asked")
    ids = ", ".join("#%d" % m["id"] for m in obj)
    return post("user", drafter, f"@{drafter}: {ids} checked your draft #{draft} "
                f"of #{q}'s conclusion. Post the final conclusion for the user.", via="agora",
                extra={"finalize": q, "draft": draft, "hidden": True}, re_=[draft] + [m["id"] for m in obj])


TURNS = os.path.join(STATE, "turns.jsonl")  # one line per agent turn: who, which messages, what came of it


def log_turn(name, ids, outcome, msg=None, note=""):
    """outcome: posted (msg = id), held (independent round), slept (nothing to add), failed, gave up, stopped,
    budget, revealed, roster, restart, notice (a warning for open chats)."""
    os.makedirs(STATE, exist_ok=True)
    with open(TURNS, "a") as f:
        f.write(json.dumps({"t": time.time(), "name": name, "ids": list(ids), "outcome": outcome, "msg": msg,
                            "note": note}) + "\n")


def turns_since(t0):
    try:
        with open(TURNS) as f:
            return [r for r in (json.loads(l) for l in f if l.strip()) if r["t"] >= t0]
    except (OSError, ValueError):
        return []


FAIL = os.path.join(STATE, "failed")  # <name>.json after a failed turn: {ids, rc, tries, next}
MAX_TRIES = 4   # the first try and 3 retries (after 1, 2 and 4 min), then the messages are given up


def failure(name, val=None, clear=False):
    p = os.path.join(FAIL, name + ".json")
    if clear:
        try:
            os.unlink(p)
        except OSError:
            pass
        return None
    if val is not None:
        os.makedirs(FAIL, exist_ok=True)
        with open(p, "w") as f:
            json.dump(val, f)
        return val
    try:
        return json.load(open(p))
    except (OSError, ValueError):
        return None


LOGIN_ERR = re.compile(r"authenticat|log ?in|logged|oauth|unauthori[sz]ed|\b401\b|credential|api key|CODEX_HOME"
                       r"|FileNotFoundError|cannot find the file|No such file", re.I)


def fatal_error(err):
    """A failure that retrying won't fix (an expired login, a missing CLI): give up on the turn at once."""
    return bool(err and LOGIN_ERR.search(err))


def note_failure(name, ids, rc, fatal=False):
    """Count a failed turn; retries back off 1, 2, 4 min (none when fatal). Returns the record."""
    f = failure(name) or {"tries": 0}
    tries = MAX_TRIES if fatal else f["tries"] + 1
    rec = {"ids": sorted(set(f.get("ids", [])) | set(ids)), "rc": rc, "tries": tries, "fatal": fatal,
           "next": time.time() + 60 * 2 ** (tries - 1), "t": time.time()}
    os.makedirs(FAIL, exist_ok=True)
    wjson(os.path.join(FAIL, name + ".json"), rec)
    return rec


RUN = os.path.join(STATE, "run")   # <name>.pid while an agent CLI runs a reply; <name>.stop once it was stopped


def running(name):
    """The pid of name's agent CLI while it works on a reply (it leads its own process group), else None."""
    try:
        return int(open(os.path.join(RUN, name + ".pid")).read().strip())
    except (OSError, ValueError):
        return None


def stop_turn(name, how="drop"):
    """Stop name's reply in progress: its CLI and everything that CLI started are killed. how: "drop" (the
    messages it was answering count as answered and whoever waits on it is woken), "quiet" (the same, but nobody
    is woken: /stop all), "retry" (they stay unread and are answered again, e.g. by a restarted serve). The turn's
    own process does the bookkeeping when its CLI exits (see stopped()). True if a reply was running."""
    pid = running(name)
    if pid is None:
        return False
    os.makedirs(RUN, exist_ok=True)
    mark = os.path.join(RUN, name + ".stop")
    with open(mark, "w") as f:
        f.write(how)
    if kill_tree(pid):
        return True
    try:
        os.unlink(mark)
    except OSError:
        pass
    return False


def stop_all():
    """/stop: end every turn, running or queued. Unread messages count as read for every agent (so nothing starts
    after the kill), open independent rounds stop waiting, pending conclusions are not asked for, nobody is
    woken. Returns the agents whose reply was running."""
    msgs = load()
    last = msgs[-1]["id"] if msgs else 0
    for n in AGENTS:   # first, so the serve can't start a turn between the kill and the bookkeeping
        if any(m["id"] > seen(n) and for_me(m, n) for m in msgs):
            seen(n, max(seen(n), last))
    for q in open_rounds():
        st = round_status(q)
        for n in members(q):
            if n not in st:
                hold_outcome(q, n, stopped=True)
    for q in revealed_rounds():
        if conclude_pending(q):
            with open(os.path.join(INDEP, str(q), "conclude"), "w") as f:
                f.write("stopped")
        elif conclusion_open(q):   # drafted or being checked: no final version comes
            with open(os.path.join(INDEP, str(q), "final"), "w") as f:
                f.write("stopped")
    return [n for n in AGENTS if stop_turn(n, "quiet")]


def stopped(name):
    """How name's last CLI run was stopped (see stop_turn), else None; clears the mark."""
    p = os.path.join(RUN, name + ".stop")
    try:
        how = open(p).read().strip() or "drop"
        os.unlink(p)
        return how
    except OSError:
        return None


def give_up(name, f):
    """After MAX_TRIES failed tries: skip those messages, visibly; the rounds they opened stop waiting for name,
    and agents waiting for name's answer are woken (it isn't coming)."""
    seen(name, max(seen(name), max(f["ids"])))
    failure(name, clear=True)
    for q in open_rounds():
        if q in f["ids"]:
            hold_outcome(q, name, failed=True)
    for x in AGENTS:
        if x != name and name in ((asleep(x) or {}).get("from") or []):
            sleep_set(x, False)
    log_turn(name, f["ids"], "gave up", note="(not retried: see the error above)" if f.get("fatal")
             else f"after {f['tries']} failed tries")


COLORS = {"user": "1;37", "claude": "38;5;209", "codex": "38;5;114", "gemini": "38;5;75"}
ACT = os.path.join(STATE, "activity")  # <name>.json per agent working on a reply: phase + running actions


def _act_rw(name, fn, existing=False):
    os.makedirs(ACT, exist_ok=True)
    p = os.path.join(ACT, name + ".json")
    with open(p + ".lock", "a") as lk:
        lock(lk)
        if existing and not os.path.exists(p):
            return
        try:
            a = json.load(open(p))
        except (OSError, ValueError):
            a = {"phase": "thinking", "items": {}}
        a = fn(a)
        if a is None:
            if os.path.exists(p):
                os.unlink(p)
        else:
            a["t"] = time.time()
            with open(tmpname(p), "w") as f:
                json.dump(a, f)
            replace(tmpname(p), p)


def busy(name, on=True, phase="thinking"):
    """Mark an agent as working (phase text) or done (removes everything it was doing)."""
    _act_rw(name, (lambda a: {**a, "phase": phase}) if on else (lambda a: None))


def busy_task(name, task):
    """What a working agent's turn is for (e.g. "drafting the conclusion"), shown before its phase or tool."""
    _act_rw(name, lambda a: {**a, "task": task} if a is not None else a)


FG_STALE = 1200   # a foreground action with no "done" after 20 min was interrupted (its post hook never ran)


def live_items(items, now):
    """An agent's actions minus finished ones whose short display time ran out and interrupted foreground ones."""
    return {k: v for k, v in items.items() if v.get("until", now + 1) > now
            and (v.get("bg") or now - v.get("since", now) < FG_STALE)}


def act(name, key, what=None, bg=False, existing=False):
    """Start (what given) or finish (what None) one concurrent action of an agent, e.g. a command."""
    def f(a):
        now = time.time()
        a["items"] = live_items(a["items"], now)
        if what is None:
            it = a["items"].get(str(key))
            if it and now - it["since"] < 1.5:  # keep quick actions on screen long enough to read
                it["until"] = it["since"] + 1.5
            else:
                a["items"].pop(str(key), None)
        else:
            a["items"][str(key)] = {"what": what, "bg": bg, "since": time.time()}
        return a
    _act_rw(name, f, existing)


def claude_hook(phase):
    """Claude Code Pre/PostToolUse hook: show claude's tool calls while it is working on a reply.
    No-op unless the hook's session is the one in state/claude.session and claude is already busy."""
    try:
        d = json.load(sys.stdin)
        want = open(os.path.join(STATE, "claude.session")).read().strip()
    except (OSError, ValueError):
        return
    if not want or d.get("session_id") != want:
        return
    tool, inp = d.get("tool_name", ""), d.get("tool_input") or {}
    key = d.get("tool_use_id") or tool
    if phase == "post":
        return act("claude", key, None, existing=True)
    what = claude_tool_text(tool, inp)
    if what:
        act("claude", key, what, bg=bool(inp.get("run_in_background")), existing=True)


def claude_tool_text(tool, inp):
    """Claude Code tool call -> short activity text (None: don't show)."""
    if tool == "Bash":
        cmd = inp.get("command", "")
        return None if "/agora " in cmd or cmd.startswith("agora ") else describe_cmd(cmd)
    if tool in ("Read", "Edit", "Write"):
        f = (inp.get("file_path") or "").replace(CODE + "/", "")
        return ("reading " if tool == "Read" else "editing ") + os.path.basename(f) if "/agora/" not in f else "reading " + f
    if tool in ("Grep", "Glob"):
        return f"searching `{(inp.get('pattern') or '')[:30]}`"
    if tool.startswith("Web") or "exa" in tool:
        return "searching the web"
    if tool == "Agent":
        return "running a subagent"
    return None


def claude_event(name, e, res):
    """Headless Claude Code (stream-json) event: activity, session id, reply text, context size."""
    t = e.get("type")
    if e.get("session_id"):
        res["sid"] = e["session_id"]
    if t == "assistant":
        msg = e.get("message") or {}
        u = msg.get("usage") or {}
        if u:  # context of the latest model call
            res["tokens"] = sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        for c in msg.get("content") or []:
            if c.get("type") == "tool_use":
                act(name, c.get("id"), claude_tool_text(c.get("name", ""), c.get("input") or {}) or c.get("name", "tool"))
            elif c.get("type") in ("thinking", "redacted_thinking"):
                busy(name, True, "thinking")
            elif c.get("type") == "text":
                busy(name, True, "writing")
    elif t == "user":
        for c in (e.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_result":
                act(name, c.get("tool_use_id"))
                busy(name, True, "thinking")
    elif t == "result":
        res["text"] = "" if e.get("is_error") else (e.get("result") or "")
        if e.get("is_error"):
            res["error"] = str(e.get("result") or e.get("subtype") or "error")[:300]


def busy_now():
    """{name: activity} for agents working on a reply (stale after ~30 min without any update)."""
    out = {}
    try:
        for fn in os.listdir(ACT):
            if fn.endswith(".json"):
                try:
                    a = json.load(open(os.path.join(ACT, fn)))
                except (OSError, ValueError):
                    continue
                if time.time() - a.get("t", 0) < 1900:
                    a["items"] = live_items(a.get("items", {}), time.time())
                    out[fn[:-5]] = a
    except OSError:
        pass
    return dict(sorted(out.items()))


def describe_cmd(cmd):
    """Turn a shell command into 'reading x' / 'searching y' / 'running `z`'."""
    import re, shlex
    m = re.match(r"^(?:/bin/)?(?:ba)?sh -l?c (.*)$", cmd.strip(), re.S)
    if m:
        try:
            cmd = shlex.split(m.group(1))[0]
        except ValueError:
            cmd = m.group(1).strip("'\"")
    cmd = cmd.replace(CODE + "/", "").replace(CODE, "code").strip()
    if "\n" in cmd:   # multi-line (heredoc / script): first line only, so the status line never wraps
        cmd = cmd.split("\n", 1)[0].rstrip() + " …"
    try:
        w = shlex.split(cmd)
    except ValueError:
        w = cmd.split()
    if not w:
        return "running a command"
    files = [x for x in w[1:] if not x.startswith("-") and ("/" in x or "." in x) and " " not in x]
    if w[0] in ("cat", "sed", "head", "tail", "nl", "less", "bat") and "|" not in cmd and files:
        return "reading " + ", ".join(files[-2:])
    if w[0] in ("rg", "grep", "ag", "git") and "|" not in cmd:
        pat = next((x for x in w[1:] if not x.startswith("-")), "")
        return f"searching `{pat[:30]}`" if pat else "searching"
    if w[0] in ("ls", "find", "tree", "fd"):
        return "listing " + (files[0] if files else "files")
    return f"running `{cmd[:50]}{'…' if len(cmd) > 50 else ''}`"


def describe_tool(tool, params):
    """Antigravity tool step -> short activity text."""
    params = params or {}
    if "CommandLine" in params:
        return describe_cmd(params["CommandLine"])
    path = next((v for k, v in params.items() if isinstance(v, str) and ("Path" in k or "File" in k or k.lower() == "path")), None)
    if path:
        path = path.replace(CODE + "/", "")
        if "view" in tool or "read" in tool:
            return "reading " + path
        if "list" in tool or "dir" in tool:
            return "listing " + path
    q = next((v for k, v in params.items() if isinstance(v, str) and ("Query" in k or "Pattern" in k)), None)
    if q:
        return f"searching `{q[:30]}`"
    return tool.replace("_", " ")


def sleep_summary(zz):
    """One line naming every sleeping agent and what wakes it."""
    if not zz:
        return "everyone awake"
    parts = []
    for n in sorted(zz):
        wf = (asleep(n) or {}).get("from")
        parts.append(n + (f" (waiting for {', '.join(wf)})" if wf else ""))
    return "💤 asleep: " + ", ".join(parts) + " — @name pings one awake"


def act_text(a):
    """One agent's activity as plain text."""
    t = _act_plain(a)
    return a["task"] + ("" if t in ("thinking", "writing") else " · " + t) if a.get("task") else t


def _act_plain(a):
    now = time.time()
    items = sorted((x for x in a.get("items", {}).values() if x.get("until", now + 1) > now), key=lambda x: x["since"])
    if not items:
        return a.get("phase", "thinking")
    fg = [x for x in items if not x["bg"]]
    bg = [x for x in items if x["bg"]]
    parts = []
    if fg:
        parts.append(fg[0]["what"] + (f" +{len(fg) - 1} more" if len(fg) > 1 else ""))
    if bg:
        parts.append(f"{len(bg)} in background" if fg or len(bg) > 1 else bg[0]["what"] + " (background)")
    return " ".join(", ".join(parts).split())   # one line, whatever the tool text held


def lockfile(name):
    os.makedirs(STATE, exist_ok=True)
    return open(os.path.join(STATE, name), "a+")


def parse_dur(d):
    if not d:
        return None
    mult = {"s": 1, "m": 60, "h": 3600}.get(d[-1].lower())
    return float(d[:-1]) * mult if mult else float(d) * 60


__all__ = [k for k in list(globals()) if not k.startswith("__")]
