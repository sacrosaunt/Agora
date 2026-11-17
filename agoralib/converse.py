"""post --via: following a discussion to its end, votes and verdicts."""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time  # noqa: F401
from .compat import *  # noqa: F401,F403
from .ws import *  # noqa: F401,F403
from .store import *  # noqa: F401,F403
from .snapshot import *  # noqa: F401,F403
from .agents import *  # noqa: F401,F403
from .serve import *  # noqa: F401,F403
from .chat import *  # noqa: F401,F403


VOTE_ASK = ("Pick one of: {opts}. Say why in a few lines, and end with a line `VOTE: <option>`; or `VOTE: abstain`, "
            "or `VOTE: insufficient evidence` if the code can't settle it (then say what would).")
VOTE = re.compile(r"^\W*VOTE:\s*(.+?)\W*$", re.I | re.M)
SPECIAL = ("abstain", "insufficient evidence")


def discussion(q, msgs):
    """Ids of q and every message answering it, directly or down a reply chain (the `re` links)."""
    ids = {q["id"]}
    for m in msgs:
        if m["id"] > q["id"] and set(m.get("re") or ()) & ids:
            ids.add(m["id"])
    return ids


def settle(q, timeout, show=print):
    """Stream the messages after question q (a message dict) until it is answered: every served agent expected to
    take it up has had a turn on its discussion (q and the replies to it: posted, had nothing to add, or failed for
    good) and none of the discussion is waiting for an agent (checked twice). Work on other topics doesn't hold it
    up. Ends early when the discussion goes quiet for 2 min with turns missing, and at timeout. Returns
    (messages, outcomes {agent: [str]}, reason, status) with status "answered", "incomplete" or "timeout"."""
    t0, shown, calm, quiet = time.time(), q["id"] - 1, 0, None
    expect = [n for n in AUTO if pinged(q, n) or "all" in q["to"] and (asleep(n) or {}).get("why") != "user"]
    while True:
        msgs = load()
        for m in msgs:
            if m["id"] > shown:
                if m["id"] > q["id"]:
                    show(fmt(m))
                shown = m["id"]
        disc = discussion(q, msgs)
        rows = [r for r in turns_since(t0 - 5) if set(r["ids"]) & disc]
        done = {r["name"] for r in rows if r["outcome"] in ("posted", "slept", "held", "gave up")}
        # an agent's messages stay unread until its reply is posted, so this also covers turns in progress
        live = [n for n in AUTO if any(m["id"] in disc and m["id"] > seen(n) and for_me(m, n) for m in msgs)]
        held = [r for r in open_rounds() if r in disc]
        calm = 0 if live or held else calm + 1
        missing = [n for n in expect if n not in done]
        reason, status = None, None
        if calm >= 2 and not missing:
            reason, status = "answered: every agent asked had its turn and nothing is pending", "answered"
        elif calm >= 2:
            quiet = quiet or time.time()
            if time.time() - quiet > 120:
                reason, status = f"incomplete: quiet for 2 min without a turn from {', '.join(missing)}", "incomplete"
        else:
            quiet = None
        if not reason and time.time() - t0 > timeout:
            reason, status = f"timed out after {timeout:.0f}s; still working: {', '.join(live) or 'none'}", "timeout"
        if reason:
            outcomes = {}
            for r in rows:
                if r["name"] in AGENTS:
                    outcomes.setdefault(r["name"], []).append(
                        f"#{r['msg']}" if r["outcome"] == "posted" else "nothing to add" if r["outcome"] == "slept"
                        else r["outcome"] + (f" ({r['note']})" if r.get("note") else ""))
            for n in expect:
                outcomes.setdefault(n, ["no turn"])
            return [m for m in msgs if m["id"] > q["id"]], outcomes, reason, status
        time.sleep(2)


def vkey(v):
    """A vote or option compared by its words and symbols, ignoring case, spacing and surrounding quotes/marks."""
    return " ".join(v.strip().strip("`*_\"'.,:;!").lower().split())


def norm_vote(v, options):
    """The option (or abstain / insufficient evidence) a VOTE line names, else None."""
    k = vkey(v)
    return next((o for o in tuple(options) + SPECIAL if vkey(o) == k), None)


def tally(msgs, options):
    """{agent: (vote, message)} from each agent's latest VOTE line (a later invalid vote cancels an earlier valid
    one), and {agent: invalid text}."""
    votes, bad = {}, {}
    for m in msgs:
        v = VOTE.findall(m["text"]) if m["from"] in AGENTS else []
        if v:
            pick = norm_vote(v[-1], options)
            if pick:
                votes[m["from"]] = (pick, m)
                bad.pop(m["from"], None)
            else:
                bad[m["from"]] = v[-1].strip()
                votes.pop(m["from"], None)
    return votes, bad


def why(m):
    """An agent's reason for its vote: its message without the VOTE line, in one line."""
    return clip(" ".join(VOTE.sub("", m["text"]).split()), 300)


