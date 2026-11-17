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


HELP = """Who answers
  TEXT                      (to all) one agent leads, in turns; the others add only corrections
  @all TEXT                 everyone answers at once, each seeing the replies already in
  /ind TEXT                 everyone answers blind; the answers are shown together, then cross-checked
                            (/ind /audit TEXT: long answers; /reveal: show the answers in so far now)
  @name,name TEXT           only them, and later lines too (/to all switches back)
  @name inside a message    pings that agent (wakes it if asleep); not inside `code`
Commands
/to NAME                  set the recipients
/audit TEXT, /design TEXT ask for a longer structured answer
/decide TEXT              record a decision; /decisions lists them
/stop [NAME]              stop every turn, running or queued (NAME: only its reply; whoever waits on it goes on)
/restart [now]            restart the serve on the code now on disk (e.g. after git pull); it waits for the
                          replies in progress, or with now stops them and they are answered again
/sleep NAME [30m] [until NAME], /wake NAME
/last N, /all             show the last N messages, or the whole thread
/add [NAME], /remove [NAME]   bring an agent into this chat, or take it out (no name: pick from a list)
/status                   agents, sessions, unread counts
/clock                    switch 12/24-hour times
/quit or Ctrl-D           leave (// at the start posts a line beginning with / as text)
Pasting several lines posts them as one message."""


HIST = 200   # messages printed again after a resize


def clip(text, width):
    """text cut to width visible columns, keeping its colour codes (and resetting them if cut)."""
    out, n = [], 0
    for part in re.split(r"(\x1b\[[0-9;?]*[A-Za-z])", text):
        if part.startswith("\x1b"):
            out.append(part)
            continue
        if n + len(part) > width:
            out.append(part[:max(0, width - n - 1)] + "…\033[0m")
            return "".join(out)
        out.append(part)
        n += len(part)
    return "".join(out)


