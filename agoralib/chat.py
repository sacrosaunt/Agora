"""The live terminal chat."""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time  # noqa: F401
import difflib
from .compat import *  # noqa: F401,F403
from .ws import *  # noqa: F401,F403
from .store import *  # noqa: F401,F403
from .snapshot import *  # noqa: F401,F403
from .agents import *  # noqa: F401,F403
from .serve import *  # noqa: F401,F403


def vwidth(s):
    """Terminal columns a string takes: wide East Asian chars and most emoji 2, combining marks 0, and an emoji
    joined on by a zero-width joiner or a skin-tone modifier nothing (they draw as one glyph)."""
    import unicodedata
    w, joined = 0, False
    for c in s:
        if joined or unicodedata.combining(c) or c in "\u200d\ufe0e\ufe0f" or "\U0001f3fb" <= c <= "\U0001f3ff":
            joined = c == "\u200d"
            continue
        w += 2 if unicodedata.east_asian_width(c) in "WF" else 1
    return w


def clip(s, width):
    """s cut to at most width columns, with … when cut."""
    if vwidth(s) <= width:
        return s
    out, w = "", 0
    for c in s:
        w += vwidth(c)
        if w > width - 1:
            break
        out += c
    return out + "…"


HELP = """@name,name at the start   address it (sticky); @name inside a message pings a sleeping agent
/to NAME                  set the recipients
/ind TEXT                 every agent answers blind; answers are revealed together (/independent)
/audit TEXT, /design TEXT ask for a longer structured answer
/decide TEXT              record a decision; /decisions lists them
/sleep NAME [30m] [until NAME], /wake NAME
/last N, /all             show the last N messages, or the whole thread
/status                   agents, sessions, unread counts
/clock                    switch 12/24-hour times
/quit or Ctrl-D           leave (// at the start posts a line beginning with / as text)"""


