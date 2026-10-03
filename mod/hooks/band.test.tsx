import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

import { SHIP, isShip, parseStatus } from './register'

const NOW = Date.parse('2026-10-02T18:00:00Z')
const HOUR = 3600000
const iso = (ms: number) => new Date(ms).toISOString()
const VOID = { value: undefined } as never

const STATUS = JSON.stringify([
  { id: 'ft-due', title: 'Checkout p95 latency after the cache fix', status: 'active', snoozed_until: '', checkpoint: { state: 'needs_human', due_at: iso(NOW - 3 * HOUR) } },
  { id: 'ft-soon', title: 'Signup emails after the DNS change', status: 'active', snoozed_until: '', checkpoint: { state: 'pending', due_at: iso(NOW + 5 * HOUR) } },
  { id: 'ft-snoozed', title: 'Snoozed one', status: 'active', snoozed_until: iso(NOW + HOUR), checkpoint: { state: 'needs_human', due_at: iso(NOW - HOUR) } },
  { id: 'ft-later', title: 'Next week', status: 'active', snoozed_until: '', checkpoint: { state: 'pending', due_at: iso(NOW + 100 * HOUR) } },
])

function world(on: On, opts: { missingCli?: boolean; dropSubmit?: boolean; noLocalBin?: boolean } = {}) {
  const clock = mock.clock(on, { now: NOW })
  mock.env(on, { HOME: '/home/test' })
  const runs: string[][] = []
  const submitted: string[] = []
  on('process.run', async ($, e) => {
    runs.push([...e.argv])
    if (opts.missingCli) throw new Error('ENOENT: followthrough')
    const stdout = e.argv[1] === 'status' ? STATUS : 'ok'
    return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } } as never
  })
  on('session.root', async () => ({ value: '/repo' }) as never)
  on('fs.exists', async ($, e) => ({ value: !opts.noLocalBin && e.path === '/home/test/.local/bin/followthrough' }) as never)
  on('session.surfaces', async () => ({ value: ['terminal'] }) as never)
  on('command.register', async ($, e) => ({ value: { command: e.name } }) as never)
  on('ui.toast', async () => VOID)
  on('prompt.submit', async ($, e) => {
    if (opts.dropSubmit) return { drop: 'blocked by a test' }
    submitted.push(e.text)
    return { text: e.text }
  })
  on('ui.render', async () => ({ type: 'Box', props: {}, children: [] }) as never)
  on('tool.call', async ($, e) =>
    ({ result: { stdout: '', stderr: '', interrupted: false, ...(String((e as { command?: string }).command).includes('&') ? { backgroundTaskId: 'bg1' } : {}) } }) as never)
  on('session.start', async () => ({ cwd: '/repo' }) as never)
  return { runs, submitted, clock }
}

const BAND = { component: 'AbovePrompt', props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 140 } as never } as const
const mount = ($: Engine) => $.ui.mount({ plugin: 'followthrough-band', surface: 'terminal', ...BAND })
async function start($: Engine, w: { clock: { settle: () => Promise<void> } }) {
  await $.session.start({ source: 'startup', cwd: '/repo', surface: 'terminal', isInteractive: true } as never)
  await w.clock.settle()
}
const bash = ($: Engine, command: string) => $.tool.call({ tool: 'Bash', tool_use_id: `t-${command.length}`, command } as never)

test('the ship pattern catches deploys and merges, not reads', () => {
  for (const c of ['gh pr merge 149 --squash', 'gcloud run deploy api --image x', 'fly deploy -a web', 'git push origin main', 'vercel deploy --prod'])
    expect(SHIP.test(c)).toBe(true)
  for (const c of ['gh pr view 149', 'gcloud run services describe api', 'git push origin feature/x', 'fly status'])
    expect(SHIP.test(c)).toBe(false)
  expect(isShip('echo "fly deploy -a prod"')).toBe(false)
  expect(isShip("git commit -m 'gh pr merge later'")).toBe(false)
  expect(isShip('fly deploy --dry-run')).toBe(false)
  expect(isShip('cd app && fly deploy -a web')).toBe(true)
})

