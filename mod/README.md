# followthrough-band

A [Claude Code mod](https://claude.dev/blog/getting-started-with-claude-code-mods/) that shows followthrough inside the
session: the checks due for the repo you are in sit above the prompt, and a session that ships something without
registering a check gets a nudge.

![followthrough-band: two overdue checks above the prompt](../docs/followthrough-band.png)

## What it shows

- **Due checks for this repo** (`followthrough status --json --repo <session root>`, refreshed every 5 minutes and after
  each turn): up to three rows, overdue or "needs you" first, each with
  - **Run** - runs the check in this session: `start` first (stops on `TAKEN`/`CLOSED`), the runbook from `show`, a
    read-only check, then `resolve` with a measured value;
  - **Snooze 1d** - `followthrough snooze <id> --for 1d`;
  - **Close** - asks for a second press, then `followthrough abandon <id> --reason ...`.
- **"Shipped without a followthrough"** - after a successful `gh pr merge`, `gcloud run deploy`, `fly deploy`,
  `git push ... main`, `vercel --prod`, `wrangler deploy`, `npm publish`, `terraform apply` or similar in this session
  with no `followthrough add` after it, a row offers **Register checks** (asks the agent to register them with the
  followthrough skill) or **Not needed**. Quoted text, dry runs and commands sent to the background do not count.

`/ft` hides or shows the band and reports the counts. Headless sessions (`claude -p`, SDK) run nothing.

## Install

The followthrough CLI must be installed first (see the [main README](../README.md#install)); the band calls
`~/.local/bin/followthrough`, else the `followthrough` on your PATH.

```bash
claude plugin marketplace add bayramannakov/followthrough
claude plugin install followthrough-band@followthrough
```

Or load it from a clone: `claude --plugin-dir <clone>/mod`. Needs Claude Code 2.1.286 or newer with mods enabled.
If no band ever appears, a Claude Code session older than 2.1.286 that is still running may have switched mods off
for everyone (it writes the flag into the shared `~/.claude.json`) - restart old sessions.

## Develop

```bash
claude plugin validate mod
claude plugin test mod        # 10 tests, mocked CLI - free
```