def chat(to="all", after=None):
    """Live view: prints every new message as it lands and reads your replies from the prompt."""
    ensure_serve()
    import threading
    try:
        import readline   # line editing + history for input() (Windows: pyreadline3)
    except ImportError:   # no line buffer to redraw: a message arriving mid-line is printed above a fresh prompt
        readline = None
    tty = ansi_terminal()
    col = (lambda c, t: f"\033[{c}m{t}\033[0m") if tty else (lambda c, t: t)
    state = {"to": to, "last": after if after is not None else (load()[-1]["id"] if load() else 0),
             "busy": {}, "stop": False, "mine": set(), "shown": False, "frame": 0, "reading": False, "posting": False,
             "toff": os.path.getsize(TURNS) if os.path.exists(TURNS) else 0}
    lock = threading.Lock()

    def plain_prompt():
        return f"[to {state['to']}] > "

    def prompt():   # for our own writes
        return col("2", f"[to {state['to']}] ") + "> "

    def rl_prompt():   # for input(): escapes inside \001…\002 so readline counts only the visible characters
        return f"\001\033[2m\002[to {state['to']}] \001\033[0m\002> " if tty and not WINDOWS else plain_prompt()

    try:   # readline's cursor position (a byte offset in the line), for redraws while you edit mid-line
        import ctypes
        rl_point = None if WINDOWS else ctypes.c_int.in_dll(ctypes.CDLL(readline.__file__), "rl_point")
    except Exception:
        rl_point = None

    def line_buffer():
        return readline.get_line_buffer() if readline else ""

    def write_then_prompt(text, buf):
        """Write text, then the prompt and the line being typed if input() is waiting. pyreadline3 (Windows)
        repaints at the screen position where it drew the prompt, so it is told the new one and redraws itself."""
        if not state["reading"]:
            return sys.stdout.write(text)
        if WINDOWS and readline and hasattr(readline, "rl"):
            sys.stdout.write(text)
            sys.stdout.flush()
            try:
                readline.rl.prompt_begin_pos = readline.rl.console.pos()
                readline.rl._update_line()
                return
            except Exception:
                pass
        sys.stdout.write(text + prompt() + hl_input(buf))

    def up():
        """Rows from the cursor up to the prompt's first row."""
        if not state["reading"]:
            return 0
        buf = line_buffer()
        if rl_point is not None:
            buf = buf.encode()[:max(0, rl_point.value)].decode(errors="ignore")
        return (vwidth(plain_prompt()) + vwidth(buf)) // max(1, shutil.get_terminal_size().columns)

    def mv(n):
        return f"\033[{n}A" if n > 0 else ""

    SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def zz_text():
        zz = state.get("zz") or {}
        return ("💤 " + ", ".join(n + (f" (waits for {', '.join(w)})" if w else "") for n, w in sorted(zz.items()))
                if zz else "")

    def has_line():
        return tty and bool(state["busy"] or state.get("zz") or state.get("notes"))

    def notes_text():
        return "  ".join(state.get("notes") or [])

    def typing_line():
        """The status line above the prompt: who is working on what, then who is asleep."""
        width = shutil.get_terminal_size().columns - 1
        nt = notes_text()
        if not state["busy"]:
            z = "  ".join(x for x in (nt, zz_text()) if x)
            return col("33" if nt else "2", clip(z, width))
        out, used = col("2", SPIN[state["frame"] % len(SPIN)]), 1
        for n, a in state["busy"].items():
            room = width - used - len(n) - 5
            if room < 8:
                out += col("2", " …")
                break
            what = clip(act_text(a), room)
            out += (col("2", " ·") if used > 1 else "") + " " + col(COLORS.get(n, "0"), n) + " " + col("2", what)
            used += len(n) + vwidth(what) + 4
        for z, c in ((nt, "33"), (zz_text(), "2")):
            if z and width - used > 12:
                z = clip("  " + z, width - used)
                out += col(c, z)
                used += vwidth(z)
        return out

    def emit(text):
        """Print above the prompt, keeping the typing line (if any) directly above what you are typing."""
        with lock:
            buf = line_buffer()
            out = ("\r" + mv(up() + (1 if state["shown"] else 0)) + "\033[J" if tty else "") + text + "\n"
            state["shown"] = has_line()
            if state["shown"]:
                out += typing_line() + "\n"
            write_then_prompt(out, buf)   # the prompt only while input() waits (else input() draws it)
            sys.stdout.flush()

    SLASH = ("help", "independent", "ind", "audit", "design", "decide", "decisions", "sleep", "wake", "to", "last", "all",
             "status", "clock", "quit", "q", "exit")   # the commands the input loop below accepts

    def hl_input(buf):
        """The typed line with pings coloured, including every name in a leading @a,b recipient list, and a leading
        valid /command in bold."""
        cmd = re.match(r"/(?:" + "|".join(SLASH) + r")(?=\s|$)", buf) if tty else None
        if cmd:
            return "\033[1m" + cmd.group(0) + "\033[22m" + pings(buf[cmd.end():])
        lead = re.match(r"@[\w,]+", buf) if tty else None
        if not lead:
            return pings(buf)
        names = lead.group(0)[1:].split(",")
        head = pings("@" + names[0]) + "".join("," + (f"\033[1;{COLORS[n.lower()].removeprefix('1;')}m{n}\033[22;39m"
                                                     if n.lower() in COLORS else n) for n in names[1:])
        return head + pings(buf[lead.end():])

    def paint_input():
        """Colour @name pings (and bold a leading /command) in the line being typed. readline draws it plain; this repaints the same characters in
        place (cursor saved and restored), so readline's view of the screen stays right; wrapped lines included."""
        buf = line_buffer()
        if buf == state.get("painted") or not state["reading"]:
            return
        before, state["painted"] = state.get("painted") or "", buf
        if "@" not in buf + before and not buf.startswith("/") and not before.startswith("/"):
            return
        sys.stdout.write("\0337" + mv(up()) + "\r" + prompt() + hl_input(buf) + "\033[J\0338")
        sys.stdout.flush()

    def animate():
        while not state["stop"]:
            time.sleep(0.05)
            if not tty:
                continue
            with lock:
                paint_input()
                state["frame"] += 1
                if state["frame"] % 2:
                    continue
                line = typing_line() if has_line() else None
                if line is not None and state["shown"]:  # redraw the line above the prompt in place
                    if line == state.get("drawn"):
                        continue
                    sys.stdout.write("\0337" + mv(up() + 1) + "\r\033[K" + line + "\0338")
                elif line is not None:
                    write_then_prompt("\r" + mv(up()) + "\033[J" + line + "\n", line_buffer())
                    state["shown"] = True
                elif state["shown"]:
                    write_then_prompt("\r" + mv(up() + 1) + "\033[J", line_buffer())
                    state["shown"] = False
                else:
                    continue
                state["drawn"] = line
                sys.stdout.flush()

    def pings(out):  # @name mentions in bold, in that participant's colour
        if not tty:
            return out
        def hl(g):
            c = COLORS.get(g.group(1).lower(), "37").removeprefix("1;")
            return f"\033[1;{c}m{g.group(0)}\033[22;39m"
        return re.sub(r"(?<![\w@])@(" + "|".join(NAMES + ("all",)) + r")\b", hl, out, flags=re.I)

    def show(m):
        head = (col(COLORS.get(m["from"], "0"), f"── #{m['id']} {author(m)}") + col("2", f" → {', '.join(m['to'])}")
                + col("35", tag(m)) + col("2", f" · {hhmm(m['ts'])}"))
        emit(head + "\n" + pings(md(m["text"])))

    def md(text):
        if not tty:
            return text.rstrip() + "\n"
        try:
            import io
            from rich.console import Console
            from rich.markdown import Markdown
            buf = io.StringIO()
            Console(file=buf, force_terminal=True, width=min(shutil.get_terminal_size().columns, 110)).print(
                Markdown(text, code_theme="ansi_dark", hyperlinks=False))
            return buf.getvalue().rstrip() + "\n"
        except Exception:
            return text.rstrip() + "\n"

    def follow():
        while not state["stop"]:
            for m in load():
                if state["posting"]:   # your message is being posted: the input loop shows it (in place of your line)
                    break
                if m["id"] > state["last"]:
                    state["last"] = m["id"]
                    if m["id"] not in state["mine"]:  # your own lines are already on screen
                        show(m)
            state["busy"] = busy_now()
            notes = []
            for q in open_rounds():
                notes.append(f"◆ #{q} independent: {len(round_status(q))}/{len(round_members())} in")
            if os.path.exists(os.path.join(STATE, "halted")):
                notes.append("⏸ budget reached: agents wait for you")
            state["notes"] = notes
            try:
                with open(TURNS) as fh:
                    fh.seek(state["toff"])
                    chunk = fh.read()
                chunk = chunk[:chunk.rfind("\n") + 1]   # whole lines only; a half-written one waits
                state["toff"] += len(chunk.encode())
            except OSError:
                chunk = ""
            for l in chunk.splitlines():
                try:
                    r = json.loads(l)
                except ValueError:
                    continue
                ids = ", ".join(f"#{i}" for i in r["ids"][-3:])
                o, n = r["outcome"], r["name"]
                if o == "slept":
                    emit(col("2", f"{n}: nothing to add ({ids})"))
                elif o == "held":
                    emit(col("2", f"{n} answered ({r['note']}; held until all are in)"))
                elif o == "failed":
                    emit(col("31", f"{n} failed on {ids}: {r['note']}"))
                elif o == "gave up":
                    emit(col("31", f"{n} gave up on {ids} {r['note']}; those messages are skipped"))
                elif o == "budget":
                    emit(col("33", f"budget reached ({r['note']} since you spoke): agents wait for your next message"))
                elif o == "revealed":
                    emit(col("35", f"◆ independent round {ids} revealed: {r['note']}"))
            zz = {n: tuple((asleep(n) or {}).get("from") or ()) for n in AGENTS
                  if asleep(n) and n not in state["busy"]}  # who sleeps, and on whom (a woken agent at work is awake)
            if zz != state.setdefault("zz", zz):
                if not tty:   # in a terminal the status line above the prompt shows this in place
                    woke = sorted(set(state["zz"]) - set(zz))
                    emit((f"{', '.join(woke)} awake; " if woke else "") + sleep_summary(set(zz)))
                state["zz"] = zz
            if not tty and list(state["busy"]) != state.get("said"):
                state["said"] = list(state["busy"])
                if state["busy"]:
                    emit("… " + "; ".join(f"{n} {act_text(a)}" for n, a in state["busy"].items()))
            time.sleep(0.5)

    print(col("2", "Type a message and press Enter to post it to all. /help lists the commands; /quit leaves."))
    if not tty and any(asleep(n) for n in AGENTS):
        print(sleep_summary({n for n in AGENTS if asleep(n)}))
    if after is not None:
        for m in load():
            if m["id"] > after:
                show(m)
                state["last"] = m["id"]
        after = None
    threading.Thread(target=follow, daemon=True).start()
    threading.Thread(target=animate, daemon=True).start()
    def reading():  # readline has drawn its prompt and waits for keys: from now on redraws must redraw the prompt
        with lock:
            state["reading"] = True
    if readline:
        readline.set_pre_input_hook(reading)
    while True:
        if not readline:
            reading()
        try:
            line = input(rl_prompt())
        except (EOFError, KeyboardInterrupt):
            with lock:
                if tty:   # clear the status line and whatever was typed
                    sys.stdout.write("\r" + mv(up() + (1 if state["shown"] else 0)) + "\033[J")
                state["reading"] = False
                state["stop"] = True
            print()
            break
        with lock:
            state["reading"] = False   # until the next input() draws its own prompt
        cols = shutil.get_terminal_size().columns
        vis = vwidth(plain_prompt()) + vwidth(line)
        echo_rows = max(1, -(-vis // cols))   # rows your typed line takes = rows from its first row to the cursor
        with lock:  # Enter left the status line above your line: clear both, redraw your line in its place
            if state["shown"] and tty:
                sys.stdout.write(f"\033[{echo_rows + 1}A\r\033[J" + prompt() + hl_input(line) + "\r\n")
                sys.stdout.flush()
                state["shown"] = False
        line = line.strip()
        if not line:
            continue
        if line in ("/q", "/quit", "/exit"):
            with lock:
                state["stop"] = True
            break
        if line == "/all" or line.startswith("/last"):
            n = line[5:].strip()
            msgs = load()
            for m in (msgs if line == "/all" else msgs[-(int(n) if n.isdigit() else 20):]):
                show(m)
            continue
        if line in ("/help", "/?"):
            emit(col("2", HELP))
            continue
        if line == "/status":
            emit(subprocess.run(SELF + ["--dir", WS, "status"], capture_output=True, text=True).stdout.rstrip())
            continue
        if line.startswith(("/sleep", "/wake")):
            cmd, *rest = line[1:].split()
            for n in (rest[:1] or [""])[0].split(","):
                if n not in AGENTS:
                    emit(col("31", f"usage: /{cmd} NAME[,NAME] [30m] [until NAME]; agents: {', '.join(AGENTS)}"))
                    break
                wf = rest[rest.index("until") + 1].split(",") if "until" in rest[:-1] else []
                dur = [x for x in rest[1:] if x != "until" and x not in wf]
                try:
                    sleep_set(n, cmd == "sleep", parse_dur(dur[0]) if cmd == "sleep" and dur else None, "user", wf)
                except ValueError:
                    emit(col("31", f"bad duration {rest[1]!r}; e.g. 30m, 2h"))
            continue
        if line.startswith("/to"):
            state["to"] = line[3:].strip() or "all"
            continue
        if line == "/clock":
            CLOCK["12h"] = not CLOCK["12h"]
            save_cfg(ICONFIG, {"clock": "12h" if CLOCK["12h"] else "24h"})
            emit(col("2", f"times now {'12' if CLOCK['12h'] else '24'}-hour (saved for every workspace)"))
            continue
        if line == "/decisions":
            emit(decisions_text(load()).rstrip() or "no decisions recorded yet (/decide TEXT records one)")
            continue
        mode = None
        mm = re.match(r"/(independent|ind|audit|design|decide)\b\s*(.*)", line, re.S)
        if line.startswith("//"):   # posted as text, one slash
            line = line[1:]
        elif not mm and re.match(r"/[A-Za-z]+(\s|$)", line):   # an unknown /word (a path like /src/x isn't one)
            w = line.split()[0]
            near = difflib.get_close_matches(w[1:].lower(), SLASH, 1, 0.6)
            emit(col("31", f"unknown command {w}" + (f" (did you mean /{near[0]}?)" if near else "")
                     + "; start with // to post it as text"))
            continue
        elif mm:
            mode, line = {"ind": "independent", "decide": "decision"}.get(mm.group(1), mm.group(1)), mm.group(2).strip()
            if not line:
                emit(col("31", f"usage: /{mm.group(1)} TEXT"))
                continue
        if line.startswith("@"):
            tgt, _, rest = line[1:].partition(" ")
            state["to"] = tgt
            if tgt.lower() != "all":   # a leading @all stays in the text: it asks everyone (no lead turn)
                line = rest.strip()
            elif not rest.strip():
                continue
            if not line:
                continue
        state["posting"] = True
        try:
            m = post("user", "all" if mode == "decision" else state["to"], line, mode=mode)
        except SystemExit as e:
            state["posting"] = False
            emit(col("31", str(e)))
            continue
        seen("user", max(seen("user"), m["id"]))
        with lock:
            fresh = state["last"] == m["id"] - 1   # nothing arrived in between, so your line is right above
            state["last"] = max(state["last"], m["id"])
        if tty and fresh:   # swap your typed line for the numbered message
            with lock:
                sys.stdout.write(f"\033[{echo_rows}A\r\033[J")
            show(m)
        else:
            state["mine"].add(m["id"])
            emit(col("2", f"posted #{m['id']}"))
        state["posting"] = False
    state["stop"] = True


__all__ = [k for k in list(globals()) if not k.startswith("__")]
