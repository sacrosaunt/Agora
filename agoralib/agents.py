"""Running the agents: sessions, CLI commands, sandboxes, streaming, one turn."""
import argparse, fcntl, json, os, re, shutil, subprocess, sys, tempfile, time  # noqa: F401
from .compat import *  # noqa: F401,F403
from .ws import *  # noqa: F401,F403
from .store import *  # noqa: F401,F403
from .snapshot import *  # noqa: F401,F403


SESS = os.path.join(STATE, "sessions")  # <name>.json: the agent's persistent CLI session


def session(name, val=None, drop=False):
    """Read (default), write (val) or drop an agent's session record."""
    f = os.path.join(SESS, name + ".json")
    if drop:
        if os.path.exists(f):
            os.unlink(f)
        return None
    if val is not None:
        os.makedirs(SESS, exist_ok=True)
        with open(f + ".tmp", "w") as g:
            json.dump(val, g)
        os.replace(f + ".tmp", f)
        return val
    try:
        return json.load(open(f))
    except (OSError, ValueError):
        return None


def stale(name, s, phash):
    """Why session s must be replaced by a fresh one (None: resume it)."""
    if not s or not s.get("id"):
        return "no session"
    if s.get("turns", 0) >= MAX_TURNS:
        return f"{s['turns']} turns"
    if s.get("tokens", 0) >= MAX_TOKENS:
        return f"{s['tokens']} tokens"
    if s.get("proto") != phash:
        return "protocol changed"
    if s.get("src", SRC_ID) != SRC_ID:
        return "source folder changed"
    return None


def codex_event(name, e, res):
    if e.get("type") == "thread.started":
        res["sid"] = e.get("thread_id")
    it = e.get("item") or {}
    typ, key = it.get("type"), it.get("id")
    if e.get("type") == "item.started":
        if typ == "command_execution":
            act(name, key, describe_cmd(it.get("command", "")))
        elif typ == "web_search":
            act(name, key, "searching the web")
        elif typ == "mcp_tool_call":
            act(name, key, "calling " + str(it.get("tool", "a tool")))
        elif typ == "reasoning":
            busy(name, True, "thinking")
    elif e.get("type") == "item.completed":
        act(name, key)
        if typ == "agent_message":
            busy(name, True, "writing")
        elif typ in ("command_execution", "web_search", "mcp_tool_call"):
            busy(name, True, "thinking")


def codex_context(sid):
    """Current context size of codex session sid: input tokens of its latest model call, from the rollout log
    (the --json turn usage sums all calls of the turn, so it overstates the context)."""
    import glob
    for f in sorted(glob.glob(os.path.join(INSTALL, ".codex-home", "sessions", "*", "*", "*", f"*{sid}.jsonl")))[-1:]:
        last = 0
        for line in open(f):
            if '"token_count"' in line:
                try:
                    info = json.loads(line)["payload"].get("info") or {}
                    last = (info.get("last_token_usage") or {}).get("input_tokens", last)
                except (ValueError, KeyError, AttributeError):
                    pass
        return last
    return 0


def agy_event(name, e, res):
    if e.get("conversation_id"):
        res["sid"] = e["conversation_id"]
    if e.get("event") == "result":
        r = e.get("result") or {}
        res["text"], res["sid"] = r.get("response", ""), r.get("conversation_id") or res.get("sid")
        return
    su = e.get("step_update") or {}
    u = su.get("usage")
    if u:  # context of the latest model call (the result's usage is cumulative over the conversation)
        res["tokens"] = (u.get("input_tokens") or 0) + (u.get("cache_read_tokens") or 0)
    key, st = su.get("step_index"), su.get("state")
    if su.get("step_type") == "tool":
        if st == "ACTIVE":
            info = su.get("tool_info") or {}
            pr = info.get("parameters") or {}
            bg = bool(pr.get("WaitMsBeforeAsync")) or bool(pr.get("Background") or pr.get("RunInBackground"))
            act(name, key, describe_tool(su.get("tool_name", "tool"), pr), bg=bg)
        else:
            act(name, key)
            busy(name, True, "thinking")
    elif su.get("step_type") == "agent_response" and st == "ACTIVE":
        busy(name, True, "writing")


