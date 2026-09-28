"""`followthrough tick`: the deterministic runner. Week-1 mode is notify-only - nothing here runs an agent.

Order per tick: scan transcripts -> state transitions (each queues its notification in the same transaction, guarded
by rowcount so a decision is never acted on after another process changed the row) -> deliver the outbox. The scan
comes first so a reminder that just fired in its session is known before its checkpoint can be called due. Each
step is contained, so none can stop the others.
"""
import datetime as dt
import fcntl
import json
import os

from . import config, core, db, notify, transcripts
from .core import RAN_IN_SESSION

MAX_DELIVERY_TRIES = 6
# Claude Code fires a recurring task up to 30 min after its cron time, a fixed offset per task id
# (code.claude.com/docs/en/scheduled-tasks; measured 23 Sep 2026: 4 of 4 daily fires exactly 30:00 late).
RECURRING_LATE = dt.timedelta(minutes=30) + dt.timedelta(minutes=10)
FIRE_SETTLE = dt.timedelta(minutes=10)    # after a fire: time for the check to run and record its verdict
FIRE_MAX_WAIT = dt.timedelta(hours=1)     # a fired turn with no end in the transcript (still running, killed)
WAKING_ONLY = ("overdue", "reminder")    # sent only 08:00-21:00 local, also on a retry
OVERDUE_RENOTIFY = dt.timedelta(days=7)   # reminder interval once a claim is past ends_at (it is never closed for you)


def _local_hour(tz, now):
    from zoneinfo import ZoneInfo
    return now.astimezone(ZoneInfo(tz)).hour


def _due_one(con, cfg, now, root, cp, grace, max_wait):
    """One pending checkpoint past due: decide whether it ran in its session, is still owned by it, or needs the
    user. Returns log lines."""
    log = []
    claim = core.get(con, cp["claim_id"])
    due = db.parse(cp["due_at"])
    sess = con.execute("SELECT * FROM sessions WHERE session_id=?", (claim["source_session_id"],)).fetchone()
    if claim["source_session_id"] and not (sess and sess["ended_at"]):
        wait = grace
        if sess and sess["last_seen"] and now - db.parse(sess["last_seen"]) < dt.timedelta(hours=2):
            live_jobs = {str(j.get("id")) for j in json.loads(sess["crons"] or "[]")}
            if claim["source_cron_id"] and claim["source_cron_id"] in live_jobs:
                wait = max_wait   # the session is alive and still has the job: it owns the check
        if claim["source_cron_id"] and claim["kind"] == "watch":
            wait = max(wait, RECURRING_LATE)   # captured from a recurring job, which may fire 30 min late
    else:
        wait = dt.timedelta(0)
    fired = transcripts.fired(con, claim, cp)
    if not fired and due + wait > now:
        return log
    if fired and transcripts.parse_ts(fired) + FIRE_SETTLE > now:
        return log                  # let a check that just fired finish and resolve itself
    quiet = fired and claim["kind"] == "watch" and cp["role"] != "final"
    reply = ""
    if fired and not quiet:
        reply, ended = transcripts.turn_after(claim, fired, root=root)
        if ended is False and transcripts.parse_ts(fired) + FIRE_MAX_WAIT > now:
            return log              # its turn is still running: the verdict may still come
    with db.tx(con):
        if claim["kind"] == "watch":  # earlier series readings nobody recorded are superseded by this one
            con.execute("UPDATE checkpoints SET state='expired', summary=summary || ' [skipped: no reading recorded]'"
                        " WHERE claim_id=? AND seq<? AND role='interim' AND state='needs_human'", (claim["id"], cp["seq"]))
        if quiet:
            # a series reading that ran where it was set: wait quietly for its verdict (no daily push)
            n = con.execute("UPDATE checkpoints SET state='needs_human', notified_at=?, summary=? WHERE id=? AND state='pending'",
                            (db.iso(now), f"{RAN_IN_SESSION} fired {fired[:16]}Z", cp["id"])).rowcount
            if n == 1:
                db.event(con, claim["id"], "ran_in_session", f"checkpoint {cp['seq']}")
                log.append(f"ran in session (quiet) {claim['id']} #{cp['seq']}")
            return log
        if fired:
            n = con.execute("UPDATE checkpoints SET state='needs_human', notified_at=?, summary=? WHERE id=? AND state='pending'",
                            (db.iso(now), f"{RAN_IN_SESSION} fired {fired[:16]}Z in session {claim['source_session_id'][:8]};"
                             " record the verdict", cp["id"])).rowcount
            event, extra = "fired", f"It ran in session {claim['source_session_id'][:8]} at {fired[:16]}Z; its verdict was not recorded."
            if n == 1 and reply:
                extra += "\n\n" + reply
                db.event(con, claim["id"], "session_reply", f"fired {fired[:16]}Z; the session's last reply: {reply}")
        else:
            n = con.execute("UPDATE checkpoints SET state='needs_human', notified_at=? WHERE id=? AND state='pending'",
                            (db.iso(now), cp["id"])).rowcount
            event, extra = "due", ""
        if n != 1:
            return log
        db.event(con, claim["id"], event, f"checkpoint {cp['seq']}")
        db.enqueue(con, claim["id"], cp["id"], event, extra, at=now)
    log.append(f"{event} {claim['id']}")
    return log


