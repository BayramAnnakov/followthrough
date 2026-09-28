# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

followthrough keeps "check the result in X hours" alive after the agent session that set it ends: a SQLite
ledger, a CLI, Claude Code hooks, a launchd runner and an agent skill. Design, decisions and what is not built:
`docs/DESIGN.md`.

## Commands

```
.venv/bin/python -m pytest                                      # all tests (fast, no network)
.venv/bin/python -m pytest tests/test_core.py::test_lease_is_exclusive
S=$(mktemp -d)                                                   # scratch ledger, never the real one
FOLLOWTHROUGH_HOME=$S .venv/bin/followthrough status
echo '<hook json>' | FOLLOWTHROUGH_HOME=$S .venv/bin/followthrough hook pre-cron   # drive a hook by hand
```

No venv yet: `uv venv .venv && uv pip install -e . pytest`.

## IMPORTANT: commands can touch the real install

- With an editable install (`uv tool install -e .`, the way to work on it; the README's plain install copies the
  package instead), an edit to `src/` takes effect at once in the launchd runner and in the hooks of every Claude
  Code session on the machine. Leave `src/` in a working state; run the tests before you stop.
- Without `FOLLOWTHROUGH_HOME`, commands use the real ledger `~/.followthrough/`, and `tick` sends real
  notifications. `install`, `install-hooks`, `install-runner` and `uninstall-runner` rewrite
  `~/.claude/settings.json` and `~/Library/LaunchAgents/`. Do not run these unless asked.
- `skill/followthrough/SKILL.md` is a symlink. Edit `src/followthrough/SKILL.md`, the copy that ships in the
  package.

## Architecture

- **The CLI is the contract** (`cli.py`). Agents, hooks and the skill all go through `start` / `resolve` /
  `add`; `hooks.py` and `SKILL.md` are thin adapters.
- **State machine** (`core.py`): a claim has checkpoints (`interim` | `final`); running a checkpoint means
  taking its lease, an attempt (`start` prints `OK <attempt-id>`). One open attempt per checkpoint is enforced
  by a unique index. No claim closes by itself: past `ends_at` it stays open as overdue (a notice in waking hours,
  then weekly) until a verdict, `cancel` or `abandon`.
- **Capture of in-session reminders has three paths that must agree** on one identity, `(session_id, CronCreate
  tool_use_id)`, in `core.capture`:
  1. `PreToolUse` hook: provisional claim (`confirmed=0`) and a claim preamble prepended to the reminder prompt.
  2. `PostToolUse` hook: links the job id and confirms. `PostToolUseFailure` cancels it.
  3. `transcripts.scan` (every tick): the backstop for hard-killed sessions. It reads `~/.claude/projects/*/*.jsonl`
     incrementally by byte offset.
  Closed claims are tombstones: a rescan must never recreate a cancelled capture.
- **Runner** (`runner.tick`): transcript scan, then state transitions, then outbox delivery. Each step is
  contained. The session that set a reminder owns it while alive (grace; longer while its `Stop` hook still
  reports the job; at least 40 min for a recurring job, which Claude Code fires up to 30 min late - so the scan
  must stay first). "It fired in its session" is detected structurally (`scheduledTaskId` == job id), never by
  matching prompt text.
- **Notifications** go only through the `outbox` table (`db.enqueue`). They are re-checked against the ledger
  right before sending (`_still_relevant`).

## Invariants

- Stdlib only (`dependencies = []`). Python 3.11+.
- Every read-check-write runs inside `with db.tx(con):` (BEGIN IMMEDIATE). A transition that must reach a person
  enqueues its notification in that same transaction. Conditional UPDATEs check `rowcount`.
- Times are stored as UTC `...Z` strings via `db.iso` / `db.parse`. Cron expressions are read in the config `tz`
  (`timeparse`, DST-aware).
- Schema change: edit `SCHEMA`, add a `MIGRATIONS` entry for a new column on an existing table, and bump
  `SCHEMA_VERSION`. Without the bump, `connect()` skips schema work on existing ledgers.
- Hooks fail open: never raise, never exit non-zero, use `db.connect(fast=True)`. If a hook fails after it
  created a claim, it cancels that claim.
- Text that leaves the transcript (ledger, Telegram, prompt files) goes through `core.redact`. Prompts never go
  in argv: `open` writes them to a 0600 file and passes a pointer.
- `data/` is the only folder a sandboxed agent may write (Codex `writable_roots`, Claude Code
  `sandbox.filesystem.allowWrite`). followthrough never reads config or binaries from it and never executes
  anything in it (see `_claude_bin`); `data/attachments/` is for the checker agent only.

## Tests

All tests are in `tests/test_core.py`. Use the `con` fixture: it points `FOLLOWTHROUGH_HOME` and
`transcripts.ROOT` at `tmp_path`, so no test touches the real ledger or real transcripts. Helpers: `Transcript`
writes fake session `.jsonl` files, `Sender` is a fake notifier, and `t()` / `NOW` give fixed times. Pass `now=`
explicitly instead of sleeping.

## Elsewhere

- `analysis/census.py` reads real transcripts. `analysis/out/` holds work details: it is gitignored; never commit it.
