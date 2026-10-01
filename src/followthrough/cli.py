"""followthrough CLI - the contract every agent and hook uses."""
import argparse
import datetime as dt
import json
import os
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys

from . import config, core, db, hooks, notify, runner, timeparse, transcripts

LABEL = "com.followthrough.tick"
# A runbook that points at its conversation needs that conversation: `open` resumes a fork of it. A bare "above"
# ("behaved as above") is not such a pointer.
NEEDS_SESSION = re.compile(r"\bthis (conversation|session)\b|\bearlier in (this|the) (conversation|session)\b"
                           r"|\b(conversation|session) above\b", re.I)
SECRET_FILE = re.compile(r"(^\.env|\.pem$|\.key$|\.p12$|^id_(rsa|ed25519|ecdsa))", re.I)
ATTACH_LIMIT = 20 * 1024 * 1024


def _bin():
    found = shutil.which("followthrough")
    return found or os.path.abspath(sys.argv[0])


def _cfg():
    return config.load()


def _fmt_claim_line(con, c, cfg, now):
    cp = core.current_checkpoint(con, c["id"])
    repo = c["repo"].replace(os.path.expanduser("~"), "~")
    if c["status"] != "active":
        when, mark = c["status"], "✓" if c["status"] == "worked" else "·"
    elif cp is None:
        when, mark = "no open checkpoint", "?"
    elif core.quiet_reading(c, cp):
        when, mark = "reading ran in session", "·"
    elif cp["state"] == "needs_human" and cp["summary"].startswith("[ran-in-session]"):
        when, mark = "ran in session - record verdict", "?"
    elif cp["state"] == "needs_human":
        when, mark = "needs you (due " + timeparse.local_str(db.parse(cp["due_at"]), c["tz"] or cfg["tz"], now) + ")", "!"
    elif cp["state"] == "running":
        when, mark = "running", "~"
    else:
        when, mark = "due " + timeparse.local_str(db.parse(cp["due_at"]), c["tz"] or cfg["tz"], now), "·"
    if c["status"] == "active" and db.parse(c["ends_at"]) < now:
        when, mark = "overdue · " + when, "!"
    if c["status"] == "active" and core.snoozed(c, now):
        when, mark = "snoozed till " + timeparse.local_str(db.parse(c["snoozed_until"]), c["tz"] or cfg["tz"], now) + " · " + when, "z"
    return f"  {mark} {c['id']:34} {when:30} {repo[-28:]:28} {c['title'][:70]}"


def cmd_status(a, con=None):
    con, cfg, now = con or db.connect(), _cfg(), db.now()
    q = "SELECT * FROM claims WHERE status='active' AND confirmed=1" if not a.all else "SELECT * FROM claims"
    params = []
    rows = con.execute(q + " ORDER BY created_at", params).fetchall()
    everything = rows
    if a.repo:
        repo = os.path.realpath(os.path.expanduser(a.repo))
        rows = [c for c in rows if c["repo"] and (os.path.realpath(c["repo"]) == repo
                                                  or os.path.realpath(c["repo"]).startswith(repo + os.sep))]

    def key(c):
        cp = core.current_checkpoint(con, c["id"])
        rank = (0 if core.needs_you(c, cp) else {"running": 1}.get(cp["state"], 2)) if cp else 3
        return (rank, cp["due_at"] if cp else "9")

    rows = sorted(rows, key=key)
    if a.json:
        out = []
        for c in rows:
            cp = core.current_checkpoint(con, c["id"])
            out.append({k: c[k] for k in ("id", "kind", "title", "repo", "status", "ends_at", "origin", "snoozed_until")} |
                       {"checkpoint": dict(cp) if cp else None})
        print(json.dumps(out, indent=1))
        return 0
    needs = sum(1 for c in rows if core.needs_you(c, core.current_checkpoint(con, c["id"])))
    soon = sum(1 for c in rows if (cp := core.current_checkpoint(con, c["id"])) and cp["state"] == "pending"
               and db.parse(cp["due_at"]) < now + dt.timedelta(hours=24))
    if a.brief:
        if not rows:
            if a.repo:  # nothing here: one global line, never other repos' titles
                g_needs = sum(1 for c in everything if core.needs_you(c, core.current_checkpoint(con, c["id"])))
                g_soon = sum(1 for c in everything if (cp := core.current_checkpoint(con, c["id"])) and cp["state"] == "pending"
                             and db.parse(cp["due_at"]) < now + dt.timedelta(hours=24))
                if g_needs or g_soon:
                    print(f"followthrough: nothing in this repo · elsewhere {g_needs} need you · {g_soon} due in 24h  (followthrough status)")
            return 0
        scope = " in this repo" if a.repo else ""
        print(f"followthrough{scope}: {needs} need you · {soon} due in 24h · {len(rows)} open  (followthrough status)")
        for c in rows[:6]:
            print(_fmt_claim_line(con, c, cfg, now))
        if len(rows) > 6:
            print(f"  … {len(rows) - 6} more")
        if a.repo:   # the rest, as one line: urgent checks elsewhere must not vanish because this repo has some
            mine = {c["id"] for c in rows}
            rest = [c for c in everything if c["id"] not in mine]
            o_needs = sum(1 for c in rest if core.needs_you(c, core.current_checkpoint(con, c["id"])))
            o_soon = sum(1 for c in rest if (cp := core.current_checkpoint(con, c["id"])) and cp["state"] == "pending"
                         and db.parse(cp["due_at"]) < now + dt.timedelta(hours=24))
            if o_needs or o_soon:
                print(f"  elsewhere: {o_needs} need you · {o_soon} due in 24h")
        return 0
    print(f"{needs} need you · {soon} due in 24h · {len(rows)} {'claims' if a.all else 'open'}")
    gave_up = con.execute("SELECT COUNT(*) FROM outbox WHERE gave_up_at!='' AND last_error!='obsolete'"
                          " AND gave_up_at>?", (db.iso(now - dt.timedelta(days=7)),)).fetchone()[0]
    if gave_up:
        print(f"  ! {gave_up} notification(s) could not be delivered in the last 7 days (followthrough show <id> for details)")
    for c in rows:
        print(_fmt_claim_line(con, c, cfg, now))
    return 0