def stream(name, cmd, stdin_text, cwd, env, log, timeout, on_event):
    """Run an agent CLI that prints JSONL events, feeding each to on_event (activity tracking); returns the process."""
    import threading
    p = subprocess.Popen(cmd, cwd=cwd, env=env, text=True, stdin=subprocess.PIPE if stdin_text else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=log)
    killer = threading.Timer(timeout, p.kill)   # started first: a child that never reads its stdin is killed too
    killer.start()
    try:
        if stdin_text:
            try:
                p.stdin.write(stdin_text)
                p.stdin.close()
            except (BrokenPipeError, OSError) as ex:
                log.write(f"[stdin] {ex!r}\n")
        for line in p.stdout:
            try:
                e = json.loads(line)
            except ValueError:
                log.write(line)
                continue
            try:
                on_event(name, e)
            except Exception as ex:  # activity display must never break a turn
                log.write(f"[activity] {ex!r}\n")
        p.wait()
    finally:
        killer.cancel()
    return p


def rules(name):
    return ("Output ONLY your message text "
            "(no preamble). Optionally make the first line `TO: name1,name2` (default: the author of the "
            "latest message), and a line `RE: 12,14` naming the messages you answer (default: all the new ones "
            "addressed to you). If you have nothing worth adding, output only `SLEEP`: nothing is posted and you "
            "stop answering messages to all until someone pings you (`@" + name + "` or addresses you by name). "
            "A first line `SLEEP` before a message posts it and then sleeps. To hold off until a specific "
            "participant has replied (e.g. you asked codex to check something), use a first line `WAIT: codex` "
            "(with or without a message after it): you skip everything until codex posts, then get a turn. "
            "Sleeping is automatic too: after a message addressed only to other agents you wait for their answer; "
            "a ping or the user writing to all wakes you. Questions to all are answered by one agent first; if "
            "another agent has already answered what you were asked, reply only with a correction or something "
            "new, otherwise output only `SLEEP`.\n\n"
            "EVIDENCE (rules 1-2): cite path:line for claims about the code and say what you didn't check; check "
            "another agent's claim in the code before agreeing with it.\n")


LENGTH = {
    "short": "LENGTH (rule 3): 1-5 short lines, about 600 characters: the answer, its evidence, and at most one "
             "line of qualification that changes the conclusion. Go longer only if the user asked for an audit, "
             "review, report or design, and then lead with a one-line summary. No recap of the thread.",
    "long": "LENGTH (rule 3, {mode} request): start with a one-line summary, then short bullets, one finding each, "
            "with path:line evidence, then what you didn't check. As long as the content needs, no longer: no "
            "preamble, recap or closing summary.",
    "independent": "INDEPENDENT ROUND: the others answer the same question without seeing your answer, and all "
                   "answers are revealed together afterwards. Give your own full answer; do not output SLEEP because "
                   "someone else may cover it. ",
    "crosscheck": "CROSS-CHECK: the independent answers to #{q} were just revealed together. Say where the others "
                  "are wrong or missed something, citing code, and whether any of them changed your mind; output "
                  "only SLEEP if you agree and have nothing to add.",
}


def turn_note(new_msgs):
    """The length and round instructions for a turn answering new_msgs."""
    modes = {m.get("mode") for m in new_msgs if m["from"] == "user"}
    rev = sorted({m["independent"] for m in new_msgs if m.get("independent")})
    s = ""
    if "independent" in modes:
        s += LENGTH["independent"]
    long = next((x for x in ("audit", "design", "independent") if x in modes), None)
    s += LENGTH["long"].format(mode=long) if long else LENGTH["short"]
    if rev:
        s += " " + LENGTH["crosscheck"].format(q=", #".join(map(str, rev)))
    return s + "\n"


def snapshot_head():
    """(content digest, HEAD line) of the current code snapshot."""
    try:
        d = json.load(open(os.path.join(STATE, "snapshot.json")))
        return d["digest"], d["head"]
    except (OSError, ValueError, KeyError):
        return "", ""


def opt(name, key, default=None):
    """Per-agent setting: env AGORA_<NAME>_<KEY>, else "<name>_<key>" in agora.json, else default."""
    return os.environ.get(f"AGORA_{name.upper()}_{key.upper()}") or CFG.get(f"{name}_{key}") or default


