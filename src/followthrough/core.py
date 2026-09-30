"""Claim operations and the state machine. Every read-check-write runs inside db.tx (BEGIN IMMEDIATE), and every
transition that should reach a person queues its notification in the same transaction (db.enqueue)."""
import datetime as dt
import hashlib
import os
import re
import secrets
import subprocess

from . import db

KINDS = ("verify", "watch", "action", "ask")
VERDICTS = ("worked", "failed", "partial", "inconclusive", "not_settled")
TERMINAL = ("worked", "failed", "partial", "inconclusive", "cancelled", "abandoned")
MAX_TRIES = 3
EARLY_RESOLVE = dt.timedelta(hours=2)   # a checkpoint may be resolved this long before it is due
RAN_IN_SESSION = "[ran-in-session]"
# the summary the runner wrote when it still closed claims at ends_at (until 2026-09-24); a person's own
# `abandon --reason "expired ..."` must not look like it
AUTO_EXPIRED = re.compile(r"expired at \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ without a verdict")


def quiet_reading(claim, cp):
    """An interim reading of a series that ran in its session. The runner sends no notice for it; it is not "needs
    you" (a later reading or the final checkpoint supersedes it)."""
    return (cp is not None and claim["kind"] == "watch" and cp["role"] == "interim" and cp["state"] == "needs_human"
            and cp["summary"].startswith(RAN_IN_SESSION))


def snoozed(claim, now=None):
    """True while the claim's notifications are held (`snooze`)."""
    return bool(claim["snoozed_until"]) and claim["snoozed_until"] > db.iso(now or db.now())


def needs_you(claim, cp, now=None):
    return (cp is not None and cp["state"] == "needs_human" and not quiet_reading(claim, cp)
            and not snoozed(claim, now))


def auto_expired(claim):
    return claim["status"] == "abandoned" and bool(AUTO_EXPIRED.fullmatch(claim["verdict_summary"] or ""))
EPHEMERAL = re.compile(r"(?:/private)?/tmp/claude-\d+/\S+|\bscratchpad\b", re.I)   # paths that die with the session

# Keyword guess for captured reminders, from the census (analysis/census.py). First match wins. The "ask" rule also
# matches the names in config `user_names` ("ask Sam", "reminder for Sam").
KIND_RULES = [
    ("action", r"\bdeploy (now|reminder)|deploy the |\bsubmit (v\d+|the current)|\bretry the\b|disable the "),
    ("ask", r"owed by|decision day|\bask (the user|me)\b|\breminder for (the user|me)\b"),
    ("watch", r"daily check|check-in|progress\b"),
]


def _user_names():
    from . import config
    try:
        return [n.lower() for n in config.load().get("user_names", []) if isinstance(n, str) and n.strip()]
    except Exception:  # noqa: BLE001 - a bad config must not stop a capture; the guess is a hint
        return []


class NotFound(Exception):
    pass


# Claim ids are generated here (new_id), but the ledger lives in the agent-writable data/ folder: an id read back
# goes into a Terminal command and a file name, so anything that does not look like one of ours is refused.
ID_RE = re.compile(r"ft-[a-z0-9][a-z0-9-]{0,79}")


def safe_id(cid):
    if not isinstance(cid, str) or not ID_RE.fullmatch(cid):
        raise NotFound(f"not a followthrough claim id: {cid!r}")
    return cid


class NotAmended(Exception):
    pass


SECRET_PATTERNS = [   # defense in depth for text that leaves the transcript (ledger, Telegram, prompts); not a guarantee
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),                                  # Telegram bot token
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b"),                                # API keys (OpenAI/Stripe style)
    re.compile(r"\b(?:ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{20,}\b"),                  # GitHub
    re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}\b"),                                   # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                                              # AWS
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),                                        # Google API key
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)\b([\w-]*(?:api[_-]?key|token|secret|password|passwd|pwd)['\"]?\s*[=:]\s*['\"]?)[^\s'\",}]{8,}"),  # also JSON
    re.compile(r"(?i)(://[^\s:/@]+:)[^\s@/]{4,}(@)"),                                # credentials in URLs
    re.compile(r"(?i)([?&](?:sig|signature|token|key|x-amz-signature)=)[^&\s]{8,}"),     # signed URLs
]