def cmd_show(a):
    con, cfg = db.connect(), _cfg()
    c = core.get(con, a.id)
    tz = c["tz"] or cfg["tz"]
    print(f"{c['id']}  [{c['kind']}]  {c['status']}{(' - ' + c['verdict_summary']) if c['verdict_summary'] else ''}")
    print(f"title:   {c['title']}")
    for k in ("repo", "change_ref", "expectation", "live_check", "origin", "source_session_id"):
        if c[k]:
            print(f"{k + ':':9}{' ' if len(k) < 8 else ' '}{c[k]}")
    ends = timeparse.local_str(db.parse(c["ends_at"]), tz)
    if c["status"] == "active" and db.parse(c["ends_at"]) < db.now():
        print(f"overdue: since {ends} - it stays open until a verdict, or until you cancel or abandon it")
    else:
        print(f"ends:    {ends}")
    if c["status"] == "active" and core.snoozed(c):
        print(f"snoozed: until {timeparse.local_str(db.parse(c['snoozed_until']), tz)} - notifications are held until then")
    print("checkpoints:")
    for cp in core.checkpoints(con, c["id"]):
        print(f"  {cp['seq']}. {timeparse.local_str(db.parse(cp['due_at']), tz):18} {cp['role']:8} {cp['state']:12} "
              f"{cp['result']} {cp['summary'][:100]}")
    if c["runbook"]:
        print("runbook:\n  " + c["runbook"].replace("\n", "\n  "))
    files = _attachments(c["id"])
    if files:
        print("attachments:\n  " + "\n  ".join(files))
    print("history:")
    for e in core.events(con, c["id"]):
        print(f"  {e['at']}  {e['kind']:18} {e['detail'][:140]}")
    return 0


