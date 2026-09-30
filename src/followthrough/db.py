"""SQLite ledger. All times are stored as UTC ISO strings ending in Z. Files are private (0700 dir, 0600 files)."""
import datetime as dt
import os
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                 -- verify | watch | action | ask
    title TEXT NOT NULL,
    repo TEXT NOT NULL DEFAULT '',
    runbook TEXT NOT NULL DEFAULT '',   -- what to do at the check; the reminder prompt when captured
    expectation TEXT NOT NULL DEFAULT '',  -- written before the data exists; never edited
    change_ref TEXT NOT NULL DEFAULT '',
    live_check TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'notify',
    status TEXT NOT NULL DEFAULT 'active',  -- active | worked | failed | partial | inconclusive | cancelled | abandoned
    verdict_summary TEXT NOT NULL DEFAULT '',
    ends_at TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'cli',     -- cli | skill | cron | import
    harness TEXT NOT NULL DEFAULT '',
    source_session_id TEXT NOT NULL DEFAULT '',
    source_cwd TEXT NOT NULL DEFAULT '',
    source_cron_id TEXT NOT NULL DEFAULT '',   -- the CronCreate job id (matches scheduledTaskId when it fires)
    source_event TEXT NOT NULL DEFAULT '',     -- the CronCreate tool_use id: identity of the capture
    confirmed INTEGER NOT NULL DEFAULT 1,      -- 0 while a hook capture waits for CronCreate to succeed
    snoozed_until TEXT NOT NULL DEFAULT '',    -- its notifications are held until then (`snooze`)
    prompt_hash TEXT NOT NULL DEFAULT '',
    tz TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    closed_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL REFERENCES claims(id),
    seq INTEGER NOT NULL,
    due_at TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'final',    -- interim | final
    state TEXT NOT NULL DEFAULT 'pending', -- pending | running | needs_human | done | expired
    tries INTEGER NOT NULL DEFAULT 0,
    notified_at TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    done_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    checkpoint_id INTEGER NOT NULL REFERENCES checkpoints(id),
    path TEXT NOT NULL,                    -- session | open | runner | manual
    pid INTEGER,                           -- the agent process (claude/codex) that took the lease, when found
    pid_start TEXT NOT NULL DEFAULT '',    -- that process's start time, so a reused pid is not mistaken for it
    started_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    ended_at TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS outbox (          -- notifications are queued in the same transaction as the transition
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL,
    checkpoint_id INTEGER,
    event TEXT NOT NULL,
    extra TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,                   -- macos | telegram
    created_at TEXT NOT NULL,
    next_try_at TEXT NOT NULL,
    tries INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT NOT NULL DEFAULT '',
    gave_up_at TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    cwd TEXT NOT NULL DEFAULT '',
    last_seen TEXT NOT NULL DEFAULT '',
    ended_at TEXT NOT NULL DEFAULT '',
    crons TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS scan_files (      -- incremental transcript scan: byte offset per file
    path TEXT PRIMARY KEY,
    inode INTEGER NOT NULL,
    offset INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_uses (    -- CronCreate calls waiting for their tool result
    tool_use_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    ts TEXT NOT NULL,
    input TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fires (           -- a scheduled prompt delivered into its session (scheduledTaskId)
    job_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    PRIMARY KEY (job_id, session_id, ts)
);
CREATE INDEX IF NOT EXISTS cp_claim ON checkpoints(claim_id, seq);
CREATE INDEX IF NOT EXISTS cp_state ON checkpoints(state, due_at);
CREATE INDEX IF NOT EXISTS claims_hash ON claims(source_session_id, prompt_hash);
CREATE INDEX IF NOT EXISTS events_claim ON events(claim_id, at);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(sent_at, gave_up_at, next_try_at);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_attempt ON attempts(checkpoint_id) WHERE ended_at='';
"""

MIGRATIONS = [  # (table, column, DDL) for databases created before the column existed
    ("claims", "source_event", "ALTER TABLE claims ADD COLUMN source_event TEXT NOT NULL DEFAULT ''"),
    ("claims", "confirmed", "ALTER TABLE claims ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 1"),
    ("attempts", "pid_start", "ALTER TABLE attempts ADD COLUMN pid_start TEXT NOT NULL DEFAULT ''"),
    ("claims", "snoozed_until", "ALTER TABLE claims ADD COLUMN snoozed_until TEXT NOT NULL DEFAULT ''"),
]
SCHEMA_VERSION = 4   # bump with every SCHEMA/MIGRATIONS change; connect() skips all schema work when current

FMT = "%Y-%m-%dT%H:%M:%SZ"


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime(FMT)


def parse(s):
    return dt.datetime.strptime(s, FMT).replace(tzinfo=dt.timezone.utc)


def _private(path, mode):
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _migrate_legacy_location():
    """Before 2026-09-23 the ledger lived at ~/.followthrough/followthrough.db; it now lives in data/ so that only
    data/ has to be writable by sandboxed agents. Move it once: checkpoint the WAL, then rename (same inode)."""
    old, new = config.path("followthrough.db"), config.data_path("followthrough.db")
    if not os.path.exists(old) or os.path.exists(new):
        return
    import fcntl
    with open(config.path("migrate.lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        if os.path.exists(old) and not os.path.exists(new):
            c = sqlite3.connect(old, timeout=30)
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            c.close()
            os.rename(old, new)
            for suffix in ("-wal", "-shm"):
                if os.path.exists(old + suffix):
                    os.unlink(old + suffix)


def connect(fast=False):
    """Open the ledger. `fast` (hooks, status) waits at most 1.5 s for a lock, so a busy ledger makes a hook give up
    (fail open) instead of stalling a session. Schema work runs only when the stored version is behind."""
    home = config.home()
    old = os.umask(0o077)
    try:
        os.makedirs(config.data_path(), exist_ok=True)
        _private(home, 0o700)
        _private(config.data_path(), 0o700)
        _migrate_legacy_location()
        p = config.data_path("followthrough.db")
        con = sqlite3.connect(p, timeout=1.5 if fast else 30, isolation_level=None)  # autocommit; writers use tx
        con.row_factory = sqlite3.Row
        con.execute(f"PRAGMA busy_timeout={1500 if fast else 30000}")
        if con.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            con.execute("PRAGMA journal_mode=WAL")
            for table, column, ddl in MIGRATIONS:
                cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
                if cols and column not in cols:
                    try:
                        con.execute(ddl)
                    except sqlite3.OperationalError as e:   # another process added it first
                        if "duplicate column" not in str(e):
                            raise
            con.executescript(SCHEMA)
            con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    finally:
        os.umask(old)
    for suffix in ("", "-wal", "-shm"):
        _private(p + suffix, 0o600)
    return con


class tx:
    """BEGIN IMMEDIATE ... COMMIT, so read-check-write sequences cannot interleave between processes.
    Nested use becomes a SAVEPOINT inside the outer transaction."""

    def __init__(self, con):
        self.con = con
        self.nested = False

    def __enter__(self):
        self.nested = self.con.in_transaction
        self.con.execute("SAVEPOINT ft" if self.nested else "BEGIN IMMEDIATE")
        return self.con

    def __exit__(self, exc_type, *_):
        if self.nested:
            self.con.execute("ROLLBACK TO ft" if exc_type else "RELEASE ft")
            if exc_type:
                self.con.execute("RELEASE ft")
        else:
            self.con.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def event(con, claim_id, kind, detail="", at=None):
    from .core import redact   # every history line is text that may have come from a transcript or an agent
    con.execute("INSERT INTO events(claim_id, at, kind, detail) VALUES (?,?,?,?)",
                (claim_id, iso(at or now()), kind, redact(detail)))


CHANNELS = ("macos", "telegram")


def enqueue(con, claim_id, checkpoint_id, event_name, extra="", at=None):
    """Queue a notification on every channel. Call inside the transaction that made the transition."""
    t = iso(at or now())
    for ch in CHANNELS:
        con.execute("INSERT INTO outbox(claim_id, checkpoint_id, event, extra, channel, created_at, next_try_at)"
                    " VALUES (?,?,?,?,?,?,?)", (claim_id, checkpoint_id, event_name, extra, ch, t, t))
