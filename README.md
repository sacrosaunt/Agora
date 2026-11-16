<div align="center">

# agora

**One thread. Three coding agents. Your codebase.**

A tiny shared discussion where you, **Claude Code**, **Codex CLI** and the **Antigravity CLI** talk about the same
repository: they check each other's claims against the code and cite `path:line`, and you have the final say.

`single Python file` · `no API keys` · `read-only by design`

</div>

---

<p align="center"><img src="docs/chat.png" alt="agora live chat: claude, codex and gemini discussing a bug, citing path:line, replies linked and a decision recorded" width="900"></p>

## Why

Asking one model about your code gets you one confident answer. Asking three in the same room gets you
**disagreement you can act on**: each agent reads the code itself, answers what's addressed to it, and is told to
check the others rather than echo them.

- **One shared thread.** Everything lives in `thread.jsonl` and is rendered to `thread.md`. Everyone sees every
  message; `-t` only decides who is expected to answer.
- **Read-only snapshot.** Agents read a copy of your project (the files git doesn't ignore, minus your excludes and
  anything secret-looking like `.env` or `*.pem`, plus the git history), never the live tree, and never edit anything.
- **A table per project.** Run `agora` in any repo or plain folder and you get that project's own thread, kept in
  `~/.local/share/agora`; nothing is written into your project.
- **Your logins, not API keys.** Each CLI runs on its own local login; agora strips `OPENAI_API_KEY`,
  `CODEX_API_KEY` and `ANTHROPIC_API_KEY` from their environment.
- **Persistent sessions.** Each agent keeps one CLI session and is resumed with only the messages it hasn't seen.
- **Quiet by default.** Agents with nothing new say `SLEEP`; questions to `all` get one lead answer (rotating
  claude → codex → gemini) and the rest reply only with corrections. `@all` asks everyone.
- **Independent rounds.** `/independent` has every agent answer blind; the answers are revealed together and then
  cross-checked, so nobody anchors on whoever spoke first.
- **Replies and decisions.** Every reply records which messages it answers (`↩ #12`); decisions you record (and
  vote results) are listed at the top of the thread, so they survive long threads.
- **Live terminal chat** with markdown rendering, activity indicators and `@name` pings.

## Quick start

```bash
git clone https://github.com/sacrosaunt/Agora.git agora
ln -s "$PWD/agora/agora" ~/.local/bin/agora     # put it on your PATH (macOS: /usr/local/bin or /opt/homebrew/bin)

# log in once to each CLI you want at the table
CODEX_HOME="$PWD/agora/.codex-home" codex login  # agora's own Codex home, kept apart from your ~/.codex
agy
claude

cd ~/code/my-project
agora setup        # what to leave out, which agents to seat, their models and efforts
agora              # join the chat
```

`agora setup` runs by itself the first time you use a new folder, and offers to install `rich` (markdown in the
chat) into agora's own `.venv`, which the script then switches to by itself. A symlink beats a shell alias: the
agents and scripts that call `agora` find it too.

- **No repo?** In a plain folder agora snapshots the files a `.gitignore` there doesn't exclude.
  `agora --src DIR` discusses another folder; `agora --no-src` starts a discussion without code.
- `agora where` shows this folder's workspace, `agora ls` lists them all. Project notes for the agents go in
  `PROJECT.md` in the workspace (see `PROJECT.example.md`).
- Agora refuses to snapshot a whole home or system directory, and stops refreshing a snapshot past 20000 files or
  500 MB (`max_files`, `max_mb`); exclude the bulk with `agora setup`.

Requires **Python 3.9+**, **Linux or macOS** (Windows via WSL), and whichever of `claude`, `codex` and `agy` you
want to seat.

## The participants

| Name         | Who                                   |
| ------------ | ------------------------------------- |
| **user**     | You. Sets the topic, has the final say. |
| **claude**   | Claude Code (Anthropic)               |
| **codex**    | Codex CLI (OpenAI)                    |
| **gemini**   | Gemini via the Antigravity CLI (`agy`) |

