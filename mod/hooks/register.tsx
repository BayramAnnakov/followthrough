import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Claim, Shipped } from '../types'

const claims = atom({ plugin: 'followthrough-band', key: 'claims' } as const, [])
const shipped = atom({ plugin: 'followthrough-band', key: 'shipped' } as const, [])
const isHidden = atom({ plugin: 'followthrough-band', key: 'isHidden' } as const, false)
const confirmClose = atom({ plugin: 'followthrough-band', key: 'confirmClose' } as const, null)

const REFRESH_MS = 5 * 60 * 1000
const MIN_GAP_MS = 60 * 1000
const DAY_MS = 24 * 60 * 60 * 1000
const MAX_ROWS = 3

/** Commands that ship a change somewhere other people see it. */
export const SHIP =
  /\b(gcloud\s+run\s+(deploy|services\s+update(-traffic)?)|gcloud\s+builds\s+submit|fly(ctl)?\s+deploy|gh\s+pr\s+merge|vercel\b[^|;&]*--prod|wrangler\s+deploy|firebase\s+deploy|npm\s+publish|pnpm\s+publish|terraform\s+apply|git\s+push\b[^|;&]*\b(main|master)\b)/
const DRY = /--dry-run|--help|\s-h\b/
const REGISTERED = /\bfollowthrough\s+(add|expect)\b/

let lastFetch = 0
let isFetching = false

/** The command with its quoted strings blanked: `echo "fly deploy"` names a deploy, it does not run one. */
export const unquoted = (command: string) => command.replace(/'[^']*'|"(?:[^"\\]|\\.)*"/g, '""')

export const isShip = (command: string) => {
  const bare = unquoted(command)
  return SHIP.test(bare) && !DRY.test(bare)
}

/** The CLI where `uv tool install` puts it, else whatever `followthrough` the PATH finds. */
async function bin($: EngineInterface) {
  const home = await $.env.get('HOME')
  const local = home === undefined ? undefined : `${home}/.local/bin/followthrough`
  return local !== undefined && (await $.fs.exists(local)) ? local : 'followthrough'
}

export function parseStatus(stdout: string, now: number): Claim[] {
  let rows: unknown
  try {
    rows = JSON.parse(stdout)
  } catch {
    return []
  }
  if (!Array.isArray(rows)) return []
  const out: Claim[] = []
  for (const row of rows as unknown[]) {
    if (typeof row !== 'object' || row === null || Array.isArray(row)) continue
    const r = row as Record<string, unknown>
    if (typeof r.id !== 'string' || typeof r.checkpoint !== 'object' || r.checkpoint === null) continue
    const cp = r.checkpoint as Record<string, unknown>
    if (typeof cp.due_at !== 'string' || Number.isNaN(Date.parse(cp.due_at))) continue
    const snoozed = typeof r.snoozed_until === 'string' && r.snoozed_until !== '' ? Date.parse(r.snoozed_until) : 0
    if (r.status !== 'active' || snoozed > now) continue
    out.push({
      id: r.id as string,
      title: typeof r.title === 'string' ? r.title : (r.id as string),
      state: String(cp.state ?? ''),
      dueAt: String(cp.due_at ?? ''),
    })
  }
  return out.sort((a, b) => Date.parse(a.dueAt) - Date.parse(b.dueAt))
}

export const isDue = (c: Claim, now: number) => c.state === 'needs_human' || Date.parse(c.dueAt) <= now

export function dueLabel(c: Claim, now: number) {
  const ms = now - Date.parse(c.dueAt)
  if (Number.isNaN(ms)) return 'due'
  if (ms < 0) return 'needs you'
  const h = Math.floor(ms / 3600000)
  return h < 1 ? 'due now' : h < 48 ? `overdue ${h}h` : `overdue ${Math.floor(h / 24)}d`
}

/** Runs the followthrough CLI; undefined when it is missing, fails to start, or times out. */
async function ft($: EngineInterface, args: string[]) {
  try {
    return await $.process.run([await bin($), ...args], { timeoutMs: 15000 })
  } catch {
    return undefined
  }
}

/** One refresh at a time; a missing or failing CLI leaves the band as it was. */
async function refresh($: EngineInterface) {
  if (isFetching) return
  isFetching = true
  try {
    lastFetch = await $.clock.now()
    const ran = await ft($, ['status', '--json', '--repo', await $.session.root()])
    if (ran === undefined || ran.exitCode !== 0) return
    await update($, claims, () => parseStatus(ran.stdout, lastFetch))
  } finally {
    isFetching = false
  }
}

async function runCheck($: EngineInterface, c: Claim) {
  await $.prompt.submit({
    text: [
      `[followthrough claim ${c.id}] Run this check now, in this session.`,
      `1) Run: followthrough start ${c.id} - it prints OK <attempt-id>. If it prints TAKEN or CLOSED, stop and tell me exactly what it printed.`,
      `2) Read the runbook with followthrough show ${c.id} and do the check. It is read-only: no deploys, sends, publishes or setting changes.`,
      `3) Record the result, always: followthrough resolve ${c.id} --attempt <attempt-id> --verdict worked|failed|partial|inconclusive --summary "<measured value vs expected, source, window>" (not_settled with --retry-at when the data is not in yet).`,
    ].join('\n'),
  })
}

async function snooze($: EngineInterface, c: Claim) {
  const ran = await ft($, ['snooze', c.id, '--for', '1d'])
  $.ui.toast(ran?.exitCode === 0 ? `Snoozed ${c.id} for a day` : `snooze failed: ${(ran?.stderr ?? 'followthrough did not run').trim().slice(0, 120)}`)
  await refresh($)
}

