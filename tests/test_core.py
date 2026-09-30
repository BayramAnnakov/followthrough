import copy
import datetime as dt
import html.parser
import json
import multiprocessing
import os
import subprocess
import sys

import pytest

from followthrough import cli, config, core, db, notify, runner, timeparse, transcripts

UTC = dt.timezone.utc
LA = "America/Los_Angeles"


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setenv("FOLLOWTHROUGH_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(transcripts, "ROOT", str(tmp_path / "no-real-transcripts"))  # never read real sessions
    monkeypatch.setitem(config.DEFAULTS, "tz", LA)   # the tests use Pacific wall-clock times on any machine
    fake = tmp_path / "bin" / "claude"                # `open` needs a claude binary; never depend on a real one
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setitem(config.DEFAULTS, "claude_bin", str(fake))
    return db.connect()


@pytest.fixture
def cfg(con):   # after `con`, so it sees the pinned zone and the scratch home
    return config.load()


def t(h=0, m=0, days=0):
    return db.now() + dt.timedelta(hours=h, minutes=m, days=days)


def add(con, dues=None, kind="verify", **kw):
    return core.add_claim(con, title=kw.pop("title", "latency drops after fix"), kind=kind,
                          dues=dues or [(t(1), "final")], **kw)[0]


class Sender:
    """Stands in for notify.deliver_channel; records (claim, event, channel); can fail the first n calls."""

    def __init__(self, fail_first=0):
        self.sent, self.fail = [], fail_first

    def __call__(self, cfg, channel, claim, cp, event, extra, bin_path):
        if self.fail > 0:
            self.fail -= 1
            return False, "boom"
        self.sent.append((claim["id"], event, channel))
        return True, "ok"

    def events(self, cid=None):
        return [e for c, e, ch in self.sent if ch == "telegram" and (cid is None or c == cid)]


def tick(con, cfg, now=None, send=None, **kw):
    return runner.tick(con, cfg, "ft", now=now, send=send or Sender(), scan=kw.pop("scan", False), **kw)


# ---------------------------------------------------------------- ledger, leases, verdicts

def test_files_are_private(con, tmp_path):
    home, data = tmp_path / "home", tmp_path / "home" / "data"
    add(con)
    assert oct(os.stat(home).st_mode & 0o777) == "0o700" and oct(os.stat(data).st_mode & 0o777) == "0o700"
    assert (data / "followthrough.db").exists()
    for f in ("followthrough.db", "followthrough.db-wal", "followthrough.db-shm"):
        if (data / f).exists():
            assert oct(os.stat(data / f).st_mode & 0o777) == "0o600", f


def test_single_checkpoint_resolves_claim(con):
    cid = add(con)
    assert core.start(con, cid, "session")[0] == "OK"
    assert "closed: worked" in core.resolve(con, cid, "worked", "p95 7.9 s vs 20.8 s")
    assert core.get(con, cid)["status"] == "worked"


def test_repeated_resolve_does_not_close_future_checkpoint(con):
    cid = add(con, dues=[(t(m=-5), "interim"), (t(24), "final")])
    assert "interim" in core.resolve(con, cid, "worked", "12h reading")
    assert "not due until" in core.resolve(con, cid, "worked", "same reading, sent twice")
    assert core.get(con, cid)["status"] == "active"
    assert "closed" in core.resolve(con, cid, "partial", "forced", checkpoint=2)


def test_lease_is_exclusive(con):
    cid = add(con)
    assert core.start(con, cid, "session")[0] == "OK"
    status, detail = core.start(con, cid, "open")
    assert status == "TAKEN" and "session" in detail


def test_lease_past_deadline_held_while_agent_alive(con):
    cid = add(con)
    core.start(con, cid, "session", pid=os.getpid(), deadline_hours=0)  # deadline passed, holder alive
    assert core.start(con, cid, "open")[0] == "TAKEN"


def test_lease_past_deadline_replaced_when_agent_dead(con):
    cid = add(con)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    core.start(con, cid, "session", pid=dead.pid, deadline_hours=0)
    assert core.start(con, cid, "open")[0] == "OK"


def _start_in_process(home, cid, q):
    os.environ["FOLLOWTHROUGH_HOME"] = home
    q.put(core.start(db.connect(), cid, "runner")[0])


def test_lease_across_processes(con, tmp_path):
    cid = add(con)
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_start_in_process, args=(str(tmp_path / "home"), cid, q)) for _ in range(4)]
    [p.start() for p in procs]
    [p.join(30) for p in procs]
    results = sorted(q.get() for _ in procs)
    assert results.count("OK") == 1 and results.count("TAKEN") == 3


def test_not_settled_retries_then_escalates(con, cfg):
    cid = add(con, dues=[(t(m=-1), "final")])
    for _ in range(core.MAX_TRIES - 1):
        assert "rescheduled" in core.resolve(con, cid, "not_settled", "billing lag", checkpoint=1)
    assert "needs you" in core.resolve(con, cid, "not_settled", "still lagging", checkpoint=1)
    s = Sender()
    tick(con, cfg, send=s)
    assert "exhausted" in s.events(cid)


def la_at(days, hour):
    """A fixed local (Los Angeles) wall-clock time `days` from today, as UTC."""
    from zoneinfo import ZoneInfo
    d = (db.now().astimezone(ZoneInfo(LA)) + dt.timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return d.astimezone(UTC)


def test_overdue_claim_stays_open_and_is_notified_in_waking_hours_then_weekly(con, cfg, capsys):
    cfg["tz"] = LA   # waking hours are read in the configured zone, not the machine's
    cid = add(con, dues=[(t(-3), "final")], ends_at=t(-1))
    s = Sender()
    tick(con, cfg, now=la_at(1, 3), send=s)                      # 03:00: due is sent, overdue waits for the morning
    assert core.get(con, cid)["status"] == "active" and "overdue" not in s.events(cid)
    failing = Sender(fail_first=10)
    tick(con, cfg, now=la_at(1, 9), send=failing)                # 09:00: queued, the first send fails
    tick(con, cfg, now=la_at(1, 9) + dt.timedelta(minutes=5), send=s)
    tick(con, cfg, now=la_at(1, 10), send=s)
    tick(con, cfg, now=la_at(2, 10), send=s)                     # a day later: no daily reminder once overdue
    assert s.events(cid) == ["due", "overdue"]
    tick(con, cfg, now=la_at(8, 10), send=s)                     # a week later: once more
    assert s.events(cid) == ["due", "overdue", "overdue"]
    c = core.get(con, cid)
    assert "followthrough abandon " + cid in notify.telegram_text(c, None, "overdue", "", LA)
    cli.main(["status"])
    assert "overdue" in capsys.readouterr().out
    cli.main(["show", cid])
    assert "overdue: since" in capsys.readouterr().out
    assert core.start(con, cid, "session")[0] == "OK"
    assert core.resolve(con, cid, "worked", "measured late") == "closed: worked"


def test_overdue_again_after_a_retry_moves_ends_at(con, cfg):
    cfg["tz"] = LA
    cid = add(con, dues=[(t(-3), "final")], ends_at=t(-1))
    s = Sender()
    tick(con, cfg, now=la_at(1, 10), send=s)
    core.resolve(con, cid, "not_settled", "billing lag", checkpoint=1, retry_at=la_at(1, 11))   # ends_at: +48h
    tick(con, cfg, now=la_at(1, 12), send=s)
    tick(con, cfg, now=la_at(3, 12), send=s)
    assert s.events(cid) == ["overdue", "due", "due", "overdue"]   # overdue (step 1) runs before due (step 3)


def test_queued_overdue_is_dropped_once_no_longer_overdue(con, cfg):
    cfg["tz"] = LA
    moved = add(con, dues=[(t(-3), "final")], ends_at=t(-1), title="retry moves ends_at")
    closed = add(con, dues=[(t(-3), "final")], ends_at=t(-1), title="resolved before the send")
    tick(con, cfg, now=la_at(1, 3))                                    # the due notices go out at night
    tick(con, cfg, now=la_at(1, 10), send=Sender(fail_first=10))       # overdue queued, the send fails
    core.resolve(con, moved, "not_settled", "lag", checkpoint=1, retry_at=la_at(2, 10))
    core.resolve(con, closed, "worked", "measured")
    s = Sender()
    tick(con, cfg, now=la_at(1, 11), send=s)
    assert s.events() == []


def test_overdue_retry_after_21h_waits_for_the_morning(con, cfg):
    cfg["tz"] = LA
    cid = add(con, dues=[(t(-3), "final")], ends_at=t(-1))
    tick(con, cfg, now=la_at(1, 3))
    tick(con, cfg, now=la_at(1, 20) + dt.timedelta(minutes=58), send=Sender(fail_first=10))   # queued, send fails
    night = Sender()
    tick(con, cfg, now=la_at(1, 22), send=night)
    assert night.events(cid) == []
    s = Sender()
    tick(con, cfg, now=la_at(2, 8), send=s)
    assert s.events(cid) == ["overdue"]


def test_overdue_notices_do_not_pile_up_while_offline(con, cfg):
    cfg["tz"] = LA
    cid = add(con, dues=[(t(-3), "final")], ends_at=t(-1))
    tick(con, cfg, now=la_at(1, 3))
    tick(con, cfg, now=la_at(1, 10), send=Sender(fail_first=10))      # queued, send fails; then 8 days offline
    s = Sender()
    tick(con, cfg, now=la_at(9, 10), send=s)                          # a new weekly notice replaces the old one
    assert s.events(cid) == ["overdue"]


def test_a_reminder_retried_after_ends_at_gives_way_to_overdue(con, cfg):
    cfg["tz"] = LA
    cid = add(con, dues=[(t(-3), "final")], ends_at=la_at(2, 12))
    tick(con, cfg, now=la_at(1, 3))                                    # due
    tick(con, cfg, now=la_at(2, 10), send=Sender(fail_first=10))      # reminder queued, send fails
    s = Sender()
    tick(con, cfg, now=la_at(2, 13), send=s)
    assert s.events(cid) == ["overdue"]


def test_a_persons_abandon_reason_starting_with_expired_is_not_reopened(con):
    cid = add(con, dues=[(t(-3), "final")])
    core.close(con, cid, "abandoned", "expired offer, dropping it")
    status, detail = core.start(con, cid, "session")
    assert status == "CLOSED" and "resolve" not in detail
    assert "note" in core.resolve(con, cid, "worked", "late")
    assert core.get(con, cid)["status"] == "abandoned"


def test_legacy_expired_claim_takes_a_late_verdict(con):
    cid = add(con, dues=[(t(-3), "final")])
    con.execute("UPDATE claims SET status='abandoned', verdict_summary='expired at 2026-09-24T10:17:00Z without a"
                " verdict' WHERE id=?", (cid,))
    status, detail = core.start(con, cid, "session")
    assert status == "CLOSED" and f"followthrough resolve {cid}" in detail
    assert "supersedes" in core.resolve(con, cid, "worked", "found it later")


def test_first_verdict_wins(con):
    cid = add(con)
    core.resolve(con, cid, "worked", "first", final=True)
    assert "note" in core.resolve(con, cid, "failed", "second")
    assert core.get(con, cid)["status"] == "worked"


def test_due_once_with_grace(con, cfg):
    fresh = add(con, dues=[(t(m=-5), "final")], title="no source session")
    graced = add(con, dues=[(t(m=-5), "final")], title="live source session", source_session_id="abc")
    s = Sender()
    tick(con, cfg, send=s)
    tick(con, cfg, send=s)
    assert s.events(fresh) == ["due"] and s.events(graced) == []


def test_lease_taken_before_due_means_no_due_notification(con, cfg):
    cid = add(con, dues=[(t(m=-5), "final")])
    core.start(con, cid, "session", pid=os.getpid())
    s = Sender()
    tick(con, cfg, send=s)
    assert s.events(cid) == []


def test_no_claim_closes_by_itself(con, cfg):
    cfg["tz"] = LA
    ids = [add(con, dues=[(t(-5 + i), "final")], ends_at=t(-1)) for i in range(3)]
    core.start(con, ids[0], "session")
    core.resolve(con, ids[1], "not_settled", "lag", checkpoint=1)
    for day in range(1, 20):
        tick(con, cfg, now=la_at(day, 10))
    assert all(core.get(con, i)["status"] == "active" for i in ids)


def test_ends_at_defaults_by_kind(con):
    ask = core.get(con, add(con, kind="ask", dues=[(t(1), "final")]))
    ver = core.get(con, add(con, kind="verify", dues=[(t(1), "final")]))
    assert db.parse(ask["ends_at"]) - db.parse(ver["ends_at"]) > dt.timedelta(days=4)


# ---------------------------------------------------------------- transcript scan

class Transcript:
    def __init__(self, root, sid, cwd="/repo"):
        self.dir = root / "-proj"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path, self.sid, self.cwd, self.n = self.dir / f"{sid}.jsonl", sid, cwd, 0

    def _w(self, rec):
        with open(self.path, "a") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def create(self, at, cron, prompt, job="job1", recurring=False, ok=True):
        self.n += 1
        tid = f"toolu_{self.n}"
        self._w({"type": "assistant", "timestamp": at, "cwd": self.cwd, "message": {"content": [
            {"type": "tool_use", "id": tid, "name": "CronCreate", "input": {"cron": cron, "prompt": prompt, "recurring": recurring}}]}})
        self._w({"type": "user", "timestamp": at, "toolUseResult": {"id": job, "humanSchedule": cron} if ok else "Error",
                 "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": not ok,
                                          "content": f"Scheduled one-shot task {job}"}]}})
        return tid

    def delete(self, at, job, ok=True):
        self.n += 1
        tid = f"toolu_del{self.n}"
        self._w({"type": "assistant", "timestamp": at, "message": {"content": [
            {"type": "tool_use", "id": tid, "name": "CronDelete", "input": {"id": job}}]}})
        self._w({"type": "user", "timestamp": at, "toolUseResult": {"id": job} if ok else "Error",
                 "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": not ok, "content": "ok"}]}})

    def fire(self, at, job, prompt):
        self._w({"type": "user", "timestamp": at, "scheduledTaskId": job, "message": {"content": prompt}})

    def say(self, at, text):
        self._w({"type": "user", "timestamp": at, "message": {"content": text}})