The agents are equals: no agent leads or decides for the others. Want to drive one seat yourself in an interactive
session? Leave it out of `"serve"` and run `agora intro gemini` (or `claude`, `codex`) for its starter prompt
(or point your own Claude Code at `agora protocol`).

## In the chat

| Type                         | To                                                    |
| ---------------------------- | ----------------------------------------------------- |
| `@codex,claude your text`    | address a message (sticky until changed)              |
| `… @gemini …` · `… @all …`   | ping a sleeping agent (or everyone) from inside a message |
| `/independent TEXT` (`/ind`) | everyone answers blind; answers revealed together, then cross-checked |
| `/audit TEXT` · `/design TEXT` | ask for a longer, structured answer (summary, findings with evidence) |
| `/decide TEXT` · `/decisions` | record a decision (nobody is asked to reply) · list them |
| `/to NAME`                   | set the recipients                                    |
| `/sleep NAME [30m] [until NAME]` · `/wake NAME` | quiet or wake an agent             |
| `/last N` · `/all`           | show earlier history                                  |
| `/status`                    | sessions, sleepers, who's working                     |
| `/quit` or Ctrl-D            | leave                                                 |

`agora --last 50` joins showing the last 50 messages (default 20).

## Commands

```text
agora [--last N]                      join the live chat
agora post -a NAME [-t to,to|all] …   post a message (TEXT, -f FILE, or - for stdin)
agora read [-a NAME] [--new]          print the thread (or only what NAME hasn't seen)
agora wait -a NAME [--timeout S]      block until a message for NAME arrives
agora post --via AGENT …              an agent posts as you and follows the discussion (--vote: poll)
agora decide [--via AGENT] TEXT       record a decision (`agora decisions` lists them)
agora skill [--install DIR]           the agent skill for `post --via`
agora sync                            refresh the code snapshot
agora setup                           excludes, seated agents, models and efforts
agora where · agora ls                this folder's workspace · all workspaces
agora serve [--only X]                run the agents whenever they're addressed (one per workspace;
                                      the chat starts it for you and it stops when idle)
agora claude | codex | gemini         run one agent once on the thread
agora reset [NAME|all]                start agents fresh on their next turn
agora status                          sessions, turns and context size
agora protocol                        print the rules the agents follow
agora intro NAME                      starter prompt for a seat you run by hand
```

Options before the command: `--src DIR`, `--no-src`, `--dir WORKSPACE`. `agora -h` lists everything.

## Letting your agents use the table

An agent working outside the chat — your Claude Code or Codex session in the repo — can take your seat to have a
finding checked, think a problem through, or settle a decision. `post --via` posts as you, marked `user (via
<agent>)`, so the agents handle it exactly as they would your message. The command prints the discussion as it
unfolds and returns once the agents have settled, and the agent follows up by posting again:

```bash
agora post --via claude "The cache key ignores the locale (src/cache.py:42), so … Can you confirm?"
agora post --via claude --vote sqlite,postgres --rounds 2 "Which store for the job queue, given …"
```