/** Close takes two presses: the first arms it, the second abandons the claim. */
async function close($: EngineInterface, c: Claim) {
  if ((await read($, confirmClose)) !== c.id) {
    await update($, confirmClose, () => c.id)
    return
  }
  await update($, confirmClose, () => null)
  const ran = await ft($, ['abandon', c.id, '--reason', 'closed from the Claude Code band without running it'])
  $.ui.toast(ran?.exitCode === 0 ? `Closed ${c.id}` : `close failed: ${(ran?.stderr ?? 'followthrough did not run').trim().slice(0, 120)}`)
  await refresh($)
}

async function registerShipped($: EngineInterface, list: Shipped[]) {
  await update($, shipped, () => [])
  const commands = list.map(s => `- \`${s.command.slice(0, 160)}\``).join('\n')
  const sent = await $.prompt.submit({
    text:
      'This session shipped these without a followthrough check:\n' +
      commands +
      '\n\nRegister followthrough checks for them with the followthrough skill: the expected effect written before the data exists, a live check, and when to look. Skip any that need no check and say why.',
  })
  if (sent.drop !== undefined) {
    // The prompt did not enter: keep the nudge so the person can try again.
    await update($, shipped, current => [...list, ...current].slice(-5))
    $.ui.toast(`followthrough-band: the prompt was dropped: ${sent.drop}`)
  }
}

async function dismissShipped($: EngineInterface) {
  await update($, shipped, () => [])
}

async function toggle($: EngineInterface) {
  await update($, isHidden, hidden => !hidden)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'ft', description: 'Show or hide the followthrough band, and refresh it' })
    // Headless runs (claude -p, graders) draw nothing: no timers, no polling.
    if (e.isInteractive) {
      // Never hold up the first prompt on the CLI: poll in the background, timer first.
      $.clock.every(REFRESH_MS, () => void refresh($).catch(() => undefined))
      void refresh($).catch(() => undefined)
    }
    return next(e)
  })

  on('command.run', { command: 'ft' }, async $ => {
    await toggle($)
    await refresh($)
    const list = await read($, claims)
    const now = await $.clock.now()
    const due = list.filter(c => isDue(c, now)).length
    return { text: `band ${(await read($, isHidden)) ? 'hidden (/ft shows it)' : 'shown'} · ${due} due here · ${list.length} open` }
  })

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const ran = await next(e)
    if (ran.deny !== undefined || ran.isError === true) return ran
    // A command sent to the background has not finished: it shipped nothing yet.
    if (ran.result?.backgroundTaskId !== undefined) return ran
    const bare = unquoted(e.command)
    if (REGISTERED.test(bare)) {
      // A check registered: it covers what shipped before it in this session.
      await update($, shipped, () => [])
      void refresh($).catch(() => undefined)
    }
    if (isShip(e.command) && !REGISTERED.test(bare)) {
      const at = await $.clock.now()
      await update($, shipped, list => [...list, { command: e.command.trim(), at }].slice(-5))
    }
    return ran
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined && (await $.clock.now()) - lastFetch > MIN_GAP_MS) await refresh($)
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || (await read($, isHidden))) return next(e)
    const now = await $.clock.now()
    const list = await read($, claims)
    const ship = await read($, shipped)
    const arming = await read($, confirmClose)
    const due = list.filter(c => isDue(c, now))
    const soon = list.filter(c => !isDue(c, now) && Date.parse(c.dueAt) - now < DAY_MS).length
    if (due.length === 0 && ship.length === 0) return next(e)

    const { Box, Button, Text } = $.ui.resolve(e)
    const width = Math.max(20, e.props.bodyColumns - 44)
    const below = await next(e)
    return (
      <Box flexDirection="column">
        {due.length > 0 && (
          <Text dimColor>
            followthrough · {due.length} due here{soon > 0 ? ` · ${soon} more in 24h` : ''}
            {due.length > MAX_ROWS ? ` · showing ${MAX_ROWS}` : ''}
          </Text>
        )}
        {due.slice(0, MAX_ROWS).map(c => (
          <Box key={`row:${c.id}`} flexDirection="row" gap={1}>
            <Text color={c.state === 'needs_human' ? 'yellow' : undefined} wrap="truncate-end">
              {`${dueLabel(c, now)} · ${c.title}`.slice(0, width)}
            </Text>
            <Button key={`run:${c.id}`} label="Run" onPress={() => runCheck($, c)} />
            <Button key={`snooze:${c.id}`} label="Snooze 1d" onPress={() => snooze($, c)} />
            <Button key={`close:${c.id}`} label={arming === c.id ? 'Confirm close' : 'Close'} onPress={() => close($, c)} />
          </Box>
        ))}
        {ship.length > 0 && (
          <Box key="shipped" flexDirection="row" gap={1}>
            <Text color="cyan" wrap="truncate-end">
              {`Shipped without a followthrough: ${ship.map(s => s.command.split(/\s+/).slice(0, 4).join(' ')).join(' · ')}`.slice(0, width)}
            </Text>
            <Button key="ship:register" variant="primary" label="Register checks" onPress={() => registerShipped($, ship)} />
            <Button key="ship:dismiss" label="Not needed" onPress={() => dismissShipped($)} />
          </Box>
        )}
        {below}
      </Box>
    )
  })
}
