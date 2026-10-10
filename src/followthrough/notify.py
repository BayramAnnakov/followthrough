"""Notifications: macOS (clickable - opens the claim's session) and Telegram (short: what changed, what to expect,
and buttons; the runbook stays in the ledger for the session that runs the check). Reminders, overdue notices and
interim readings wait for one morning digest per channel (runner.deliver).

`deliver_channel` never raises: it returns (ok, detail), plus the Telegram message id when the message has buttons,
and the runner's outbox retries failures. Secrets: the
Telegram token is read from an env file at send time, passed to curl on stdin (never argv), and never logged; errors
are reported by type or HTTP status only.
"""
import html
import json
import os
import shlex
import shutil
import subprocess
import tempfile

from . import core, db, timeparse

HEADLINES = {
    "due": "due now",
    "fired": "ran in its session - record the verdict",
    "reminder": "still waiting for you",
    "stale": "a check was started but never resolved",
    "exhausted": "not settled after 3 tries - needs you",
    "overdue": "overdue - still open, waiting for your verdict",
    "expired": "expired without a verdict",   # legacy rows queued before overdue claims stayed open
    "test": "test notification",
}
TELEGRAM_LIMIT = 4000


def open_command(bin_path, claim_id):
    if claim_id == "ft-test":  # the test notification has no claim behind it
        return f"{shlex.quote(bin_path)} status"
    return f"{shlex.quote(bin_path)} open {shlex.quote(core.safe_id(claim_id))}"


def _terminal_script(app, command):
    cmd = command.replace("\\", "\\\\").replace('"', '\\"')
    if app == "iTerm":
        return (f'tell application "iTerm" to create window with default profile command "{cmd}"', "")
    return (f'tell application "Terminal" to do script "{cmd}"', 'tell application "Terminal" to activate')


def terminal_osascript_args(app, command):
    """argv for osascript that opens a new Terminal (or iTerm) window running `command`."""
    a, b = _terminal_script(app, command)
    return ["osascript", "-e", a] + (["-e", b] if b else [])


def _terminal_notifier():
    return shutil.which("terminal-notifier") or next(
        (p for p in ("/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier") if os.path.exists(p)), "")


def macos(cfg, claim, event, bin_path):
    if not cfg["macos"].get("enabled", True):
        return True, "disabled"
    title = "followthrough · " + HEADLINES.get(event, event)
    subtitle = core.redact(claim["title"])[:60]
    message = f"{claim['id']} · click to open the session"
    tn = _terminal_notifier()
    if tn and event != "expired":
        execute = " ".join(shlex.quote(x) for x in terminal_osascript_args(
            cfg["macos"].get("terminal_app", "Terminal"), open_command(bin_path, claim["id"])))
        args = [tn, "-title", title, "-subtitle", subtitle, "-message", message, "-group", claim["id"],
                "-execute", execute, "-sound", "default"]
    elif tn:
        args = [tn, "-title", title, "-subtitle", subtitle, "-message", claim["id"], "-group", claim["id"]]
    else:  # no terminal-notifier: a plain notification, not clickable
        message = f"{claim['id']} · run: followthrough open {claim['id']}"
        esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
        args = ["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}" subtitle "{esc(subtitle)}"']
    r = subprocess.run(args, capture_output=True, timeout=20)
    return r.returncode == 0, f"exit {r.returncode}"


def _read_env_value(env_file, key):
    try:
        with open(os.path.expanduser(env_file)) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:]
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def _cut(s, n):
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def telegram_text(claim, cp, event, extra, tz, button=False, close=False):
    """Short HTML for Telegram: the title, what changed, what to expect, and how to open it. The runbook is not sent:
    the session that runs the check reads it from the ledger. Secret-shaped strings are redacted; fields are cut
    before escaping, so the markup is never cut."""
    def e(t, n=None):   # redact the whole value, then cut, then escape: a cut must never split a secret
        t = core.redact(str(t))
        return html.escape(_cut(t, n) if n else t)
    lines = [f"<b>⏰ followthrough · {e(HEADLINES.get(event, event))}</b>", f"<b>{e(claim['title'], 200)}</b>"]
    meta = [f"kind: {e(claim['kind'], 20)}"]
    if claim["repo"]:
        meta.append(f"repo: {e(claim['repo'].replace(os.path.expanduser('~'), '~'), 80)}")
    if cp is not None:
        meta.append(f"checkpoint {int(cp['seq'])} ({e(cp['role'], 20)}), due {e(timeparse.local_str(db.parse(cp['due_at']), tz))}")
    lines.append(" · ".join(meta))
    if claim["change_ref"]:
        lines.append(f"<b>Change:</b> {e(claim['change_ref'], 160)}")
    if claim["expectation"]:
        lines.append(f"<b>Expected:</b> {e(claim['expectation'], 300)}")
    if extra:
        first, reply = extra.partition("\n\n")[::2] if event == "fired" else (extra, "")   # reply after a blank line
        lines.append(f"<i>{e(first, 300)}</i>")
        if reply:
            lines.append(f"<b>Its last reply:</b>\n{e(reply, 900)}")
    lines.append("")
    lines.append(f"▶ <code>/ft open {claim['id']}</code>" if button else f"▶ <code>followthrough open {claim['id']}</code>")
    if event in ("fired", "stale", "exhausted"):
        lines.append(f"✓ <code>followthrough resolve {claim['id']} --verdict worked|failed|partial|inconclusive --summary \"…\"</code>")
    if event == "overdue" and not close:
        lines.append(f"✗ no longer needed: <code>followthrough abandon {claim['id']} --reason \"…\"</code>")
    return _cut_lines(lines)


