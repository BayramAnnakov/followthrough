"""`followthrough tick`: the deterministic runner. Week-1 mode is notify-only - nothing here runs an agent.

Order per tick: scan transcripts -> state transitions (each queues its notification in the same transaction, guarded
by rowcount so a decision is never acted on after another process changed the row) -> deliver the outbox (or hold it
for the morning digest) -> remove the buttons of closed claims from sent Telegram messages. The scan
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


def _zone(tz):
    from zoneinfo import ZoneInfo
    return ZoneInfo(tz)


def _local_hour(tz, now):
    return now.astimezone(_zone(tz)).hour


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
        # an interim reading or a retry after not_settled goes in the morning digest; a final checkpoint gets its own
        db.enqueue(con, claim["id"], cp["id"], event, extra, at=now, digest=cp["role"] == "interim" or cp["tries"] > 0)
    log.append(f"{event} {claim['id']}")
    return log


def transitions(con, cfg, now, root=None):
    log = []
    waking = 8 <= _local_hour(cfg["tz"], now) < 21

    # 0. Snoozes that ended. Transitions below run as usual during a snooze and deliver holds what they queue. The
    # held notices collapse into one on every channel: the latest one still relevant, else a reminder (or the overdue
    # notice) if the claim needs you. The reminder rhythm restarts from now.
    for c in con.execute("SELECT * FROM claims WHERE status='active' AND snoozed_until!='' AND snoozed_until<=?",
                         (db.iso(now),)).fetchall():
        with db.tx(con):
            if con.execute("UPDATE claims SET snoozed_until='' WHERE id=? AND status='active' AND snoozed_until=?",
                           (c["id"], c["snoozed_until"])).rowcount != 1:
                continue
            db.event(con, c["id"], "snooze_over", f"snoozed until {c['snoozed_until']}", at=now)
            claim = con.execute("SELECT * FROM claims WHERE id=?", (c["id"],)).fetchone()
            pick = None
            for r in con.execute("SELECT * FROM outbox WHERE claim_id=? AND sent_at='' AND gave_up_at='' ORDER BY id DESC",
                                 (c["id"],)).fetchall():
                rcp = con.execute("SELECT * FROM checkpoints WHERE id=?", (r["checkpoint_id"],)).fetchone() if r["checkpoint_id"] else None
                if _still_relevant(r, claim, rcp, now):
                    pick = (r["event"], r["checkpoint_id"], r["extra"])
                    break
            con.execute("UPDATE outbox SET gave_up_at=?, last_error='obsolete' WHERE claim_id=? AND sent_at='' AND gave_up_at=''",
                        (db.iso(now), c["id"]))
            cp = core.current_checkpoint(con, c["id"])
            if pick is None and claim["ends_at"] < db.iso(now):
                db.event(con, c["id"], "overdue", f"past {claim['ends_at']} without a verdict; still open", at=now)
                pick = ("overdue", None, "The snooze is over.")
            elif pick is None and core.needs_you(claim, cp, now):
                pick = ("reminder", cp["id"], "The snooze is over.")
            if pick is None:
                log.append(f"snooze over {c['id']} (nothing needs you)")
                continue
            if cp is not None and cp["state"] == "needs_human":
                con.execute("UPDATE checkpoints SET notified_at=? WHERE id=?", (db.iso(now), cp["id"]))
            db.enqueue(con, c["id"], pick[1], pick[0], pick[2], at=now)
        log.append(f"snooze over {c['id']} ({pick[0]})")

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
                db.enqueue(con, c["id"], None, "overdue", at=now, digest=True)
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
                db.enqueue(con, a["claim_id"], a["checkpoint_id"], "stale", f"started via {a['path']} but never resolved",
                           at=now, digest=True)
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
    # nor for overdue claims (step 1 reminds those weekly), nor while snoozed.
    if waking:
        cutoff = db.iso(now - dt.timedelta(hours=cfg["renotify_hours"]))
        for cp in con.execute("SELECT cp.* FROM checkpoints cp JOIN claims c ON c.id=cp.claim_id WHERE c.status='active'"
                              " AND c.ends_at>=? AND cp.state='needs_human' AND cp.notified_at<? AND cp.summary NOT LIKE ?"
                              " AND c.snoozed_until<=?",   # a snooze sends one reminder when it ends (step 0)
                              (db.iso(now), cutoff, RAN_IN_SESSION + "%", db.iso(now))).fetchall():
            with db.tx(con):
                n = con.execute("UPDATE checkpoints SET notified_at=? WHERE id=? AND state='needs_human' AND notified_at<?",
                                (db.iso(now), cp["id"], cutoff)).rowcount
                if n == 1:
                    db.enqueue(con, cp["claim_id"], cp["id"], "reminder", at=now, digest=True)
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


def _unpack(result):
    """A sender returns (ok, detail) or (ok, detail, telegram message id)."""
    ok, detail, *rest = result
    return ok, detail, (rest[0] if rest else None)


def _mark(con, rows, ok, detail, now, mid=None, idx=None, chat=""):
    """Record one send attempt for `rows` (one notification, or every row a digest showed). Call inside db.tx."""
    for r in rows:
        tries = (r["tries"] if isinstance(r["tries"], int) else 0) + 1
        if ok:
            con.execute("UPDATE outbox SET sent_at=?, tries=?, last_error='' WHERE id=?", (db.iso(now), tries, r["id"]))
            db.event(con, r["claim_id"], f"notified_{r['event']}", f"{r['channel']} {detail}")
        elif tries >= MAX_DELIVERY_TRIES:
            con.execute("UPDATE outbox SET gave_up_at=?, tries=?, last_error=? WHERE id=?", (db.iso(now), tries, detail, r["id"]))
            db.event(con, r["claim_id"], "notify_gave_up", f"{r['channel']} {r['event']}: {detail}")
        else:
            nxt = now + dt.timedelta(minutes=2 ** tries)
            con.execute("UPDATE outbox SET next_try_at=?, tries=?, last_error=? WHERE id=?", (db.iso(nxt), tries, detail, r["id"]))
    if ok and mid is not None:
        for r in rows:
            con.execute("INSERT OR IGNORE INTO sent_buttons(chat_id, message_id, claim_id, idx, sent_at) VALUES (?,?,?,?,?)",
                        (chat, mid, r["claim_id"], idx(r) if idx else 0, db.iso(now)))


def _fresh(con, r, now, log):
    """Re-read the ledger right before a send. (claim, cp) when the row should go now; None when it is held by a
    snooze or no longer relevant (then it is dropped as obsolete)."""
    claim = con.execute("SELECT * FROM claims WHERE id=?", (r["claim_id"],)).fetchone()
    cp = con.execute("SELECT * FROM checkpoints WHERE id=?", (r["checkpoint_id"],)).fetchone() if r["checkpoint_id"] else None
    if claim is not None and claim["status"] == "active" and core.snoozed(claim, now):
        return None                 # held until the snooze ends (transitions step 0)
    if not _still_relevant(r, claim, cp, now):
        con.execute("UPDATE outbox SET gave_up_at=?, last_error='obsolete' WHERE id=? AND sent_at=''", (db.iso(now), r["id"]))
        log.append(f"dropped obsolete {r['channel']} {r['event']} {r['claim_id']}")
        return None
    return claim, cp


def deliver(con, cfg, bin_path, now, send=None, send_digest=None):
    """Send queued notifications, one channel per row; failures back off and retry, then give up visibly.

    With `digest` on (the default): a row marked digest (reminders, overdue notices, stale attempts, interim readings,
    retries) and any row queued at night wait for the morning digest - one message per channel per local
    day, sent at the first tick in waking hours that has something to say; notices that would go alone in that tick
    join it. Only a final checkpoint that is due (or ran in its session), a check that needs you after 3 tries and
    the end of a snooze go out on their own, in waking hours - and several of those in one tick share one message."""
    if send is None:
        send, send_digest = notify.deliver_channel, notify.deliver_digest
    log = []
    rows = con.execute("SELECT * FROM outbox WHERE sent_at='' AND gave_up_at='' AND next_try_at<=? ORDER BY id",
                       (db.iso(now),)).fetchall()
    waking = 8 <= _local_hour(cfg["tz"], now) < 21
    use_digest = cfg.get("digest", True)
    local = now.astimezone(_zone(cfg["tz"]))
    today = local.date().isoformat()
    held_rows, alone_rows = {}, {}
    for r in rows:
        if not use_digest and r["event"] in WAKING_ONLY and not waking:
            continue                # a retry or a Mac that wakes at night: keep it for the morning
        try:
            night = not 8 <= _local_hour(cfg["tz"], db.parse(r["created_at"])) < 21   # queued at night (any night)
        except (TypeError, ValueError):
            night = False           # a malformed row must not block the others
        held = use_digest and (r["digest"] or not waking or night)
        if held and (not waking or db.kv_get(con, f"digest:{r['channel']}") == today):
            continue                # waits for the next morning digest
        if not core.ID_RE.fullmatch(r["claim_id"] or ""):   # the ledger is agent-writable; ids end up in commands
            con.execute("UPDATE outbox SET gave_up_at=?, last_error='invalid claim id' WHERE id=?", (db.iso(now), r["id"]))
            log.append(f"refused {r['channel']} {r['event']}: invalid claim id")
            continue
        (held_rows if held else alone_rows).setdefault(r["channel"], []).append(r)
    for channel in sorted(set(held_rows) | set(alone_rows)):
        # relevance first: a row that is obsolete or snoozed must not decide what goes (nor spend the day's digest)
        held = _live(con, held_rows.get(channel, []), now, log)
        alone = _live(con, alone_rows.get(channel, []), now, log)
        if held:                    # the day's digest goes now; what would go alone joins it
            log += _send_batch(con, cfg, channel, sorted(held + alone, key=lambda x: x[0]["id"]), now, bin_path, send,
                               send_digest, "digest", kv_key=f"digest:{channel}", today=today)
        elif use_digest and len({r["claim_id"] for r, _, _ in alone}) > 1:
            log += _send_batch(con, cfg, channel, alone, now, bin_path, send, send_digest, "batch")
        elif use_digest and len(alone) > 1:     # one claim, several notices: its newest one says it
            *older, newest = alone
            with db.tx(con):
                for r, _, _ in older:
                    con.execute("UPDATE outbox SET gave_up_at=?, last_error='superseded' WHERE id=? AND sent_at=''",
                                (db.iso(now), r["id"]))
            log += _send_one(con, cfg, channel, newest[0], now, bin_path, send)
        else:
            for r, _, _ in alone:
                log += _send_one(con, cfg, channel, r, now, bin_path, send)
    return log


def _live(con, rows, now, log):
    """[(row, claim, cp)] for the rows that should go now (see _fresh); a row that cannot be read is a failed try."""
    out = []
    for r in rows:
        try:
            fresh = _fresh(con, r, now, log)
        except Exception as e:  # noqa: BLE001 - one bad row must not stop the others
            with db.tx(con):
                _mark(con, [r], False, type(e).__name__, now)
            log.append(f"FAILED {r['channel']} {r['event']} {r['claim_id']} ({type(e).__name__})")
            continue
        if fresh is not None:
            out.append((r, *fresh))
    return out


def _send_one(con, cfg, channel, r, now, bin_path, send):
    log = []
    try:
        fresh = _fresh(con, r, now, log)    # again: an earlier channel's send took time
        if fresh is None:
            return log
        # at-least-once: a crash after the provider accepted but before sent_at is recorded sends it again
        ok, detail, mid = _unpack(send(cfg, channel, fresh[0], fresh[1], r["event"], r["extra"], bin_path))
    except Exception as e:  # noqa: BLE001 - a notifier must never break the tick
        ok, detail, mid = False, type(e).__name__, None
    with db.tx(con):
        _mark(con, [r], ok, detail, now, mid, chat=str(cfg["telegram"].get("chat_id", "")))
    log.append(f"{'sent' if ok else 'FAILED'} {channel} {r['event']} {r['claim_id']} ({detail})")
    return log


def _send_batch(con, cfg, channel, live, now, bin_path, send, send_digest, label, kv_key=None, today=""):
    """One message for many notices, one line per claim (its newest notice), oldest queued first. live: [(row, claim,
    cp)] in outbox id order. Lines that do not fit stay queued for the next message; only the claims shown are marked."""
    log = []
    latest, first, rows_of = {}, {}, {}
    for r, claim, cp in live:   # in id order: the last one per claim is its newest notice
        first.setdefault(r["claim_id"], r["id"])
        rows_of.setdefault(r["claim_id"], []).append(r)
        latest[r["claim_id"]] = {"claim": claim, "cp": cp, "event": r["event"], "extra": r["extra"]}
    items = []
    for cid in sorted(latest, key=first.get):
        try:                    # a claim whose line cannot be drawn fails alone, not the whole message
            notify.digest_text([latest[cid]], cfg["tz"])
            items.append(latest[cid])
        except Exception as e:  # noqa: BLE001
            with db.tx(con):
                _mark(con, rows_of[cid], False, type(e).__name__, now)
            log.append(f"FAILED {channel} {label} line {cid} ({type(e).__name__})")
    if not items:
        return log
    shown = items[:notify.DIGEST_MAX]
    try:
        shown = items[:notify.digest_fit(items, cfg["tz"], cfg["telegram"].get("open_button"))]
        if send_digest is None:     # a sender without digests (tests): one message per claim
            results = [_unpack(send(cfg, channel, it["claim"], it["cp"], it["event"], it["extra"], bin_path)) for it in shown]
            ok, mid = all(x[0] for x in results), None
            detail = "ok" if ok else "failed"
        else:
            ok, detail, mid = _unpack(send_digest(cfg, channel, shown, bin_path, len(items)))
    except Exception as e:  # noqa: BLE001
        ok, detail, mid = False, type(e).__name__, None
    order = {it["claim"]["id"]: n for n, it in enumerate(shown, 1)}
    with db.tx(con):
        _mark(con, [r for cid in order for r in rows_of[cid]], ok, f"{label} {detail}", now, mid,
              idx=lambda r: order[r["claim_id"]], chat=str(cfg["telegram"].get("chat_id", "")))
        if ok and kv_key:
            db.kv_set(con, kv_key, today)
    log.append(f"{'sent' if ok else 'FAILED'} {channel} {label} of {len(shown)}"
               f"{f' ({len(items) - len(shown)} left for the next)' if len(shown) < len(items) else ''} ({detail})")
    return log


def clear_buttons(con, cfg, now, edit):
    """Remove a closed claim's buttons from the Telegram messages that carried them, so an old message cannot be
    tapped. A digest keeps the buttons of its claims that are still open. Failures back off and give up visibly."""
    if not cfg["telegram"].get("enabled") or edit is None:
        return []
    log = []
    chat_now = str(cfg["telegram"].get("chat_id", ""))
    todo = con.execute("SELECT DISTINCT s.chat_id, s.message_id FROM sent_buttons s LEFT JOIN claims c ON c.id=s.claim_id"
                       " WHERE s.cleared_at='' AND s.next_try_at<=? AND (c.status IS NULL OR c.status!='active')",
                       (db.iso(now),)).fetchall()
    for chat, mid in todo:
        group = con.execute("SELECT s.*, c.status FROM sent_buttons s LEFT JOIN claims c ON c.id=s.claim_id"
                            " WHERE s.chat_id=? AND s.message_id=? AND s.cleared_at=''", (chat, mid)).fetchall()
        closed = [r for r in group if r["status"] != "active"]
        if any(r["next_try_at"] > db.iso(now) for r in closed):
            continue                # the message is backing off (its tries are counted per message)
        if chat != chat_now:        # the config points at another chat now: never edit a message in the wrong one
            with db.tx(con):
                con.execute("UPDATE sent_buttons SET cleared_at=?, last_error='chat changed' WHERE chat_id=? AND message_id=?"
                            " AND cleared_at=''", (db.iso(now), chat, mid))
            log.append(f"gave up clearing buttons on message {mid}: the configured chat changed")
            continue
        keep = [(r["idx"], r["claim_id"]) for r in group if r["status"] == "active" and r["idx"] > 0]
        markup = notify.digest_markup(cfg, keep) if keep else None
        try:
            done, detail = edit(cfg, chat, mid, markup)
        except Exception as e:  # noqa: BLE001
            done, detail = False, type(e).__name__
        with db.tx(con):
            if done:
                for r in (group if markup is None else closed):
                    con.execute("UPDATE sent_buttons SET cleared_at=?, last_error='' WHERE chat_id=? AND message_id=? AND claim_id=?",
                                (db.iso(now), chat, mid, r["claim_id"]))
            else:
                tries = max(r["tries"] for r in closed) + 1
                gave_up = tries >= MAX_DELIVERY_TRIES
                for r in closed:
                    con.execute("UPDATE sent_buttons SET tries=?, last_error=?, next_try_at=?, cleared_at=? WHERE chat_id=?"
                                " AND message_id=? AND claim_id=?",
                                (tries, detail, db.iso(now + dt.timedelta(minutes=2 ** tries)), db.iso(now) if gave_up else "",
                                 chat, mid, r["claim_id"]))
        log.append(f"{'cleared' if done else 'FAILED to clear'} buttons of {len(closed)} closed claim(s) on message {mid} ({detail})")
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
        for step in (lambda: transitions(con, cfg, now, root=root),
                     lambda: deliver(con, cfg, bin_path, now, send, getattr(send, "digest", None)),
                     lambda: clear_buttons(con, cfg, now, getattr(send, "edit", None) if send else notify.telegram_set_buttons)):
            try:
                log += step()
            except Exception as e:  # noqa: BLE001
                log.append(f"ERROR in tick step: {type(e).__name__}: {core.redact(str(e))[:200]}")
    return log
