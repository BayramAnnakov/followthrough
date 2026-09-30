# followthrough - design

What it is for, how it works, the decisions behind it, and what is not built. The code is the reference for
details; this file is for the "why".

## The problem

Coding agents make promises about the future: "I'll check the p95 in 24 hours", "look at the bill on Friday",
"remind me on decision day". In Claude Code the usual tool is an in-session reminder (`CronCreate`). It lives only
as long as the session: close the terminal, restart, or let the laptop sleep through it, and the check is gone,
with nothing to say it was ever promised.

Measured on the author's own transcripts before followthrough existed (`analysis/census.py` reproduces the method on
yours): 90 `CronCreate` calls in 23 sessions over two months. Of 76 one-shot reminders already due, 50 have a later
turn with the same prompt text, i.e. they fired (a text match, so if anything it over-counts fires). For reminders set 36 hours or more ahead, 3 of 7 fired. "No fire seen" is an upper bound on
loss, not a count (some were deliberately superseded or re-created), but the pattern is plain: the longer the
horizon, the more likely the check silently disappears - and the long-horizon checks are the ones that say whether a
change worked.

The content of a good check was never the problem. Agents write good runbooks when asked. The missing pieces are
**durability** (the check outlives the session), **a close-out** (every check ends in a verdict or an explicit
decision), and **one overview** (what is waiting, across every repo).

## Principles

1. **The CLI is the contract.** Hooks, the skill and any agent with a shell use `followthrough add / start /
   resolve / status`. Harness-specific code is a thin adapter.
2. **Shadow, do not seize.** The session that set a reminder runs it while it is alive, with its full context. The
   ledger holds a durable copy and takes over only when that session is gone.
3. **The expectation is written before the data exists** and never edited. Later information is appended as notes.
4. **A deterministic runner.** Code decides when a check is due, who holds it, and what state it is in. Nothing in
   the runner calls a model.
5. **Nothing closes by itself.** A check ends with a verdict, or with a person cancelling or abandoning it. A check
   that passes its end time stays open as *overdue*. (One automatic close: a hook capture whose `CronCreate` never
   succeeded is cancelled after an hour, because that reminder never existed.)
6. **Nothing unattended can change production.** Today the runner only notifies; a person opens the check.

## How it works

### Claims and checkpoints

A **claim** is one promise: a title, a kind, the change it is about, a falsifiable expectation, a runbook (how to
look), a repo, and one or more **checkpoints** (for example 24 h interim, 7 d final). Kinds:

| Kind | Meaning |
|---|---|
| `verify` | a change should move a metric or make something true |
| `watch` | a series of readings (a daily check) with an end date |
| `action` | a deferred step a person must take; followthrough only reminds |
| `ask` | a decision or a question for a person |

Running a checkpoint means taking its **lease** (`followthrough start` prints `OK <attempt-id>`). One open attempt
per checkpoint is enforced by a unique index, so a session that fires and a person who opens the check from a
notification cannot both run it. The verdict is `worked`, `failed`, `partial`, `inconclusive`, or `not_settled`
with a retry time (at most three, then it goes to the person). The first verdict stands; changing it later takes an
explicit `amend` with a reason, and the old verdict stays in the history.

### Capture: three paths, one identity

In-session reminders are captured without the agent doing anything. The three paths agree on one identity, the
session id plus the `CronCreate` tool-call id:

1. A `PreToolUse` hook on `CronCreate` creates a provisional claim and prefixes the reminder's prompt with a claim
   line ("take the lease, do the check, record the verdict"). The session keeps its reminder.
2. `PostToolUse` links the job id and confirms the claim; `PostToolUseFailure` cancels it.
3. The runner scans the transcripts (`~/.claude/projects/*/*.jsonl`, incrementally by byte offset) as a backstop
   for sessions that were killed before a hook could finish.

A cancelled capture is a tombstone: a rescan never brings it back. Short waits (under 30 minutes) and sub-daily
recurring jobs are in-task monitors, not follow-ups, and are skipped.

Checks that do not start as reminders are registered by the agent through the skill (`followthrough add`) at ship
time, with the expectation, the baseline, the runbook, and any files the check needs copied into the ledger
(`--attach`; session scratchpads are deleted with the session).

### The runner

A launchd job runs `followthrough tick` every 2 minutes. Each tick, in this order, each step contained:

1. **Scan** transcripts: new reminders, deleted reminders, and reminders that fired (a `scheduledTaskId` on a user
   turn equal to the job id - structural, never by matching prompt text).
2. **Transitions**: a due checkpoint whose session is gone becomes "needs you"; a live session gets a grace
   period to run its own reminder (longer while its `Stop` hook still reports the job; at least 40 minutes for a
   recurring job, which Claude Code fires up to 30 minutes late - so the scan must come first). A reminder that
   fired but recorded no verdict is reported with the session's last reply. Stale attempts, retries and overdue
   claims are handled here too.