def agent_cmd(name, sid, work, out):
    """The CLI command for one turn of name: fresh when sid is None, else resuming session sid.
    Returns (cmd, prompt_on_stdin)."""
    if name == "codex":  # codex exec resume has no -C: the cwd (work) is the workspace either way
        base = ["codex", "exec"] + (["resume"] if sid else []) + ["--json", "--skip-git-repo-check"]
        return base + ([] if sid else ["-C", work]) + [
            "-c", "model_reasoning_effort=" + opt("codex", "effort", "medium")] + (
            ["-c", "model=" + json.dumps(opt("codex", "model"))] if opt("codex", "model") else []) + [
            "-o", out] + codex_perms() + ([sid] if sid else []) + ["-"], True
    if name == "gemini":  # Antigravity print mode: workspace = code snapshot + work dir; approvals auto-denied
        return [AGY, "--model", opt("gemini", "model", "gemini-4-hydrogen-high"),
                "--add-dir", CODE] + (["--add-dir", ALSO_DIR] if ALSO else []) + [
                "--input-format", "stream-json", "--output-format", "stream-json"] + (
                ["--conversation", sid] if sid else []) + ["-p="], True   # prompt on stdin, not in argv
    # claude: Claude Code print mode on the local login, read-only tools plus a sandboxed shell; no user
    # settings/hooks, MCP servers or skills (they cost ~150k context tokens per turn); prompt on stdin
    shell = claude_sandbox(work)
    cmd = [CLAUDE, "-p", "--output-format", "stream-json", "--verbose", "--setting-sources", "project,local",
           "--strict-mcp-config", "--disable-slash-commands", "--add-dir", CODE] + (["--add-dir", ALSO_DIR] if ALSO else []) + [
           "--tools", "Read", "Grep", "Glob"] + (["Bash", "--settings", json.dumps(shell)] if shell else [])
    for key in ("model", "effort"):
        if opt("claude", key):
            cmd += ["--" + key, opt("claude", key)]
    return cmd + (["--resume", sid] if sid else []), True


def codex_perms():
    """-c overrides granting this workspace's paths to the permission profile set in agora's Codex home, if any
    (its config names the install's own paths only). Entries add to the profile's; its denies stay."""
    if WS == INSTALL:
        return []
    try:
        cfg = open(os.path.join(INSTALL, ".codex-home", "config.toml")).read()
    except OSError:
        return []
    m = re.search(r'(?m)^default_permissions\s*=\s*"([\w-]+)"', cfg)
    if not m or f"[permissions.{m.group(1)}" not in cfg:
        return []
    fs = {CODE: "read", MD: "read", STATE: "deny"}
    if ALSO:
        fs[ALSO_DIR] = "read"
    if SRC:
        fs[SRC] = "deny"   # the live tree: agents read the snapshot
    return ["-c", f"permissions.{m.group(1)}.filesystem={{" + ", ".join(f"{json.dumps(k)}={json.dumps(v)}"
                                                                       for k, v in fs.items()) + "}"]


def claude_sandbox(work):
    """Claude Code settings giving headless claude a shell like codex's: commands run in Claude Code's OS sandbox
    (bubblewrap on Linux, Seatbelt on macOS), reading only the snapshots and its work dir, writing only its work
    dir, with no network. None (no shell) when off in the config or the sandbox can't run here."""
    on = os.environ.get("AGORA_CLAUDE_SHELL", CFG.get("claude_shell", True))
    if str(on).lower() in ("false", "0", "no", "off") or sys.platform not in ("linux", "darwin"):
        return None
    if sys.platform == "linux" and not (shutil.which("bwrap") and shutil.which("socat")):
        return None
    deny = sorted({os.path.expanduser("~"), "/root", "/home", "/Users", "/mnt", "/media", "/srv", "/Volumes", INSTALL,
                   WS, DATA} | ({SRC} if SRC else set()) | {os.path.realpath(p) for p in ALSO.values()})
    return {"sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
                        "network": {"allowedDomains": []},
                        "filesystem": {"denyRead": deny, "allowRead": [CODE, ALSO_DIR, work],
                                       "denyWrite": [CODE, ALSO_DIR]}}}


def can_shell(name):
    return name != "claude" or claude_sandbox(os.path.join(WS, "work", "claude")) is not None


