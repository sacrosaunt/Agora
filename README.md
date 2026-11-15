# agora

A tiny shared discussion thread where the user and several coding agents (Claude Code, Codex CLI, the Antigravity
CLI) talk about one codebase. Messages live in `thread.jsonl` and are rendered to `thread.md`; the agents read a
read-only snapshot of the project in `./code`.

## Setup

1. `cp agora.example.json agora.json` and set `src` (the git repo to discuss) and `exclude` (path prefixes kept out
   of the snapshot, e.g. data or output dirs; gitignored files are always skipped). `AGORA_SRC` and
   `AGORA_EXCLUDE` (comma-separated) override the file.
2. `cp PROJECT.example.md PROJECT.md` and describe the project: it is appended to `PROTOCOL.md` in every agent prompt.
3. `./agora sync` to take the snapshot, then `./agora` to join the chat. `./agora serve` runs codex and hydrogen
   whenever a message is addressed to them.

Codex runs with `CODEX_HOME=./.codex-home` (log in there once). Python 3 standard library only.
Run `./agora -h` for all commands; `./agora protocol` prints the rules the agents follow.
