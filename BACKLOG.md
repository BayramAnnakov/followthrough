# Backlog

Known work that is real, understood and not done. Each item says what it costs to leave it. Found in the first
week of live use on the author's machine (from the ledger, `tick.log` and the transcripts of the sessions that ran
checks) and in code reviews.

## Notifications and timing

### `due`, `fired` and `stale` notify at night
Only reminders and `overdue` notices respect waking hours (08:00-21:00). A check added with `--at +24h` late at
night is due late the next night too; in the first week 5 of 33 notifications arrived between 21:00 and 08:00.
Cost of leaving it: a phone ping at 3 AM for a check nobody can act on until morning. Fix: hold `due`, `fired`
and `stale` until 08:00 local (the send-time check already drops them if the claim was resolved overnight), or
send them silently.

### Grace applies to claims that no session can fire
Grace (30 min for the setting session to run its own reminder) is applied whenever `source_session_id` is set,
also for a Codex session or a CLI `add --session`, which have no in-session reminder. Cost of leaving it: 30 min of
delay with no reason. Fix: apply grace only when the claim has a `source_cron_id`.

### A started check whose session stays open is never reported
The claim line makes the in-session agent run `start`, so the checkpoint becomes `running`. If the agent then
forgets `resolve`, the runner reports it only as a stale attempt: after the 3 h deadline AND once the agent process
has exited. An interactive session can stay open for days, so the check sits in `running` with no notification
until its `ends_at` (then the weekly `overdue` notice reaches it), and the "fired" path (which carries the
session's last reply) never sees it because it handles only `pending` checkpoints. Cost of leaving it: the main
path (claim line -> start -> check) has no reminder when its last step is skipped. Fix: once the fired turn has
ended with the lease still open, notify with the reply as for an unleased fire, without taking the lease away.

## Capture

### A scan error on a code bug skips transcript records for good
Anything except `OperationalError` is treated as bad content: the offset moves past the record. On the first day a
mid-edit bug in an editable install skipped 40 records in 14 sessions; only a manual `import-crons --rescan`
brought them back. Cost of leaving it: captures lost with only a line in `tick.log`. Fix: captures are idempotent,
so re-scan the last few days when the installed code changes (or once a day).

### A re-created daily job with edited text becomes a second watch
A session re-created its daily job while the old one was still live, and the text differed by one date. A fresh
rescan makes two watches with overlapping checkpoints. A job whose prompt tells the agent to re-create it weekly
with a new date makes this recur. Cost of leaving it: two claims for one check, and a false `due` on the one whose
job no longer fires. Fix needs care: same session + same cron + recurring + near-identical text, never merging an
unrelated job at the same time (see `test_recurring_job_recreated_in_same_session_is_one_watch`).

## Platforms

### Linux
The runner is a launchd job and notifications use `osascript` / `terminal-notifier`. The ledger, CLI, hooks, skill
and tests are platform-neutral (the test suite passes on Linux with `git` and `ps` installed). Cost of leaving it: macOS only. Fix: a systemd user
timer for `followthrough tick` and `notify-send` for notifications.

## Telegram buttons for other users

The buttons ("Open on Mac", snooze) are opt-in (`open_button`, `snooze_buttons`, default off), because nothing in
this repo handles their callbacks: for a new user they would spin and do nothing. The author's handler lives in a
separate personal bot.

### Ship the callback handler as a library function
`followthrough.telegram.handle_update(update, cfg)`: accept only the configured chat and ids matching
`^ft-[a-z0-9-]+$`, answer the callback, then run `open-terminal` (`ft:open:<id>`) or `snooze <id> --for <for>`
(`ft:snooze:<id>:<for>`, `<for>` matching `timeparse.SNOOZE_RE`), and escape the CLI output before replying in HTML. An existing bot calls it. Cost of leaving it: every
user who wants the button writes the security checks themselves, and copies drift from the callback format this
repo sends.

### Optional poller for a bot used only by followthrough
`followthrough telegram-poll` as a second launchd job (long-poll `getUpdates`), built on the handler above. Only
for a dedicated bot: Telegram allows one update consumer per token, so it breaks any other bot on that token.
Limit to state in the docs: "Open on Mac" works only while the Mac is awake and logged in.
