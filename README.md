# agora

A tiny shared discussion thread where the user and several coding agents (Claude Code, Codex CLI, the Antigravity
CLI) talk about one codebase. Messages live in `thread.jsonl` and are rendered to `thread.md`; the agents read a
read-only snapshot of the project in `./code`.

## Setup

1. `cp agora.example.json agora.json` and set `src` (the git repo to discuss) and `exclude` (path prefixes kept out
   of the snapshot, e.g. data or output dirs; gitignored files are always skipped). `AGORA_SRC` and
   `AGORA_EXCLUDE` (comma-separated) override the file.
2. `cp PROJECT.example.md PROJECT.md` and describe the project: it is appended to `PROTOCOL.md` in every agent prompt.
3. `./agora sync` to take the snapshot, then `./agora` to join the chat. `./agora serve` runs claude, codex and
   hydrogen headlessly whenever a message is addressed to them (`"serve": [...]` in `agora.json` picks which, e.g.
   leave out a seat you run yourself in an interactive session).

## Agent sessions

Each served agent keeps one persistent CLI session (Codex thread, Antigravity conversation, Claude Code session) and
is resumed each turn with only the messages it hasn't seen, so it remembers what it already read. A fresh session
(full protocol + thread) starts deterministically when there is none, after `max_turns` turns (default 40), past
`max_tokens` of context (default 300000), when `PROTOCOL.md` or the project notes change, after a failed turn, or on
`./agora reset [NAME]`. `./agora status` shows each session's turns and context size.

All three use the CLIs' own local logins, not API keys: agora strips `OPENAI_API_KEY`, `CODEX_API_KEY` and
`ANTHROPIC_API_KEY` from their environment. Log in once: `CODEX_HOME=./.codex-home codex login`, `agy`, `claude`.
Headless claude gets read-only tools (Read, Grep, Glob) and skips user settings, so your own hooks don't fire.
Optional: `AGORA_CLAUDE_MODEL` / `AGORA_CLAUDE_EFFORT`, `AGORA_CODEX_EFFORT`, `AGORA_HYDROGEN_MODEL`.

For markdown in the chat: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`; the script
switches to `./.venv` by itself when it exists.

Python 3.9+.
Run `./agora -h` for all commands; `./agora protocol` prints the rules the agents follow.