def _repo_for(arg):
    """The folder a claim belongs to: --repo as given, else the git top level of the current folder. Never the home
    folder - a claim filed there starts its check in ~ and is invisible from the repo it is about."""
    d = os.path.realpath(os.path.expanduser(arg or os.getcwd()))
    if not arg:
        try:
            r = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                d = os.path.realpath(r.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    if d in (os.path.realpath(os.path.expanduser("~")), os.path.realpath("/")):
        raise SystemExit(f"followthrough: {d} is not a project folder; pass --repo <the repository the change is in>")
    return d


def _attachments(cid):
    root = config.data_path("attachments", cid)
    out = []
    for base, _, names in os.walk(root):
        out += sorted(os.path.join(base, n) for n in names)
    return out


def _attach_check(cid, paths):
    """Validate files to attach, before anything is created: they exist, hold no symlinks (a link could pull in a
    secret or a huge tree), no secret-looking names, fit the size limit, and collide with nothing already attached.
    Returns their real paths."""
    srcs = [os.path.realpath(os.path.expanduser(p)) for p in paths]
    problems = [f"{p}: not found" for p in srcs if not os.path.exists(p)]
    problems += [f"{p}: not a regular file or folder" for p in srcs
                 if os.path.exists(p) and not (os.path.isdir(p) or stat.S_ISREG(os.stat(p).st_mode))]
    if problems:
        raise SystemExit("followthrough: cannot attach:\n  " + "\n  ".join(problems))
    files = []
    for src in srcs:
        if os.path.isfile(src):
            files.append(src)
            continue
        for base, dirs, names in os.walk(src):
            for n in dirs + names:
                f = os.path.join(base, n)
                if os.path.islink(f):
                    problems.append(f"{f}: a symlink (attach the real file)")
                elif n in names and not stat.S_ISREG(os.lstat(f).st_mode):
                    problems.append(f"{f}: not a regular file")
                elif n in names:
                    files.append(f)
    problems += [f"{f}: looks like a secret" for f in files if SECRET_FILE.search(os.path.basename(f))]
    names = [os.path.basename(p) for p in srcs]
    dest_root = config.data_path("attachments", cid) if cid else None   # no claim yet (add): nothing attached
    problems += [f"{n}: attached twice or already attached (rename one)" for n in sorted(set(names))
                 if names.count(n) > 1 or (dest_root and os.path.lexists(os.path.join(dest_root, n)))]
    total = sum(os.path.getsize(f) for f in files)
    if total > ATTACH_LIMIT:
        problems.append(f"{total // 1024} KB in total; the limit is {ATTACH_LIMIT // 1024} KB")
    if problems:
        raise SystemExit("followthrough: cannot attach:\n  " + "\n  ".join(problems))
    return srcs


def _attach(con, cid, srcs):
    """Copy validated files or folders into the ledger (data/attachments/<claim>/), so a check can still use them
    after the session scratchpad that held them is gone. They are for the checker agent to read or run -
    followthrough itself never executes anything from data/. data/ is agent-writable, so nothing there is trusted:
    the copy is made in a fresh staging folder and renamed into place, which never follows a planted symlink.
    Returns the copied paths."""
    import tempfile
    cid = core.safe_id(cid)   # it names a folder; with --dedup it comes straight from the agent-writable ledger
    root = config.data_path("attachments")
    dest_root = os.path.join(root, cid)
    old = os.umask(0o077)
    try:
        for d in (root, dest_root):
            if os.path.islink(d):
                raise SystemExit(f"followthrough: {d} is a symlink; refusing to write through it")
            os.makedirs(d, exist_ok=True)
        stage = tempfile.mkdtemp(prefix=".staging-", dir=dest_root)
        copied = []
        try:
            for src in srcs:
                tmp = os.path.join(stage, os.path.basename(src))
                (shutil.copytree(src, tmp) if os.path.isdir(src) else shutil.copy2(src, tmp))
                for base, dirs, names in os.walk(tmp):   # private, keeping only the owner's exec bit
                    for n in dirs + names:
                        f = os.path.join(base, n)
                        if not os.path.islink(f):
                            os.chmod(f, 0o700 if os.path.isdir(f) or os.stat(f).st_mode & 0o100 else 0o600)
                if os.path.isfile(tmp):
                    os.chmod(tmp, 0o700 if os.stat(tmp).st_mode & 0o100 else 0o600)
                dst = os.path.join(dest_root, os.path.basename(src))
                os.rename(tmp, dst)                        # replaces a planted link itself, never its target
                copied.append(dst)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    finally:
        os.umask(old)
    core.note(con, cid, "attached (copied into the ledger): " + ", ".join(copied))
    return copied


def cmd_attach(a):
    con = db.connect()
    c = core.get(con, a.id)
    for p in _attach(con, c["id"], _attach_check(c["id"], a.paths)):
        print(p)
    return 0


def cmd_add(a):
    con, cfg = db.connect(), _cfg()
    tz = a.tz or cfg["tz"]
    dues = [(timeparse.parse_when(t, tz), "interim") for t in a.at]
    runbook = a.runbook or ""
    if a.runbook_file:
        with open(a.runbook_file) as fh:
            runbook = fh.read()
    title = core.redact(a.title or core.title_from(core.redact(runbook) or "untitled"))
    kind = a.kind or core.guess_kind(runbook + " " + title)
    ends = timeparse.parse_when(a.ends, tz) if a.ends else None
    repo = _repo_for(a.repo)
    if a.attach:   # a bad attachment must fail before the claim exists
        _attach_check(None, a.attach)
    cid, created = core.add_claim(
        con, title=title, kind=kind, dues=dues, repo=repo,
        runbook=runbook, expectation=a.expect or "", change_ref=a.change or "", live_check=a.live_check or "",
        ends_at=ends, origin=a.origin, harness=a.harness or "", source_session_id=a.session or "",
        source_cwd="", tz=tz, phash=core.prompt_hash(runbook) if a.dedup and runbook else "")
    print(cid if created else f"{cid} (already exists)")
    if a.attach:
        try:
            files = _attach(con, cid, _attach_check(cid, a.attach))
        except (OSError, SystemExit) as e:
            if created:
                core.close(con, cid, "cancelled", f"its attachments could not be copied: {e}")
            raise
        for p in files:
            print(f"attached {p}")
    elif core.EPHEMERAL.search(" ".join(filter(None, (runbook, a.live_check)))):
        print(f"warning: the runbook names a session scratchpad or /tmp path, which is deleted with the session. Copy "
              f"what the check needs into the ledger: followthrough attach {cid} <files>", file=sys.stderr)
    return 0


def cmd_start(a):
    con = db.connect()
    pid, pid_start = core.agent_pid()
    status, detail = core.start(con, a.id, a.path, pid=pid, pid_start=pid_start,
                                deadline_hours=_cfg()["attempt_deadline_hours"])
    print(f"{status} {detail}")
    return 0 if status == "OK" else 1


def cmd_resolve(a):
    con, cfg = db.connect(), _cfg()
    retry = timeparse.parse_when(a.retry_at, cfg["tz"]) if a.retry_at else None
    print(core.resolve(con, a.id, a.verdict, a.summary or "", retry_at=retry, final=a.final, checkpoint=a.checkpoint,
                       attempt=a.attempt))
    return 0


def cmd_amend(a):
    try:
        print(core.amend(db.connect(), a.id, a.verdict, a.summary, a.reason))
    except (ValueError, core.NotAmended) as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def cmd_close(a, status):
    con = db.connect()
    was = core.close(con, a.id, status, a.reason)
    if was != "active":
        print(f"{a.id} is already closed ({was}); nothing changed", file=sys.stderr)
        return 1
    print(f"{status}: {a.id}")
    return 0


def cmd_expect(a):
    con = db.connect()
    if core.expect(con, a.id, " ".join(a.text)):
        print("expectation set")
        return 0
    print("this claim already has an expectation; it is never edited - add a note instead", file=sys.stderr)
    return 1


def cmd_snooze(a):
    con, cfg = db.connect(), _cfg()
    c = core.get(con, a.id)
    tz = c["tz"] or cfg["tz"]
    try:
        until = (None if a.off else timeparse.snooze_until(a.for_, tz) if a.for_ else timeparse.parse_when(a.until, tz))
        end = core.snooze(con, c["id"], until, source=a.source)
    except (ValueError, core.NotSnoozed) as e:
        print(e, file=sys.stderr)
        return 1
    if a.off:
        print(f"snooze ended: {c['id']} (anything held goes out on the next tick)")
    else:
        print(f"snoozed {c['id']} until {timeparse.local_str(db.parse(end), tz)}")
    return 0


def cmd_note(a):
    core.note(db.connect(), a.id, " ".join(a.text))
    return 0


PREAMBLE = """This is followthrough claim {id} ({kind}), checkpoint {seq} due {due}. Repo: {repo}
1) Run: {bin} start {id}  - it prints OK <attempt-id>. If it prints TAKEN or CLOSED, stop and tell the user
   exactly what it printed (for a claim closed as expired, it says how to record a late verdict if they want the check).
2) Do the check below. It is read-only: no deploys, sends, publishes or setting changes; if the check needs one,
   stop and say so. Confirm the change is still live first when a live check is given. Never print secrets.
3) Record the result, always - also when the expectation was wrong:
   {bin} resolve {id} --attempt <attempt-id> --verdict worked|failed|partial|inconclusive --summary "<measured value vs expected, source, window>"
   If the data is not settled yet: {bin} resolve {id} --attempt <attempt-id> --verdict not_settled --retry-at +12h --summary "<why>"
   Never record worked without a measured value; keep measured and inferred apart.
{details}
--- the original reminder / runbook ---
{runbook}
"""


def build_prompt(con, c, cfg):
    cp = core.pick_checkpoint(con, c, db.now())
    tz = c["tz"] or cfg["tz"]
    details = []
    if c["change_ref"]:
        details.append(f"What changed: {c['change_ref']}")
    if c["expectation"]:
        details.append(f"Expectation (written before the data existed): {c['expectation']}")
    if c["live_check"]:
        details.append(f"Live check (run first): {c['live_check']}")
    notes = [e["detail"] for e in core.events(con, c["id"]) if e["kind"] in ("note", "expectation", "session_reply", "amended")]
    if notes:
        details.append("Notes:\n" + "\n".join(f"  - {n}" for n in notes[-10:]))
    files = _attachments(c["id"])
    if files:
        details.append("Attached files (copies made at registration; use these, the originals may be gone):\n"
                       + "\n".join(f"  - {f}" for f in files))
    return core.redact(PREAMBLE.format(id=c["id"], kind=c["kind"], seq=cp["seq"] if cp else "-", repo=c["repo"],
                                       due=timeparse.local_str(db.parse(cp["due_at"]), tz) if cp else "-", bin=_bin(),
                                       details="\n".join(details), runbook=c["runbook"] or c["title"]))


def _claude_bin(cfg):
    """The claude binary to launch: configured or found on PATH; absolute, executable, never inside the ledger's
    agent-writable data/ folder."""
    cand = cfg["claude_bin"] or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    real = os.path.realpath(cand)
    if not os.path.isabs(cand) or not os.access(real, os.X_OK) or real.startswith(os.path.realpath(config.data_path()) + os.sep):
        raise SystemExit(f"refusing to launch claude_bin={cand!r}: must be an absolute, executable path outside the ledger")
    return cand


def cmd_open(a):
    con, cfg = db.connect(), _cfg()
    c = core.get(con, a.id)
    prompt = build_prompt(con, c, cfg)
    claude = _claude_bin(cfg)
    fork = a.fork or (not a.fresh and c["source_session_id"] and transcripts.find(c["source_session_id"])
                      and NEEDS_SESSION.search(c["runbook"]))
    # A resumed session must start in the folder it was recorded in; a fresh one starts in the claim's repo.
    candidates = [c["source_cwd"], c["repo"]] if fork else [c["repo"], c["source_cwd"]]
    cwd = next((d for d in candidates if d and os.path.isdir(d)), os.getcwd())
    # the full prompt goes to a private file; argv carries only a pointer (argv is visible in the process list)
    os.makedirs(config.path("prompts"), mode=0o700, exist_ok=True)
    pfile = config.path("prompts", f"{c['id']}.md")
    fd = os.open(pfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(prompt)
    pointer = f"Read {pfile} and follow those instructions exactly. It is a followthrough check ({c['id']})."
    args = [claude] + (["--resume", c["source_session_id"], "--fork-session"] if fork and c["source_session_id"] else [])
    args.append(pointer)
    if a.print:
        print(f"cd {cwd}\n" + " ".join(args) + "\n---\n" + prompt)
        return 0
    os.environ["FOLLOWTHROUGH_PARENT_CLAIM"] = c["id"]
    os.chdir(cwd)
    os.execv(claude, args)


def cmd_open_terminal(a):
    """Open a new Terminal window on this Mac running `followthrough open <id>` (used by notification clicks and
    the Telegram button)."""
    con, cfg = db.connect(), _cfg()
    c = core.get(con, a.id)
    args = notify.terminal_osascript_args(cfg["macos"].get("terminal_app", "Terminal"), notify.open_command(_bin(), c["id"]))
    r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    core.note(con, c["id"], f"open-terminal requested ({a.source}); osascript exit {r.returncode}")
    print(f"{'opened' if r.returncode == 0 else 'FAILED'} {c['id']}")
    return r.returncode


def cmd_tick(a):
    con, cfg = db.connect(), _cfg()
    for line in runner.tick(con, cfg, _bin(), dry=a.dry_run):
        print(f"{db.iso(db.now())} {line}")
    return 0


def cmd_notify_test(a):
    con, cfg = db.connect(), _cfg()
    if a.id:
        c = core.get(con, a.id)
        cp = core.current_checkpoint(con, c["id"])
    else:
        c = {"id": "ft-test", "title": "followthrough test notification", "kind": "verify", "repo": os.getcwd(),
             "change_ref": "", "expectation": "This message arrives with full context.", "runbook": "Nothing to do.",
             "tz": cfg["tz"]}
        cp = None
    for ch, (ok, d) in notify.send_all(cfg, c, cp, "test", "", _bin()).items():
        print(f"{ch}: {'ok' if ok else 'FAIL'} ({d})")
    return 0


def cmd_import_crons(a):
    """Scan Claude Code transcripts now (the runner does this every tick). --rescan re-reads files from the start;
    captures are idempotent, so a rescan never duplicates."""
    con, cfg = db.connect(), _cfg()
    if a.rescan:
        con.execute("DELETE FROM scan_files")
    log = transcripts.scan(con, cfg, lookback_days=a.days, budget_s=600)
    for line in log:
        print(line)
    print(f"{sum(1 for l in log if l.startswith(('created', 'linked', 'rescheduled')))} captured or updated")
    return 0


def cmd_install_runner(a):
    bin_path = _bin()
    home = os.path.expanduser("~")
    logdir = config.path("logs")
    os.makedirs(logdir, mode=0o700, exist_ok=True)
    for f in ("tick.log", "tick.err"):
        if os.path.exists(os.path.join(logdir, f)):
            os.chmod(os.path.join(logdir, f), 0o600)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [bin_path, "tick"],
        "StartInterval": a.interval,
        "RunAtLoad": True,
        "Umask": 0o077,
        "EnvironmentVariables": {"PATH": f"{home}/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
                                 "HOME": home, "FOLLOWTHROUGH_HOME": config.home()},
        "StandardOutPath": os.path.join(logdir, "tick.log"),
        "StandardErrorPath": os.path.join(logdir, "tick.err"),
    }
    dest = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as fh:
        plistlib.dump(plist, fh)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", dest], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", dest], capture_output=True, text=True)
    print(f"installed {dest} (every {a.interval}s) -> {bin_path} tick; launchctl exit {r.returncode} {r.stderr.strip()}")
    return r.returncode