NOW = dt.datetime(2026, 9, 23, 21, 0, tzinfo=UTC)  # 14:00 PDT


def scan(con, cfg, root, now=NOW):
    return transcripts.scan(con, cfg, now=now, root=str(root))


def claims(con):
    return con.execute("SELECT * FROM claims ORDER BY created_at, id").fetchall()


def test_scan_captures_successful_create_only(con, cfg, tmp_path):
    tr = Transcript(tmp_path / "p", "s1")
    tr.create("2026-09-23T20:59:00Z", "53 18 23 9 *", "24h check of the deploy", job="j-ok")
    tr.create("2026-09-23T20:59:00Z", "0 19 23 9 *", "this one failed to schedule", job="j-bad", ok=False)
    tr.create("2026-09-23T20:59:00Z", "10 14 23 9 *", "poll the build (in-task wait)", job="j-short")
    scan(con, cfg, tmp_path / "p")
    assert [r["source_cron_id"] for r in claims(con)] == ["j-ok"]
    scan(con, cfg, tmp_path / "p")
    assert len(claims(con)) == 1


def test_scan_survives_a_bad_file(con, cfg, tmp_path):
    root = tmp_path / "p"
    Transcript(root, "s1").create("2026-09-23T20:59:00Z", "53 18 23 9 *", "24h check", job="j1")
    (root / "-proj" / "bad.jsonl").write_bytes(
        b'{"type":"assistant","timestamp":"not-a-time","message":{"content":[{"type":"tool_use","id":"x",'
        b'"name":"CronCreate","input":{"cron":"garbage","prompt":"p"}}]}}\n'
        b'{"type":"user","timestamp":"nope","toolUseResult":{"id":"jx","humanSchedule":"garbage"},'
        b'"message":{"content":[{"type":"tool_result","tool_use_id":"x"}]}}\n\xff\xfe\n')
    log = scan(con, cfg, root)
    assert len(claims(con)) == 1, log


def test_delete_cancels_and_is_never_resurrected(con, cfg, tmp_path):
    root = tmp_path / "p"
    tr = Transcript(root, "s1")
    tr.create("2026-09-23T20:59:00Z", "53 18 23 9 *", "24h check", job="j1")
    scan(con, cfg, root)
    tr.delete("2026-09-23T21:01:00Z", "j1")
    scan(con, cfg, root)
    assert claims(con)[0]["status"] == "cancelled"
    con.execute("DELETE FROM scan_files")  # full rescan
    scan(con, cfg, root)
    assert len(claims(con)) == 1 and claims(con)[0]["status"] == "cancelled"


def test_manual_cancel_is_not_resurrected_by_rescan(con, cfg, tmp_path):
    root = tmp_path / "p"
    Transcript(root, "s1").create("2026-09-23T20:59:00Z", "53 18 23 9 *", "24h check", job="j1")
    scan(con, cfg, root)
    core.close(con, claims(con)[0]["id"], "cancelled", "not needed")
    con.execute("DELETE FROM scan_files")
    scan(con, cfg, root)
    assert len(claims(con)) == 1


def test_first_seen_after_due_is_still_imported(con, cfg, tmp_path):
    root = tmp_path / "p"
    tr = Transcript(root, "s1")
    tr.create("2026-09-23T10:00:00Z", "0 9 23 9 *", "recent, overdue by 5h", job="j1")    # due 16:00Z
    tr.create("2026-09-19T10:00:00Z", "0 9 19 9 *", "old, overdue by 4 days", job="j2")
    scan(con, cfg, root)
    assert [r["source_cron_id"] for r in claims(con)] == ["j1"]


def test_same_prompt_in_two_sessions_is_two_claims(con, cfg, tmp_path):
    root = tmp_path / "p"
    Transcript(root, "s1").create("2026-09-23T20:59:00Z", "53 18 23 9 *", "same words", job="a")
    Transcript(root, "s2").create("2026-09-23T20:59:00Z", "53 18 23 9 *", "same words", job="b")
    scan(con, cfg, root)
    assert len(claims(con)) == 2


def test_recreate_in_same_session_reschedules(con, cfg, tmp_path):
    root = tmp_path / "p"
    tr = Transcript(root, "s1")
    tr.create("2026-09-23T20:59:00Z", "53 18 23 9 *", "step 2 reminder", job="a")
    tr.create("2026-09-23T21:30:00Z", "0 22 23 9 *", "step 2 reminder", job="b")
    scan(con, cfg, root)
    rows = claims(con)
    assert len(rows) == 1 and rows[0]["source_cron_id"] == "b"
    assert core.current_checkpoint(con, rows[0]["id"])["due_at"] == "2026-09-24T05:00:00Z"


def test_daily_recurring_is_a_watch_and_hourly_is_ignored(con, cfg, tmp_path):
    root = tmp_path / "p"
    tr = Transcript(root, "s1")
    tr.create("2026-09-23T20:59:00Z", "7 8 * * *", "daily check of the ad test", job="d", recurring=True)
    tr.create("2026-09-23T20:59:00Z", "13 * * * *", "hourly progress", job="h", recurring=True)
    scan(con, cfg, root)
    rows = claims(con)
    assert [r["kind"] for r in rows] == ["watch"]
    cps = core.checkpoints(con, rows[0]["id"])
    assert len(cps) == 7 and cps[-1]["role"] == "final" and cps[0]["due_at"] == "2026-09-24T15:07:00Z"