test('odd ledger output yields no claims and never throws', () => {
  expect(parseStatus('[null, 3, "x", {"id": 1}, {"id": "a"}, {"id": "b", "checkpoint": {"due_at": "nope"}}]', NOW)).toEqual([])
  expect(parseStatus('not json', NOW)).toEqual([])
  expect(parseStatus('{"id": "a"}', NOW)).toEqual([])
})

test('a missing followthrough CLI leaves an empty band and a working nudge', async ($, on) => {
  const w = world(on, { missingCli: true })
  await start($, w)
  await bash($, 'gh pr merge 7 --squash')
  const ui = await mount($)
  expect(await ui.find({ type: 'Text', text: /due here/ })).toBeUndefined()
  expect((await ui.find({ type: 'Text', text: /Shipped/ }))?.text).toContain('gh pr merge 7')
})

test('a deploy sent to the background is not counted until it finishes', async ($, on) => {
  const w = world(on)
  await start($, w)
  await bash($, 'fly deploy -a web &')
  const ui = await mount($)
  expect(await ui.find({ type: 'Text', text: /Shipped/ })).toBeUndefined()
})

test('a dropped Register prompt keeps the nudge', async ($, on) => {
  const w = world(on, { dropSubmit: true })
  await start($, w)
  await bash($, 'gh pr merge 9')
  const ui = await mount($)
  await ui.press({ key: 'ship:register' })
  await ui.unmount()
  expect((await (await mount($)).find({ type: 'Text', text: /Shipped/ }))?.text).toContain('gh pr merge 9')
})

test('the band lists due claims for this repo and skips snoozed and later ones', async ($, on) => {
  const w = world(on)
  await start($, w)
  expect(w.runs[0]).toEqual(['/home/test/.local/bin/followthrough', 'status', '--json', '--repo', '/repo'])
  const ui = await mount($)
  expect((await ui.find({ type: 'Text', text: /due here/ }))?.text).toBe('followthrough · 1 due here · 1 more in 24h')
  expect((await ui.find({ type: 'Text', text: /Checkout p95/ }))?.text).toBe('overdue 3h · Checkout p95 latency after the cache fix')
  expect(await ui.find({ type: 'Text', text: /Snoozed one/ })).toBeUndefined()
  await ui.press({ key: 'run:ft-due' })
  expect(w.submitted.at(-1)).toContain('[followthrough claim ft-due]')
  expect(w.submitted.at(-1)).toContain('1) Run: followthrough start ft-due')
})

test('Close abandons only on the second press', async ($, on) => {
  const w = world(on)
  await start($, w)
  const ui = await mount($)
  await ui.press({ key: 'close:ft-due' })
  expect(w.runs.some(r => r[1] === 'abandon')).toBe(false)
  expect((await ui.find({ key: 'close:ft-due' }))?.text).toContain('Confirm close')
  await ui.press({ key: 'close:ft-due' })
  expect(w.runs.find(r => r[1] === 'abandon')?.slice(1, 3)).toEqual(['abandon', 'ft-due'])
})

test('a merge with no followthrough raises the nudge, and registering clears it', async ($, on) => {
  const w = world(on)
  await start($, w)
  await bash($, 'gh pr merge 149 --squash')
  const ui = await mount($)
  expect((await ui.find({ type: 'Text', text: /Shipped/ }))?.text).toContain('gh pr merge 149')
  await ui.press({ key: 'ship:register' })
  expect(w.submitted.at(-1)).toContain('`gh pr merge 149 --squash`')
  await ui.unmount()
  const again = await mount($)
  expect(await again.find({ type: 'Text', text: /Shipped/ })).toBeUndefined()
})

test('a followthrough add in the same session clears the nudge', async ($, on) => {
  const w = world(on)
  await start($, w)
  await bash($, 'fly deploy -a web-api')
  await bash($, 'followthrough add --title "deploy check" --due +6h')
  const ui = await mount($)
  expect(await ui.find({ type: 'Text', text: /Shipped/ })).toBeUndefined()
})

test('without ~/.local/bin/followthrough the band runs the followthrough on the PATH', async ($, on) => {
  const w = world(on, { noLocalBin: true })
  await start($, w)
  expect(w.runs[0]?.[0]).toBe('followthrough')
})
