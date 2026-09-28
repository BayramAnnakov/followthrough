"""Claude Code hook handlers: `followthrough hook <event>`, reading the hook's JSON on stdin.

Every handler fails open: on any error it prints nothing and exits 0, so Claude Code behaves exactly as it would
without followthrough. Handlers do nothing inside sessions that followthrough itself started as checkers
(FOLLOWTHROUGH_ROLE=checker) or in ignored folders (config `ignore_paths`).

- pre-cron      PreToolUse(CronCreate): register the reminder as a claim and prefix its prompt with the claim id,
                so the reminder, when it fires in its session, takes the lease and records its verdict.
- post-cron     PostToolUse(CronCreate): store the job id the tool returned.
- cron-failed   PostToolUseFailure(CronCreate): cancel the claim - the reminder never existed.
- stop          Stop (async): session liveness + its live crons.
- session-end   SessionEnd: the session is gone; its open claims no longer wait for it (no grace).
- session-start SessionStart: this repo's open claims (for installs without a custom SessionStart script).
"""
import datetime as dt
import json
import os
import sys

from . import config, core, db, timeparse

PREFIX = "[followthrough claim {cid}]"


def _dues_for(cfg, cron, recurring, now):
    tz = cfg["tz"]
    if recurring:
        period = timeparse.cron_period(cron, now, tz)
        if period is None or period < dt.timedelta(hours=23):
            return None, None
        fires = [f for f in timeparse.next_fires(cron, now, tz, count=8) if f <= now + dt.timedelta(days=7)]
        return ([(f, "interim") for f in fires] or None), "watch"
    due = timeparse.cron_fire_time(cron, now, tz)
    if due is None or due - now < dt.timedelta(minutes=cfg.get("capture_min_minutes", 30)):
        return None, None
    return [(due, "final")], None


def claim_preamble(bin_path, cid):
    return (f"{PREFIX.format(cid=cid)} This reminder is tracked by followthrough. "
            f"1) Run: {bin_path} start {cid} - it prints OK <attempt-id>; if it prints TAKEN or CLOSED, stop and tell "
            f"the user. 2) Run: {bin_path} show {cid} for the expectation and notes. 3) Do the check below - read-only: "
            f"no deploys, sends, publishes or setting changes; never print secrets. 4) Record the result: {bin_path} "
            f"resolve {cid} --attempt <attempt-id> --verdict worked|failed|partial|inconclusive --summary "
            f"\"<measured value vs expected, source, window>\" (or --verdict not_settled --retry-at +12h if the data is "
            f"not settled). A note is not a verdict; never record worked without a measured value.\n\n")


def pre_cron(data, cfg, bin_path):
    if data.get("tool_name") != "CronCreate" or config.ignored(cfg, data.get("cwd")):
        return None
    ti = dict(data.get("tool_input") or {})
    prompt, cron = ti.get("prompt", ""), ti.get("cron", "")
    if not prompt or not cron or prompt.startswith("[followthrough claim "):
        return None
    now = db.now()
    dues, kind = _dues_for(cfg, cron, bool(ti.get("recurring", True)), now)   # CronCreate's own default is recurring
    if not dues:
        return None
    con = db.connect(fast=True)
    # provisional until PostToolUse (or the scan) confirms the job exists; the runner cancels it after an hour otherwise
    cid, what = core.capture(con, session_id=data.get("session_id", ""), source_event=data.get("tool_use_id", ""),
                             cron_id="", prompt=prompt, dues=dues, cwd=data.get("cwd", ""), tz=cfg["tz"], kind=kind,
                             origin="cron", confirmed=False)
    _CREATED["cid"] = cid if what == "created" else None
    ti["prompt"] = claim_preamble(bin_path, cid) + prompt
    c = core.get(con, cid)
    due = core.current_checkpoint(con, cid)
    when = timeparse.local_str(db.parse(due["due_at"]), cfg["tz"]) if due else "?"
    context = (f"followthrough: this reminder is also saved as claim {cid} ({c['kind']}, due {when}). "
               f"It survives this session: if the session is closed, the user is notified and can "
               f"run `followthrough open {cid}`. Mention the claim id when you confirm the reminder.")
    if core.EPHEMERAL.search(prompt):
        context += (f" The reminder names a session scratchpad or /tmp path, which is deleted with this session: copy "
                    f"what the check needs into the ledger with `followthrough attach {cid} <files>`.")
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": ti,
        "additionalContext": context,
    }}


