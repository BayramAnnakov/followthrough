---
name: followthrough
description: Register, run and close follow-up checks on shipped changes so they survive the session - "did this fix actually cut latency / cost / errors", "check the result in 24h / next week". Use when you ship or deploy a change that has an expected effect, when you are about to promise to check something later, when a prompt starts with "[followthrough claim ...]" or "This is followthrough claim ...", or when the user asks what is waiting to be re-evaluated, whether a change worked, or to see open follow-ups.
---

# followthrough

followthrough is a local ledger of **claims**: a change, what it should do, when to look, and how to look. A
runner on this machine notifies the user when a check is due, even if the session that set it is gone, and every
claim ends with a verdict or an explicit reason. The CLI is `followthrough` (if it is not on PATH, try
`~/.local/bin/followthrough`). `followthrough --help` lists every command.

## When a reminder fires

A prompt that starts with `[followthrough claim <id>]` or `This is followthrough claim <id>` is a tracked check.

1. Take the lease first: `followthrough start <id>`. It prints `OK <attempt-id>`. If it prints `TAKEN` or
   `CLOSED`, stop and tell the user what it printed: another session is doing or did this check, or the claim is
   closed. A claim closed as `expired` (an older rule; claims now stay open when overdue) still takes a late
   verdict: if the user wants the check run, run it and record it with the `resolve` command that message gives.
2. Read the claim: `followthrough show <id>` - the expectation, what changed, the live check and the notes may
   have been added after the reminder was written.
3. Do the check. It is read-only: never deploy, send, publish or change settings during a check; if the runbook
   asks for a write, stop and say so. When a live check is given, run it first. Never print secrets.
4. Record the result - always, including when the expectation was wrong:
   `followthrough resolve <id> --attempt <attempt-id> --verdict <v> --summary "<measured value vs expected, the source, the window>"`
   - `worked` - the expectation held, and you measured it.
   - `failed` - it did not hold.
   - `partial` - part held; say which part.
   - `inconclusive` - the data cannot decide (too little traffic, the change is no longer live, a tool was
     denied); say why.
   - `not_settled --retry-at +12h` - the data is not complete yet (billing lag, a sample too small to read).
   Never record `worked` from reasoning alone. Keep what you measured apart from what you inferred.
   A note (`followthrough note`) is not a verdict.
   The first verdict stands. Change it only when the user asks (new data, a relabel):
   `followthrough amend <id> --verdict <v> --summary "..." --reason "user asked: ..."` - the old one stays in the history.
5. If the user wants to look again later:
   - the data cannot decide yet ("check again in 3 days") - resolve `not_settled --retry-at +3d`: the same claim
     moves to that date (the third time, it goes to the user instead);
   - a verdict stands but they want another reading ("it worked; look again in a week") - resolve now, then
     register a new check with `followthrough add ... --at +7d`, a runbook that repeats the measurement, and a
     note naming the earlier claim id.

## When you ship a change with an expected effect

Register it while the session still knows the details - the check may be run by a fresh session with none of
this context.

- **What changed** - read it from the running system (the revision or image that is serving, the label version
  that is live, the ad change in the account history), not from git: `--change`.
- **The expectation, written before the data exists** - falsifiable: a number, a direction, or "no effect":
  `--expect "p95 of /api/search below 10 s (baseline 20.8 s, week of Sep 16-22)"`. It is set once and never
  edited; add notes instead (`followthrough note <id> ...`).
- **The baseline, measured now** with the same query the check will use, plus a control that the change should
  not move, when one exists.
- **When to look** - after the data settles (billing exports lag about a day; small samples need days):
  `--at +24h --at +7d`. Several `--at` flags make interim checkpoints; the last one is final.
- **A self-contained runbook** - where to read, which command or query, what to compare, what the instrument
  cannot see, and "read-only; never print secrets": `--runbook-file <file>` or `--runbook "..."`. The ledger
  stores the runbook's text. Refer to credentials by name (an env var, a secret store); never paste a secret into
  a runbook.
- **Files the check needs** (a script, a query, a census) - never a session scratchpad or `/tmp` path: those are
  deleted with the session. Copy them into the ledger with `--attach <file|dir>` (or `followthrough attach <id>`
  later); the check's prompt lists the copies.
- **The repo** - `add` files the claim under the git repository of the current folder. Pass `--repo` when the
  change lives in another repository or you are not inside one.
- **A live check**, when the change can be reverted: a read-only command that shows it is still live.

```
followthrough add "/api/search latency after cache fix" --kind verify --at +12h --at +3d \
  --change "Cloud Run revision api-00051, image bd752be9" \
  --expect "p95 below 10 s; 5xx rate not above baseline" --runbook-file ./search-latency-check.md
```

Kinds: `verify` (did a change work), `watch` (a series of readings), `action` (something the user must do -
followthrough only reminds), `ask` (a decision or a question for the user).

If the project keeps a change journal (for example a `CHANGE-JOURNAL.md`), add the row there too and put the
claim id in it.

## In-session reminders

In Claude Code, a `CronCreate` reminder set 30 minutes or more ahead is registered automatically, and its prompt
gets the `[followthrough claim <id>]` line (the hook tells you the claim id). Do not register it a second time.
If the reminder names scratchpad files, attach them: `followthrough attach <id> <files>`.
When the reminder is a check on a change, give the claim its expectation while you still know it:
`followthrough expect <id> "p95 below 10 s (baseline 20.8 s)"` - this works once; later additions are notes.

## Overview

`followthrough status` (most urgent first; `--repo .` for this project), `followthrough show <id>` (checkpoints
and history), `followthrough open <id>` (start a session for a check), `followthrough cancel <id> --reason ...`.
When the user wants a check later but still wants it ("snooze it", "remind me tomorrow"):
`followthrough snooze <id> --for 3h` (or `--for morning`, `--until "2026-10-01 08:00"`). It holds the notifications
and does not close or move the check; a verdict ends the snooze.