HOOK_EVENTS = [  # (event, matcher, handler, extra fields)
    ("PreToolUse", "CronCreate", "pre-cron", {"timeout": 10}),
    ("PostToolUse", "CronCreate", "post-cron", {"timeout": 10}),
    ("PostToolUseFailure", "CronCreate", "cron-failed", {"timeout": 10}),
    ("Stop", None, "stop", {"async": True}),
    ("SessionEnd", None, "session-end", {"timeout": 5}),
]


def _strip_ours(groups):
    out = []
    for g in groups or []:
        hs = [h for h in g.get("hooks", [])
              if not ("followthrough" in str(h.get("command", "")) and " hook " in str(h.get("command", "")))]
        if hs:
            out.append({**g, "hooks": hs})
    return out


def cmd_install_hooks(a):
    """Merge followthrough's hooks into a Claude Code settings file (idempotent; backs the file up first).
    Prints only event names - settings files can hold secrets."""
    path = os.path.expanduser(a.settings)
    data = {}
    if os.path.exists(path):
        with open(path) as fh:
            data = json.load(fh)
        backup = f"{path}.bak-followthrough-{db.now().strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
    # sandboxed Bash must be able to write the ledger, or `followthrough start` fails inside a check
    fs = data.setdefault("sandbox", {}).setdefault("filesystem", {})
    writable = [p for p in fs.get("allowWrite", []) if p != config.data_path()]
    if not a.remove:
        writable.append(config.data_path())
    if writable:
        fs["allowWrite"] = writable
    else:
        fs.pop("allowWrite", None)
        if not fs:
            data["sandbox"].pop("filesystem")
        if not data["sandbox"]:
            data.pop("sandbox")
    hooks_cfg = data.setdefault("hooks", {})
    for ev in {e for e, *_ in HOOK_EVENTS} | {"SessionStart"}:
        hooks_cfg[ev] = _strip_ours(hooks_cfg.get(ev))
        if not hooks_cfg[ev]:
            del hooks_cfg[ev]
    if not a.remove:
        events = HOOK_EVENTS + ([("SessionStart", None, "session-start", {"timeout": 10})] if a.session_start else [])
        b = shlex.quote(_bin())
        for ev, matcher, handler, extra in events:
            # the guard keeps a missing binary (another machine, a cloud session) from erroring on every turn
            cmd = f'[ -x {b} ] && [ -z "$CLAUDE_CODE_REMOTE" ] && exec {b} hook {handler}; exit 0'
            group = {"hooks": [{"type": "command", "command": cmd, **extra}]}
            if matcher:
                group = {"matcher": matcher, **group}
            hooks_cfg.setdefault(ev, []).append(group)
    tmp = path + ".tmp-followthrough"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp, os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o600)
    os.replace(tmp, path)
    print(("removed" if a.remove else "installed") + " followthrough hooks in " + path + ": "
          + ", ".join(e for e, *_ in HOOK_EVENTS) + f"; sandbox write access to {config.data_path()}"
          + (" (removed)" if a.remove else ""))
    return 0


