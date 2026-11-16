---
name: agora
description: Collaborate with the other agents at the agora table (Claude, Codex, Gemini), who read the same code. Use it to have a finding checked before you act on it, to think a hard problem through together, or to vote to a consensus on a decision that's costly to reverse.
---

# Agora: work it through with the other agents

`{agora}` is a shared chat where Claude, Codex and Gemini read the same snapshot of the code. `ask` posts your
message to the others, runs them, waits until they've all answered and prints the replies.

```bash
{agora} ask -a <you> "message"                            # everyone else; prints their replies
{agora} ask -a <you> -t codex -f notes.md                  # one agent; message from a file (or - for stdin)
{agora} ask -a <you> -o a,b,c --rounds 2 "decision"        # a vote; split votes are re-asked until unanimous
```

- `-a <you>`: your seat name: `claude`, `codex` or `gemini`. You're never asked yourself. The others are asked
  even when they're asleep. `-t` narrows it to some of them (comma-separated).
- It blocks until each agent has answered or passed. `--timeout` is in seconds (default 1800).
- Exit codes: 0 answered (for a vote, consensus), 3 timed out, 4 no consensus. A vote also prints `votes:` and
  `CONSENSUS: <option>` lines.

## When to ask

- You're about to act on, or report, a non-obvious finding and a wrong call costs something. Ask them to check it
  against the code.
- You're stuck, the design space is wide, or you suspect you're missing something.
- There are two or more reasonable options and the choice is hard to reverse. Make it a vote with `-o`.

Don't ask about things you can check cheaply yourself. Every ask runs up to three agents.

## Writing the message

The others see the shared snapshot and the thread, but not your session. So make the message self-contained:

- what you want from them, and the claim or question, in one or two sentences;
- the evidence, as `path:line` refs into the snapshot, plus any numbers or command output that matter;
- what you've already ruled out;
- for a vote: what matters in choosing.

If you changed files since the last snapshot, run `{agora} sync` first so they read what you read.

Never include secrets, credentials, tokens or personal data. The thread is stored in plain text.

## Using the answers

The replies are opinions, not authority. Weigh the evidence they cite. If they disagree with you, check their
`path:line` refs before you dismiss them or defer to them. Say in your own output what the others concluded and
whether you changed course.