def transitions(con, cfg, now, root=None):
    log = []
    waking = 8 <= _local_hour(cfg["tz"], now) < 21

    # 1. Overdue: a claim past ends_at stays open - only a verdict or a person (cancel, abandon) closes it. It gets an
    # "overdue" notice in waking hours, repeated weekly (and anew after a not_settled retry moved ends_at).
    if waking:
        for c in con.execute("SELECT * FROM claims c WHERE status='active' AND confirmed=1 AND ends_at<? AND NOT EXISTS"
                             " (SELECT 1 FROM events e WHERE e.claim_id=c.id AND e.kind='overdue' AND e.at>=MAX(c.ends_at, ?))",
                             (db.iso(now), db.iso(now - OVERDUE_RENOTIFY))).fetchall():
            with db.tx(con):
                if not con.execute("SELECT 1 FROM claims WHERE id=? AND status='active' AND ends_at=?",
                                   (c["id"], c["ends_at"])).fetchone():
                    continue
                db.event(con, c["id"], "overdue", f"past {c['ends_at']} without a verdict; still open", at=now)
                con.execute("UPDATE outbox SET gave_up_at=?, last_error='obsolete' WHERE claim_id=? AND event='overdue'"
                            " AND sent_at='' AND gave_up_at=''", (db.iso(now), c["id"]))   # one pending notice, not a pile
                db.enqueue(con, c["id"], None, "overdue", at=now)
            log.append(f"overdue {c['id']}")

    # 1b. Hook captures whose CronCreate never succeeded (denied, failed, session killed mid-call) never became real.
    for c in con.execute("SELECT id FROM claims WHERE status='active' AND confirmed=0 AND created_at<?",
                         (db.iso(now - dt.timedelta(hours=1)),)).fetchall():
        core.close(con, c["id"], "cancelled", "the in-session reminder was never confirmed (CronCreate denied or failed)")
        log.append(f"unconfirmed {c['id']}")

    # 2. Stale attempts: past the deadline AND the agent process that took the lease is gone.
    for a in con.execute("SELECT a.*, cp.claim_id FROM attempts a JOIN checkpoints cp ON cp.id=a.checkpoint_id"
                         " JOIN claims c ON c.id=cp.claim_id WHERE a.ended_at='' AND a.deadline<? AND c.status='active'",
                         (db.iso(now),)).fetchall():
        if core.pid_alive(a["pid"], a["pid_start"]):
            continue
        with db.tx(con):
            n = con.execute("UPDATE attempts SET ended_at=?, outcome='stale' WHERE id=? AND ended_at=''",
                            (db.iso(now), a["id"])).rowcount
            m = con.execute("UPDATE checkpoints SET state='needs_human', notified_at=? WHERE id=? AND state='running'",
                            (db.iso(now), a["checkpoint_id"])).rowcount
            if n == 1 and m == 1:
                db.event(con, a["claim_id"], "stale_attempt", f"{a['path']} {a['id']}")
                db.enqueue(con, a["claim_id"], a["checkpoint_id"], "stale", f"started via {a['path']} but never resolved", at=now)
                log.append(f"stale {a['claim_id']}")

    # 3. Due: pending checkpoints past due (+ grace while the session that set it may still run it itself).
    grace = dt.timedelta(minutes=cfg["grace_minutes"])
    max_wait = dt.timedelta(hours=cfg.get("live_session_max_wait_hours", 6))
    for cp in con.execute("SELECT cp.* FROM checkpoints cp JOIN claims c ON c.id=cp.claim_id WHERE c.status='active'"
                          " AND c.confirmed=1 AND cp.state='pending' AND cp.due_at<=? ORDER BY cp.due_at", (db.iso(now),)).fetchall():
        try:
            log += _due_one(con, cfg, now, root, cp, grace, max_wait)
        except Exception as e:  # noqa: BLE001 - one bad claim or transcript must not stop the others
            log.append(f"ERROR on {cp['claim_id']}: {type(e).__name__}: {core.redact(str(e))[:200]}")

    # 4. Re-notify "needs you" at most every renotify_hours, in waking hours; never for ones that ran in session,
    # nor for overdue claims (step 1 reminds those weekly).
    if waking:
        cutoff = db.iso(now - dt.timedelta(hours=cfg["renotify_hours"]))
        for cp in con.execute("SELECT cp.* FROM checkpoints cp JOIN claims c ON c.id=cp.claim_id WHERE c.status='active'"
                              " AND c.ends_at>=? AND cp.state='needs_human' AND cp.notified_at<? AND cp.summary NOT LIKE ?",
                              (db.iso(now), cutoff, RAN_IN_SESSION + "%")).fetchall():
            with db.tx(con):
                n = con.execute("UPDATE checkpoints SET notified_at=? WHERE id=? AND state='needs_human' AND notified_at<?",
                                (db.iso(now), cp["id"], cutoff)).rowcount
                if n == 1:
                    db.enqueue(con, cp["claim_id"], cp["id"], "reminder", at=now)
                    log.append(f"reminder {cp['claim_id']}")
    return log