def test_watch_gets_only_future_checkpoints_and_fires_match_per_checkpoint(con, cfg, tmp_path):
    root = tmp_path / "p"
    tr = Transcript(root, "s1")
    tr.create("2026-09-20T03:29:00Z", "7 8 * * *", "daily check of the ad test", job="d", recurring=True)  # set Sep 19 PT
    tr.fire("2026-09-23T15:07:10Z", "d", "daily check of the ad test")   # today's fire, before NOW
    scan(con, cfg, root)                                                  # NOW = Sep 23 21:00Z
    (c,) = claims(con)
    dues = [cp["due_at"] for cp in core.checkpoints(con, c["id"])]
    assert dues == ["2026-09-24T15:07:00Z", "2026-09-25T15:07:00Z", "2026-09-26T15:07:00Z"]
    tr.fire("2026-09-24T15:07:05Z", "d", "daily check of the ad test")
    scan(con, cfg, root, now=dt.datetime(2026, 9, 24, 15, 20, tzinfo=UTC))
    s = Sender()
    runner.tick(con, cfg, "ft", now=dt.datetime(2026, 9, 24, 15, 40, tzinfo=UTC), send=s, scan=False)
    assert s.events(c["id"]) == []                                        # series reading ran in session: quiet
    assert core.checkpoints(con, c["id"])[0]["state"] == "needs_human"   # ...but it stays open for its verdict
    s2 = Sender()
    runner.tick(con, cfg, "ft", now=dt.datetime(2026, 9, 25, 15, 50, tzinfo=UTC), send=s2, scan=False)
    assert s2.events(c["id"]) == ["due"]                                  # no fire by 15:47 on Sep 25: it did not run
    assert core.checkpoints(con, c["id"])[0]["state"] == "expired"       # Sep 24 never got a verdict: superseded


def test_recurring_job_recreated_in_same_session_is_one_watch(con, cfg, tmp_path):
    root = tmp_path / "p"
    tr = Transcript(root, "s1")
    tr.create("2026-09-20T03:29:00Z", "7 8 * * *", "daily check of the ad test", job="d1", recurring=True)
    tr.create("2026-09-23T03:27:00Z", "7 8 * * *", "daily check of the ad test", job="d2", recurring=True)
    tr.create("2026-09-23T03:28:00Z", "7 8 * * *", "an unrelated daily job at the same time", job="u", recurring=True)
    scan(con, cfg, root)
    by_job = {c["source_cron_id"]: c for c in claims(con)}
    assert set(by_job) == {"d2", "u"}                                     # same text merges; different text never does
    assert len([cp for cp in core.checkpoints(con, by_job["d2"]["id"]) if cp["state"] == "pending"]) == 6
    assert by_job["u"]["runbook"] == "an unrelated daily job at the same time"


def test_fired_is_structural_not_textual(con, cfg, tmp_path):
    root = tmp_path / "p"
    prompt = "Reminder: check the latency after the fix"
    tr = Transcript(root, "s1")
    tr.create("2026-09-23T10:00:00Z", "0 9 23 9 *", prompt, job="j1")
    tr.say("2026-09-23T16:30:00Z", f"quoting it: {prompt}")          # a human quoting it is not a fire
    tr2 = Transcript(root, "s2")
    tr2.create("2026-09-23T10:00:00Z", "0 9 23 9 *", prompt + " (b)", job="j2")
    tr2.fire("2026-09-23T16:00:05Z", "j2", prompt + " (b)")
    scan(con, cfg, root, now=dt.datetime(2026, 9, 23, 16, 50, tzinfo=UTC))
    s = Sender()
    runner.tick(con, cfg, "ft", now=dt.datetime(2026, 9, 23, 17, 0, tzinfo=UTC), send=s, scan=False)
    by_session = {r["source_session_id"]: r["id"] for r in claims(con)}
    assert s.events(by_session["s1"]) == ["due"]
    assert s.events(by_session["s2"]) == ["fired"]


# ---------------------------------------------------------------- time

def test_cron_fire_time_local_wall_clock():
    set_at = dt.datetime(2026, 9, 23, 9, 50, tzinfo=UTC)
    assert timeparse.cron_fire_time("53 14 23 9 *", set_at, LA) == dt.datetime(2026, 9, 23, 21, 53, tzinfo=UTC)


def test_cron_fire_time_fall_back_picks_the_future_fold():
    set_at = dt.datetime(2026, 11, 1, 9, 10, tzinfo=UTC)        # 01:10 PST, after the first 01:30 PDT
    assert timeparse.cron_fire_time("30 1 1 11 *", set_at, LA) == dt.datetime(2026, 11, 1, 9, 30, tzinfo=UTC)


def test_cron_fire_time_spring_forward_gap_shifts_forward():
    set_at = dt.datetime(2026, 3, 7, 12, 0, tzinfo=UTC)
    assert timeparse.cron_fire_time("30 2 8 3 *", set_at, LA) == dt.datetime(2026, 3, 8, 10, 30, tzinfo=UTC)  # 03:30 PDT


def test_past_pinned_date_is_overdue_not_next_year():
    set_at = dt.datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
    assert timeparse.cron_fire_time("0 9 20 9 *", set_at, LA).year == 2026


def test_wildcard_one_shot_and_weeks():
    set_at = dt.datetime(2026, 9, 23, 20, 0, tzinfo=UTC)          # 13:00 PDT
    assert timeparse.cron_fire_time("0 8 * * *", set_at, LA) == dt.datetime(2026, 9, 24, 15, 0, tzinfo=UTC)
    assert timeparse.parse_when("+2w", LA, set_at) == set_at + dt.timedelta(weeks=2)


def test_parse_when_local():
    now = dt.datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
    assert timeparse.parse_when("2026-10-01 08:00", LA, now) == dt.datetime(2026, 10, 1, 15, 0, tzinfo=UTC)


# ---------------------------------------------------------------- notifications and surfaces