def _cut_lines(lines):
    """Join lines, dropping whole lines from the end of the body (never the markup inside one) past the limit."""
    text = "\n".join(lines)
    while len(text) > TELEGRAM_LIMIT and len(lines) > 2:
        lines = lines[:-2] + lines[-1:]
        text = "\n".join(lines)
    return text


DIGEST_MAX = 15          # lines in one digest at most; the rest stay queued for the next message
DIGEST_LABELS = {
    "due": "due", "fired": "ran in its session, verdict missing", "reminder": "waiting for you",
    "stale": "started, never resolved", "exhausted": "not settled after 3 tries", "overdue": "overdue",
}


def _digest_label(item):
    cp, ev = item["cp"], item["event"]
    label = DIGEST_LABELS.get(ev, ev)
    if ev == "due" and cp is not None and cp["tries"]:
        label = "due again (retry after not settled)"
    elif ev == "due" and cp is not None and cp["role"] == "interim":
        label = "interim reading due"
    return label


def digest_text(items, tz, button=False, total=None):
    """One message for many claims. items: [{"claim", "cp", "event", "extra"}], one per claim, all of them listed;
    `total` counts the ones left for a later message too. Use digest_fit to pick how many items fit."""
    def e(t, n):
        return html.escape(_cut(core.redact(str(t)), n))
    n = total or len(items)
    lines = [f"<b>☀️ followthrough · {n} check{'s' if n != 1 else ''} wait{'' if n != 1 else 's'} for you</b>", ""]
    for i, it in enumerate(items, 1):
        c = it["claim"]
        where = str(c["repo"] or "").replace(os.path.expanduser("~"), "~").rstrip("/").rsplit("/", 1)[-1]
        lines.append(f"{i}. <b>{e(c['title'], 90)}</b>")
        lines.append(f"    {e(_digest_label(it), 60)}{' · ' + e(where, 40) if where else ''} · <code>{e(c['id'], 70)}</code>")
    if n > len(items):
        lines.append(f"…and {n - len(items)} more, in the next message: "
                     f"<code>{'/ft status' if button else 'followthrough status'}</code>")
    lines += ["", ("Open one with the buttons below, or" if button else "Open one:")
              + f" <code>{'/ft open' if button else 'followthrough open'} &lt;id&gt;</code>"]
    return "\n".join(lines)


def digest_fit(items, tz, button=False):
    """How many of `items` one message can list: at most DIGEST_MAX, and within Telegram's length limit. The text,
    the buttons and the notices marked sent all come from that many, so nothing is acknowledged unseen."""
    k = min(len(items), DIGEST_MAX)
    while k > 1 and len(digest_text(items[:k], tz, button, total=len(items))) > TELEGRAM_LIMIT:
        k -= 1
    return k