def redact(text):
    """Replace secret-shaped substrings with [REDACTED]."""
    if not text:
        return text
    for rx in SECRET_PATTERNS:
        text = rx.sub(lambda m: (m.group(1) if m.lastindex and m.lastindex >= 1 else "") + "[REDACTED]"
                      + (m.group(2) if m.lastindex and m.lastindex >= 2 else ""), text)
    return text


def guess_kind(prompt, names=None):
    p = prompt.lower()
    names = _user_names() if names is None else [n.lower() for n in names]
    for kind, rx in KIND_RULES:
        if kind == "ask" and names:
            rx += "".join(rf"|\bask {re.escape(n)}\b|\breminder for {re.escape(n)}\b" for n in names)
        if re.search(rx, p):
            return kind
    return "verify"


def prompt_hash(text):
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()[:16]


def title_from(text, limit=80):
    first = re.split(r"(?<=[.!?])\s|\n", " ".join(text.split()), maxsplit=1)[0]
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def new_id(title, when, tz=""):
    from zoneinfo import ZoneInfo
    if tz:
        when = when.astimezone(ZoneInfo(tz))
    words = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if not w.isdigit()]
    stop = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "check", "reminder", "after", "is", "it",
            "set", "today", "now", "this", "that", "with", "from", "me", "user"} | set(_user_names())
    slug = "-".join([w for w in words if w not in stop][:3]) or "claim"
    return f"ft-{when.strftime('%m%d')}-{slug[:28]}-{secrets.token_hex(2)}"


def default_ends_at(kind, last_due):
    return last_due + (dt.timedelta(days=7) if kind in ("ask", "action") else dt.timedelta(hours=48))