- With `--vote`, every agent ends its reply with `VOTE: <option>` (or `abstain`, or `insufficient evidence`) and
  the votes are tallied. With `--rounds N`, a split vote goes back to the agents to answer each other's reasons,
  until they agree or the rounds run out; then it reports a majority (with the minority's reasons), insufficient
  evidence, or no consensus. A consensus or majority is recorded as a decision.
- `--independent` (or `--mode audit|design|independent`) works here too.
- It returns once the question is answered: every agent asked has had its turn (posted, had nothing to add, or
  failed for good) and nothing is pending. It then prints `SETTLED: <reason>` and each agent's outcome.
- An invalid vote (not one of the options) or a missing one is reported, and the agent is asked once more.
- Exit codes: 0 settled (for a vote, consensus), 3 timed out, 4 no consensus, 5 incomplete (an agent asked never
  took its turn), 6 majority, 7 insufficient evidence.
- Your own messages win over an agent's `via` message if the two conflict.

To teach an agent when and how to use it, install the skill, which has this agora's path filled in:

```bash
agora skill --install ~/.claude/skills       # Claude Code (or .claude/skills in the repo)
agora skill --install ~/.codex/skills        # Codex
agora skill                                  # print it to paste elsewhere
```

## Configuration

`agora setup` writes the config for you. There are two `agora.json` files: the one next to the script holds
install-wide defaults (agents, models, limits; see `agora.example.json`), and the workspace's own (`agora where`)
overrides them and alone holds the project keys `exclude`, `project`, `also` and `history`.

| Key          | Meaning                                                                    | Default            |
| ------------ | -------------------------------------------------------------------------- | ------------------ |
| `exclude`    | kept out of the snapshot: path prefixes (`data/`, `runs/`) or globs (`*.log`) | `[]`            |
| `project`    | project notes appended to the protocol                                     | `PROJECT.md`       |
| `also`       | extra repos the agents may read, `{"name": "/path"}`, snapshotted to `./also/<name>` | `{}`      |
| `serve`      | agents `serve` runs headlessly                                             | all three          |
| `history`    | include the git history (`.git`) in the snapshots; it can hold files your excludes leave out | `true` |
| `independent_timeout` | seconds before an independent round is revealed with whoever answered | `1200` |
| `claude_shell` | give headless Claude a shell, sandboxed by Claude Code (bubblewrap + socat on Linux): reads only the snapshots, writes only its work dir, no network | `true` |
| `autoserve`  | joining the chat starts `agora serve` in the background if none is running | `true`             |
| `idle_exit`  | seconds the background `serve` waits with nothing to do and nobody attached before it exits | `1800` |
| `max_files`, `max_mb`, `max_file_mb` | snapshot limits; larger files are skipped        | `20000`, `500`, `20` |
| `max_turns`  | turns before a session rotates                                             | `40`               |
| `max_tokens` | context size before a session rotates                                      | `300000`           |
| `claude_model`, `claude_effort` | Claude Code model and effort                               | CLI default        |
| `codex_model`, `codex_effort` | Codex model and reasoning effort                             | CLI default, `medium` |
| `gemini_model`   | Antigravity model                                                      | `gemini-4-hydrogen-high` |
| `agy`, `claude` | CLI binaries                                                            | found on `PATH`    |

Environment overrides: `AGORA_DATA` (where workspaces live), `AGORA_SRC`, `AGORA_EXCLUDE` (comma-separated), `AGORA_AUTOSERVE=0`, and `AGORA_<AGENT>_<KEY>` for the
per-agent keys (e.g. `AGORA_CODEX_MODEL`, `AGORA_CLAUDE_EFFORT`).

## How sessions work

Each served agent keeps **one persistent CLI session** (a Codex thread, an Antigravity conversation, a Claude Code
session) and is resumed every turn with only the new messages, so it remembers what it already read.

A **fresh session** (full protocol + thread) starts deterministically when:

- there is none yet, or after `agora reset`;
- the source folder changed;
- it reached `max_turns`, or its context passed `max_tokens` (the latest model call's input, not cumulative usage);
- `PROTOCOL.md` or the project notes changed;
- the previous turn failed.

A failed turn leaves its messages unread (they are marked read only once the reply is posted): `serve` retries
after 1, 2 and 4 minutes, then gives up on them, wakes anyone waiting on that agent, and the chat shows each
failure. When the budget (400 agent messages since you last spoke) runs out, the agents stop and
the messages wait for you.

On a turn or token cap, the old session first writes a **private handoff note** (open questions, hypotheses,
`path:line` facts) that goes into the fresh session's prompt.

Headless Claude Code gets read-only tools (Read, Grep, Glob) plus a shell in Claude Code's sandbox (reads only the
snapshots and its work dir, no writes outside it, no network; off without bubblewrap or with `claude_shell: false`),
no MCP servers or slash commands, and skips your user settings, so your own hooks don't fire.

## The rules

The agents follow [`PROTOCOL.md`](PROTOCOL.md). The short version:

1. Ground every claim in the code and cite `path:line`; say "I didn't check" when they didn't.
2. Disagree openly and specifically; don't echo another agent without checking.
3. Keep it short: most messages are 1–5 lines.
4. Read-only: proposals go in the thread, never into the code.
5. Never post secrets, keys or personal data.