def digest_markup(cfg, pairs):
    """Buttons for a digest: per line "▶ n" (open) and "✖ n" (close), two lines per row. pairs: [(n, claim_id)]."""
    t = cfg["telegram"]
    buttons = []
    for n, cid in pairs:
        pair = []
        if t.get("open_button"):
            pair.append({"text": f"▶ {n}", "callback_data": f"ft:open:{cid}"})
        if t.get("close_button"):
            pair.append({"text": f"✖ {n}", "callback_data": f"ft:close:{cid}"})
        pair = [b for b in pair if _fits(b)]
        if pair:
            buttons.append(pair)
    rows = [sum(buttons[i:i + 2], []) for i in range(0, len(buttons), 2)]
    return {"inline_keyboard": rows} if rows else None


def _fits(b):
    """Telegram rejects the whole message when one button's data is too long: such a button is dropped instead. The
    close button must leave room for its confirmation, ":y"."""
    return len(b["callback_data"].encode()) + (2 * b["callback_data"].startswith("ft:close:")) <= CALLBACK_LIMIT


CALLBACK_LIMIT = 64   # bytes of callback_data Telegram accepts


def _snooze_label(dur):
    return f"💤 till {timeparse.MORNING_HOUR}:00" if dur == "morning" else f"💤 {dur}"