def choose(title, options, col=None, ticked=(), empty_ok=False, status=None):
    """A checklist in the terminal: options [(name, note)]; ↑/↓ (or j/k) move, Space (or the item's number) ticks,
    Enter confirms, Esc or q cancels. ticked: names ticked at the start. status(ticked) -> a line shown under the
    title, redrawn as you tick. Plain: Enter with nothing ticked takes the highlighted one, cancel returns [].
    empty_ok: Enter returns what is ticked (possibly nothing), cancel returns None. Not in a terminal it returns
    None at once (callers fall back to typed input). Long lists scroll. Draws below the cursor, erases itself."""
    col = col or (lambda c, t: f"\033[{c}m{t}\033[0m")
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or not options:
        return None
    if WINDOWS:
        import msvcrt
        def key():
            c = msvcrt.getwch()
            if c in ("\x00", "\xe0"):
                return {"H": "up", "P": "down", "I": "pgup", "Q": "pgdn"}.get(msvcrt.getwch(), "")
            return c
        restore = lambda: None
    else:
        import termios, tty, select
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        restore = lambda: termios.tcsetattr(fd, termios.TCSADRAIN, old)
        def key():
            c = os.read(fd, 1).decode(errors="ignore")
            if c == "\x1b":
                if not select.select([fd], [], [], 0.05)[0]:
                    return "\x1b"   # Esc by itself
                seq = os.read(fd, 2).decode(errors="ignore")   # one sequence at a time: a wheel burst sends many
                if seq in ("[5", "[6"):
                    os.read(fd, 1)   # the "~" of PgUp / PgDn
                return {"[A": "up", "[B": "down", "OA": "up", "OB": "down", "[5": "pgup", "[6": "pgdn"}.get(seq, "")
            return c
    names = [n for n, _ in options]
    cur, ticked, drawn, top = 0, {n for n in ticked if n in names}, 0, 0
    w = max(len(n) for n in names)
    try:
        sys.stdout.write("\033[?25l")   # no cursor while picking
        while True:
            height = max(3, shutil.get_terminal_size((80, 24)).lines - 4 - (1 if status else 0))
            top = min(max(top, cur - height + 1), cur, max(0, len(options) - height))
            rows = [col("1", title) + col("2", "   ↑↓ move · Space tick · Enter confirm · Esc cancel")]
            if status:
                rows.append(status(set(ticked)))
            for i in range(top, min(len(options), top + height)):
                n, note = options[i]
                mark = "[x]" if n in ticked else "[ ]"
                line = f"{'›' if i == cur else ' '} {str(i + 1).rjust(len(str(len(options))))}. {mark} {n.ljust(w)}  "
                rows.append((col("1", line) if i == cur else line) + col("2", note))
            if len(options) > height:
                rows.append(col("2", f"  ({top + 1}-{min(len(options), top + height)} of {len(options)})"))
            width = shutil.get_terminal_size((80, 24)).columns - 1
            rows = [clip(r, width) for r in rows]   # a wrapped row would throw off the redraw and push the list down
            sys.stdout.write((f"\033[{drawn}A" if drawn else "") + "".join("\r\033[K" + r + "\n" for r in rows)
                             + ("\033[J" if drawn > len(rows) else ""))
            sys.stdout.flush()
            drawn = len(rows)
            k = key()
            if k in ("up", "k"):
                cur = (cur - 1) % len(options)
            elif k in ("down", "j", "\t"):
                cur = (cur + 1) % len(options)
            elif k == "pgup":
                cur = max(0, cur - height)
            elif k == "pgdn":
                cur = min(len(options) - 1, cur + height)
            elif k == " ":
                ticked ^= {names[cur]}
            elif k.isdigit() and 1 <= int(k) <= len(options):
                cur = int(k) - 1
                ticked ^= {names[cur]}
            elif k in ("\r", "\n"):
                if empty_ok:
                    return [n for n in names if n in ticked]
                return [n for n in names if n in ticked] or [names[cur]]
            elif k in ("\x1b", "q", "\x03", "\x04"):
                return None if empty_ok else []
    finally:
        restore()
        sys.stdout.write((f"\033[{drawn}A" if drawn else "") + "\r\033[J\033[?25h")
        sys.stdout.flush()


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
             "busy": {}, "stop": False, "mine": set(), "hist": [], "shown": False, "frame": 0, "reading": False, "posting": False,
             "toff": os.path.getsize(TURNS) if os.path.exists(TURNS) else 0, "members": round_members()}
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

    def seated():
        """Who is in this chat: the served agents, any agent run by hand that was active lately, and anyone asleep."""
        return [n for n in AGENTS if n in (state.get("members") or ()) or n in (state.get("zz") or {})]

    def members_text():
        """The idle status line's roll call: each agent in its colour, a sleeping one dim with 💤 (and on whom it
        waits). Plain text (with its visible width) when colour is off."""
        zz, parts = state.get("zz") or {}, []
        for n in seated():
            if n in zz:
                w = f" (waits for {', '.join(zz[n])})" if zz[n] else ""
                parts.append((col("2", f"{n} 💤{w}"), len(n) + 3 + len(w)))
            else:
                parts.append((col(COLORS.get(n, "0"), n), len(n)))
        if not parts:
            return "", 0
        head = "in this chat: "
        return col("2", head) + col("2", " · ").join(p for p, _ in parts), len(head) + sum(w for _, w in parts) + 3 * (len(parts) - 1)

    def has_line():
        return tty and bool(state["busy"] or state.get("zz") or state.get("notes") or state.get("members"))

    def notes_text():
        return "  ".join(state.get("notes") or [])

    def typing_line():
        """The status line above the prompt: who is working on what, then who is asleep."""
        width = shutil.get_terminal_size().columns - 1
        nt = notes_text()
        if not state["busy"]:
            who, ww = members_text()
            if not nt:
                return who if ww <= width else col("2", clip(zz_text() or "", width))
            out = col("33", clip(nt, width))
            return out + ("  " + who if who and vwidth(nt) + 2 + ww <= width else "")
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

    def emit(text, again=False):
        """Print above the prompt, keeping the typing line (if any) directly above what you are typing.
        again: replace the line printed last (it is redrawn with more in it)."""
        with lock:
            buf = line_buffer()
            state["slept"] = None   # the next "nothing to add" starts a new line
            out = ("\r" + mv(up() + (1 if state["shown"] else 0) + (1 if again else 0)) + "\033[J" if tty else "") \
                + text + "\n"
            state["shown"] = has_line()
            if state["shown"]:
                out += typing_line() + "\n"
            write_then_prompt(out, buf)   # the prompt only while input() waits (else input() draws it)
            sys.stdout.flush()

    SLASH = ("help", "independent", "ind", "audit", "design", "decide", "decisions", "sleep", "wake", "to", "last", "all",
             "status", "clock", "add", "remove", "kick", "stop", "restart", "reveal", "quit", "q", "exit")   # the commands the input loop below accepts

    def recips(s):
        """'codex,@claude' (spaces or commas) as a recipient list 'codex,claude'; '' for none; False after
        reporting an unknown name."""
        names = [t.lstrip("@").lower() for t in re.split(r"[\s,]+", s.strip()) if t.strip("@ ")]
        bad = [n for n in names if n not in NAMES + ("all",)]
        if bad:
            near = difflib.get_close_matches(bad[0], NAMES + ("all",), 1, 0.5)
            emit(col("31", f"no one called {bad[0]!r}" + (f" (did you mean {near[0]}?)" if near else "")
                     + f"; recipients: {', '.join(NAMES + ('all',))}. Nothing was posted."))
            return False
        return ",".join(names)

    def paste_rest():
        """The rest of a multi-line paste: the lines that arrived with the one input() returned and still wait in
        the terminal. Returns (lines, whether the terminal already echoed them)."""
        if not tty:
            return [], False
        out = []
        if WINDOWS:   # the console echoes them as input() reads them
            import msvcrt
            time.sleep(0.03)
            while msvcrt.kbhit():
                try:
                    out.append(input())
                except (EOFError, KeyboardInterrupt):
                    break
                time.sleep(0.03)
            return out, True
        import select   # readline read up to the first newline; the rest arrived unechoed, while it had echo off
        buf = b""
        try:
            while select.select([sys.stdin], [], [], 0.03)[0]:
                b = os.read(sys.stdin.fileno(), 65536)
                if not b:
                    break
                buf += b
        except (OSError, ValueError):
            pass
        return buf.decode(errors="replace").splitlines(), False

    def address(line):
        """A leading @name,name: sets the recipients (sticky) and comes off the text. A leading @all stays in it
        (it asks everyone: no lead turn) unless a /command follows. None: nothing to post (a typo, or no text)."""
        if not re.match(r"@\s*[A-Za-z]", line):
            return line
        tgt, _, rest = line[1:].lstrip().partition(" ")   # "@ codex hi" works like "@codex hi"
        tgt, punct = re.match(r"(.*?)([?!.:;]*)$", tgt).groups()   # "@codex: hi", "@codex?"
        tgt = recips(tgt)
        if tgt is False:   # a typo: nothing posted, and the recipients stay what they were
            return None
        state["to"] = tgt
        rest = rest.strip()
        if punct.strip(":;"):   # "@codex? are you there": a question to them, posted as typed
            return line
        if not rest:
            return None
        return rest if tgt.lower() != "all" or rest.startswith("/") else line

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
        size, since = shutil.get_terminal_size().columns, None
        while not state["stop"]:
            time.sleep(0.05)
            if not tty:
                continue
            c = shutil.get_terminal_size().columns
            if c != size:   # wait until the resize settles (dragging fires many), then lay everything out again
                size, since = c, time.time()
            elif since and time.time() - since > 0.3:
                since = None
                redraw()
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

    def pings(out, only=None):  # @name mentions in bold, in that participant's colour (only: just these names)
        if not tty or only == []:
            return out
        def hl(g):
            c = COLORS.get(g.group(1).lower(), "37").removeprefix("1;")
            return f"\033[1;{c}m{g.group(0)}\033[22;39m"
        return re.sub(r"(?<![\w@])@(" + "|".join(only or NAMES + ("all",)) + r")\b", hl, out, flags=re.I)

    def cites(out):
        """Inline #N references to messages in the thread: bold, in the cited author's colour."""
        if not tty or "#" not in out:
            return out
        au = state.setdefault("authors", {})
        def one(q):
            q = int(q)
            # older than what this chat has shown, or newer than any message seen: re-read the thread
            if q not in au and (not state.get("all_authors") or q > max(au, default=0)):
                au.update({x["id"]: x["from"] for x in load()})
                state["all_authors"] = True
            if q not in au:
                return None
            return COLORS.get(au[q], "37").removeprefix("1;")
        def hl(g):
            # "#86,89,91" and "#97–98": the bare numbers after a citation are citations too
            parts = re.split(r"(#?\d+)", g.group(0)[len(g.group(1)):])
            return g.group(1) + "".join(f"\033[1;{c}m{p}\033[22;39m" if i % 2 and (c := one(p.lstrip("#"))) else p
                                        for i, p in enumerate(parts))
        # preceded by a start, a non-word character or a colour code (rich styles text right before it)
        return re.sub(r"(^|\x1b\[[0-9;]*m|[^\w#&/])#\d{1,6}(?:(?:,|, #|[–-])#?\d{1,6})*(?!\w)",
                      hl, out, flags=re.M)

    def render(m):
        state.setdefault("authors", {})[m["id"]] = m["from"]
        head = (col(COLORS.get(m["from"], "0"), f"── #{m['id']} {author(m)}") + col("2", f" → {', '.join(m['to'])}")
                + col("35", tag(m)) + col("2", f" · {hhmm(m['ts'])}"))
        return head + "\n" + cites(pings(md(m["text"]), [n for n in NAMES + ("all",) if mentioned(m["text"], n)]))

    def show(m):
        if m.get("hidden"):   # agent bookkeeping (a conclusion's ask, draft and checks): the final one shows
            state.setdefault("hid", set()).add(m["id"])
            return
        state["hist"].append(m)
        emit(render(m))

    def redraw():
        """After a resize: clear the screen and scrollback and print the messages shown so far again at the new
        width (the terminal's own re-wrap of lines laid out for the old width breaks words and indents)."""
        with lock:
            buf = line_buffer()
            out = "\033[H\033[2J\033[3J" + "".join(render(m) + "\n" for m in state["hist"][-HIST:])
            state["shown"] = has_line()
            if state["shown"]:
                out += typing_line() + "\n"
            write_then_prompt(out, buf)
            sys.stdout.flush()

    def md(text):
        if not tty:
            return text.rstrip() + "\n"
        width = min(shutil.get_terminal_size().columns, 110)
        try:
            import io
            from rich.console import Console
            from rich.markdown import Markdown
            buf = io.StringIO()
            Console(file=buf, force_terminal=True, width=width).print(
                Markdown(text, code_theme="ansi_dark", hyperlinks=False))
            # rich pads every line to the full width; a narrower window would wrap that padding into blank rows
            return re.sub(r"[ \t]+((?:\x1b\[[0-9;]*m)*)$", r"\1", buf.getvalue().rstrip(), flags=re.M) + "\n"
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
            state["members"] = round_members()
            notes = []
            for q in open_rounds():
                st = round_status(q)
                ans = sorted(h["ts"] for h in st.values() if h.get("text") and h.get("ts"))
                hint = ""   # 2+ answers in and the rest still at it a minute after the last one: offer them now
                if len(ans) >= 2 and len(st) < len(members(q)):
                    try:
                        if time.time() - time.mktime(time.strptime(ans[-1], "%Y-%m-%d %H:%M:%S")) > 60:
                            hint = f" · /reveal shows the {len(ans)} in now"
                    except ValueError:
                        pass
                notes.append(f"◆ #{q} independent: {len(st)}/{len(members(q))} in{hint}")
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
                if r["ids"] and set(r["ids"]) <= state.get("hid", set()) and o in ("slept", "posted"):
                    continue   # a turn on hidden bookkeeping only (e.g. a check with nothing to object)
                if o == "slept":   # the same agent passing again, with nothing in between: one line
                    prev = state.get("slept") if tty else None
                    same = bool(prev) and prev[0] == n
                    got = prev[1] + [i for i in r["ids"] if i not in prev[1]] if same else list(r["ids"])
                    emit(col("2", f"{n}: nothing to add ({', '.join(f'#{i}' for i in got[-4:])})"), again=same)
                    state["slept"] = (n, got)
                elif o == "approved":
                    emit(col("2", f"{n} approved the conclusion of #{r['note']}"))
                elif o == "held":
                    emit(col("2", f"{n} answered ({r['note']}; held until all are in)"))
                elif o == "failed":
                    emit(col("31", f"{n} failed on {ids}: {r['note']}"))
                elif o == "gave up" and not r["note"].startswith("(not retried"):   # the failure line said it
                    emit(col("31", f"{n} gave up on {ids} {r['note']}; those messages are skipped"))
                elif o == "budget":
                    emit(col("33", f"budget reached ({r['note']} since you spoke): agents wait for your next message"))
                elif o == "stopped":
                    emit(col("33", f"■ {n} stopped on {ids}: {r['note']}"))
                elif o == "restart":
                    emit(col("36", f"◇ {r['note']}"))
                elif o == "notice":
                    emit(col("33", f"⚠ {r['note']}"))
                elif o == "roster":
                    emit(col("36", f"◇ {r['note']}"))
                    state["members"] = round_members()
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
    if not tty and state["members"]:   # a terminal shows the roll call in the status line instead
        print("In this chat: " + ", ".join(state["members"]))
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
            state["slept"] = None      # your line now sits under it: don't redraw over it
        cols = max(1, shutil.get_terminal_size().columns)   # 0 in a pty with no size
        rows = lambda t: max(1, -(-vwidth(t) // cols))
        echo_rows = rows(plain_prompt() + line)   # rows your typed line takes = rows from its first row to the cursor
        extra, echoed = paste_rest()   # the other lines of a paste: one message, not one per line
        if echoed:
            echo_rows += sum(rows(x) for x in extra)
        with lock:  # Enter left the status line above your line: clear both, redraw your line in its place
            if tty and (state["shown"] or extra):
                sys.stdout.write(f"\033[{echo_rows + (1 if state['shown'] else 0)}A\r\033[J" + prompt() + hl_input(line)
                                 + "\r\n" + "".join(pings(x) + "\r\n" for x in extra))
                sys.stdout.flush()
                state["shown"] = False
        if extra and not echoed:
            echo_rows += sum(rows(x) for x in extra)
        line = "\n".join([line] + extra).strip()
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
        mr = re.match(r"/(add|remove|kick)(?:\s+(.*))?$", line)
        if mr:
            add, names = mr.group(1) == "add", [x.strip().lstrip("@").lower() for x in
                                                 re.split(r"[\s,]+", mr.group(2) or "") if x.strip()]
            cur = auto()
            pool = [n for n in AGENTS if (n not in cur) == add]
            bad = [n for n in names if n not in AGENTS]
            if bad:
                near = difflib.get_close_matches(bad[0], AGENTS, 1, 0.5)
                emit(col("31", f"no agent {bad[0]!r}" + (f" (did you mean {near[0]}?)" if near else "")
                         + f"; agents: {', '.join(AGENTS)}"))
                continue
            if not names and not pool:
                emit(col("2", "every agent is already in this chat" if add else "no agent is served in this chat"))
                continue
            if not names:
                if not tty:
                    emit(col("31", f"usage: /{mr.group(1)} NAME[,NAME]; " + ("not in this chat: " if add else "in it: ")
                             + ", ".join(pool)))
                    continue
                zz = state.get("zz") or {}
                def note(n):
                    if add:
                        b = {"claude": CLAUDE, "codex": "codex", "gemini": AGY}[n]
                        return "" if shutil.which(b) else "its CLI isn't installed here"
                    return ("working now; finishes this turn" if n in state["busy"] else "asleep" if n in zz else "")
                with lock:
                    names = choose(("Add to" if add else "Remove from") + " this chat:", [(n, note(n)) for n in pool], col) or []
                if not names:
                    continue
            if not seat(names, add):
                emit(col("2", f"{', '.join(names)} {'already' if add else 'not'} in this chat"))
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
        ms = re.match(r"/stop(?:\s+(.*))?$", line)
        if ms:
            arg = [x.strip().lstrip("@").lower() for x in re.split(r"[\s,]+", ms.group(1) or "") if x.strip()]
            work = [n for n in AGENTS if running(n)]
            bad = [n for n in arg if n not in AGENTS + ("all",)]
            if bad:
                near = difflib.get_close_matches(bad[0], AGENTS + ("all",), 1, 0.5)
                emit(col("31", f"no agent {bad[0]!r}" + (f" (did you mean {near[0]}?)" if near else "")
                         + "; usage: /stop [NAME|all]"))
                continue
            if not arg or "all" in arg:   # everything stops: running and queued turns, nobody is woken
                done = stop_all()
                emit(col("33", f"■ stopped {', '.join(done)}; nothing queued" if done else
                         "■ nobody was working; queued turns cleared"))
                continue
            if not work:
                emit(col("2", "nobody is working on a reply"))
                continue
            names = arg   # one agent: whoever waits on it gets the turn
            how = "drop"
            for n in names:
                if n not in work or not stop_turn(n, how):
                    emit(col("2", f"{n} isn't working on a reply"))
            continue
        mr = re.match(r"/restart(\s+now)?\s*$", line)
        if line.startswith("/restart") and re.match(r"/restart(\s|$)", line):
            if not mr:
                emit(col("31", "usage: /restart [now]"))
                continue
            was = serve_running()
            wait = restart_serve(now=bool(mr.group(1)))
            emit(col("36", "◇ " + ("started the serve" if not was else
                                    f"stopping {', '.join(wait)}; the serve restarts, then answers again" if wait and mr.group(1) else
                                    f"the serve restarts once {', '.join(wait)} finish{'es' if len(wait) == 1 else ''} "
                                    "(/restart now stops them)" if wait else "restarting the serve")))
            continue
        if line == "/reveal":
            qs = [q for q in open_rounds() if any(h.get("text") for h in round_status(q).values())]
            if not qs:
                emit(col("2", "no independent round has an answer in yet" if open_rounds() else "no independent round is open"))
                continue
            late = sorted({n for q in qs for n in members(q) if n not in round_status(q)})
            reveal_due(force=True, only=qs)
            if late:
                emit(col("2", f"{', '.join(late)} still working: {'its' if len(late) == 1 else 'their'} answer posts when done"))
            continue
        if re.match(r"/to(\s|$)", line):
            tgt = recips(line[3:])
            if tgt is not False:
                state["to"] = tgt or "all"
            continue
        if line == "/clock":
            CLOCK["12h"] = not CLOCK["12h"]
            save_cfg(ICONFIG, {"clock": "12h" if CLOCK["12h"] else "24h"})
            emit(col("2", f"times now {'12' if CLOCK['12h'] else '24'}-hour (saved for every workspace)"))
            continue
        if line == "/decisions":
            emit(decisions_text(load()).rstrip() or "no decisions recorded yet (/decide TEXT records one)")
            continue
        addressed = line.startswith("@")
        line = address(line)   # @codex /ind TEXT: the recipients first, then the command
        if line is None:
            continue
        mode, shape = None, None
        mm = re.match(r"/(independent|ind|audit|design|decide)\b\s*(.*)", line, re.S)
        if line.startswith("//"):   # posted as text, one slash
            line = line[1:]
        elif not mm and not addressed and re.match(r"/[A-Za-z]+(\s|$)", line):   # an unknown /word (a path like /src/x isn't one)
            w = line.split()[0]
            near = difflib.get_close_matches(w[1:].lower(), SLASH, 1, 0.6)
            emit(col("31", f"unknown command {w}" + (f" (did you mean /{near[0]}?)" if near else "")
                     + "; /help lists the commands, // at the start posts it as text"))
            continue
        elif mm:
            mode, line = {"ind": "independent", "decide": "decision"}.get(mm.group(1), mm.group(1)), mm.group(2).strip()
            sm = re.match(r"/(audit|design)\b\s*(.*)", line, re.S) if mode == "independent" else None
            if sm:   # /ind /audit TEXT: blind, with long answers
                shape, line = sm.group(1), sm.group(2).strip()
            if not line:
                emit(col("31", f"usage: /{mm.group(1)} TEXT"))
                continue
            line = address(line)   # /ind @codex TEXT
            if line is None:
                continue
        state["posting"] = True
        try:
            m = post("user", "all" if mode == "decision" else state["to"], line, mode=mode,
                     extra={"shape": shape} if shape else None)
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
            state["hist"].append(m)
            emit(col("2", f"posted #{m['id']}"))
        state["posting"] = False
        away = [n for n in AGENTS if (n in m["to"] or mentioned(m["text"], n)) and n not in round_members()]
        if away:
            emit(col("33", f"{', '.join(away)} {'is' if len(away) == 1 else 'are'} not in this chat, so nobody "
                           f"answers as {' or '.join(away)}: /add {','.join(away)} brings {'it' if len(away) == 1 else 'them'} in"))
    state["stop"] = True


__all__ = [k for k in list(globals()) if not k.startswith("__")]
