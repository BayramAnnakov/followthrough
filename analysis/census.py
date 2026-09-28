"""Census of in-session reminders (CronCreate calls) in Claude Code transcripts.

Measures how often your own in-session reminders get lost (docs/DESIGN.md, "The problem"). Reads
~/.claude/projects/*/*.jsonl, prints tables, and writes analysis/out/census.json (prompts truncated; never commit that
file - it holds work details). Cron times are read in the followthrough config time zone.

    python analysis/census.py [--days 45]
"""
import argparse
import collections
import datetime as dt
import glob
import json
import os
import re

from followthrough import config, core, timeparse

ROOT = os.path.expanduser("~/.claude/projects")
OUT = os.path.join(os.path.dirname(__file__), "out")

KINDS = {"action": "deferred action", "ask": "human / decision-day", "watch": "monitoring series"}  # core.KIND_RULES
CODE_ANCHOR = r"\bcommit\b|\bpr #?\d|pull request|\bdeploy|\bimage\b|revision|merged|migration|shipped|\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}\b"


def kind_of(prompt):
    """The same keyword guess followthrough uses for captured reminders; approximate by construction."""
    return KINDS.get(core.guess_kind(prompt), "unclassified (mostly post-ship verification)")


def text_of(content):
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content or [] if isinstance(b, dict))


def norm(s):
    return " ".join(s.split())


def load_calls(days):
    cutoff = dt.datetime.now().timestamp() - days * 86400
    calls, users = [], collections.defaultdict(list)
    for f in glob.glob(f"{ROOT}/*/*.jsonl"):
        if os.path.getmtime(f) < cutoff:
            continue
        sid = os.path.basename(f)[:-6]
        with open(f, errors="ignore") as fh:
            for line in fh:
                if '"CronCreate"' not in line and '"type":"user"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") == "user":
                    users[sid].append((o.get("timestamp", ""), norm(text_of(o.get("message", {}).get("content")))))
                    continue
                for b in o.get("message", {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "CronCreate":
                        i = b.get("input", {})
                        calls.append(dict(sid=sid, ts=o.get("timestamp"), cwd=o.get("cwd") or "",
                                          cron=i.get("cron", ""), recurring=bool(i.get("recurring", True)),  # tool default is true
                                          prompt=i.get("prompt", "")))
    return calls, users


def fire_time(cron, set_at, tz):
    """The one-shot's due time, or None when a wildcard leaves no single date (as followthrough reads it)."""
    m, h, dom, mon, _ = cron.split()
    if "*" in (m, h, dom, mon):
        return None
    return timeparse.cron_fire_time(cron, set_at, tz)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45)
    args = ap.parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    tz = config.load()["tz"]
    calls, users = load_calls(args.days)
    calls.sort(key=lambda c: c["ts"] or "")
    if not calls:
        print(f"no CronCreate calls in transcripts touched in the last {args.days} days")
        return
    one = [c for c in calls if not c["recurring"]]
    print(f"CronCreate calls: {len(calls)}  one-shot: {len(one)}  recurring: {len(calls) - len(one)}  "
          f"sessions: {len({c['sid'] for c in calls})}  span: {calls[0]['ts'][:10]} .. {calls[-1]['ts'][:10]}")
    print("kinds:", dict(collections.Counter(kind_of(c["prompt"]) for c in calls)))
    print("code anchor in prompt:", dict(collections.Counter(bool(re.search(CODE_ANCHOR, c["prompt"].lower())) for c in calls)))
    horizon, fate = collections.Counter(), collections.Counter()
    rows: list[dict[str, object]] = []
    for c in one:
        created = dt.datetime.fromisoformat(c["ts"].replace("Z", "+00:00"))
        ft = fire_time(c["cron"], created, tz)
        if ft is None:  # a one-shot with a wildcard field; no single due time
            continue
        hours = (ft - created).total_seconds() / 3600
        band = "<12h" if hours < 12 else "12-36h" if hours < 36 else "36h-8d" if hours < 192 else ">8d"
        horizon[band] += 1
        if ft > now:
            state = "pending"
        else:  # fired = the prompt text appears as a later user turn in the same file: a text match, so it can
            # over-count (a user quoting the reminder); followthrough itself uses the fired turn's scheduledTaskId
            key = norm(c["prompt"])[:70]
            state = "fired" if any(ts > c["ts"] and key in t for ts, t in users[c["sid"]]) else "no fire seen"
        fate[(band if band in ("<12h", "12-36h") else ">=36h", state)] += 1
        rows.append({"set_at": c["ts"], "due": ft.isoformat(), "hours": round(hours, 1), "state": state,
                     "kind": kind_of(c["prompt"]), "cwd": c["cwd"].replace(os.path.expanduser("~"), "~"),
                     "session": c["sid"][:8], "prompt": norm(c["prompt"])[:400]})
    print("horizon:", dict(horizon))
    for k in sorted(fate):
        print("  fate", k, fate[k])
    print("pending now:")
    for r in rows:
        if r["state"] == "pending":
            print(f"  {r['due'][:16]}  {r['cwd'][-40:]:40}  {r['prompt'][:70]}")
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "census.json"), "w") as fh:
        json.dump(rows, fh, indent=1)


if __name__ == "__main__":
    main()
