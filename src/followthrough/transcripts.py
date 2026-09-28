"""Incremental scan of Claude Code transcripts (~/.claude/projects/*/*.jsonl).

Structural, not textual: a reminder is captured only when its CronCreate call has a successful result carrying the
job id (`toolUseResult.id`); a CronDelete of that id cancels it; a user record with `scheduledTaskId` equal to the
job id is the reminder firing in its session. Each file is read from the byte offset where the last scan stopped,
and an error in one file never stops the others.
"""
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import time

from . import config, core, db, timeparse

ROOT = os.path.expanduser("~/.claude/projects")
MARKERS = (b'"CronCreate"', b'"CronDelete"', b'"humanSchedule"', b'"scheduledTaskId"')
OVERDUE_LOOKBACK = dt.timedelta(hours=48)   # a reminder first seen after its due time is still imported this late
MAX_WATCH_CHECKPOINTS = 8


SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def find(session_id, root=None):
    if not isinstance(session_id, str) or not SESSION_RE.fullmatch(session_id):   # a file name, never a path or glob
        return None
    hits = glob.glob(os.path.join(root or ROOT, "*", f"{session_id}.jsonl"))
    return hits[0] if hits else None


def _tool_blocks(rec, kind):
    c = rec.get("message", {}).get("content")
    return [b for b in c if isinstance(b, dict) and b.get("type") == kind] if isinstance(c, list) else []


def parse_ts(s):
    """A transcript timestamp ("2026-09-23T21:53:00.123Z") as an aware datetime."""
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _handle_create(con, cfg, now, pending, result_rec, log):
    """A successful CronCreate result: turn the pending call into a claim (or skip it, with a reason)."""
    info = result_rec.get("toolUseResult") or {}
    job_id = str(info.get("id") or "")
    inp = json.loads(pending["input"])
    prompt, cron = inp.get("prompt", ""), inp.get("cron", "")
    recurring = bool(inp.get("recurring", True))
    set_at = parse_ts(pending["ts"])
    tz = cfg["tz"]
    if not job_id or not prompt or not cron or config.ignored(cfg, pending["cwd"]):
        return
    if recurring:
        period = timeparse.cron_period(cron, set_at, tz)
        if period is None or period < dt.timedelta(hours=23):
            return  # sub-daily recurring jobs are in-task monitors, not follow-ups
        fires = timeparse.next_fires(cron, set_at, tz, count=MAX_WATCH_CHECKPOINTS)
        # the in-session job's own 7-day life, and only fires still ahead: past ones are history, not checks to do
        fires = [f for f in fires if now < f <= set_at + dt.timedelta(days=7)]
        if not fires:
            return
        dues = [(f, "interim") for f in fires]
        kind = "watch"
    else:
        due = timeparse.cron_fire_time(cron, set_at, tz)
        if due is None or due - set_at < dt.timedelta(minutes=cfg.get("capture_min_minutes", 30)):
            return
        if due < now - OVERDUE_LOOKBACK:
            return
        dues, kind = [(due, "final")], None
    cid, what = core.capture(con, session_id=pending["session_id"], source_event=pending["tool_use_id"],
                             cron_id=job_id, prompt=prompt, dues=dues, cwd=pending["cwd"], tz=tz, kind=kind)
    if what != "exists":
        log.append(f"{what} {cid} (session {pending['session_id'][:8]}, job {job_id})")