3. **Deliver** the outbox. Every notification is queued in the same transaction as the transition that caused it
   and re-checked against the ledger right before sending, so a check resolved in the meantime is never announced.

### Notifications and opening a check

macOS notifications (clickable with `terminal-notifier`) and, optionally, Telegram with the full runbook. Clicking
runs `followthrough open <id>`: an interactive Claude session in the claim's repo, with the check's instructions in
a private prompt file. When the runbook refers to "this conversation", the session is forked from the one that set
the reminder, so the context comes back.

Reminders and overdue notices are sent only between 08:00 and 21:00 local time; due and fired notices are sent
when they happen.

`followthrough snooze <id> --for 3h` (or a Telegram snooze button) holds a claim's notifications. Nothing else
changes: checkpoints still become due, and what they queue waits in the outbox. When the snooze ends, you get one
notice per channel: the latest held one that still applies, or else a reminder if the claim still needs you. A verdict
ends a snooze. A notice already being sent when the snooze is set can still arrive.

### Overview

`followthrough status` lists what needs you, what is running and what is due, most urgent first. A `SessionStart`
hook prints a short version for the current repo when a session starts, and one line about the rest.

## Safety

- The runner never runs an agent; it writes only to its own folder and sends notifications.
- Checks are read-only by instruction: the claim line and the skill tell the agent never to deploy, send or change
  settings during a check, and to stop and say so if the runbook asks for a write.
- Text is passed through a secret redactor where it is stored (claims, history, pending tool calls) and again where
  it leaves (notifications, the prompt file `open` writes). It catches known shapes (API keys, tokens, `password=`,
  bearer headers, credentials in URLs); it is a safety net, not a guarantee: keep secrets out of reminders.
- Prompts never go on a command line (the process list is readable); `open` writes them to a 0600 file and passes a
  pointer. The ledger and its folders are private to the user.
- Claim ids read back from the ledger are checked against the format followthrough generates before they go into
  a Terminal command or a file name, since the ledger folder is writable by sandboxed agents.
- `~/.followthrough/data/` is the only folder a sandboxed agent needs to write. followthrough never reads config
  from it and never executes anything in it.
- Hooks fail open: on any error the reminder behaves exactly as it would without followthrough.

## Decisions worth knowing

- **Shadow instead of replacing the in-session reminder.** The session that set a check has the context to run it
  well; the durable copy is insurance, not the primary path.
- **Structural fire detection.** The same prompt text recurs (a daily job, a reminder re-created after a restart);
  the job id on the fired turn identifies exactly one reminder.
- **Overdue claims stay open (reversed after the first live day).** The first version closed a claim as `abandoned`
  48 hours after its due time. In live use this closed checks at 3 AM that the person could not react to, and a
  check that had actually been done in chat lost its result. Now a claim past its end time gets an overdue notice
  in waking hours, then one a week, and stays open until someone decides.
- **Verdicts are not edited by agents.** The first verdict wins; `amend` exists for the person, with a reason.
- **Stdlib only.** Python 3.11+, SQLite in WAL mode, no dependencies.

## The first week of live use

The author's machine, 23-27 Sep 2026 (one person, one week - read the numbers as a sanity check, not a benchmark):

- 35 checkpoints came due. 24 got a verdict, 4 were cancelled by the person, 3 were superseded by a later reading,
  4 were notified and are waiting. None is past due without a notice.
- Of those due 36 hours or more after they were set: 7 verdicts, 2 cancelled, 2 superseded, 0 silent.
- Every eligible in-session reminder set that week (7 of 7) became a claim. Most checks did not start as reminders:
  38 of 59 claims were registered with `followthrough add`, mostly by agents following the skill, and 21 came from
  in-session reminders.
- 18 verdicts: 13 worked, 4 partial, 1 inconclusive, 0 failed. These were recorded by the agents that ran the checks
  and have not yet been reviewed by the person; 0 failed is as much a question as a result.
- From notification to verdict: median 83 minutes over 15 checks; 6 took 22 minutes or less.

## Not built yet

- **Linux** (the runner is a launchd job; notifications use `osascript` / `terminal-notifier`).
- **Unattended checks.** Running a check with no person present (a restricted agent session, or declared read-only
  collectors with no model at all) is designed but not built; today every check is opened by a person.
- **The Telegram button handler** ("Open on Mac", snooze). The buttons are opt-in; something that polls the bot's
  updates has to handle them, and nothing in this repo does yet.
- **Quiet hours for due and fired notices**, and the other known gaps in `BACKLOG.md`.