def agent_env(name):
    """Local CLI logins only: drop API keys (they would switch the CLIs to API billing) and, for claude, the
    variables that tie a nested Claude Code to the session that started agora."""
    drop = {"OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY"}
    env = {k: v for k, v in os.environ.items()
           if k not in drop and not (name == "claude" and (k.startswith("CLAUDE_CODE_") or k in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT")))}
    if name == "codex":
        env["CODEX_HOME"] = os.path.join(INSTALL, ".codex-home")
    return env


def invoke(name, sid, prompt, work, log, timeout):
    """One CLI run of name (fresh when sid is None, else resuming sid). Returns (process, text, res)."""
    out = os.path.join(STATE, name + "_last.md")
    if os.path.exists(out):
        os.unlink(out)
    res = {}
    cmd, on_stdin = agent_cmd(name, sid, work, out)
    if not on_stdin:
        cmd.append("-p=" + prompt)
    elif name == "gemini":   # agy reads stream-json events on stdin
        prompt = json.dumps({"event": "user", "message": {"role": "user", "content": prompt}}) + "\n"
    handler = {"codex": codex_event, "gemini": agy_event, "claude": claude_event}[name]
    r = stream(name, cmd, prompt if on_stdin else None, work, agent_env(name), log, timeout,
               lambda n, e: handler(n, e, res))
    text = (open(out).read() if name == "codex" and os.path.exists(out) else res.get("text") or "").strip()
    if name != "codex":
        log.write(text + "\n")
    if name == "codex" and res.get("sid"):
        res["tokens"] = codex_context(res["sid"])
    return r, text, res


HANDOFF = ("Your session is about to be replaced by a fresh one ({why}); it will get the protocol and the whole "
           "thread again, but not your private working memory. Write a handoff note for it, at most 1500 characters: "
           "open questions, live hypotheses, checks you meant to do, and key facts with path:line. Output only the "
           "note; it is not posted to the thread.")


def run_agent(name, timeout=1800, do_sync=True, served=False):
    """One turn of name at a time (serve and a one-shot `agora NAME` would otherwise answer the same messages)."""
    with lockfile(name + ".run.lock") as lk:
        lock(lk)
        if served and not any(m["id"] > seen(name) and for_me(m, name) for m in load()):
            return "nothing"   # answered by a one-shot run while this one waited
        return _run_agent(name, timeout, do_sync)


def _run_agent(name, timeout=1800, do_sync=True):
    """Run one headless turn of an agent on the thread and post its reply. Resumes the agent's persistent
    session with just the unseen messages, or starts a fresh one (see stale())."""
    if do_sync:
        sync()
    msgs = load()
    thread = "".join(fmt(m) for m in msgs)
    if len(thread) > 200_000:
        thread = "[earlier messages truncated; full thread in " + MD + "]\n\n" + thread[-200_000:]
    new_msgs = [m for m in msgs if for_me(m, name) and m["id"] > seen(name)]
    new = [m["id"] for m in new_msgs]
    note = turn_note(new_msgs)
    asked = [q for q in open_rounds() if q in new]   # independent rounds this turn answers
    work = os.path.join(WS, "work", name)
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, "THREAD.md"), "w") as f:   # decisions first: they survive the truncation
        f.write("# Thread so far\n\n" + decisions_text(msgs) + thread)
    proto = protocol()
    phash = __import__("hashlib").sha1((proto + rules(name)).encode()).hexdigest()[:12]
    head, head_line = snapshot_head()
    fresh_prompt = (proto + f"\n\n# You are: {name}\n\nThe codebase is at {CODE} (read-only). " + also_note() + "The full thread is in "
                    f"{work}/THREAD.md: read it first. Reply to the new message(s) addressed to you: {new}. "
                    "Only read files under the codebase" + (", the extra snapshots" if ALSO else "") + " and that thread file. "
                    + ("You can run read-only shell commands there (git log/show/blame, grep, tests that don't need "
                       "the network). " if can_shell(name) else "") + rules(name)
                    + note)
    log = open(os.path.join(STATE, name + ".log"), "a")
    s = session(name)
    why = stale(name, s, phash)
    if why and why.endswith(("turns", "tokens")):  # a cap, not a fault: let the old session hand over first
        log.write(f"\n=== {time.ctime()} handoff from {s['id']} ({why})\n")
        log.flush()
        busy(name, True, "writing a handoff note")
        r, note, _ = invoke(name, s["id"], HANDOFF.format(why=why), work, log, min(timeout, 600))
        if r.returncode == 0 and note:
            fresh_prompt += ("\n\n# Handoff note from your previous session (private, not in the thread)\n\n"
                             + note[:3000] + "\n")
    for attempt in (0, 1):
        if why:
            s, prompt = None, fresh_prompt
        else:
            unseen = [m for m in msgs if m["id"] > s.get("upto", 0) and m["from"] != name]
            prompt = ("# New in the thread since your last turn\n\n" + ("".join(fmt(m) for m in unseen) or "(nothing)\n")
                      + (f"\nThe code snapshot at {CODE} changed since your last turn (HEAD {head_line}); re-read files you rely on. " + also_note() + "\n"
                         if head != s.get("head") else "")
                      + f"\nReply to the message(s) addressed to you: {new}. Same rules as before: " + rules(name) + note)
        log.write(f"\n=== {time.ctime()} replying to {new}: " + (f"fresh session ({why})" if why else
                  f"resuming {s['id']} (turn {s.get('turns', 0) + 1})") + "\n")
        log.flush()
        r, text, res = invoke(name, s and s["id"], prompt, work, log, timeout)
        if r.returncode == 0 and text and res.get("sid"):
            session(name, {"id": res["sid"], "turns": (s or {}).get("turns", 0) + 1, "tokens": res.get("tokens", 0),
                           "upto": msgs[-1]["id"] if msgs else 0, "proto": phash, "head": head, "src": SRC_ID,
                           "started": (s or {}).get("started") or time.strftime("%Y-%m-%d %H:%M:%S")})
            break
        session(name, drop=True)  # never resume a session whose last turn failed
        if why or r.returncode < 0:  # a fresh run failed, or the turn was killed (timeout): don't retry
            break
        why = f"resume failed (rc {r.returncode})"
        log.write(why + "; retrying fresh\n")
    if r.returncode or not text:   # the messages stay unseen: serve retries with backoff, then gives up visibly
        f = note_failure(name, new, r.returncode)
        log_turn(name, new, "failed", note=f"rc {r.returncode}, try {f['tries']}/{MAX_TRIES}")
        print(f"{name} failed (rc {r.returncode}); see {STATE}/{name}.log", file=sys.stderr)
        return None
    to, zz, wf, re_ = msgs[-1]["from"] if msgs else "all", False, [], list(new)
    for _ in range(4):  # directive lines: TO: ..., RE: ..., SLEEP, WAIT: name, in any order
        first, _, rest = text.strip().partition("\n")
        d = first.strip().strip("`*")
        if d.upper().startswith("TO:"):
            to, text = d[3:].strip(), rest
        elif d.rstrip(".").upper() == "SLEEP":
            zz, text = True, rest
        elif d.upper().startswith("RE:"):
            ids = [int(x) for x in re.findall(r"\d+", d[3:]) if 0 < int(x) <= len(msgs)]
            re_, text = ids or re_, rest
        elif d.upper().startswith("WAIT:"):
            wf = [x.strip().lower() for x in d[5:].split(",") if x.strip().lower() in NAMES and x.strip().lower() != name]
            zz, text = True, rest
    sleep_set(name, False)
    m = post(name, to, text, hold=asked, re_=re_) if text.strip() else None
    if msgs:   # read only once the reply is out: a crash before this retries the turn instead of losing it
        seen(name, max(seen(name), msgs[-1]["id"]))
    failure(name, clear=True)
    if m and m.get("held"):
        log_turn(name, new, "held", note="independent round " + ", ".join(f"#{q}" for q in m["rounds"]))
        return m
    for q in asked:   # slept without answering an independent round it was asked: don't wait for it
        hold_outcome(q, name, slept=True)
    log_turn(name, new, "posted" if m else "slept", msg=m and m["id"])
    if zz:
        sleep_set(name, True, why="waiting for " + ",".join(wf) if wf else "nothing to add", until_from=wf)
        # no deadlock: whoever waits on name gets the turn if name waits on them (e.g. name was the lead) or if
        # name went quiet without posting (their wait would otherwise only end on the timeout)
        for x in AGENTS:
            if x != name and name in ((asleep(x) or {}).get("from") or []) and (x in wf or not m):
                sleep_set(x, False)
                print(f"{x} was waiting for {name}; woken", file=sys.stderr)
        print(f"{name} " + (f"waits for {','.join(wf)}" if wf else "sleeps until pinged"), file=sys.stderr)
    return m or ("slept" if zz else None)


def agent_streak(msgs):
    n = 0
    for m in reversed(msgs):
        if m["from"] == "user":
            break
        n += 1
    return n


__all__ = [k for k in list(globals()) if not k.startswith("__")]