def post_cron(data, cfg, bin_path):
    if config.ignored(cfg, data.get("cwd")):
        return None
    resp = data.get("tool_response")
    job = resp.get("id") if isinstance(resp, dict) else None
    if not job:
        return None
    con = db.connect(fast=True)
    with db.tx(con):
        n = con.execute("UPDATE claims SET source_cron_id=?, confirmed=1 WHERE source_session_id=? AND source_event=?"
                        " AND (source_cron_id='' OR confirmed=0)",
                        (str(job), data.get("session_id", ""), data.get("tool_use_id", ""))).rowcount
        if n:
            row = con.execute("SELECT id FROM claims WHERE source_session_id=? AND source_event=?",
                              (data.get("session_id", ""), data.get("tool_use_id", ""))).fetchone()
            db.event(con, row["id"], "linked", f"cron job {job} (PostToolUse)")
    return None


def cron_failed(data, cfg, bin_path):
    con = db.connect(fast=True)
    row = con.execute("SELECT id FROM claims WHERE source_session_id=? AND source_event=? AND status='active'",
                      (data.get("session_id", ""), data.get("tool_use_id", ""))).fetchone()
    if row:
        core.close(con, row["id"], "cancelled", "CronCreate failed in the session; the reminder was never scheduled")
    return None


def stop(data, cfg, bin_path):
    if config.ignored(cfg, data.get("cwd")):
        return None
    crons = [{k: c.get(k) for k in ("id", "schedule", "recurring")} for c in data.get("session_crons") or []]
    con = db.connect(fast=True)
    if not crons and not con.execute("SELECT 1 FROM claims WHERE source_session_id=? AND status='active'",
                                     (data.get("session_id", ""),)).fetchone():
        return None
    # a resumed session (--resume restores unexpired crons) is alive again: clear ended_at
    con.execute("INSERT INTO sessions(session_id, cwd, last_seen, crons) VALUES (?,?,?,?) ON CONFLICT(session_id) DO UPDATE"
                " SET last_seen=excluded.last_seen, crons=excluded.crons, cwd=excluded.cwd, ended_at=''",
                (data.get("session_id", ""), data.get("cwd", ""), db.iso(db.now()), json.dumps(crons)))
    return None


def session_end(data, cfg, bin_path):
    con = db.connect(fast=True)
    sid = data.get("session_id", "")
    if not con.execute("SELECT 1 FROM claims WHERE source_session_id=? AND status='active'", (sid,)).fetchone():
        return None
    con.execute("INSERT INTO sessions(session_id, cwd, ended_at) VALUES (?,?,?) ON CONFLICT(session_id) DO UPDATE"
                " SET ended_at=excluded.ended_at", (sid, data.get("cwd", ""), db.iso(db.now())))
    return None


def session_start(data, cfg, bin_path):
    import argparse
    from . import cli
    cli.cmd_status(argparse.Namespace(brief=True, repo=data.get("cwd") or os.getcwd(), json=False, all=False),
                   con=db.connect(fast=True))   # a busy ledger (a tick mid-scan) must not stall session start
    return None


HANDLERS = {"pre-cron": pre_cron, "post-cron": post_cron, "cron-failed": cron_failed, "stop": stop,
            "session-end": session_end, "session-start": session_start}


_CREATED = {"cid": None}   # a claim this invocation created, to cancel if anything after it fails


def run(event, bin_path, stdin=None):
    """Entry point. Never raises and never exits non-zero."""
    _CREATED["cid"] = None
    try:
        raw = (stdin or sys.stdin).read()
        data = json.loads(raw) if raw.strip() else {}
        out = HANDLERS[event](data, config.load(), bin_path)
        if out:
            print(json.dumps(out))
    except Exception as e:  # noqa: BLE001 - fail open
        if _CREATED["cid"]:   # the reminder runs without the claim line, so the claim must not pretend otherwise;
            try:              # no tombstone either: the transcript scan may still capture the reminder if it was set
                core.discard_provisional(db.connect(fast=True), _CREATED["cid"])
            except Exception:  # noqa: BLE001
                pass
        try:
            with open(config.path("logs", "hooks.err"), "a") as fh:
                fh.write(f"{db.iso(db.now())} {event} {type(e).__name__}: {core.redact(str(e))[:200]}\n")
        except OSError:
            pass
    return 0