def verdict(votes, voters, final):
    """(label, option, exit code) for a tally, or None to vote again. Consensus: every voter voted, all for one
    option (abstentions allowed). After the last round: majority (over half of the voters for one option),
    insufficient evidence (over half say so), else no consensus."""
    picks = {n: v for n, (v, _) in votes.items()}
    real = {n: v for n, v in picks.items() if v != "abstain"}
    if set(picks) >= set(voters) and len(set(real.values())) == 1 and "insufficient evidence" not in real.values():
        return "CONSENSUS", next(iter(real.values())), 0
    if not final:
        return None
    for v in set(picks.values()) - {"abstain"}:
        if sum(1 for x in picks.values() if x == v) * 2 > len(voters):
            return ("INSUFFICIENT EVIDENCE", v, 7) if v == "insufficient evidence" else ("MAJORITY", v, 6)
    return "NO CONSENSUS", None, 4


def converse(via, to, text, options=None, rounds=1, timeout=1800.0, mode=None):
    """An agent speaking for the user: post as the user, then follow the discussion it starts until the question
    is answered (see settle), and print who answered and why it stopped. With options it's a vote of every agent,
    re-asked up to `rounds` times until unanimous; agents that gave no valid vote are asked once more within the
    round. Exit code: 0 settled (vote: consensus), 3 timed out, 4 no consensus, 5 incomplete (an agent asked
    never took its turn), 6 majority, 7 insufficient evidence."""
    ensure_serve()
    if not AUTO:
        print("note: no agents are run by `agora serve` here; nothing will answer automatically", file=sys.stderr)
    if options:
        keys = [vkey(o) for o in options]
        if len(set(keys)) < len(keys) or set(keys) & set(SPECIAL) or not all(keys):
            print(f"ambiguous or reserved vote options {options}: they must differ beyond case and spacing, and "
                  f"not be {' or '.join(SPECIAL)}", file=sys.stderr)
            return 2
        to = ",".join(AGENTS)   # named explicitly: everyone votes, sleepers included, no lead-answer turns
    vote = VOTE_ASK.format(opts=", ".join(options)) if options else ""
    body = text.strip() + ("\n\n" + vote if vote else "")

    def follow(q):
        print(f"posted #{q['id']} as user via {via}; following the discussion…"
              + (f" (watch it live: run `agora` in {SRC})" if SRC else ""), file=sys.stderr, flush=True)
        new, outcomes, reason, status = settle(q, timeout)
        print(f"\nSETTLED: {reason}")
        for n, o in sorted(outcomes.items()):
            print(f"  {n}: {', '.join(o)}")
        return new, status

    for rnd in range(1, rounds + 1):
        q = post("user", to, body, via=via, mode=mode)
        new, status = follow(q)
        if status == "timeout":
            return 3
        if not options:
            return 0 if status == "answered" else 5
        votes, bad = tally(new, options)
        redo = [n for n in AUTO if n not in votes]
        if redo:   # one more chance within the round for agents with no valid vote
            nq = post("user", ",".join(redo), " ".join("@" + n for n in redo) + ": you gave no valid vote on #"
                      f"{q['id']}. Reply with your vote now, choosing exactly one of: {', '.join(options)} (or "
                      "abstain / insufficient evidence), as a last line `VOTE: <option>`.", via=via, re_=[q["id"]])
            more, status = follow(nq)
            if status == "timeout":
                return 3
            votes, bad = tally(new + more, options)
        absent = [n for n in AUTO if n not in votes and n not in bad]
        print("votes: " + (", ".join(f"{n} {v}" for n, (v, _) in votes.items()) or "none")
              + (f"; invalid: {', '.join(f'{n} {v!r}' for n, v in bad.items())}" if bad else "")
              + (f"; no vote: {', '.join(absent)}" if absent else ""))
        res = verdict(votes, AUTO, rnd == rounds)
        if res:
            label, pick, code = res
            print(f"{label}" + (f": {pick}" if pick and label != "INSUFFICIENT EVIDENCE" else ""))
            for n, (v, m) in votes.items():
                if label != "NO CONSENSUS" and v != pick:
                    print(f"  minority {n} ({v}, #{m['id']}): {why(m)}")
            if pick:
                post("user", "all", f"Vote on #{q['id']}: {label.lower()}"
                     + (f" for {pick}" if label != "INSUFFICIENT EVIDENCE" else "") + " ("
                     + ", ".join(f"{n} {v}" for n, (v, _) in votes.items()) + ").", via=via, mode="decision",
                     re_=[q["id"]])
            return code
        body = (f"Round {rnd + 1}: no consensus yet on #{q['id']} (" + (", ".join(f"{n}: {v}" for n, (v, _) in
                votes.items()) or "no valid votes") + ")."
                + " Read the others' reasons above, answer the strongest one you disagree with, and vote again. "
                + vote)


__all__ = [k for k in list(globals()) if not k.startswith("__")]