SKILL_TARGETS = ("~/.claude/skills/followthrough", "~/.codex/skills/followthrough")


STARTER_CONFIG = """# followthrough settings (defaults: followthrough/config.py). Uncomment a line to change it.
tz = "{tz}"   # the zone your sessions run in: cron times of reminders are read in it
# user_names = ["Sam"]   # "ask Sam" / "reminder for Sam" in a reminder -> kind `ask` (a decision for you)
# ignore_paths = []      # never capture sessions under these folders
# grace_minutes = 30     # how long a live session gets to run its own reminder first

[macos]
enabled = true
# terminal_app = "Terminal"   # or "iTerm"

[telegram]
enabled = false
# env_file = "~/.config/followthrough/telegram.env"   # a file with a line BOT_TOKEN=...; the token is never stored here
# token_key = "BOT_TOKEN"
# chat_id = ""
"""


def _write_starter_config():
    """A commented config.toml (0600) for a new machine; an existing one is never touched. Returns its path or None."""
    p = config.path("config.toml")
    if os.path.exists(p):
        return None
    os.makedirs(config.home(), mode=0o700, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(STARTER_CONFIG.format(tz=config.load()["tz"]))
    return p


def cmd_install(a):
    """One command for a new machine: the skill for Claude Code and Codex, the hooks, the runner, and what is left
    to configure by hand."""
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "SKILL.md")
    for t in SKILL_TARGETS:
        d = os.path.expanduser(t)
        if os.path.islink(d):
            os.unlink(d)
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, "SKILL.md")
        if os.path.lexists(dst):
            os.unlink(dst)
        (os.symlink if a.link else shutil.copyfile)(src, dst)
        print(f"skill -> {dst}")
    if _write_starter_config():
        print(f"config -> {config.path('config.toml')}")
    cmd_install_hooks(argparse.Namespace(settings=a.settings, remove=False, session_start=not a.no_session_start))
    rc = cmd_install_runner(argparse.Namespace(interval=a.interval))
    if rc:
        print(f"\nWARNING: the runner did not load (launchctl exit {rc}): nothing will notify you until it does. "
              f"Retry with `followthrough install-runner`.")
    cfg = config.load()
    print(f"\nNext steps:\n  - check the time zone used for reminders: tz = {cfg['tz']!r} in {config.path('config.toml')}"
          f"\n  - clickable macOS notifications need: brew install terminal-notifier"
          f"\n  - optional Telegram: set [telegram] enabled, env_file, token_key, chat_id in the config"
          f"\n  - Codex sandbox: add {config.data_path()} to [sandbox_workspace_write] writable_roots in ~/.codex/config.toml"
          f"\n  - Codex: add a line to ~/.codex/AGENTS.md pointing at the followthrough skill")
    return 1 if rc else 0