class _TagCheck(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag


def test_telegram_text_fits_and_markup_is_whole(con):
    cid = add(con, runbook="<b>" * 3000 + "&" * 2000, expectation="x" * 5000)
    c = core.get(con, cid)
    text = notify.telegram_text(c, core.current_checkpoint(con, cid), "due", "", LA)
    assert len(text) <= notify.TELEGRAM_LIMIT
    p = _TagCheck()
    p.feed(text)
    assert p.stack == []


def test_brief_status_never_shows_other_repos(con, tmp_path, capsys):
    here, there = tmp_path / "here", tmp_path / "there"
    here.mkdir()
    there.mkdir()
    add(con, dues=[(t(m=-5), "final")], repo=str(there), title="SECRET-TITLE-ELSEWHERE")
    cli.main(["status", "--brief", "--repo", str(here)])
    assert "SECRET-TITLE-ELSEWHERE" not in capsys.readouterr().out


def test_guess_kind():
    assert core.guess_kind("DEPLOY REMINDER (billing-worker)") == "action"
    assert core.guess_kind("Reminder: today is the pricing decision day") == "ask"
    assert core.guess_kind("24h check of api PR #8 latency") == "verify"


def test_user_names_make_ask_and_leave_the_id(con, tmp_path):
    assert core.guess_kind("Reminder for Sam: pick the plan") == "verify"
    assert core.guess_kind("Reminder for Sam: pick the plan", names=["Sam"]) == "ask"
    with open(config.path("config.toml"), "w") as fh:
        fh.write('user_names = ["Sam"]\n')
    assert core.guess_kind("ask sam which plan") == "ask"
    assert "sam" not in core.new_id("Reminder for Sam: pricing plan", t())


# ---------------------------------------------------------------- hooks

import io  # noqa: E402

from followthrough import hooks  # noqa: E402


def run_hook(event, payload, capsys):
    hooks.run(event, "/abs/followthrough", stdin=io.StringIO(json.dumps(payload)))
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


def cron_payload(cron, prompt, recurring=False, tool_use_id="toolu_A", session="S1", cwd="/tmp"):
    return {"hook_event_name": "PreToolUse", "tool_name": "CronCreate", "session_id": session, "cwd": cwd,
            "tool_use_id": tool_use_id, "tool_input": {"cron": cron, "prompt": prompt, "recurring": recurring}}


def _cron_in(hours):
    from zoneinfo import ZoneInfo
    t = (db.now() + dt.timedelta(hours=hours)).astimezone(ZoneInfo(LA))
    return f"{t.minute} {t.hour} {t.day} {t.month} *"


def test_pre_cron_captures_and_rewrites_the_full_input(con, capsys):
    out = run_hook("pre-cron", cron_payload(_cron_in(3), "24h check of the fix"), capsys)
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    ti = hso["updatedInput"]
    assert set(ti) == {"cron", "prompt", "recurring"} and ti["cron"] == _cron_in(3) and ti["recurring"] is False
    (c,) = con.execute("SELECT * FROM claims").fetchall()
    assert ti["prompt"].startswith(f"[followthrough claim {c['id']}]") and ti["prompt"].endswith("24h check of the fix")
    assert "/abs/followthrough start " + c["id"] in ti["prompt"]
    # the job id arrives in PostToolUse; the scanner later sees the same tool_use id and does not duplicate
    run_hook("post-cron", {"session_id": "S1", "tool_use_id": "toolu_A", "cwd": "/tmp", "tool_response": {"id": "job9"}}, capsys)
    assert core.get(con, c["id"])["source_cron_id"] == "job9"
    cid, what = core.capture(con, session_id="S1", source_event="toolu_A", cron_id="job9", prompt="24h check of the fix",
                             dues=[(t(3), "final")], cwd="/tmp", tz=LA)
    assert (cid, what) == (c["id"], "exists")


def test_pre_cron_ignores_short_waits_hourly_jobs_and_its_own_prefix(con, capsys):
    assert run_hook("pre-cron", cron_payload(_cron_in(0.2), "poll again"), capsys) is None
    assert run_hook("pre-cron", cron_payload("13 * * * *", "hourly progress", recurring=True), capsys) is None
    assert run_hook("pre-cron", cron_payload(_cron_in(3), "[followthrough claim x] already tracked"), capsys) is None
    assert con.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0


def test_pre_cron_fails_open(con, capsys, monkeypatch):
    monkeypatch.setattr(hooks.db, "connect", lambda: (_ for _ in ()).throw(OSError("disk gone")))
    assert run_hook("pre-cron", cron_payload(_cron_in(3), "24h check"), capsys) is None   # no output: tool runs unchanged


def test_pre_cron_respects_ignore_paths_and_checker_role(con, capsys, monkeypatch, tmp_path):
    base = config.load()
    monkeypatch.setattr(hooks.config, "load", lambda: {**base, "ignore_paths": [str(tmp_path / "bot")]})
    (tmp_path / "bot").mkdir()
    assert run_hook("pre-cron", cron_payload(_cron_in(3), "x", cwd=str(tmp_path / "bot")), capsys) is None
    assert run_hook("pre-cron", cron_payload(_cron_in(3), "control", tool_use_id="toolu_C"), capsys) is not None
    monkeypatch.setenv("FOLLOWTHROUGH_ROLE", "checker")
    assert run_hook("pre-cron", cron_payload(_cron_in(3), "y", tool_use_id="toolu_D"), capsys) is None
    assert con.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1   # only the control


def test_cron_failed_cancels(con, capsys):
    run_hook("pre-cron", cron_payload(_cron_in(3), "24h check"), capsys)
    run_hook("cron-failed", {"session_id": "S1", "tool_use_id": "toolu_A", "cwd": "/tmp"}, capsys)
    (c,) = con.execute("SELECT * FROM claims").fetchall()
    assert c["status"] == "cancelled"


def test_session_end_removes_grace(con, cfg, capsys):
    cid = add(con, dues=[(t(m=-5), "final")], source_session_id="S9")
    s = Sender()
    tick(con, cfg, send=s)
    assert s.events(cid) == []                         # grace: the live session may still run it
    run_hook("session-end", {"session_id": "S9", "cwd": "/tmp", "reason": "prompt_input_exit"}, capsys)
    tick(con, cfg, send=s)
    assert s.events(cid) == ["due"]                    # session gone: notify now


def test_install_hooks_is_idempotent_and_keeps_other_settings(con, tmp_path, capsys):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"env": {"SECRET": "x"}, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other.sh"}]}]}}))
    for _ in range(2):
        cli.main(["install-hooks", "--settings", str(p)])
    d = json.loads(p.read_text())
    assert d["env"] == {"SECRET": "x"}
    cmds = [h["command"] for g in d["hooks"]["Stop"] for h in g["hooks"]]
    assert cmds.count("other.sh") == 1 and sum("hook stop" in c for c in cmds) == 1
    assert d["hooks"]["PreToolUse"][0]["matcher"] == "CronCreate"
    assert "SECRET" not in capsys.readouterr().out
    cli.main(["install-hooks", "--settings", str(p), "--remove"])
    d = json.loads(p.read_text())
    assert "PreToolUse" not in d["hooks"] and [h["command"] for g in d["hooks"]["Stop"] for h in g["hooks"]] == ["other.sh"]


# ---------------------------------------------------------------- phase 2 review regressions

def test_telegram_open_button_is_opt_in(con):
    cid = add(con)
    c, cp = core.get(con, cid), core.current_checkpoint(con, cid)
    off = copy.deepcopy(config.DEFAULTS)
    off["telegram"]["chat_id"] = "1"
    on = copy.deepcopy(off)
    on["telegram"]["open_button"] = True
    # default: no button nobody handles, and no hint pointing at one
    assert "reply_markup" not in notify.telegram_message(off, "x", cid)
    assert "/ft open" not in notify.telegram_text(c, cp, "due", "", LA)
    assert notify.telegram_message(on, "x", cid)["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"ft:open:{cid}"
    assert f"/ft open {cid}" in notify.telegram_text(c, cp, "due", "", LA, button=True)
    assert "reply_markup" not in notify.telegram_message(on, "x", "ft-test")


def test_secrets_are_redacted_before_storage_and_telegram(con):
    prompt = "Check the API with Authorization: Bearer abcdefghijklmnopqrstuvwx and token=supersecretvalue123 at https://u:hunter22@h.io/x"
    cid, _ = core.capture(con, session_id="S", source_event="t1", cron_id="j", prompt=prompt, dues=[(t(3), "final")],
                          cwd="/tmp", tz=LA)
    c = core.get(con, cid)
    for secret in ("abcdefghijklmnopqrstuvwx", "supersecretvalue123", "hunter22"):
        assert secret not in c["runbook"] and secret not in c["title"]
        assert secret not in notify.telegram_text(c, core.current_checkpoint(con, cid), "due", "", LA)


def test_open_passes_a_pointer_not_the_prompt(con, capsys, tmp_path):
    cid = add(con, runbook="the long runbook text", repo=str(tmp_path))
    core.expect(con, cid, "p95 below 10 s")
    core.note(con, cid, "baseline 20.8 s")
    cli.main(["open", cid, "--print"])
    out = capsys.readouterr().out
    argv_line = out.splitlines()[1]
    assert "the long runbook text" not in argv_line and "Read " in argv_line
    pfile = os.path.join(os.environ["FOLLOWTHROUGH_HOME"], "prompts", f"{cid}.md")
    body = open(pfile).read()
    assert oct(os.stat(pfile).st_mode & 0o777) == "0o600"
    assert "p95 below 10 s" in body and "baseline 20.8 s" in body and "--attempt" in body and "read-only" in body


def test_claude_bin_inside_the_ledger_is_refused(con, monkeypatch):
    evil = os.path.join(os.environ["FOLLOWTHROUGH_HOME"], "data", "claude")
    open(evil, "w").write("#!/bin/sh\n")
    os.chmod(evil, 0o700)
    with pytest.raises(SystemExit):
        cli._claude_bin({**config.load(), "claude_bin": evil})


def test_unconfirmed_capture_is_hidden_then_cancelled(con, cfg, capsys):
    run_hook("pre-cron", cron_payload(_cron_in(3), "24h check"), capsys)
    (c,) = con.execute("SELECT * FROM claims").fetchall()
    assert c["confirmed"] == 0
    cli.main(["status"])
    assert c["id"] not in capsys.readouterr().out
    tick(con, cfg, now=db.now() + dt.timedelta(hours=2))
    assert core.get(con, c["id"])["status"] == "cancelled"


def test_failure_after_capture_leaves_no_tombstone_and_the_scan_recovers(con, cfg, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(hooks.timeparse, "local_str", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    cron = _cron_in(3)
    assert run_hook("pre-cron", cron_payload(cron, "24h check", session="s1", tool_use_id="toolu_1"), capsys) is None
    assert claims(con) == []                       # the reminder runs without the claim line: no claim pretends otherwise
    root = tmp_path / "p"
    Transcript(root, "s1").create(db.iso(db.now()), cron, "24h check", job="j1")   # ...and it was scheduled after all
    scan(con, cfg, root, now=db.now())
    (c,) = claims(con)
    assert c["status"] == "active" and c["confirmed"] == 1 and c["source_cron_id"] == "j1"


def test_denied_cron_delete_keeps_the_claim(con, cfg, tmp_path):
    root = tmp_path / "p"
    tr = Transcript(root, "s1")
    tr.create("2026-09-23T20:59:00Z", "53 18 23 9 *", "24h check", job="j1")
    tr.delete("2026-09-23T21:00:00Z", "j1", ok=False)
    scan(con, cfg, root)
    assert claims(con)[0]["status"] == "active"


def test_delete_then_recreate_at_same_time_is_a_new_claim(con, cfg, tmp_path):
    root = tmp_path / "p"
    tr = Transcript(root, "s1")
    tr.create("2026-09-23T20:59:00Z", "53 18 23 9 *", "24h check", job="j1")
    tr.delete("2026-09-23T21:00:00Z", "j1")
    tr.create("2026-09-23T21:01:00Z", "53 18 23 9 *", "24h check", job="j2")
    scan(con, cfg, root)
    by_job = {r["source_cron_id"]: r["status"] for r in claims(con)}
    assert by_job == {"j1": "cancelled", "j2": "active"}


def test_resolve_is_bound_to_the_attempt(con):
    cid = add(con, dues=[(t(m=-5), "interim"), (t(m=90), "final")])
    ok, aid = core.start(con, cid, "session")
    assert "interim" in core.resolve(con, cid, "worked", "12h", attempt=aid)
    assert "already recorded" in core.resolve(con, cid, "worked", "12h again", attempt=aid)
    assert core.get(con, cid)["status"] == "active"          # the repeat did not close the final checkpoint


def test_start_on_a_series_takes_the_latest_due_reading(con):
    cid = add(con, kind="watch", dues=[(t(-48), "interim"), (t(-24), "interim"), (t(m=-5), "interim"), (t(24), "final")])
    ok, aid = core.start(con, cid, "session")
    seq = con.execute("SELECT cp.seq FROM attempts a JOIN checkpoints cp ON cp.id=a.checkpoint_id WHERE a.id=?", (aid,)).fetchone()[0]
    assert seq == 3
    core.resolve(con, cid, "worked", "today", attempt=aid)
    states = [cp["state"] for cp in core.checkpoints(con, cid)]
    assert states == ["expired", "expired", "done", "pending"]


def test_live_session_keeps_ownership_until_max_wait(con, cfg, capsys):
    cid = add(con, dues=[(t(m=-40), "final")], source_session_id="S7", source_cron_id="job7")
    run_hook("stop", {"session_id": "S7", "cwd": "/tmp", "session_crons": [{"id": "job7", "schedule": "x", "recurring": False}]}, capsys)
    s = Sender()
    tick(con, cfg, send=s)
    assert s.events(cid) == []                                # past grace, but the live session still has the job
    tick(con, cfg, now=db.now() + dt.timedelta(hours=7), send=s)
    assert s.events(cid) == ["due"]


def test_stop_hook_clears_ended_at_on_resume(con, capsys):
    add(con, source_session_id="S8")
    run_hook("session-end", {"session_id": "S8", "cwd": "/tmp"}, capsys)
    run_hook("stop", {"session_id": "S8", "cwd": "/tmp", "session_crons": []}, capsys)
    assert con.execute("SELECT ended_at FROM sessions WHERE session_id='S8'").fetchone()[0] == ""


def test_obsolete_notification_is_dropped(con, cfg):
    cid = add(con, dues=[(t(m=-5), "final")])
    runner.transitions(con, cfg, db.now())                   # queues "due"
    core.resolve(con, cid, "worked", "done before delivery")
    s = Sender()
    runner.deliver(con, cfg, "ft", db.now(), send=s)
    assert s.sent == []


def test_a_daily_fire_does_not_close_the_previous_day(con, cfg, tmp_path):
    root = tmp_path / "p"
    tr = Transcript(root, "s1")
    tr.create("2026-09-23T03:29:00Z", "7 8 * * *", "daily check", job="d", recurring=True)
    tr.fire("2026-09-25T15:07:03Z", "d", "daily check")      # Sep 24 missed (asleep), Sep 25 fired
    scan(con, cfg, root, now=dt.datetime(2026, 9, 23, 21, 0, tzinfo=UTC))
    (c,) = claims(con)
    cps = core.checkpoints(con, c["id"])
    assert transcripts.fired(con, c, cps[0]) is None and transcripts.fired(con, c, cps[1]) is not None


def test_scan_retries_a_record_after_a_busy_database(con, cfg, tmp_path, monkeypatch):
    import sqlite3
    root = tmp_path / "p"
    Transcript(root, "s1").create("2026-09-23T20:59:00Z", "53 18 23 9 *", "24h check", job="j1")
    real, calls = transcripts._process_record, {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:                                   # the result record, the first time
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)
    monkeypatch.setattr(transcripts, "_process_record", flaky)
    scan(con, cfg, root)
    assert claims(con) == []
    scan(con, cfg, root)
    assert len(claims(con)) == 1


def test_hook_commands_are_guarded(con, tmp_path):
    p = tmp_path / "s.json"
    cli.main(["install-hooks", "--settings", str(p), "--session-start"])
    d = json.loads(p.read_text())
    cmd = d["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert cmd.startswith("[ -x ") and cmd.endswith("; exit 0") and "CLAUDE_CODE_REMOTE" in cmd
    assert "SessionStart" in d["hooks"]
    r = subprocess.run(["sh", "-c", cmd.replace(cli._bin(), "/nonexistent/followthrough")], capture_output=True)
    assert r.returncode == 0 and r.stderr == b""


def test_legacy_ledger_location_is_migrated(tmp_path, monkeypatch):
    import sqlite3
    home = tmp_path / "old"
    home.mkdir()
    monkeypatch.setenv("FOLLOWTHROUGH_HOME", str(home))
    c = sqlite3.connect(home / "followthrough.db")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE marker(x)")
    c.execute("INSERT INTO marker VALUES (42)")
    c.commit()
    c.close()
    con = db.connect()
    assert not (home / "followthrough.db").exists() and (home / "data" / "followthrough.db").exists()
    assert con.execute("SELECT x FROM marker").fetchone()[0] == 42


def test_agent_pid_runs_for_real():
    """Exercise the real ps parsing (a live test found a header line breaking it)."""
    pid, start = core.agent_pid()                       # may be None outside an agent; must not raise
    assert pid is None or (isinstance(pid, int) and start)
    assert core.process_start(os.getpid())
    assert core.pid_alive(os.getpid(), core.process_start(os.getpid()))
    assert not core.pid_alive(os.getpid(), "Mon Jan  1 00:00:00 2001")   # a reused pid with another start time


def test_cli_start_runs_for_real(con, capsys):
    cid = add(con)
    assert cli.main(["start", cid]) == 0
    assert capsys.readouterr().out.startswith("OK at-")


# ---------------------------------------------------------------- first day of live use (2026-09-23)

def _assistant(tr, at, text=None, tool=False):
    blocks = ([{"type": "text", "text": text}] if text else []) + ([{"type": "tool_use", "id": f"tu{at}", "name": "Bash", "input": {}}] if tool else [])
    tr._w({"type": "assistant", "timestamp": at, "message": {"content": blocks}})


def _turn_end(tr, at):
    tr._w({"type": "system", "subtype": "turn_duration", "timestamp": at})


def test_recurring_fire_30_min_late_is_not_called_due(con, cfg, tmp_path):
    """Claude Code fires a daily job up to 30 min late (measured: exactly 30:00). The scan now runs before the
    transitions, and a recurring claim waits past that window, so the in-session run is seen, not reported as due."""
    root = tmp_path / "p"
    tr = Transcript(root, "s1")
    tr.create("2026-09-20T03:29:00Z", "7 8 * * *", "daily check of the ad test", job="d", recurring=True)
    scan(con, cfg, root)
    (c,) = claims(con)
    s = Sender()
    runner.tick(con, cfg, "ft", now=dt.datetime(2026, 9, 24, 15, 36, 59, tzinfo=UTC), send=s, scan=True, root=str(root))
    tr.fire("2026-09-24T15:37:00Z", "d", "daily check of the ad test")
    runner.tick(con, cfg, "ft", now=dt.datetime(2026, 9, 24, 15, 37, 1, tzinfo=UTC), send=s, scan=True, root=str(root))
    runner.tick(con, cfg, "ft", now=dt.datetime(2026, 9, 24, 15, 50, tzinfo=UTC), send=s, scan=True, root=str(root))
    assert s.events(c["id"]) == []
    assert core.checkpoints(con, c["id"])[0]["state"] == "needs_human"      # ran in session: waits for its verdict


def test_a_fire_nobody_scanned_yet_is_seen_before_the_due_decision(con, cfg, tmp_path):
    """The Mac slept through the late window: the first tick comes after it, with the fire still unscanned."""
    root = tmp_path / "p"
    tr = Transcript(root, "s1")
    tr.create("2026-09-20T03:29:00Z", "7 8 * * *", "daily check", job="d", recurring=True)
    scan(con, cfg, root)
    (c,) = claims(con)
    tr.fire("2026-09-24T15:37:00Z", "d", "daily check")
    s = Sender()
    runner.tick(con, cfg, "ft", now=dt.datetime(2026, 9, 24, 16, 30, tzinfo=UTC), send=s, scan=True, root=str(root))
    assert s.events(c["id"]) == []


def test_recurring_watch_is_due_after_the_late_window(con, cfg, tmp_path):
    root = tmp_path / "p"
    Transcript(root, "s1").create("2026-09-20T03:29:00Z", "7 8 * * *", "daily check", job="d", recurring=True)
    scan(con, cfg, root)
    (c,) = claims(con)
    s = Sender()
    runner.tick(con, cfg, "ft", now=dt.datetime(2026, 9, 24, 15, 45, tzinfo=UTC), send=s, scan=True, root=str(root))
    assert s.events(c["id"]) == []
    runner.tick(con, cfg, "ft", now=dt.datetime(2026, 9, 24, 15, 48, tzinfo=UTC), send=s, scan=True, root=str(root))
    assert s.events(c["id"]) == ["due"]


def _one_shot_fired(con, cfg, tmp_path, prompt="Reminder: check the latency after the fix"):
    root = tmp_path / "p"
    tr = Transcript(root, "s1")
    tr.create("2026-09-23T10:00:00Z", "0 9 23 9 *", prompt, job="j1")      # due 16:00Z
    tr.fire("2026-09-23T16:00:00.500Z", "j1", prompt)
    return root, tr


def test_fired_notification_carries_the_sessions_reply(con, cfg, tmp_path):
    root, tr = _one_shot_fired(con, cfg, tmp_path)
    _assistant(tr, "2026-09-23T16:00:05Z", "Checking the live revision first.", tool=True)
    _assistant(tr, "2026-09-23T16:02:00Z", "**Check complete.** p95 is 7.9 s vs 20.8 s baseline (served logs, 12 h).")
    _turn_end(tr, "2026-09-23T16:02:00Z")
    tr.say("2026-09-23T16:30:00Z", "thanks, unrelated next question")
    s = Sender()
    runner.tick(con, cfg, "ft", now=dt.datetime(2026, 9, 23, 16, 11, tzinfo=UTC), send=s, scan=True, root=str(root))
    (c,) = claims(con)
    assert s.events(c["id"]) == ["fired"]
    (extra,) = {r["extra"] for r in con.execute("SELECT extra FROM outbox WHERE claim_id=?", (c["id"],))}
    assert "p95 is 7.9 s vs 20.8 s" in extra and "Checking the live revision" not in extra and "**" not in extra
    text = notify.telegram_text(core.get(con, c["id"]), core.current_checkpoint(con, c["id"]), "fired", extra, LA)
    assert "Its last reply" in text and "p95 is 7.9 s" in text
    assert "p95 is 7.9 s vs 20.8 s" in cli.build_prompt(con, core.get(con, c["id"]), cfg)   # a fork need not redo it


def test_fired_waits_while_its_turn_runs(con, cfg, tmp_path):
    root, tr = _one_shot_fired(con, cfg, tmp_path)
    _assistant(tr, "2026-09-23T16:00:05Z", "Pulling a week of logs, this takes a while.", tool=True)
    s = Sender()
    runner.tick(con, cfg, "ft", now=dt.datetime(2026, 9, 23, 16, 30, tzinfo=UTC), send=s, scan=True, root=str(root))
    (c,) = claims(con)
    assert s.events(c["id"]) == []                                           # turn still running: no nag
    runner.tick(con, cfg, "ft", now=dt.datetime(2026, 9, 23, 17, 1, tzinfo=UTC), send=s, scan=True, root=str(root))
    assert s.events(c["id"]) == ["fired"]                                    # an hour on, it is reported anyway


def test_turn_after_ignores_injected_text_and_finds_the_fire_in_a_large_file(con, tmp_path):
    root = tmp_path / "p"
    tr = Transcript(root, "s1")
    tr.fire("2026-09-23T16:00:00Z", "j1", "check it")
    tr._w({"type": "user", "timestamp": "2026-09-23T16:00:30Z",                 # 9 MB after the fire: the first
           "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "x" * (9 << 20)}]}})  # window misses it
    tr._w(None)                                                                  # a "null" line must not crash
    tr._w({"type": "user", "isMeta": True, "timestamp": "2026-09-23T16:00:01Z",
           "message": {"content": [{"type": "text", "text": "Base directory for this skill: ..."}]}})
    _assistant(tr, "2026-09-23T16:01:00Z", "Done: it worked.")
    _turn_end(tr, "2026-09-23T16:01:00Z")
    claim = {"source_session_id": "s1", "source_cron_id": "j1"}
    assert transcripts.turn_after(claim, "2026-09-23T16:00:00Z", root=str(root)) == ("Done: it worked.", True)
    assert transcripts.turn_after(claim, "2026-09-23T17:00:00Z", root=str(root)) == ("", None)


def test_amend_changes_a_closed_verdict_and_keeps_the_old_one(con, capsys):
    cid = add(con)
    core.start(con, cid, "session")
    core.resolve(con, cid, "partial", "key worked; #99 had no traffic")
    assert "amend" in core.resolve(con, cid, "worked", "relabel")            # points the agent at amend
    assert core.get(con, cid)["status"] == "partial"
    assert cli.main(["amend", cid, "--verdict", "worked", "--summary", "key worked; #99 split out",
                     "--reason", "user asked: relabel"]) == 0
    c = core.get(con, cid)
    assert c["status"] == "worked" and c["verdict_summary"] == "key worked; #99 split out"
    (ev,) = [e for e in core.events(con, cid) if e["kind"] == "amended"]
    assert "partial -> worked" in ev["detail"] and "#99 had no traffic" in ev["detail"]


def test_amend_refuses_an_active_claim(con):
    cid = add(con)
    with pytest.raises(core.NotAmended):
        core.amend(con, cid, "worked", "x", "user asked")
    assert cli.main(["amend", cid, "--verdict", "worked", "--summary", "x", "--reason", "user asked"]) == 1
    assert core.get(con, cid)["status"] == "active"
    with pytest.raises(ValueError):
        core.amend(con, cid, "not_settled", "x", "user asked")


def test_add_files_the_claim_under_the_git_repo_never_home(con, tmp_path, monkeypatch, capsys):
    home, repo = tmp_path / "me", tmp_path / "me" / "proj"
    (repo / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repo / "sub")
    cli.main(["add", "latency check", "--at", "+1d"])
    cid = capsys.readouterr().out.split()[0]
    assert core.get(con, cid)["repo"] == os.path.realpath(repo)
    monkeypatch.chdir(home)
    with pytest.raises(SystemExit):
        cli.main(["add", "filed nowhere", "--at", "+1d"])
    assert cli.main(["add", "explicit", "--at", "+1d", "--repo", str(repo)]) == 0


def test_attach_copies_files_the_prompt_then_lists(con, tmp_path, capsys):
    scratch = tmp_path / "scratchpad"
    (scratch / "census").mkdir(parents=True)
    (scratch / "lf.py").write_text("print('fail-closed client')\n")
    (scratch / "census" / "rows.json").write_text("[]")
    cid = add(con, runbook="run scratchpad/lf.py", repo=str(tmp_path))
    assert cli.main(["attach", cid, str(scratch / "lf.py"), str(scratch / "census")]) == 0
    root = os.path.join(os.environ["FOLLOWTHROUGH_HOME"], "data", "attachments", cid)
    assert open(os.path.join(root, "lf.py")).read() == "print('fail-closed client')\n"
    assert oct(os.stat(os.path.join(root, "census", "rows.json")).st_mode & 0o777) == "0o600"
    prompt = cli.build_prompt(con, core.get(con, cid), config.load())
    assert os.path.join(root, "lf.py") in prompt and os.path.join(root, "census", "rows.json") in prompt
    (scratch / ".env").write_text("TOKEN=x")
    with pytest.raises(SystemExit):
        cli.main(["attach", cid, str(scratch / ".env")])


def test_add_warns_when_the_runbook_names_a_scratchpad(con, tmp_path, capsys):
    cli.main(["add", "7-day check", "--at", "+7d", "--repo", str(tmp_path),
              "--runbook", "use /private/tmp/claude-501/-x/abc/scratchpad/lf.py"])
    assert "followthrough attach" in capsys.readouterr().err


def test_open_forks_only_when_the_runbook_points_at_its_conversation(con, capsys, tmp_path):
    (tmp_path / "no-real-transcripts" / "-proj").mkdir(parents=True)
    (tmp_path / "no-real-transcripts" / "-proj" / "S1.jsonl").write_text("{}\n")
    plain = add(con, runbook="worked = the agent behaved as above", repo=str(tmp_path), source_session_id="S1")
    needs = add(con, runbook="Reminder set earlier in this session: check it", repo=str(tmp_path), source_session_id="S1")
    other = add(con, runbook="compare latency with that session from Tuesday", repo=str(tmp_path), source_session_id="S1")
    cli.main(["open", other, "--print"])
    assert "--resume" not in capsys.readouterr().out
    cli.main(["open", plain, "--print"])
    assert "--resume" not in capsys.readouterr().out
    cli.main(["open", needs, "--print"])
    assert "--resume S1 --fork-session" in capsys.readouterr().out


def test_install_hooks_gives_the_sandbox_write_access_to_the_ledger_only(con, tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"sandbox": {"network": {"allowedDomains": ["a.io"]}, "filesystem": {"allowWrite": ["~/.kube"]}}}))
    for _ in range(2):
        cli.main(["install-hooks", "--settings", str(p)])
    d = json.loads(p.read_text())
    assert d["sandbox"]["filesystem"]["allowWrite"] == ["~/.kube", config.data_path()]
    assert d["sandbox"]["network"] == {"allowedDomains": ["a.io"]}
    cli.main(["install-hooks", "--settings", str(p), "--remove"])
    assert json.loads(p.read_text())["sandbox"]["filesystem"]["allowWrite"] == ["~/.kube"]
    q = tmp_path / "fresh.json"
    cli.main(["install-hooks", "--settings", str(q)])
    cli.main(["install-hooks", "--settings", str(q), "--remove"])
    assert "sandbox" not in json.loads(q.read_text())


def test_capture_hook_warns_about_a_scratchpad_path(con, capsys):
    out = run_hook("pre-cron", cron_payload(_cron_in(3), "24h check using scratchpad/lf.py"), capsys)
    assert "followthrough attach" in out["hookSpecificOutput"]["additionalContext"]


def test_redact_catches_json_quoted_secrets():
    text = '{"password":"synthetic_password_12345", "access_token": "abcd1234efgh5678", "n": 1}'
    out = core.redact(text)
    assert "synthetic_password_12345" not in out and "abcd1234efgh5678" not in out and '"n": 1' in out


def test_amend_history_keeps_every_full_summary(con):
    cid = add(con)
    core.start(con, cid, "session")
    core.resolve(con, cid, "partial", "A" * 400)
    core.amend(con, cid, "worked", "B" * 400, "user asked")
    core.amend(con, cid, "failed", "C" * 10, "user asked again")
    history = " ".join(e["detail"] for e in core.events(con, cid) if e["kind"] == "amended")
    assert "A" * 400 in history and "B" * 400 in history and "C" * 10 in history


def test_attach_refuses_symlinks_collisions_and_planted_links(con, tmp_path):
    cid = add(con, repo=str(tmp_path))
    src = tmp_path / "s"
    (src / "a").mkdir(parents=True)
    (src / "b").mkdir()
    (src / "a" / "rows.json").write_text("[1]")
    (src / "b" / "rows.json").write_text("[2]")
    secret = tmp_path / "outside"
    secret.mkdir()
    (secret / ".env").write_text("TOKEN=x")
    os.symlink(secret, src / "a" / "linked")
    with pytest.raises(SystemExit):                       # a symlinked dir would pull in .env unseen
        cli.main(["attach", cid, str(src / "a")])
    os.unlink(src / "a" / "linked")
    with pytest.raises(SystemExit):                       # two files named rows.json would overwrite each other
        cli.main(["attach", cid, str(src / "a" / "rows.json"), str(src / "b" / "rows.json")])
    target = tmp_path / "trusted.toml"
    target.write_text("trusted")
    dest = os.path.join(os.environ["FOLLOWTHROUGH_HOME"], "data", "attachments", cid)
    os.makedirs(dest)
    os.symlink(target, os.path.join(dest, "config.toml"))  # planted by a sandboxed agent
    (src / "config.toml").write_text("attacker")
    with pytest.raises(SystemExit):
        cli.main(["attach", cid, str(src / "config.toml")])
    assert target.read_text() == "trusted"
    os.unlink(os.path.join(dest, "config.toml"))
    with pytest.raises(SystemExit):                       # a fifo or device would block or never end
        cli.main(["attach", cid, "/dev/zero"])
    os.mkfifo(src / "b" / "pipe")
    with pytest.raises(SystemExit):
        cli.main(["attach", cid, str(src / "b")])
    assert cli.main(["attach", cid, str(src / "a" / "rows.json")]) == 0
    assert open(os.path.join(dest, "rows.json")).read() == "[1]"


def test_add_with_a_bad_attachment_creates_no_claim(con, tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["add", "7-day check", "--at", "+7d", "--repo", str(tmp_path), "--attach", str(tmp_path / "missing.py")])
    assert claims(con) == []


def test_a_damaged_transcript_does_not_stop_other_claims(con, cfg, tmp_path):
    root, tr = _one_shot_fired(con, cfg, tmp_path)
    tr._w(None)
    tr._w([1, 2])
    other = add(con, dues=[(dt.datetime(2026, 9, 23, 16, 5, tzinfo=UTC), "final")], title="unrelated")  # after it
    s = Sender()
    runner.tick(con, cfg, "ft", now=dt.datetime(2026, 9, 23, 16, 11, tzinfo=UTC), send=s, scan=True, root=str(root))
    assert s.events(other) == ["due"]


def test_a_bad_claim_in_the_due_loop_does_not_stop_the_next(con, cfg, monkeypatch):
    first = add(con, dues=[(dt.datetime(2026, 9, 23, 16, 0, tzinfo=UTC), "final")], title="first")
    second = add(con, dues=[(dt.datetime(2026, 9, 23, 16, 5, tzinfo=UTC), "final")], title="second")
    real = transcripts.fired
    monkeypatch.setattr(transcripts, "fired", lambda con, claim, cp: (_ for _ in ()).throw(ValueError("bad"))
                        if claim["id"] == first else real(con, claim, cp))
    s = Sender()
    log = runner.tick(con, cfg, "ft", now=dt.datetime(2026, 9, 23, 17, 0, tzinfo=UTC), send=s, scan=False)
    assert s.events(second) == ["due"] and any(first in line and "ERROR" in line for line in log)


def test_a_quiet_series_reading_is_not_counted_as_needs_you(con, capsys):
    series = add(con, kind="watch", dues=[(t(-2), "interim"), (t(22), "interim"), (t(46), "final")], title="daily series")
    fired = add(con, dues=[(t(-2), "final")], title="one-shot that ran in session")
    for cid in (series, fired):   # what the runner writes when a reminder ran in its session without a verdict
        con.execute("UPDATE checkpoints SET state='needs_human', notified_at=?, summary=? WHERE claim_id=? AND seq=1",
                    (db.iso(t(-1)), core.RAN_IN_SESSION + " fired", cid))
    cli.main(["status", "--brief"])
    out = capsys.readouterr().out
    assert "1 need you" in out
    assert "reading ran in session" in out.split(series)[1].splitlines()[0]
    assert "record verdict" in out.split(fired)[1].splitlines()[0]


def test_install_writes_a_private_starter_config_once(con):
    import stat
    import tomllib
    p = cli._write_starter_config()
    assert p and stat.S_IMODE(os.stat(p).st_mode) == 0o600
    cfg = config.load()
    assert cfg["tz"] == LA and cfg["macos"]["enabled"] is True and cfg["telegram"]["enabled"] is False
    assert set(tomllib.load(open(p, "rb"))) == {"tz", "macos", "telegram"}   # everything else stays commented out
    with open(p, "a") as fh:
        fh.write("# mine\n")
    assert cli._write_starter_config() is None and open(p).read().endswith("# mine\n")


def test_a_planted_claim_id_never_reaches_a_command_or_a_path(con, cfg):
    bad = "ft-x; echo INJECTED"
    good = add(con)
    con.execute("UPDATE claims SET id=? WHERE id=?", (bad, good))      # what an agent with write access to data/ could do
    with pytest.raises(core.NotFound):
        core.get(con, bad)
    assert cli.main(["open", "ft-x", "--print"]) != 0
    assert not os.path.exists(config.path("prompts"))                   # no prompt file was written anywhere
    with pytest.raises(core.NotFound):
        notify.open_command("/bin/ft", bad)
    assert notify.open_command("/bin/ft", "ft-0924-ok-1a2b") == "/bin/ft open ft-0924-ok-1a2b"
    db.enqueue(con, bad, None, "due")                                   # not held for waking hours
    s = Sender()
    assert any("invalid claim id" in line for line in runner.deliver(con, cfg, "ft", db.now(), send=s))
    assert s.sent == []


def test_text_is_redacted_on_every_write_path(con, cfg, tmp_path):
    secret = "supersecretvalue123"
    leak = f"token={secret}"
    cid = add(con, title=f"check {leak}", runbook=f"run with {leak}", expectation=f"e {leak}", change_ref=f"c {leak}",
              live_check=f"curl -H 'token: {secret}'", dues=[(t(-1), "final")])
    core.note(con, cid, f"note {leak}")
    other = add(con)
    core.close(con, other, "cancelled", f"reason {leak}")
    blank = add(con, title="captured")
    core.expect(con, blank, f"p95 below 10 s {leak}")
    old = add(con, dues=[(t(-1), "final")])
    con.execute("UPDATE claims SET change_ref=? WHERE id=?", (f"stored before redaction existed: {leak}", old))
    prompt = cli.build_prompt(con, core.get(con, cid), cfg) + cli.build_prompt(con, core.get(con, old), cfg)
    core.resolve(con, cid, "worked", f"measured {leak}", final=True)
    root = tmp_path / "p"
    Transcript(root, "s1")._w({"type": "assistant", "timestamp": db.iso(db.now()), "message": {"content": [
        {"type": "tool_use", "id": "toolu_9", "name": "CronCreate", "input": {"cron": "0 9 * * *", "prompt": leak}}]}})
    scan(con, cfg, root, now=db.now())                                 # no result yet: the call waits in pending_uses
    assert secret not in prompt
    con.execute("UPDATE claims SET change_ref='' WHERE id=?", (old,))   # that row was planted raw on purpose
    stored = [str(tuple(r)) for tbl in ("claims", "events", "pending_uses", "checkpoints")
              for r in con.execute(f"SELECT * FROM {tbl}")]
    assert con.execute("SELECT COUNT(*) FROM pending_uses").fetchone()[0] == 1
    assert not [x for x in stored if secret in x]


def test_install_reports_a_runner_that_did_not_load(con, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    real = subprocess.run

    def fake(args, *a, **k):
        if args and args[0] == "launchctl":
            return subprocess.CompletedProcess(args, 78, "", "Bootstrap failed: 5: Input/output error")
        return real(args, *a, **k)
    monkeypatch.setattr(cli.subprocess, "run", fake)
    assert cli.main(["install", "--settings", str(tmp_path / "settings.json")]) == 1
    assert "WARNING: the runner did not load" in capsys.readouterr().out


def test_brief_status_keeps_one_line_for_other_repos(con, tmp_path, capsys):
    here, there = tmp_path / "here", tmp_path / "there"
    here.mkdir()
    there.mkdir()
    add(con, dues=[(t(3), "final")], repo=str(here), title="local check")
    add(con, dues=[(t(m=-5), "final")], repo=str(there), title="SECRET-TITLE-ELSEWHERE")
    tick(con, config.load())
    cli.main(["status", "--brief", "--repo", str(here)])
    out = capsys.readouterr().out
    assert "local check" in out and "elsewhere: 1 need you" in out and "SECRET-TITLE-ELSEWHERE" not in out



def test_ledger_values_never_become_paths_or_markup(con, tmp_path):
    src = tmp_path / "check.py"
    src.write_text("print(1)\n")
    outside = tmp_path / "outside-ledger"
    with pytest.raises(core.NotFound):
        cli._attach(con, str(outside), [str(src)])                      # what --dedup could hand over from the ledger
    assert not outside.exists()
    (tmp_path / "other-session.jsonl").write_text("{}\n")
    for sid in (str(tmp_path / "other-session"), "../x", "*", ""):
        assert transcripts.find(sid) is None
    cid = add(con)
    con.execute("UPDATE claims SET kind='<b>INJECTED</b>' WHERE id=?", (cid,))
    con.execute("UPDATE checkpoints SET role='<i>x</i>' WHERE claim_id=?", (cid,))
    c = core.get(con, cid)
    text = notify.telegram_text(c, core.current_checkpoint(con, cid), "due", "", LA)
    assert "<b>INJECTED</b>" not in text and "&lt;b&gt;INJECTED" in text and "<i>x</i>" not in text


def test_a_cut_never_splits_a_secret(con, capsys):
    key = "sk-" + "a" * 40
    runbook = "x" * 70 + " " + key + " more"
    assert cli.main(["add", "--at", "+1h", "--runbook", runbook, "--repo", "/tmp"]) == 0
    cid = capsys.readouterr().out.split()[0]
    c = core.get(con, cid)
    assert "sk-a" not in c["id"] and "sk-a" not in c["title"] and key not in c["runbook"]
    old = add(con)
    con.execute("UPDATE claims SET title=? WHERE id=?", ("t " + "y" * 190 + " " + key, old))   # a row stored raw
    assert "sk-a" not in notify.telegram_text(core.get(con, old), None, "due", "", LA)


def test_a_secret_in_a_reminder_still_gives_one_claim(con, cfg, capsys, tmp_path):
    prompt = "24h check; token=supersecretvalue123 for the api"
    cron = _cron_in(3)
    run_hook("pre-cron", cron_payload(cron, prompt, session="s1", tool_use_id="toolu_1"), capsys)
    root = tmp_path / "p"
    tr = Transcript(root, "s1")
    tr.create(db.iso(db.now()), cron, prompt, job="j1")                 # the hook's own call, seen again by the scan
    tr.create(db.iso(db.now()), cron, prompt, job="j2")                 # re-created in the same session
    tr._w({"type": "assistant", "timestamp": db.iso(db.now()), "message": {"content": [{"type": "tool_use",
           "id": "toolu_x", "name": "CronCreate", "input": {"cron": cron, "prompt": "p", "extra": "token=supersecretvalue123"}}]}})
    scan(con, cfg, root, now=db.now())
    assert len([c for c in claims(con) if c["status"] == "active"]) == 1
    pending = [json.loads(r["input"]) for r in con.execute("SELECT input FROM pending_uses")]
    assert all(set(p) <= {"cron", "recurring", "id", "prompt", "_tool"} for p in pending)


# ---------------------------------------------------------------- snooze

def test_snooze_holds_notices_and_sends_them_when_it_ends(con, cfg):
    cfg["tz"] = LA
    cid = add(con, dues=[(t(-3), "final")], ends_at=t(days=5))
    core.snooze(con, cid, la_at(1, 12), source="test")
    s = Sender()
    tick(con, cfg, now=la_at(1, 10), send=s)                     # due while snoozed: queued, held
    assert s.events(cid) == [] and core.current_checkpoint(con, cid)["state"] == "needs_human"
    tick(con, cfg, now=la_at(1, 12), send=s)                     # snooze over: the held notice goes, no extra reminder
    tick(con, cfg, now=la_at(1, 13), send=s)
    assert s.events(cid) == ["due"]
    assert core.get(con, cid)["snoozed_until"] == ""
    assert [e["kind"] for e in core.events(con, cid)].count("snooze_over") == 1


def test_snooze_after_a_notice_sends_one_reminder_in_waking_hours(con, cfg):
    cfg["tz"] = LA
    cid = add(con, dues=[(t(-3), "final")], ends_at=t(days=5))
    s = Sender()
    tick(con, cfg, now=la_at(1, 10), send=s)
    assert s.events(cid) == ["due"]
    core.snooze(con, cid, la_at(1, 22))
    tick(con, cfg, now=la_at(1, 15), send=s)
    tick(con, cfg, now=la_at(1, 22), send=s)                     # ends at night: the reminder waits for 08:00
    assert s.events(cid) == ["due"]
    tick(con, cfg, now=la_at(2, 8), send=s)
    tick(con, cfg, now=la_at(2, 9), send=s)                      # not a second one: notified_at moved at the wake
    assert s.events(cid) == ["due", "reminder"]
    row = con.execute("SELECT extra FROM outbox WHERE claim_id=? AND event='reminder'", (cid,)).fetchone()
    assert row["extra"] == "The snooze is over."
    c, cp = core.get(con, cid), core.current_checkpoint(con, cid)
    assert "The snooze is over." in notify.telegram_text(c, cp, "reminder", row["extra"], LA)


def test_snooze_on_an_overdue_claim_ends_with_one_overdue_notice(con, cfg):
    cfg["tz"] = LA
    cid = add(con, dues=[(t(-3), "final")], ends_at=t(-1))
    s = Sender()
    tick(con, cfg, now=la_at(1, 10), send=s)
    assert s.events(cid) == ["overdue", "due"]
    core.snooze(con, cid, la_at(1, 12))
    tick(con, cfg, now=la_at(1, 12), send=s)
    tick(con, cfg, now=la_at(1, 13), send=s)
    tick(con, cfg, now=la_at(2, 13), send=s)
    tick(con, cfg, now=la_at(8, 11), send=s)                     # a week after the first notice, not after the wake
    assert s.events(cid) == ["overdue", "due", "overdue"]
    tick(con, cfg, now=la_at(8, 13), send=s)                     # the weekly rhythm restarts from the wake
    assert s.events(cid) == ["overdue", "due", "overdue", "overdue"]


def test_a_verdict_ends_the_snooze_and_closed_claims_cannot_be_snoozed(con, cfg):
    series = add(con, dues=[(t(-2), "interim"), (t(days=2), "final")], kind="watch")
    core.snooze(con, series, t(10))
    core.resolve(con, series, "worked", "reading 1", checkpoint=1)
    assert core.get(con, series)["snoozed_until"] == ""            # the next reading notifies as usual
    done = add(con, dues=[(t(-1), "final")])
    core.resolve(con, done, "worked", "p95 8 s")
    with pytest.raises(core.NotSnoozed):
        core.snooze(con, done, t(3))
    with pytest.raises(core.NotSnoozed):
        core.snooze(con, series, t(-1))                             # in the past
    with pytest.raises(core.NotSnoozed):
        core.snooze(con, series, t(days=31))
    with pytest.raises(core.NotSnoozed):
        core.snooze(con, series, None)                              # --off when not snoozed


def test_snooze_cli_and_status(con, cfg, capsys):
    cid = add(con, dues=[(t(-3), "final")])
    con.execute("UPDATE checkpoints SET state='needs_human' WHERE claim_id=?", (cid,))
    assert cli.main(["snooze", cid, "--for", "3h", "--source", "telegram-button"]) == 0
    assert f"snoozed {cid} until" in capsys.readouterr().out
    assert "snoozed" in [e["kind"] for e in core.events(con, cid)]
    assert "telegram-button" in core.events(con, cid)[-1]["detail"]
    cli.main(["status"])
    out = capsys.readouterr().out
    assert out.startswith("0 need you") and "snoozed till" in out
    cli.main(["show", cid])
    assert "snoozed: until" in capsys.readouterr().out
    assert cli.main(["snooze", cid, "--for", "3 hours"]) == 1
    assert "snooze for" in capsys.readouterr().err
    assert cli.main(["snooze", cid, "--off"]) == 0
    assert not core.snoozed(core.get(con, cid), t(0, 1))
    s = Sender()
    tick(con, cfg, now=la_at(1, 10), send=s)                     # the next tick ends it and sends one reminder
    assert core.get(con, cid)["snoozed_until"] == "" and s.events(cid) == ["reminder"]


def test_snooze_until_morning():
    from zoneinfo import ZoneInfo
    z = ZoneInfo(LA)
    at = lambda *a: dt.datetime(*a, tzinfo=z).astimezone(UTC)  # noqa: E731
    assert timeparse.snooze_until("morning", LA, at(2026, 10, 1, 7, 30)) == at(2026, 10, 1, 9)
    assert timeparse.snooze_until("morning", LA, at(2026, 10, 1, 9)) == at(2026, 10, 2, 9)
    assert timeparse.snooze_until("morning", LA, at(2026, 10, 31, 22)) == at(2026, 11, 1, 9)  # across the DST end
    assert at(2026, 11, 1, 9) - at(2026, 10, 31, 22) == dt.timedelta(hours=12)
    assert timeparse.snooze_until("morning", LA, at(2027, 3, 13, 22)) == at(2027, 3, 14, 9)   # across the DST start
    assert at(2027, 3, 14, 9) - at(2027, 3, 13, 22) == dt.timedelta(hours=10)
    assert timeparse.snooze_until("90m", LA, at(2026, 10, 1, 7)) == at(2026, 10, 1, 8, 30)
    for bad in ("", "+3h", "3 h", "tomorrow", "0.5h"):
        with pytest.raises(ValueError):
            timeparse.snooze_until(bad, LA)


def test_telegram_snooze_buttons_are_opt_in(con):
    cid = add(con)
    off = copy.deepcopy(config.DEFAULTS)
    off["telegram"]["chat_id"] = "1"
    assert "reply_markup" not in notify.telegram_message(off, "x", cid, "due")
    on = copy.deepcopy(off)
    on["telegram"]["snooze_buttons"] = ["1h", "morning", "bad value", 3]
    rows = notify.telegram_message(on, "x", cid, "due")["reply_markup"]["inline_keyboard"]
    assert [b["callback_data"] for b in rows[0]] == [f"ft:snooze:{cid}:1h", f"ft:snooze:{cid}:morning"]
    assert rows[0][1]["text"] == "💤 till 9:00"
    on["telegram"]["open_button"] = True
    rows = notify.telegram_message(on, "x", cid, "reminder")["reply_markup"]["inline_keyboard"]
    assert rows[0][0]["callback_data"] == f"ft:open:{cid}" and len(rows[1]) == 2
    assert len(notify.telegram_message(on, "x", cid, "expired")["reply_markup"]["inline_keyboard"]) == 1
    assert "reply_markup" not in notify.telegram_message(on, "x", "ft-test", "test")
    long_id = "ft-" + "a" * 50                                   # callback data over Telegram's 64 bytes: no button
    assert [b["callback_data"] for r in notify.telegram_message(on, "x", long_id, "due")["reply_markup"]["inline_keyboard"]
            for b in r] == [f"ft:open:{long_id}"]
    assert "reply_markup" not in notify.telegram_message(on, "x", "ft-" + "a" * 60, "due")


def test_snooze_column_is_added_to_an_old_ledger(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv("FOLLOWTHROUGH_HOME", str(tmp_path / "h"))
    db.connect().close()
    p = tmp_path / "h" / "data" / "followthrough.db"
    c = sqlite3.connect(p)
    c.execute("ALTER TABLE claims DROP COLUMN snoozed_until")
    c.execute("PRAGMA user_version=3")
    c.commit()
    c.close()
    con = db.connect()
    assert "snoozed_until" in {r[1] for r in con.execute("PRAGMA table_info(claims)")}


def test_a_long_snooze_ends_with_one_notice_per_channel(con, cfg):
    cfg["tz"] = LA
    cid = add(con, dues=[(t(-3), "final")], ends_at=t(days=10))
    s = Sender()
    tick(con, cfg, now=la_at(1, 10), send=s)
    core.snooze(con, cid, la_at(4, 10))
    for d in (2, 3):                                             # no daily reminder piles up while snoozed
        tick(con, cfg, now=la_at(d, 11), send=s)
    assert not con.execute("SELECT 1 FROM outbox WHERE claim_id=? AND event='reminder'", (cid,)).fetchone()
    tick(con, cfg, now=la_at(4, 10), send=s)
    tick(con, cfg, now=la_at(4, 12), send=s)
    assert s.events(cid) == ["due", "reminder"]
    tick(con, cfg, now=la_at(5, 11), send=s)                     # the daily rhythm restarts from the wake
    tick(con, cfg, now=la_at(6, 10), send=s)
    assert s.events(cid) == ["due", "reminder", "reminder"]


def test_snooze_end_is_not_swallowed_by_another_channels_retry(con, cfg):
    cfg["tz"] = LA
    cid = add(con, dues=[(t(-3), "final")], ends_at=t(days=10))
    sent = []

    def send(cfg, channel, claim, cp, event, extra, bin_path):   # Telegram works, macOS keeps failing
        if channel == "macos":
            return False, "boom"
        sent.append(event)
        return True, "ok"
    tick(con, cfg, now=la_at(1, 10), send=send)
    core.snooze(con, cid, la_at(1, 12))
    tick(con, cfg, now=la_at(1, 12), send=send)                  # the macOS retry is held, then superseded
    assert sent == ["due", "due"]
    pending = con.execute("SELECT channel FROM outbox WHERE claim_id=? AND sent_at='' AND gave_up_at=''", (cid,)).fetchall()
    assert [r["channel"] for r in pending] == ["macos"]          # one notice left to retry, not two


def test_a_held_notice_that_became_obsolete_gives_way_to_the_reminder(con, cfg):
    cfg["tz"] = LA
    cid = add(con, dues=[(t(-3), "final")], ends_at=t(days=10))
    core.snooze(con, cid, la_at(1, 12))
    tick(con, cfg, now=la_at(1, 10))                             # due queued and held
    con.execute("UPDATE outbox SET event='expired' WHERE claim_id=?", (cid,))   # a legacy row: never relevant here
    s = Sender()
    tick(con, cfg, now=la_at(1, 12), send=s)
    assert s.events(cid) == ["reminder"]