def _process_record(con, cfg, now, sid, rec, log):
    t = rec.get("type")
    if t == "assistant":
        for b in _tool_blocks(rec, "tool_use"):
            name, inp = b.get("name"), b.get("input") or {}
            if name in ("CronCreate", "CronDelete") and b.get("id"):
                # both wait for their tool result: only a successful call changes the ledger
                con.execute("INSERT OR IGNORE INTO pending_uses(tool_use_id, session_id, cwd, ts, input) VALUES (?,?,?,?,?)",
                            (b["id"], sid, rec.get("cwd") or "", rec.get("timestamp") or db.iso(now),
                             json.dumps({**{k: inp[k] for k in ("cron", "recurring", "id") if k in inp},
                                         "prompt": core.redact(inp.get("prompt", "")), "_tool": name})))
    elif t == "user":
        if rec.get("scheduledTaskId"):
            con.execute("INSERT OR IGNORE INTO fires(job_id, session_id, ts) VALUES (?,?,?)",
                        (str(rec["scheduledTaskId"]), sid, rec.get("timestamp") or db.iso(now)))
        for b in _tool_blocks(rec, "tool_result"):
            pending = con.execute("SELECT * FROM pending_uses WHERE tool_use_id=?", (b.get("tool_use_id"),)).fetchone()
            if pending is None:
                continue
            con.execute("DELETE FROM pending_uses WHERE tool_use_id=?", (pending["tool_use_id"],))
            if b.get("is_error"):
                continue
            inp = json.loads(pending["input"])
            if inp.get("_tool") == "CronDelete":
                cid = core.cancel_cron(con, sid, str(inp.get("id")), f"CronDelete {inp.get('id')} in session {sid[:8]}")
                if cid:
                    log.append(f"cancelled {cid} (CronDelete in session)")
            elif isinstance(rec.get("toolUseResult"), dict):
                _handle_create(con, cfg, now, pending, rec, log)


def scan(con, cfg, now=None, root=None, lookback_days=45, budget_s=20.0):
    """Read new transcript bytes since the last scan. Returns log lines. Never raises for a bad file."""
    now = now or db.now()
    root = root or ROOT
    log, started = [], time.monotonic()
    cutoff = now.timestamp() - lookback_days * 86400
    try:
        files = sorted(glob.glob(os.path.join(root, "*", "*.jsonl")), key=lambda f: os.path.getmtime(f) if os.path.exists(f) else 0,
                       reverse=True)
    except OSError as e:
        return [f"scan: cannot list transcripts ({type(e).__name__})"]
    for f in files:
        if time.monotonic() - started > budget_s:
            log.append("scan: time budget reached; continuing next tick")
            break
        try:
            st = os.stat(f)
            if st.st_mtime < cutoff:
                continue
            row = con.execute("SELECT inode, offset FROM scan_files WHERE path=?", (f,)).fetchone()
            offset = row["offset"] if row and row["inode"] == st.st_ino and row["offset"] <= st.st_size else 0
            if offset == st.st_size:
                continue
            with open(f, "rb") as fh:
                fh.seek(offset)
                data = fh.read(st.st_size - offset)
            end = data.rfind(b"\n") + 1   # only complete lines; a half-written last line waits for the next scan
            chunk = data[:end]
            sid = os.path.basename(f)[:-6]
            done = end                    # bytes safely handled; stops early on a retryable failure
            # results of calls we are waiting on (CronCreate/CronDelete) carry only their tool_use id
            waiting = {r[0].encode() for r in con.execute("SELECT tool_use_id FROM pending_uses WHERE session_id=?", (sid,))}
            if any(m in chunk for m in MARKERS) or any(w in chunk for w in waiting):
                pos = 0
                for line in chunk.split(b"\n"):
                    line_start, pos = pos, pos + len(line) + 1
                    if not any(m in line for m in MARKERS) and not any(w in line for w in waiting):
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue          # malformed: skip for good
                    try:
                        with db.tx(con):
                            _process_record(con, cfg, now, sid, rec, log)
                        for b in _tool_blocks(rec, "tool_use"):
                            if b.get("name") in ("CronCreate", "CronDelete") and b.get("id"):
                                waiting.add(b["id"].encode())
                    except sqlite3.OperationalError as e:   # busy/locked/disk: retry this record next scan
                        done = line_start
                        log.append(f"scan: will retry {sid[:8]} ({core.redact(str(e))[:60]})")
                        break
                    except Exception as e:  # noqa: BLE001 - bad content: skip this record, keep going
                        log.append(f"scan: skipped a record in {sid[:8]} ({type(e).__name__}: {core.redact(str(e))[:80]})")
            con.execute("INSERT INTO scan_files(path, inode, offset) VALUES (?,?,?) ON CONFLICT(path) DO UPDATE"
                        " SET inode=excluded.inode, offset=excluded.offset", (f, st.st_ino, offset + done))
        except Exception as e:  # noqa: BLE001 - deleted mid-read, unreadable, etc.
            log.append(f"scan: skipped {os.path.basename(f)[:8]} ({type(e).__name__})")
    con.execute("DELETE FROM pending_uses WHERE ts < ?", (db.iso(now - dt.timedelta(days=2)),))
    return log


