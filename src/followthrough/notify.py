"""Notifications: macOS (clickable - opens the claim's session) and Telegram (full context + an "Open on Mac" button).

`deliver_channel` never raises: it returns (ok, detail) and the runner's outbox retries failures. Secrets: the
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


def telegram_text(claim, cp, event, extra, tz, button=False):
    """HTML for Telegram, at most TELEGRAM_LIMIT characters. Secret-shaped strings are redacted; fields are truncated
    before escaping, so the markup is never cut; the runbook gets whatever room is left."""
    def e(t, n=None):   # redact the whole value, then cut, then escape: a cut must never split a secret
        t = core.redact(str(t))
        return html.escape(_cut(t, n) if n else t)
    head = [f"<b>⏰ followthrough · {e(HEADLINES.get(event, event))}</b>", f"<b>{e(claim['title'], 200)}</b>", ""]
    meta = [f"kind: {e(claim['kind'], 20)}"]
    if claim["repo"]:
        meta.append(f"repo: {e(claim['repo'].replace(os.path.expanduser('~'), '~'), 120)}")
    if cp is not None:
        meta.append(f"checkpoint {int(cp['seq'])} ({e(cp['role'], 20)}), due {e(timeparse.local_str(db.parse(cp['due_at']), tz))}")
    head.append(" · ".join(meta))
    if claim["change_ref"]:
        head.append(f"<b>Change:</b> {e(claim['change_ref'], 300)}")
    if claim["expectation"]:
        head.append(f"<b>Expected:</b> {e(claim['expectation'], 600)}")
    if extra:
        first, reply = extra.partition("\n\n")[::2] if event == "fired" else (extra, "")   # reply after a blank line
        head.append(f"<i>{e(first, 300)}</i>")
        if reply:
            head.append(f"<b>Its last reply:</b>\n{e(reply, 1600)}")
    hint = f"  (or the button, or <code>/ft open {claim['id']}</code>)" if button else ""
    tail = ["", f"▶ <code>followthrough open {claim['id']}</code>{hint}",
            f"✓ <code>followthrough resolve {claim['id']} --verdict worked|failed|partial|inconclusive --summary \"…\"</code>"]
    if event == "overdue":
        tail.append(f"✗ no longer needed: <code>followthrough abandon {claim['id']} --reason \"…\"</code>")
    fixed = "\n".join(head + tail)
    rb = core.redact(claim["runbook"] or "")
    room = TELEGRAM_LIMIT - len(fixed) - len("\n\n<b>Runbook:</b>\n") - 10
    if rb and room > 80:
        raw = rb
        while raw and len(e(raw)) > room:
            raw = raw[: int(len(raw) * 0.85)]
        body = e(raw) + (" …" if len(raw) < len(rb) else "")
        return "\n".join(head + ["", "<b>Runbook:</b>", body] + tail)
    return fixed


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
    # Telegram rejects the whole message when one button's data is too long: drop that button instead. The close
    # button must leave room for its confirmation, ":y".
    fits = lambda b: len(b["callback_data"].encode()) + (2 * b["callback_data"].startswith("ft:close:")) <= CALLBACK_LIMIT
    rows = [r for r in ([b for b in row if fits(b)] for row in rows) if r]
    if rows:
        msg["reply_markup"] = {"inline_keyboard": rows}
    return msg


def telegram(cfg, text, claim_id="", event=""):
    t = cfg["telegram"]
    if not t.get("enabled"):
        return True, "disabled"
    token = _read_env_value(t.get("env_file", ""), t.get("token_key", "BOT_TOKEN"))
    if not token or not t.get("chat_id"):
        return False, "telegram not configured (token or chat_id missing)"
    msg = telegram_message(cfg, text, claim_id, event)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix="ft-tg-", suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(msg, fh)
        cfg_stdin = (f'url = "https://api.telegram.org/bot{token}/sendMessage"\n'
                     f'header = "Content-Type: application/json"\n'
                     f'data-binary = "@{tmp}"\n')
        r = subprocess.run(["curl", "-s", "--max-time", "20", "-o", "/dev/null", "-w", "%{http_code}", "-K", "-"],
                           input=cfg_stdin, capture_output=True, text=True, timeout=30)
        code = r.stdout.strip()
        return code == "200", f"http {code or 'none'}"
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def deliver_channel(cfg, channel, claim, cp, event, extra, bin_path):
    """Send one notification on one channel. Never raises."""
    try:
        if channel == "macos":
            return macos(cfg, claim, event, bin_path)
        if channel == "telegram":
            text = telegram_text(claim, cp, event, extra, claim["tz"] or cfg["tz"], button=cfg["telegram"].get("open_button"))
            return telegram(cfg, text, claim["id"], event)
        return False, f"unknown channel {channel}"
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__


def send_all(cfg, claim, cp, event, extra, bin_path):
    """Immediate send on every channel (used by notify-test)."""
    return {ch: deliver_channel(cfg, ch, claim, cp, event, extra, bin_path) for ch in db.CHANNELS}