def _still_relevant(row, claim, cp, now):
    """Re-check a queued notification against the ledger right before sending it."""
    if claim is None:
        return False
    if row["event"] == "expired":   # legacy: until 2026-09-24 the runner closed a claim at ends_at
        return core.auto_expired(claim)
    if row["event"] == "overdue":   # still open and still past its end (a not_settled retry moves ends_at)
        return claim["status"] == "active" and claim["ends_at"] < db.iso(now)
    if row["event"] == "test":
        return True
    if row["event"] == "reminder" and claim["ends_at"] < db.iso(now):
        return False                # overdue now: the overdue notice replaces the daily reminder
    return claim["status"] == "active" and cp is not None and cp["state"] == "needs_human"


def deliver(con, cfg, bin_path, now, send=None):
    """Send queued notifications, one channel per row; failures back off and retry, then give up visibly."""
    send = send or notify.deliver_channel
    log = []
    rows = con.execute("SELECT * FROM outbox WHERE sent_at='' AND gave_up_at='' AND next_try_at<=? ORDER BY id",
                       (db.iso(now),)).fetchall()
    waking = 8 <= _local_hour(cfg["tz"], now) < 21
    for r in rows:
        if r["event"] in WAKING_ONLY and not waking:
            continue                # a retry or a Mac that wakes at night: keep it for the morning
        if not core.ID_RE.fullmatch(r["claim_id"] or ""):   # the ledger is agent-writable; ids end up in commands
            con.execute("UPDATE outbox SET gave_up_at=?, last_error='invalid claim id' WHERE id=?", (db.iso(now), r["id"]))
            log.append(f"refused {r['channel']} {r['event']}: invalid claim id")
            continue
        try:
            claim = con.execute("SELECT * FROM claims WHERE id=?", (r["claim_id"],)).fetchone()
            cp = con.execute("SELECT * FROM checkpoints WHERE id=?", (r["checkpoint_id"],)).fetchone() if r["checkpoint_id"] else None
            if not _still_relevant(r, claim, cp, now):
                con.execute("UPDATE outbox SET gave_up_at=?, last_error='obsolete' WHERE id=?", (db.iso(now), r["id"]))
                log.append(f"dropped obsolete {r['channel']} {r['event']} {r['claim_id']}")
                continue
            # at-least-once: a crash after the provider accepted but before sent_at is recorded sends it again
            ok, detail = send(cfg, r["channel"], claim, cp, r["event"], r["extra"], bin_path)
        except Exception as e:  # noqa: BLE001 - a notifier must never break the tick
            ok, detail = False, type(e).__name__
        tries = r["tries"] + 1
        if ok:
            con.execute("UPDATE outbox SET sent_at=?, tries=?, last_error='' WHERE id=?", (db.iso(now), tries, r["id"]))
            db.event(con, r["claim_id"], f"notified_{r['event']}", f"{r['channel']} {detail}")
        elif tries >= MAX_DELIVERY_TRIES:
            con.execute("UPDATE outbox SET gave_up_at=?, tries=?, last_error=? WHERE id=?", (db.iso(now), tries, detail, r["id"]))
            db.event(con, r["claim_id"], "notify_gave_up", f"{r['channel']} {r['event']}: {detail}")
        else:
            nxt = now + dt.timedelta(minutes=2 ** tries)
            con.execute("UPDATE outbox SET next_try_at=?, tries=?, last_error=? WHERE id=?", (db.iso(nxt), tries, detail, r["id"]))
        log.append(f"{'sent' if ok else 'FAILED'} {r['channel']} {r['event']} {r['claim_id']} ({detail})")
    return log


def tick(con, cfg, bin_path, now=None, dry=False, send=None, scan=True, root=None):
    """One pass. Returns log lines. Holds a file lock so two ticks never overlap."""
    now = now or db.now()
    os.makedirs(config.home(), exist_ok=True)
    with open(config.path("tick.lock"), "w") as lockf:
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return ["another tick is running; skipped"]
        if dry:
            due = con.execute("SELECT COUNT(*) FROM checkpoints cp JOIN claims c ON c.id=cp.claim_id WHERE c.status='active'"
                              " AND cp.state='pending' AND cp.due_at<=?", (db.iso(now),)).fetchone()[0]
            queued = con.execute("SELECT COUNT(*) FROM outbox WHERE sent_at='' AND gave_up_at=''").fetchone()[0]
            return [f"[dry-run] {due} checkpoint(s) past due, {queued} notification(s) queued"]
        log = []
        if scan:
            try:
                log += transcripts.scan(con, cfg, now, root=root)
            except Exception as e:  # noqa: BLE001
                log.append(f"ERROR in scan: {type(e).__name__}: {core.redact(str(e))[:200]}")
        for step in (lambda: transitions(con, cfg, now, root=root), lambda: deliver(con, cfg, bin_path, now, send)):
            try:
                log += step()
            except Exception as e:  # noqa: BLE001
                log.append(f"ERROR in tick step: {type(e).__name__}: {core.redact(str(e))[:200]}")
    return log
