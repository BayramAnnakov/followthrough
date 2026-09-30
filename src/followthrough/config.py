"""Configuration: ~/.followthrough/config.toml merged over defaults. FOLLOWTHROUGH_HOME overrides the location.

Layout - the split matters for sandboxed agents (Codex `writable_roots`): only `data/` needs to be writable by an
agent; `config.toml` (notification targets, the claude binary) and `logs/` must not be.
  ~/.followthrough/config.toml    trusted settings, written by you or `followthrough install`
  ~/.followthrough/data/          the ledger (followthrough.db + -wal/-shm) - the only agent-writable part
  ~/.followthrough/prompts/       check prompts handed to `claude` by `followthrough open`
  ~/.followthrough/logs/          runner and hook logs
"""
import copy
import os
import tomllib


def _system_tz():
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


DEFAULTS = {
    "tz": _system_tz(),           # wall clock used to read cron expressions: the zone your sessions run in
    "grace_minutes": 30,          # a live session gets this long to run its own reminder first
    "live_session_max_wait_hours": 6,  # ...longer while its Stop hook still reports the job scheduled
    "capture_min_minutes": 30,    # reminders set closer than this are in-task waits, not follow-ups
    "ignore_paths": [],           # sessions under these folders are never captured (e.g. an isolated bot)
    "user_names": [],             # names your reminders call you ("ask Sam", "reminder for Sam") -> kind "ask"
    "renotify_hours": 24,         # repeat a "needs you" notification at most this often
    "attempt_deadline_hours": 3,  # an interactive attempt that never resolves becomes "needs you" again
    "claude_bin": "",             # absolute path to claude; found on PATH when empty
    "macos": {"enabled": True, "terminal_app": "Terminal"},
    # open_button: an "Open on Mac" button + `/ft open` hint. Only useful when a process polling this bot's updates
    # handles callback data "ft:open:<id>" (by running `followthrough open-terminal <id>`); nothing here does.
    # snooze_buttons: e.g. ["1h", "3h", "morning"] - one button each, callback data "ft:snooze:<id>:<for>", for the
    # same process to run `followthrough snooze <id> --for <for>`.
    "telegram": {"enabled": False, "env_file": "", "token_key": "BOT_TOKEN", "chat_id": "", "open_button": False,
                 "snooze_buttons": []},
}


def home():
    return os.environ.get("FOLLOWTHROUGH_HOME", os.path.expanduser("~/.followthrough"))


def path(*parts):
    return os.path.join(home(), *parts)


def data_path(*parts):
    return os.path.join(home(), "data", *parts)


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def ignored(cfg, cwd):
    """True for checker sessions started by followthrough and for sessions under an ignored folder."""
    if os.environ.get("FOLLOWTHROUGH_ROLE") == "checker":
        return True
    real = os.path.realpath(cwd or ".")
    for p in cfg.get("ignore_paths", []):
        base = os.path.realpath(os.path.expanduser(p))
        if real == base or real.startswith(base + os.sep):
            return True
    return False


def load():
    p = path("config.toml")
    if not os.path.exists(p):
        return copy.deepcopy(DEFAULTS)
    with open(p, "rb") as fh:
        return _merge(DEFAULTS, tomllib.load(fh))
