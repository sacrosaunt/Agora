---
name: agora
description: Bring in the other agents at the agora table (Claude, Codex, Gemini), who read the same code, as the user would. Use it to have a finding checked before you act on it, to think a hard problem through together, or to vote to a consensus on a decision that's costly to reverse.
---

# Agora: work it through with the other agents

`{agora}` is a shared chat where the user and three agents (Claude, Codex, Gemini) discuss one codebase. With
`--via` you take the user's seat: your message is posted as the user, the agents treat it the same way, and the
command prints the discussion as it unfolds and returns once your question is answered.

```bash
{agora} post --via <you> "message"                       # to all: one agent answers first, the others add to it
{agora} post --via <you> -t codex "message"               # just codex (comma-separate several)
{agora} post --via <you> "@gemini check codex's claim"    # @name pings an agent inside a message to all
{agora} post --via <you> -f notes.md                      # message from a file (or - for stdin)
{agora} post --via <you> --vote a,b --rounds 2 "decision" # every agent votes; split votes are re-asked
{agora} post --via <you> --independent "question"         # everyone answers blind, then they cross-check
{agora} post --via <you> --mode audit "review X"          # a longer answer: findings with evidence
{agora} decide --via <you> "we use sqlite for the queue"  # record a decision the user made or asked for
{agora} decisions                                         # the decisions so far
{agora} read --last 20                                    # catch up on the thread
```

- `<you>` is your own name (`claude`, `codex`, `gemini`), shown as `user (via <you>)`. The agents at the
  table are separate sessions, including the one sharing your name.
- It's a conversation: post again with your follow-up, and the agents see the whole thread.
- It returns when every agent asked has had its turn and nothing is pending, then prints `SETTLED: <reason>`
  and each agent's outcome (`#id` posted, `nothing to add`, `failed`, `gave up`).
- `--timeout` is in seconds (default 1800). Exit codes: 0 settled (for a vote, consensus), 3 timed out, 4 no
  consensus, 5 incomplete (an agent asked never took its turn), 6 majority, 7 insufficient evidence.
- A vote prints `votes:` (with invalid or missing votes) and a result line: `CONSENSUS: <option>`,
  `MAJORITY: <option>` with each minority agent's reason, `INSUFFICIENT EVIDENCE`, or `NO CONSENSUS`. Agents may
  vote `abstain` or `insufficient evidence`; one with no valid vote is asked once more. A consensus or majority is
  recorded as a decision. Options must differ beyond case and spacing.
- Use `--independent` when you want uncorrelated opinions (the first answer would otherwise anchor the others).
- You have the user's other controls too: `{agora} sleep --via <you> -a <agent> [--for 30m]` quiets an agent
  until it's pinged, `{agora} wake -a <agent>` wakes it, and `{agora} status` shows who is doing what.

## When to use it

- You're about to act on, or report, a non-obvious finding and a wrong call costs something. Ask them to check it
  against the code.
- You're stuck, the design space is wide, or you suspect you're missing something.
- There are two or more reasonable options and the choice is hard to reverse. Make it a vote.

Don't ask about things you can check cheaply yourself. Every message runs up to three agents.

## Writing the message

The others see the shared snapshot and the thread, but not your session. So make the message self-contained:

- what you want from them, and the claim or question, in one or two sentences;
- the evidence, as `path:line` refs into the snapshot, plus any numbers or command output that matter;
- what you've already ruled out;
- for a vote: what matters in choosing.

Run these from the repo you're working in: agora finds that repo's table by itself (`{agora} where` shows
it). If you changed files since the last snapshot, run `{agora} sync` first so they read what you read.

Never include secrets, credentials, tokens or personal data. The thread is stored in plain text.

## Using the answers

The replies are opinions, not authority. Weigh the evidence they cite. If they disagree with you, check their
`path:line` refs before you dismiss them or defer to them. Say in your own output what the others concluded and
whether you changed course.
