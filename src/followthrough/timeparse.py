"""Parse user-facing times ('+12h', '2026-10-01 08:00') and cron expressions (Claude Code's CronCreate format)."""
import datetime as dt
import re
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc


def parse_when(text, tz, now=None):
    """Return an aware UTC datetime. Relative: +90m, +12h, +2d, +1w. Absolute: local wall time or ISO with offset."""
    now = now or dt.datetime.now(UTC)
    s = text.strip()
    m = re.fullmatch(r"\+?(\d+(?:\.\d+)?)\s*(m|min|h|d|w)", s)
    if m:
        n, unit = float(m.group(1)), m.group(2)
        delta = {"m": dt.timedelta(minutes=n), "min": dt.timedelta(minutes=n), "h": dt.timedelta(hours=n),
                 "d": dt.timedelta(days=n), "w": dt.timedelta(weeks=n)}[unit]
        return (now + delta).replace(microsecond=0).astimezone(UTC)
    s = s.replace("Z", "+00:00")
    t = dt.datetime.fromisoformat(s.replace(" ", "T", 1) if re.match(r"\d{4}-\d\d-\d\d \d", s) else s)
    if t.tzinfo is None:
        t = to_utc(t, tz)[0]
    return t.astimezone(UTC)


def to_utc(naive, tz):
    """UTC instants for a local wall time: two for an ambiguous (fall-back) time, and for a non-existent
    (spring-forward) time the instant just after the gap, i.e. the wall time shifted forward by the gap."""
    z = ZoneInfo(tz)
    out = []
    for fold in (0, 1):
        u = naive.replace(tzinfo=z, fold=fold).astimezone(UTC)
        if u.astimezone(z).replace(tzinfo=None) == naive and u not in out:
            out.append(u)
    if not out:  # non-existent local time
        out.append(naive.replace(tzinfo=z, fold=0).astimezone(UTC))
    return sorted(out)


def _field(spec, lo, hi):
    vals = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
            if step != 1:
                b = hi
        vals.update(range(a, b + 1, step))
    return {v for v in vals if lo <= v <= hi}


def parse_cron(cron):
    parts = cron.split()
    if len(parts) != 5:
        raise ValueError(f"not a 5-field cron expression: {cron!r}")
    minute, hour, dom, mon, dow = parts
    dows = {0 if v == 7 else v for v in _field(dow, 0, 7)}
    return {"min": sorted(_field(minute, 0, 59)), "hour": sorted(_field(hour, 0, 23)), "dom": _field(dom, 1, 31),
            "mon": _field(mon, 1, 12), "dow": dows, "dom_any": dom == "*", "dow_any": dow == "*"}


def next_fires(cron, after, tz, count=1, horizon_days=400):
    """The next `count` fire instants (UTC) strictly after `after`, with the wall clock in `tz`."""
    c = parse_cron(cron)
    z = ZoneInfo(tz)
    day = after.astimezone(z).date()
    out = []
    for _ in range(horizon_days + 1):
        dom_ok, dow_ok = day.day in c["dom"], (day.isoweekday() % 7) in c["dow"]
        day_ok = day.month in c["mon"] and (
            (dom_ok and dow_ok) if (c["dom_any"] or c["dow_any"]) else (dom_ok or dow_ok))
        if day_ok:
            for h in c["hour"]:
                for m in c["min"]:
                    for u in to_utc(dt.datetime(day.year, day.month, day.day, h, m), tz):
                        if u > after and u not in out:
                            out.append(u)
                            if len(out) >= count:
                                return out
        day += dt.timedelta(days=1)
    return out


def cron_fire_time(cron, set_at, tz):
    """Fire instant of a one-shot CronCreate set at `set_at`.

    A fully pinned date ('M H DoM Mon *') resolves in `set_at`'s year even when that is already in the past (a
    reminder whose time passed is overdue, not next year's). Anything else is the next match after `set_at`.
    Returns None for a malformed expression.
    """
    try:
        parts = cron.split()
        if len(parts) == 5 and all(p.isdigit() for p in parts[:4]) and parts[4] == "*":
            minute, hour, dom, mon = (int(p) for p in parts[:4])
            year = set_at.astimezone(ZoneInfo(tz)).year
            try:
                cands = to_utc(dt.datetime(year, mon, dom, hour, minute), tz)
            except ValueError:
                return None
            later = [u for u in cands if u >= set_at - dt.timedelta(minutes=1)]
            if later:
                return later[0]
            try:  # set in late December for early January
                nxt = to_utc(dt.datetime(year + 1, mon, dom, hour, minute), tz)[0]
                if nxt - set_at < dt.timedelta(days=60):
                    return nxt
            except ValueError:
                pass
            return cands[-1]
        fires = next_fires(cron, set_at - dt.timedelta(minutes=1), tz)
        return fires[0] if fires else None
    except (ValueError, IndexError):
        return None


def cron_period(cron, after, tz):
    """Gap between the next two fires, or None."""
    try:
        f = next_fires(cron, after, tz, count=2)
    except (ValueError, IndexError):
        return None
    return f[1] - f[0] if len(f) == 2 else None


def local_str(t, tz, now=None):
    """Short local rendering: '14:53 today', 'Sep 28 08:17'."""
    z = ZoneInfo(tz)
    lt = t.astimezone(z)
    ln = (now or dt.datetime.now(UTC)).astimezone(z)
    if lt.date() == ln.date():
        return lt.strftime("%H:%M today")
    if lt.date() == (ln + dt.timedelta(days=1)).date():
        return lt.strftime("%H:%M tomorrow")
    return lt.strftime("%b %d %H:%M")