def telegram_message(cfg, text, claim_id="", event=""):
    """The sendMessage payload. The buttons are opt-in (telegram.open_button, .snooze_buttons, .close_button): their
    callbacks are handled only by a process that polls this bot's updates, and without one they silently do nothing."""
    t = cfg["telegram"]
    msg = {"chat_id": str(t["chat_id"]), "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if not claim_id or claim_id == "ft-test":
        return msg
    rows = []
    if t.get("open_button"):
        rows.append([{"text": "▶ Open on Mac", "callback_data": f"ft:open:{claim_id}"}])
    if event not in ("expired", "test"):
        row = [{"text": _snooze_label(d), "callback_data": f"ft:snooze:{claim_id}:{d}"}
               for d in t.get("snooze_buttons") or [] if isinstance(d, str) and timeparse.SNOOZE_RE.fullmatch(d)]
        if t.get("close_button"):
            row.append({"text": "✖ close", "callback_data": f"ft:close:{claim_id}"})
        rows.append(row)
    rows = [r for r in ([b for b in row if _fits(b)] for row in rows) if r]
    if rows:
        msg["reply_markup"] = {"inline_keyboard": rows}
    return msg


def _tg_call(cfg, method, payload):
    """POST one Bot API call. Returns (http status, parsed JSON body or None). The token goes to curl on stdin."""
    token = _read_env_value(cfg["telegram"].get("env_file", ""), cfg["telegram"].get("token_key", "BOT_TOKEN"))
    if not token:
        return "", None
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix="ft-tg-", suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
        cfg_stdin = (f'url = "https://api.telegram.org/bot{token}/{method}"\n'
                     f'header = "Content-Type: application/json"\n'
                     f'data-binary = "@{tmp}"\n')
        r = subprocess.run(["curl", "-s", "--max-time", "20", "-w", "\n%{http_code}", "-K", "-"],
                           input=cfg_stdin, capture_output=True, text=True, timeout=30)
        body, _, code = r.stdout.rpartition("\n")
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = None
        return code.strip(), parsed
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _send_message(cfg, msg):
    """sendMessage. Returns (ok, detail, message_id): the id only when the message has buttons, so they can be
    removed once their claim closes (runner.clear_buttons)."""
    t = cfg["telegram"]
    if not t.get("enabled"):
        return True, "disabled", None
    if not t.get("chat_id") or not _read_env_value(t.get("env_file", ""), t.get("token_key", "BOT_TOKEN")):
        return False, "telegram not configured (token or chat_id missing)", None
    code, body = _tg_call(cfg, "sendMessage", msg)
    mid = None
    if code == "200" and "reply_markup" in msg and isinstance(body, dict):
        mid = (body.get("result") or {}).get("message_id")
        mid = mid if isinstance(mid, int) else None
    return code == "200", f"http {code or 'none'}", mid


def telegram(cfg, text, claim_id="", event=""):
    return _send_message(cfg, telegram_message(cfg, text, claim_id, event))


def telegram_set_buttons(cfg, chat_id, message_id, markup):
    """Replace a sent message's buttons (markup None removes them). Returns (done, detail): done for 200, and for a 400
    that says the message already has these buttons or is gone; anything else (no answer, another 400, 401/403, 429,
    5xx) is for the caller to retry with backoff and then give up."""
    body = {"chat_id": str(chat_id), "message_id": int(message_id)}
    if markup:
        body["reply_markup"] = markup
    code, resp = _tg_call(cfg, "editMessageReplyMarkup", body)
    desc = str((resp or {}).get("description", "")).lower() if isinstance(resp, dict) else ""
    settled = code == "400" and any(s in desc for s in EDIT_SETTLED)
    return code == "200" or settled, f"http {code or 'none'}" + (f" ({desc[:80]})" if code == "400" else "")


EDIT_SETTLED = ("message is not modified", "message to edit not found", "message can't be edited")


def deliver_channel(cfg, channel, claim, cp, event, extra, bin_path):
    """Send one notification on one channel. Never raises."""
    try:
        if channel == "macos":
            return macos(cfg, claim, event, bin_path)
        if channel == "telegram":
            text = telegram_text(claim, cp, event, extra, claim["tz"] or cfg["tz"], button=cfg["telegram"].get("open_button"),
                                 close=cfg["telegram"].get("close_button"))
            return telegram(cfg, text, claim["id"], event)
        return False, f"unknown channel {channel}"
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__


def macos_digest(cfg, items, bin_path, total=None):
    if not cfg["macos"].get("enabled", True):
        return True, "disabled"
    n = total or len(items)
    title = f"followthrough · {n} check{'s' if n != 1 else ''} wait{'' if n != 1 else 's'} for you"
    subtitle = core.redact(items[0]["claim"]["title"])[:60] + (f" and {n - 1} more" if n > 1 else "")
    tn = _terminal_notifier()
    command = f"{shlex.quote(bin_path)} status"
    if tn:
        execute = " ".join(shlex.quote(x) for x in terminal_osascript_args(cfg["macos"].get("terminal_app", "Terminal"), command))
        args = [tn, "-title", title, "-subtitle", subtitle, "-message", "click to see them", "-group", "ft-digest",
                "-execute", execute, "-sound", "default"]
    else:
        esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
        args = ["osascript", "-e", f'display notification "run: followthrough status" with title "{esc(title)}" subtitle "{esc(subtitle)}"']
    r = subprocess.run(args, capture_output=True, timeout=20)
    return r.returncode == 0, f"exit {r.returncode}"


def deliver_digest(cfg, channel, items, bin_path, total=None):
    """Send one digest listing `items` (one per claim, already fitted with digest_fit; `total` counts the rest too) on
    one channel. Returns (ok, detail[, message_id]). Never raises."""
    try:
        if channel == "macos":
            return macos_digest(cfg, items, bin_path, total)
        if channel == "telegram":
            msg = {"chat_id": str(cfg["telegram"].get("chat_id", "")), "parse_mode": "HTML", "disable_web_page_preview": True,
                   "text": digest_text(items, cfg["tz"], button=cfg["telegram"].get("open_button"), total=total)}
            markup = digest_markup(cfg, list(enumerate((it["claim"]["id"] for it in items), 1)))
            if markup:
                msg["reply_markup"] = markup
            return _send_message(cfg, msg)
        return False, f"unknown channel {channel}"
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__


def send_all(cfg, claim, cp, event, extra, bin_path):
    """Immediate send on every channel (used by notify-test). Returns {channel: (ok, detail)}."""
    return {ch: tuple(deliver_channel(cfg, ch, claim, cp, event, extra, bin_path))[:2] for ch in db.CHANNELS}