def _ps(pid, fields):
    try:
        fmt = ",".join(f"{f}=" for f in fields.split(","))   # "=" on every field: no header line
        return subprocess.run(["ps", "-o", fmt, "-p", str(pid)], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def process_start(pid):
    return " ".join(_ps(pid, "lstart").split()) if pid else ""


def agent_pid():
    """(pid, start time) of the nearest claude/codex ancestor process - the session holding a lease - or (None, '')."""
    pid = os.getppid()
    for _ in range(10):
        if pid <= 1:
            return None, ""
        out = _ps(pid, "ppid,comm")
        if not out:
            return None, ""
        parts = out.split(None, 1)
        if not parts or not parts[0].isdigit():
            return None, ""
        ppid, comm = parts[0], (parts[1] if len(parts) > 1 else "")
        if re.search(r"(^|/)(claude|codex)$", comm.strip(), re.I):
            return pid, process_start(pid)
        pid = int(ppid)
    return None, ""


def pid_alive(pid, start=""):
    """True if `pid` runs and, when `start` is known, is the same process (not a reused pid)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return not start or process_start(pid) == start


def _insert_claim(con, *, title, kind, dues, repo, runbook, expectation, change_ref, live_check, mode, ends_at,
                  origin, harness, source_session_id, source_cwd, source_cron_id, source_event, tz, phash,
                  confirmed=True, cid=None):
    title, runbook, expectation, change_ref, live_check = (redact(x) for x in (title, runbook, expectation, change_ref,
                                                                               live_check))
    cid = cid or new_id(title, dues[0][0], tz)   # after redaction: the id is built from the title
    ends = ends_at or default_ends_at(kind, dues[-1][0])
    con.execute(
        "INSERT INTO claims(id, kind, title, repo, runbook, expectation, change_ref, live_check, mode, ends_at, origin,"
        " harness, source_session_id, source_cwd, source_cron_id, source_event, prompt_hash, tz, created_at, confirmed)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, kind, title, repo, runbook, expectation, change_ref, live_check, mode, db.iso(ends), origin, harness,
         source_session_id, source_cwd, source_cron_id, source_event, phash, tz, db.iso(db.now()), 1 if confirmed else 0))
    for seq, (due, role) in enumerate(dues, 1):
        con.execute("INSERT INTO checkpoints(claim_id, seq, due_at, role) VALUES (?,?,?,?)",
                    (cid, seq, db.iso(due), role))
    db.event(con, cid, "created", f"origin={origin} checkpoints={len(dues)}")
    return cid


def _norm_dues(dues):
    if not dues:
        raise ValueError("at least one checkpoint time is required")
    dues = sorted(dues)
    if dues[-1][1] != "final":
        dues[-1] = (dues[-1][0], "final")
    return dues


def add_claim(con, *, title, kind, dues, repo="", runbook="", expectation="", change_ref="", live_check="",
              mode="notify", ends_at=None, origin="cli", harness="", source_session_id="", source_cwd="",
              source_cron_id="", source_event="", tz="", phash=""):
    """Create a claim. Returns (claim_id, created). With `phash`, an active claim with the same runbook in the same
    session (or with no session) is reused instead of duplicated."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    dues = _norm_dues(dues)
    with db.tx(con):
        if phash:
            row = con.execute("SELECT id FROM claims WHERE prompt_hash=? AND source_session_id=? AND status='active'",
                              (phash, source_session_id)).fetchone()
            if row:
                db.event(con, row["id"], "dedup", f"same runbook registered again (origin={origin})")
                return row["id"], False
        cid = _insert_claim(con, title=title, kind=kind, dues=dues, repo=repo, runbook=runbook, expectation=expectation,
                            change_ref=change_ref, live_check=live_check, mode=mode, ends_at=ends_at, origin=origin,
                            harness=harness, source_session_id=source_session_id, source_cwd=source_cwd,
                            source_cron_id=source_cron_id, source_event=source_event, tz=tz, phash=phash)
    return cid, True


def capture(con, *, session_id, source_event, cron_id, prompt, dues, cwd, tz, kind=None, origin="cron",
            harness="claude-code", confirmed=True, cid=None):
    """Record an in-session reminder. Identity is (session, CronCreate tool_use id); terminal claims are tombstones,
    so a cancelled capture is never recreated. Returns (claim_id, what) with what in
    {'created', 'exists', 'linked', 'rescheduled'}."""
    dues = _norm_dues(dues)
    phash = prompt_hash(redact(prompt))   # the scan only ever sees the redacted prompt; the hook must hash the same
    prompt = redact(prompt)
    with db.tx(con):
        row = con.execute("SELECT id, source_cron_id, confirmed FROM claims WHERE source_session_id=? AND source_event=?",
                          (session_id, source_event)).fetchone()
        if row:
            if cron_id and (not row["source_cron_id"] or not row["confirmed"]):  # the hook captured it before success
                con.execute("UPDATE claims SET source_cron_id=?, confirmed=1 WHERE id=?", (cron_id, row["id"]))
                db.event(con, row["id"], "linked", f"cron job {cron_id} (scan)")
            return row["id"], "exists"
        # Same reminder already known without its identity (imported earlier) or created twice at the same time -
        # among ACTIVE claims only: a cancelled claim is a tombstone for its own creation, never for a new one.
        for r in con.execute("SELECT c.id, c.source_event, cp.due_at FROM claims c JOIN checkpoints cp ON cp.claim_id=c.id"
                             " WHERE c.source_session_id=? AND c.prompt_hash=? AND cp.role='final' AND c.status='active'",
                             (session_id, phash)):
            if abs(db.parse(r["due_at"]) - dues[-1][0]) <= dt.timedelta(minutes=2):
                if not r["source_event"]:
                    con.execute("UPDATE claims SET source_event=?, source_cron_id=? WHERE id=?",
                                (source_event, cron_id, r["id"]))
                    db.event(con, r["id"], "linked", f"cron job {cron_id}")
                    return r["id"], "linked"
                db.event(con, r["id"], "dedup", f"duplicate CronCreate {source_event} (job {cron_id})")
                return r["id"], "exists"
        # Same reminder re-created at a new time in the same session: move the open checkpoint.
        row = con.execute("SELECT id FROM claims WHERE source_session_id=? AND prompt_hash=? AND status='active'"
                          " ORDER BY created_at DESC LIMIT 1", (session_id, phash)).fetchone()
        if row and len(dues) > 1:  # a recurring job re-created: its future checkpoints replace the pending ones
            con.execute("DELETE FROM checkpoints WHERE claim_id=? AND state='pending'", (row["id"],))
            last = con.execute("SELECT COALESCE(MAX(seq), 0) FROM checkpoints WHERE claim_id=?", (row["id"],)).fetchone()[0]
            for i, (due, role) in enumerate(dues, 1):
                con.execute("INSERT INTO checkpoints(claim_id, seq, due_at, role) VALUES (?,?,?,?)", (row["id"], last + i, db.iso(due), role))
            con.execute("UPDATE claims SET source_event=?, source_cron_id=?, runbook=?, prompt_hash=?, ends_at=? WHERE id=?",
                        (source_event, cron_id, prompt, phash, db.iso(default_ends_at("watch", dues[-1][0])), row["id"]))
            db.event(con, row["id"], "rescheduled", f"recurring job re-created as {cron_id}; {len(dues)} checkpoints")
            return row["id"], "rescheduled"
        if row and len(dues) == 1:
            cp = current_checkpoint(con, row["id"])
            if cp and cp["state"] == "pending":
                con.execute("UPDATE checkpoints SET due_at=?, notified_at='' WHERE id=?", (db.iso(dues[0][0]), cp["id"]))
                con.execute("UPDATE claims SET source_event=?, source_cron_id=?, ends_at=MAX(ends_at, ?) WHERE id=?",
                            (source_event, cron_id, db.iso(default_ends_at(kind or guess_kind(prompt), dues[0][0])), row["id"]))
                db.event(con, row["id"], "rescheduled", f"re-created as job {cron_id} for {db.iso(dues[0][0])}")
                return row["id"], "rescheduled"
        cid = _insert_claim(con, title=title_from(redact(prompt)), kind=kind or guess_kind(prompt), dues=dues, repo=cwd,
                            runbook=prompt, expectation="", change_ref="", live_check="", mode="notify", ends_at=None,
                            origin=origin, harness=harness, source_session_id=session_id, source_cwd=cwd,
                            source_cron_id=cron_id, source_event=source_event, tz=tz, phash=phash,
                            confirmed=confirmed, cid=cid)
    return cid, "created"


def cancel_cron(con, session_id, cron_id, reason):
    """A CronDelete in the session: cancel the matching active claim, if any."""
    with db.tx(con):
        row = con.execute("SELECT id FROM claims WHERE source_session_id=? AND source_cron_id=? AND status='active'",
                          (session_id, cron_id)).fetchone()
        if not row:
            return None
        _close_in_tx(con, row["id"], "cancelled", reason)
    return row["id"]


def get(con, ref):
    """Claim by exact id or unique prefix."""
    row = con.execute("SELECT * FROM claims WHERE id=?", (ref,)).fetchone()
    if row:
        safe_id(row["id"])
        return row
    rows = con.execute("SELECT * FROM claims WHERE id LIKE ? ESCAPE '\\'",
                       (ref.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",)).fetchall()
    if len(rows) == 1:
        safe_id(rows[0]["id"])
        return rows[0]
    raise NotFound(f"no claim matches {ref!r}" if not rows else f"{ref!r} is ambiguous ({len(rows)} claims)")


def checkpoints(con, claim_id):
    return con.execute("SELECT * FROM checkpoints WHERE claim_id=? ORDER BY seq", (claim_id,)).fetchall()


def current_checkpoint(con, claim_id):
    return con.execute("SELECT * FROM checkpoints WHERE claim_id=? AND state NOT IN ('done','expired')"
                       " ORDER BY seq LIMIT 1", (claim_id,)).fetchone()


def pick_checkpoint(con, claim, now):
    """The checkpoint a check started now is about: for a series (watch), the latest open one already due (or due
    within EARLY_RESOLVE); otherwise the first open one."""
    if claim["kind"] == "watch":
        row = con.execute("SELECT * FROM checkpoints WHERE claim_id=? AND state NOT IN ('done','expired') AND due_at<=?"
                          " ORDER BY seq DESC LIMIT 1", (claim["id"], db.iso(now + EARLY_RESOLVE))).fetchone()
        if row:
            return row
    return current_checkpoint(con, claim["id"])


def start(con, ref, path, pid=None, deadline_hours=3, pid_start=""):
    """Take the lease on the claim's current checkpoint. Returns ('OK', attempt_id) | ('TAKEN', text) | ('CLOSED', text).

    An open attempt blocks a new one while its deadline has not passed or its agent process is still alive."""
    claim = get(con, ref)
    with db.tx(con):
        claim = con.execute("SELECT * FROM claims WHERE id=?", (claim["id"],)).fetchone()
        if claim["status"] != "active":
            if auto_expired(claim):
                return "CLOSED", (f"claim is abandoned ({claim['verdict_summary']}). If the user wants the check run "
                                  f"now, run it and record it without --attempt: followthrough resolve {claim['id']} "
                                  f"--verdict <v> --summary \"...\" - a late verdict replaces the expiry")
            return "CLOSED", f"claim is {claim['status']}"
        now = db.now()
        cp = pick_checkpoint(con, claim, now)
        if cp is None:
            return "CLOSED", "no open checkpoint"
        live = con.execute("SELECT * FROM attempts WHERE checkpoint_id=? AND ended_at=''", (cp["id"],)).fetchone()
        if live:
            if db.parse(live["deadline"]) > now or pid_alive(live["pid"], live["pid_start"]):
                return "TAKEN", f"by {live['path']} since {live['started_at']} (attempt {live['id']})"
            con.execute("UPDATE attempts SET ended_at=?, outcome='stale' WHERE id=? AND ended_at=''",
                        (db.iso(now), live["id"]))
            db.event(con, claim["id"], "stale_attempt", f"{live['path']} {live['id']} replaced")
        aid = "at-" + secrets.token_hex(4)
        con.execute("INSERT INTO attempts(id, checkpoint_id, path, pid, pid_start, started_at, deadline) VALUES (?,?,?,?,?,?,?)",
                    (aid, cp["id"], path, pid, pid_start, db.iso(now), db.iso(now + dt.timedelta(hours=deadline_hours))))
        con.execute("UPDATE checkpoints SET state='running' WHERE id=?", (cp["id"],))
        db.event(con, claim["id"], "started", f"{path} {aid} checkpoint {cp['seq']} pid={pid}")
    return "OK", aid


def resolve(con, ref, verdict, summary="", retry_at=None, final=False, checkpoint=None, attempt=None):
    """Record a verdict. It applies to checkpoint `checkpoint` (its seq) when given; otherwise to the current
    checkpoint only if it is running, waiting for a human, or due within EARLY_RESOLVE - a repeated verdict never
    closes a future checkpoint. Returns a short description of what happened."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    summary = redact(summary)
    claim = get(con, ref)
    cid = claim["id"]
    with db.tx(con):
        claim = con.execute("SELECT * FROM claims WHERE id=?", (cid,)).fetchone()
        now = db.now()
        if claim["status"] != "active":
            if auto_expired(claim) and verdict != "not_settled":
                con.execute("UPDATE claims SET status=?, verdict_summary=?, closed_at=? WHERE id=?",
                            (verdict, summary, db.iso(now), cid))
                db.event(con, cid, "late_resolution", f"{verdict}: {summary}")
                return f"late verdict recorded: {verdict} (supersedes expired)"
            db.event(con, cid, "note", f"verdict after close ({verdict}): {summary}")
            return (f"claim already {claim['status']}; recorded as a note. Only if the user asks to change the verdict: "
                    f"followthrough amend {cid} --verdict <v> --summary \"...\" --reason \"user asked: ...\"")
        if attempt:
            a = con.execute("SELECT a.*, cp.claim_id FROM attempts a JOIN checkpoints cp ON cp.id=a.checkpoint_id"
                            " WHERE a.id=?", (attempt,)).fetchone()
            if a is None or a["claim_id"] != cid:
                db.event(con, cid, "note", f"{verdict}: {summary} (unknown attempt {attempt})")
                return f"attempt {attempt} does not belong to {cid}; recorded as a note"
            if a["ended_at"]:
                db.event(con, cid, "note", f"{verdict}: {summary} (attempt {attempt} already ended: {a['outcome']})")
                return f"attempt {attempt} already recorded ({a['outcome']}); this one is a note"
            cp = con.execute("SELECT * FROM checkpoints WHERE id=? AND state NOT IN ('done','expired')",
                             (a["checkpoint_id"],)).fetchone()
        elif checkpoint is not None:
            cp = con.execute("SELECT * FROM checkpoints WHERE claim_id=? AND seq=? AND state NOT IN ('done','expired')",
                             (cid, checkpoint)).fetchone()
        else:
            cp = current_checkpoint(con, cid)
            if cp is not None and cp["state"] == "pending" and not final and db.parse(cp["due_at"]) > now + EARLY_RESOLVE:
                db.event(con, cid, "note", f"{verdict}: {summary} (checkpoint {cp['seq']} not due until {cp['due_at']})")
                return (f"checkpoint {cp['seq']} is not due until {cp['due_at']} - recorded as a note; "
                        f"pass --checkpoint {cp['seq']} (or --final) to apply it")
        if cp is None:
            db.event(con, cid, "note", f"{verdict}: {summary}")
            return "no matching open checkpoint; recorded as a note"
        con.execute("UPDATE claims SET snoozed_until='' WHERE id=?", (cid,))   # a snooze was about this checkpoint
        con.execute("UPDATE attempts SET ended_at=?, outcome=? WHERE checkpoint_id=? AND ended_at=''",
                    (db.iso(now), verdict, cp["id"]))
        if verdict == "not_settled":
            tries = cp["tries"] + 1
            if tries >= MAX_TRIES:
                con.execute("UPDATE checkpoints SET tries=?, state='needs_human', notified_at=?, summary=? WHERE id=?",
                            (tries, db.iso(now), summary, cp["id"]))
                db.event(con, cid, "not_settled", f"attempt {tries}/{MAX_TRIES} - needs a human: {summary}")
                db.enqueue(con, cid, cp["id"], "exhausted", f"not settled after {tries} attempts: {summary}")
                return f"not settled after {tries} attempts - needs you"
            nxt = retry_at or (now + dt.timedelta(hours=24))
            con.execute("UPDATE checkpoints SET tries=?, state='pending', due_at=?, notified_at='', summary=? WHERE id=?",
                        (tries, db.iso(nxt), summary, cp["id"]))
            if db.parse(claim["ends_at"]) < nxt + dt.timedelta(hours=48):
                con.execute("UPDATE claims SET ends_at=? WHERE id=?", (db.iso(nxt + dt.timedelta(hours=48)), cid))
            db.event(con, cid, "not_settled", f"retry at {db.iso(nxt)}: {summary}")
            return f"not settled - rescheduled to {db.iso(nxt)}"
        con.execute("UPDATE checkpoints SET state='done', result=?, summary=?, done_at=? WHERE id=?",
                    (verdict, summary, db.iso(now), cp["id"]))
        db.event(con, cid, "reading", f"checkpoint {cp['seq']} ({cp['role']}): {verdict}: {summary}")
        # a later reading supersedes earlier interim ones that never got theirs
        n = con.execute("UPDATE checkpoints SET state='expired', summary=summary || ' [skipped: a later reading was recorded]'"
                        " WHERE claim_id=? AND seq<? AND role='interim' AND state NOT IN ('done','expired')",
                        (cid, cp["seq"])).rowcount
        if n:
            db.event(con, cid, "skipped", f"{n} earlier interim checkpoint(s) without a reading")
        more = current_checkpoint(con, cid)
        if cp["role"] == "final" or final or more is None:
            if more is not None:
                con.execute("UPDATE checkpoints SET state='expired' WHERE claim_id=? AND state NOT IN ('done','expired')", (cid,))
            con.execute("UPDATE claims SET status=?, verdict_summary=?, closed_at=? WHERE id=?",
                        (verdict, summary, db.iso(now), cid))
            db.event(con, cid, "closed", verdict)
            return f"closed: {verdict}"
        return f"interim reading recorded ({verdict}); next checkpoint {more['due_at']}"


AMENDABLE = ("worked", "failed", "partial", "inconclusive")


def amend(con, ref, verdict, summary, reason):
    """Change a closed claim's verdict because a person asked (new data, a relabel). Agents keep "the first verdict
    wins"; this is the explicit exception, and the earlier verdict stays in the history. Returns a description."""
    if verdict not in AMENDABLE:
        raise ValueError(f"verdict must be one of {AMENDABLE}")
    if not reason.strip() or not summary.strip():
        raise ValueError("a summary and a reason are required")
    claim = get(con, ref)
    with db.tx(con):
        c = con.execute("SELECT * FROM claims WHERE id=?", (claim["id"],)).fetchone()
        if c["status"] == "active":
            raise NotAmended(f"{c['id']} is still active - record its verdict with resolve")
        n = con.execute("UPDATE claims SET status=?, verdict_summary=? WHERE id=? AND status=?",
                        (verdict, redact(summary), c["id"], c["status"])).rowcount
        if n != 1:
            raise NotAmended(f"{c['id']} changed meanwhile; nothing amended")
        db.event(con, c["id"], "amended", redact(f"{c['status']} -> {verdict} ({reason}). Was: {c['verdict_summary']} "
                                                 f"Now: {summary}"))
    return f"amended {c['id']}: {c['status']} -> {verdict}"


def _close_in_tx(con, cid, status, reason):
    reason = redact(reason)
    now = db.iso(db.now())
    con.execute("UPDATE claims SET status=?, verdict_summary=?, closed_at=? WHERE id=? AND status='active'",
                (status, reason, now, cid))
    con.execute("UPDATE checkpoints SET state='expired' WHERE claim_id=? AND state NOT IN ('done','expired')", (cid,))
    con.execute("UPDATE attempts SET ended_at=?, outcome=? WHERE ended_at='' AND checkpoint_id IN"
                " (SELECT id FROM checkpoints WHERE claim_id=?)", (now, status, cid))
    db.event(con, cid, status, reason)


def discard_provisional(con, cid):
    """Delete a hook capture that never became real (the hook failed before the reminder was rewritten). Unlike a
    cancellation it leaves no tombstone, so the transcript scan can still capture the reminder if it was scheduled."""
    with db.tx(con):
        if con.execute("DELETE FROM claims WHERE id=? AND confirmed=0 AND status='active'", (cid,)).rowcount:
            con.execute("DELETE FROM attempts WHERE checkpoint_id IN (SELECT id FROM checkpoints WHERE claim_id=?)", (cid,))
            for table in ("checkpoints", "events", "outbox"):
                con.execute(f"DELETE FROM {table} WHERE claim_id=?", (cid,))


def close(con, ref, status, reason):
    """Cancel or abandon a claim."""
    if status not in ("cancelled", "abandoned"):
        raise ValueError("status must be cancelled or abandoned")
    claim = get(con, ref)
    with db.tx(con):
        _close_in_tx(con, claim["id"], status, reason)


def expect(con, ref, text):
    """Set the claim's expectation once. It is never edited afterwards (append notes instead)."""
    claim = get(con, ref)
    with db.tx(con):
        n = con.execute("UPDATE claims SET expectation=? WHERE id=? AND expectation=''", (redact(text), claim["id"])).rowcount
        if n:
            db.event(con, claim["id"], "expectation", redact(text)[:300])
    return n == 1


MAX_SNOOZE = dt.timedelta(days=30)


class NotSnoozed(Exception):
    pass


def snooze(con, ref, until, source="cli"):
    """Hold the claim's notifications until `until`; `until=None` ends the snooze now. The runner holds what it
    queues meanwhile and, when the snooze ends, sends one notice (runner.transitions step 0). A verdict ends it too.
    Returns the new snoozed_until (UTC string)."""
    claim = get(con, ref)
    now = db.now()
    if until is not None and not now < until <= now + MAX_SNOOZE:
        raise NotSnoozed(f"a snooze must end in the future and within {MAX_SNOOZE.days} days")
    with db.tx(con):
        c = con.execute("SELECT * FROM claims WHERE id=?", (claim["id"],)).fetchone()
        if c["status"] != "active":
            raise NotSnoozed(f"{c['id']} is {c['status']}; nothing to snooze")
        if until is None:
            if not snoozed(c, now):
                raise NotSnoozed(f"{c['id']} is not snoozed")
            until = now   # the next tick ends it, and sends what was held
        con.execute("UPDATE claims SET snoozed_until=? WHERE id=?", (db.iso(until), c["id"]))
        db.event(con, c["id"], "snoozed" if until > now else "unsnoozed", f"until {db.iso(until)} ({source})", at=now)
    return db.iso(until)


def note(con, ref, text):
    claim = get(con, ref)
    with db.tx(con):
        db.event(con, claim["id"], "note", text)


def events(con, claim_id):
    return con.execute("SELECT * FROM events WHERE claim_id=? ORDER BY id", (claim_id,)).fetchall()