FIRE_WINDOW = (dt.timedelta(minutes=10), dt.timedelta(hours=24))   # a fire counts for a checkpoint due in this window


def fired(con, claim, cp):
    """When the claim's reminder fired in its own session for this checkpoint, else None. Structural: the fire record
    carries scheduledTaskId == the CronCreate job id, and it must fall near this checkpoint's due time (a recurring job
    fires once per checkpoint)."""
    if not claim["source_cron_id"] or not claim["source_session_id"]:
        return None
    due = db.parse(cp["due_at"])
    lo, hi = due - FIRE_WINDOW[0], due + FIRE_WINDOW[1]
    nxt = con.execute("SELECT MIN(due_at) FROM checkpoints WHERE claim_id=? AND seq>?", (cp["claim_id"], cp["seq"])).fetchone()[0]
    if nxt:  # a series: a fire belongs to the nearest checkpoint, so stop halfway to the next one
        hi = min(hi, due + (db.parse(nxt) - due) / 2)
    row = con.execute("SELECT MIN(ts) AS ts FROM fires WHERE job_id=? AND session_id=? AND ts>=? AND ts<=?",
                      (claim["source_cron_id"], claim["source_session_id"],
                       lo.strftime("%Y-%m-%dT%H:%M:%S"), hi.strftime("%Y-%m-%dT%H:%M:%S.999Z"))).fetchone()
    return row["ts"] if row and row["ts"] else None


REPLY_LIMIT = 1500   # characters of the session's reply kept for a notification or a prompt
TURN_END = ("turn_duration", "stop_hook_summary")   # system records Claude Code writes when a turn ends


def _is_prompt(rec):
    """A user record that starts a new turn: typed text or another scheduled fire - not a tool result, not text
    the harness injects mid-turn (isMeta, e.g. a loaded skill)."""
    if rec.get("scheduledTaskId"):
        return True
    if rec.get("isMeta"):
        return False
    c = rec.get("message", {}).get("content")
    return isinstance(c, str) or (isinstance(c, list) and bool(_tool_blocks(rec, "text")) and not _tool_blocks(rec, "tool_result"))


def _lines_after_fire(path, job, stamp):
    """The transcript bytes after the fire record (scheduledTaskId == job, timestamp == stamp), or None. Reads from
    the end in growing windows: the fire is usually recent and transcripts can be hundreds of MB."""
    size = os.path.getsize(path)
    n = 1 << 23
    while True:
        start = max(0, size - n)
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(size - start)
        if start:
            data = data[data.find(b"\n") + 1:]
        pos = 0
        for line in data.split(b"\n"):
            pos += len(line) + 1
            if job in line and stamp in line and b"scheduledTaskId" in line:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and str(rec.get("scheduledTaskId")) == job.decode() and rec.get("timestamp") == stamp.decode():
                    return data[pos:]
        if not start:
            return None
        n *= 8


def turn_after(claim, fired_ts, root=None):
    """(the session's last reply in the turn that this fire started, whether that turn has ended). A turn ends at
    Claude Code's turn-end record or at the next prompt. ("", None) when the transcript or the fire is not found
    or cannot be read. Never raises: one damaged transcript must not stop the runner's other claims."""
    try:
        return _turn_after(claim, fired_ts, root)
    except Exception:  # noqa: BLE001
        return "", None


def _turn_after(claim, fired_ts, root):
    path = find(claim["source_session_id"], root)
    if not path or not claim["source_cron_id"]:
        return "", None
    try:
        rest = _lines_after_fire(path, claim["source_cron_id"].encode(), fired_ts.encode())
    except OSError:
        return "", None
    if rest is None:
        return "", None
    last, ended = "", False
    for line in rest.split(b"\n"):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("isSidechain"):
            continue
        t = rec.get("type")
        if (t == "system" and rec.get("subtype") in TURN_END) or (t == "user" and _is_prompt(rec)):
            ended = True
            break
        if t == "assistant":
            text = "\n".join(b.get("text", "") for b in _tool_blocks(rec, "text") if b.get("text", "").strip())
            if text:
                last = text
    last = core.redact(last.replace("**", "").strip())
    return (last if len(last) <= REPLY_LIMIT else last[: REPLY_LIMIT - 1].rstrip() + "…"), ended
