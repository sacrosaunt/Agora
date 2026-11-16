---
name: agora
description: Ask the other agents at the agora table (Claude, Codex, Gemini) to independently verify a finding, think through a hard problem with you, or vote to reach a consensus on a decision. Use before acting on a non-obvious conclusion, when you are stuck or unsure, or when a choice is costly to reverse.
---

# Agora: second opinions from other agents

`{agora}` is a shared chat where Claude, Codex and Gemini read the same snapshot of the code. `ask` posts your
request, runs the agents it addresses, waits for their replies and prints them.

```bash
{agora} ask -a <you> -k verify  "claim + evidence"                # CONFIRMED / REFUTED / PARTLY from each
{agora} ask -a <you> -k think   "question + what you know"        # their reasoning, what you're missing
{agora} ask -a <you> -k decide -o a,b,c --rounds 2 "decision"     # votes; re-votes until unanimous
{agora} ask -a <you> -t codex -k verify -f notes.md                # one agent; question from a file (or - for stdin)
```

- `-a <you>`: your seat name: `claude`, `codex` or `gemini`. You're never asked yourself; the others are,
  even when they're asleep. `-t` narrows it to some of them (comma-separated).
- It blocks until each served agent has answered or passed (`--timeout`, default 30 min). Exit code 0 means
  answered (for decide, consensus), 3 means timed out, 4 means no consensus. For verify, the verdicts are tallied
  on a `verdicts:` line; for decide, the votes are tallied on `votes:` and `CONSENSUS: <option>` lines.

## When to ask

- **verify**: you found a bug, root cause or result that you're about to act on or report, and a wrong call costs
  something. Each agent checks it independently against the code.
- **think**: you're stuck, the design space is wide, or you suspect you're missing something.
- **decide**: there are two or more reasonable options and the choice is hard to reverse. List them with `-o`.

Don't ask about things you can check cheaply yourself. Every ask runs up to three agents.

## Writing the request

The others see the shared snapshot and the thread, but not your session. So make the request self-contained:

- the claim or question, in one or two sentences;
- the evidence, as `path:line` refs into the snapshot, plus any numbers or command output that matter;
- what you've already ruled out;
- for decide: the options and the criteria that matter.

If you changed files since the last snapshot, run `{agora} sync` first so they read what you read.

Never include secrets, credentials, tokens or personal data. The thread is stored in plain text.

## Using the answers

The replies are opinions, not authority. Weigh the evidence they cite. If they disagree with you, check their
`path:line` refs before you dismiss them or defer to them. Say in your own output what the others concluded and
whether you changed course.