def cmd_uninstall_runner(a):
    dest = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", dest], capture_output=True)
    if os.path.exists(dest):
        os.unlink(dest)
    print("runner uninstalled")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="followthrough", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="open claims, most urgent first")
    s.add_argument("--repo"), s.add_argument("--brief", action="store_true"), s.add_argument("--json", action="store_true")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("show", help="one claim with checkpoints and history")
    s.add_argument("id"), s.set_defaults(fn=cmd_show)

    s = sub.add_parser("add", help="register a claim")
    s.add_argument("title", nargs="?")
    s.add_argument("--at", action="append", required=True, help="checkpoint time: +12h, +2d, '2026-10-01 08:00' (repeatable)")
    s.add_argument("--kind", choices=core.KINDS)
    s.add_argument("--repo"), s.add_argument("--runbook"), s.add_argument("--runbook-file")
    s.add_argument("--expect", help="the expectation, written before the data exists")
    s.add_argument("--change", help="what changed: commit, revision, console change")
    s.add_argument("--live-check", help="read-only command that proves the change is still live")
    s.add_argument("--ends"), s.add_argument("--tz"), s.add_argument("--session"), s.add_argument("--harness")
    s.add_argument("--origin", default="cli", choices=["cli", "skill", "cron", "import"])
    s.add_argument("--dedup", action="store_true", help="skip if an active claim has the same runbook")
    s.add_argument("--attach", action="append", help="copy a file or folder the check needs into the ledger (repeatable)")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("attach", help="copy files or folders a check needs into the ledger (session scratchpads are deleted)")
    s.add_argument("id"), s.add_argument("paths", nargs="+"), s.set_defaults(fn=cmd_attach)

    s = sub.add_parser("start", help="take the lease on the current checkpoint (prints OK / TAKEN / CLOSED)")
    s.add_argument("id"), s.add_argument("--path", default="session", choices=["session", "open", "runner", "manual"])
    s.set_defaults(fn=cmd_start)

    s = sub.add_parser("resolve", help="record a verdict")
    s.add_argument("id"), s.add_argument("--verdict", required=True, choices=core.VERDICTS)
    s.add_argument("--summary"), s.add_argument("--retry-at"), s.add_argument("--final", action="store_true")
    s.add_argument("--checkpoint", type=int, help="checkpoint number to resolve (default: the current one, if due)")
    s.add_argument("--attempt", help="the attempt id printed by `start` - binds the verdict to that check")
    s.set_defaults(fn=cmd_resolve)

    s = sub.add_parser("amend", help="change a closed claim's verdict when the user asks; the old one stays in history")
    s.add_argument("id"), s.add_argument("--verdict", required=True, choices=core.AMENDABLE)
    s.add_argument("--summary", required=True, help="the new verdict summary")
    s.add_argument("--reason", required=True, help="why it changed, and who asked")
    s.set_defaults(fn=cmd_amend)

    for name, status in (("cancel", "cancelled"), ("abandon", "abandoned")):
        s = sub.add_parser(name, help=f"mark a claim {status}")
        s.add_argument("id"), s.add_argument("--reason", required=True)
        s.set_defaults(fn=lambda a, st=status: cmd_close(a, st))

    s = sub.add_parser("expect", help="set a claim's expectation once (for captured reminders; never edited later)")
    s.add_argument("id"), s.add_argument("text", nargs="+"), s.set_defaults(fn=cmd_expect)

    s = sub.add_parser("snooze", help="hold a claim's notifications for a while; a reminder follows when it ends")
    s.add_argument("id")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--for", dest="for_", metavar="FOR", help="30m, 3h, 2d, 1w, or morning (the next 09:00)")
    g.add_argument("--until", help="a time: '2026-10-01 08:00' (local) or ISO with an offset")
    g.add_argument("--off", action="store_true", help="end the snooze now")
    s.add_argument("--source", default="cli"), s.set_defaults(fn=cmd_snooze)

    s = sub.add_parser("note", help="append a note to a claim's history")
    s.add_argument("id"), s.add_argument("text", nargs="+"), s.set_defaults(fn=cmd_note)

    s = sub.add_parser("open", help="start an interactive Claude session for the claim (fork of its source session when useful)")
    s.add_argument("id"), s.add_argument("--fork", action="store_true"), s.add_argument("--fresh", action="store_true")
    s.add_argument("--print", action="store_true", help="print the command and prompt instead of running it")
    s.set_defaults(fn=cmd_open)

    s = sub.add_parser("open-terminal", help="open a Terminal window on this Mac running `followthrough open <id>`")
    s.add_argument("id"), s.add_argument("--source", default="cli"), s.set_defaults(fn=cmd_open_terminal)

    s = sub.add_parser("hook", help="Claude Code hook entry point (reads the hook JSON on stdin; always exits 0)")
    s.add_argument("event", choices=sorted(hooks.HANDLERS))
    s.set_defaults(fn=lambda a: hooks.run(a.event, _bin()))

    s = sub.add_parser("tick", help="one runner pass (launchd calls this)")
    s.add_argument("--dry-run", action="store_true"), s.set_defaults(fn=cmd_tick)

    s = sub.add_parser("notify-test", help="send a test notification on every channel")
    s.add_argument("--id"), s.set_defaults(fn=cmd_notify_test)

    s = sub.add_parser("import-crons", help="import pending in-session reminders from Claude Code transcripts")
    s.add_argument("--days", type=int, default=45), s.add_argument("--rescan", action="store_true")
    s.set_defaults(fn=cmd_import_crons)

    s = sub.add_parser("install-runner", help="install the launchd job")
    s.add_argument("--interval", type=int, default=120), s.set_defaults(fn=cmd_install_runner)
    s = sub.add_parser("uninstall-runner", help="remove the launchd job")
    s.set_defaults(fn=cmd_uninstall_runner)

    s = sub.add_parser("install-hooks", help="add followthrough's hooks to a Claude Code settings file")
    s.add_argument("--settings", default="~/.claude/settings.json"), s.add_argument("--remove", action="store_true")
    s.add_argument("--session-start", action="store_true", help="also show open claims at session start")
    s.set_defaults(fn=cmd_install_hooks)

    s = sub.add_parser("install", help="install everything: skill (Claude Code + Codex), hooks, launchd runner")
    s.add_argument("--settings", default="~/.claude/settings.json"), s.add_argument("--no-session-start", action="store_true")
    s.add_argument("--interval", type=int, default=120), s.add_argument("--link", action="store_true",
                                                                         help="symlink the skill instead of copying it")
    s.set_defaults(fn=cmd_install)

    a = p.parse_args(argv)
    try:
        return a.fn(a) or 0
    except core.NotFound as e:
        print(e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
