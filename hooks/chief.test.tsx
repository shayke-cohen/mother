import { expect, mock, test } from 'claude-code/testing'
import { chiefStatusText } from './chief'

const WS = '/work/ws'
const NOW = Date.parse('2026-11-02T10:00:00Z')

function worker(over: Record<string, unknown> = {}) {
  return {
    key: 'PLAT-1', title: 'Fix the login bug', status: 'idle', held: false, running: false,
    progress: 'patched the handler, tests next', cost: 0.42, runs: 2, last_run_at: '2026-11-02T09:55:00Z',
    session_id: 'sess-1', queued: 0, resume: 'claude --resume sess-1', ...over,
  }
}

function status(over: Record<string, unknown> = {}) {
  return {
    version: 1, at: '2026-11-02T10:00:00Z', paused: false,
    chief: { alive: true, heartbeat_at: '2026-11-02T09:59:40Z', session_id: 'chief-1' },
    budget: { spent: 1.5, limit: 20 },
    workers: [worker()],
    ...over,
  }
}

type Cli = { exitCode: number; stdout?: string; stderr?: string }

// A fake host: `mother path` answers the workspace, `mother chief <verb>` what `chief` says, the rest exits 0.
async function boot($: any, on: any, role: string, chief: (argv: string[]) => Cli | undefined = () => undefined) {
  const clock = mock.clock(on, { now: NOW })
  mock.env(on, { HOME: '/home/m' })
  const out = { argv: [] as string[][], submits: [] as string[], toasts: [] as string[], status: [] as (string | undefined)[], opened: [] as string[] }
  on('store.get', () => ({ value: undefined }))
  on('store.set', () => ({ value: undefined }))
  on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
  on('ui.status', (_: any, e: any) => { out.status.push(e.text); return { value: undefined } })
  on('ui.toast', (_: any, e: any) => { out.toasts.push(e.text); return { value: undefined } })
  on('ui.open', (_: any, e: any) => { out.opened.push(e.id); return { value: { isPlaced: true } } })
  on('command.register', (_: any, e: any) => ({ value: { command: e.name } }))
  on('prompt.submit', (_: any, e: any, next: any) => { out.submits.push(e.text); return next(e) })
  on('fs.stat', () => ({ deny: 'ENOENT' }))
  on('fs.read', () => ({ deny: 'ENOENT' }))
  on('process.run', (_: any, e: any) => {
    const argv = [...e.argv] as string[]
    out.argv.push(argv)
    const ok = (stdout = '') => ({ value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
    if (argv[2] === 'path') return ok(WS + '\n')
    if (argv[2] === 'chief') {
      const r = chief(argv.slice(3))
      if (r) return { value: { exitCode: r.exitCode, stdout: r.stdout ?? '', stderr: r.stderr ?? '', isStdoutTruncated: false, isStderrTruncated: false } }
      if (argv[3] === 'role') return ok(role + '\n')
      if (argv[3] === 'status') return ok(JSON.stringify(status()))
      if (argv[3] === 'tick') return ok(JSON.stringify({ paused: false, decided: [], wake: null, judge: [], dispatched: [], reconciled: [] }))
    }
    return ok()
  })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  return { clock, out }
}

const chiefCalls = (out: { argv: string[][] }, verb: string) => out.argv.filter(a => a[2] === 'chief' && a[3] === verb)

const PANE = {
  plugin: 'mother', component: 'Pane', requestId: 'mother-chief',
  props: { title: 'Chief', isFocused: true, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} },
} as const
const SURFACES = ['terminal', 'desktop'] as const
const texts = async (ui: any) => (await ui.findAll({ type: 'Text' })).map((t: any) => String(t.text))
const hasText = async (ui: any, re: RegExp) => (await texts(ui)).some((t: string) => re.test(t))

test('test_console_never_ticks', async ($, on) => {
  const { clock, out } = await boot($, on, 'console')
  const before = chiefCalls(out, 'status').length
  expect(before).toBeGreaterThan(0)                  // session.start loaded the status
  await clock.advance(60_000)
  await clock.advance(60_000)
  expect(chiefCalls(out, 'tick')).toEqual([])
  expect(out.submits).toEqual([])
  expect(chiefCalls(out, 'status').length).toBeGreaterThan(before)   // the console still refreshes
})

test('test_chief_wakes_on_wake_text', async ($, on) => {
  const wake = 'Chief wake: 2 inbox lines need a decision. Follow routines/chief.md.'
  const { clock, out } = await boot($, on, 'chief', argv =>
    argv[0] === 'tick' ? { exitCode: 0, stdout: JSON.stringify({ paused: false, wake, judge: ['L1', 'L2'] }) } : undefined)
  expect(out.submits).toEqual([])
  await clock.advance(30_000)
  expect(chiefCalls(out, 'tick').map(a => a.slice(4))).toEqual([['--json']])
  expect(out.submits).toEqual([wake])
})

test('test_chief_no_wake_when_null', async ($, on) => {
  const { clock, out } = await boot($, on, 'chief')
  await clock.advance(30_000)
  await clock.advance(30_000)
  expect(chiefCalls(out, 'tick').length).toBe(2)
  expect(out.submits).toEqual([])
})

test('test_pane_lists_workers_on_terminal_and_desktop', async ($, on) => {
  await boot($, on, 'console')
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /Fix the login bug/)).toBe(true)
    expect(await hasText(ui, /patched the handler, tests next/)).toBe(true)
    expect(await ui.find({ key: 'say:PLAT-1' })).toBeDefined()
    expect((await ui.find({ key: 'hold:PLAT-1' }))?.text).toBe('Take over')
    await ui.unmount()
  }
})

test('test_chat_passes_text_after_dashdash', async ($, on) => {
  const { out } = await boot($, on, 'console')
  for (const surface of SURFACES) {
    out.argv.length = 0
    const ui = await $.ui.mount({ ...PANE, surface })
    await ui.input({ key: 'say:PLAT-1', text: '--help' })
    const say = chiefCalls(out, 'say')
    expect(say.length).toBe(1)
    expect(say[0].slice(2)).toEqual(['chief', 'say', 'PLAT-1', '--', '--help'])
    expect(say[0][0]).toBe('python3')
    expect(chiefCalls(out, 'status').length).toBeGreaterThan(0)   // the pane reloads after a say
    await ui.unmount()
  }
})

test('test_take_over_and_hand_back', async ($, on) => {
  let held = false
  const { out } = await boot($, on, 'console', argv => {
    if (argv[0] === 'hold') { held = true; return { exitCode: 0, stdout: 'held PLAT-1\n' } }
    if (argv[0] === 'release') { held = false; return { exitCode: 0, stdout: 'released PLAT-1\n' } }
    if (argv[0] === 'status') return { exitCode: 0, stdout: JSON.stringify(status({ workers: [worker({ held })] })) }
    return undefined
  })
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...PANE, surface })
    await ui.press({ key: 'hold:PLAT-1' })
    expect(chiefCalls(out, 'hold').at(-1)?.slice(3)).toEqual(['hold', 'PLAT-1'])
    expect((await ui.find({ key: 'release:PLAT-1' }))?.text).toBe('Hand back')
    expect(await ui.find({ key: 'hold:PLAT-1' })).toBeUndefined()
    expect(await hasText(ui, /claude --resume sess-1/)).toBe(true)
    await ui.press({ key: 'release:PLAT-1' })
    expect(chiefCalls(out, 'release').at(-1)?.slice(3)).toEqual(['release', 'PLAT-1'])
    expect((await ui.find({ key: 'hold:PLAT-1' }))?.text).toBe('Take over')
    await ui.unmount()
  }
})

test('test_takeover_hides_resume_while_running', async ($, on) => {
  await boot($, on, 'console', argv =>
    argv[0] === 'status' ? { exitCode: 0, stdout: JSON.stringify(status({ workers: [worker({ held: true, running: true, status: 'working' })] })) } : undefined)
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /finishing a turn — wait to resume/)).toBe(true)
    expect(await hasText(ui, /claude --resume/)).toBe(false)
    expect((await ui.find({ key: 'release:PLAT-1' }))?.text).toBe('Hand back')
    await ui.unmount()
  }
})

test('test_status_text_alive_and_down', async () => {
  const s: any = status({ workers: [worker({ status: 'working', queued: 1 }), worker({ key: 'PLAT-2', status: 'idle', queued: 2 })] })
  expect(chiefStatusText(s, 'chief')).toBe(' · chief ● 1 working · 3 queued')
  const down: any = { ...s, chief: { ...s.chief, alive: false } }
  expect(chiefStatusText(down, 'console')).toBe(' · chief ○ 1 working · 3 queued')
  expect(chiefStatusText(null, null)).toBe('')
})

test('test_status_line_carries_the_chief', async ($, on) => {
  const { out } = await boot($, on, 'console')
  expect(out.status.at(-1)).toBe('◆ no tick here · chief ● 0 working · 0 queued')
})

test('test_cards_sorted_by_attention', async ($, on) => {
  const ws = [
    worker({ key: 'A', title: 'Idle new', status: 'idle', last_run_at: '2026-11-02T09:59:00Z' }),
    worker({ key: 'B', title: 'Working', status: 'working', running: true }),
    worker({ key: 'C', title: 'Held', status: 'idle', held: true }),
    worker({ key: 'D', title: 'Failed', status: 'failed' }),
    worker({ key: 'E', title: 'Idle old', status: 'idle', last_run_at: '2026-11-01T09:00:00Z' }),
  ]
  await boot($, on, 'console', argv => argv[0] === 'status' ? { exitCode: 0, stdout: JSON.stringify(status({ workers: ws })) } : undefined)
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  const keys = (await ui.findAll({ type: 'Input' })).map((i: any) => String(i.key))
  expect(keys).toEqual(['say:D', 'say:C', 'say:B', 'say:A', 'say:E'])
  await ui.unmount()
})

test('test_chief_command_open_status_pause', async ($, on) => {
  const { out } = await boot($, on, 'console', argv =>
    argv[0] === 'status' && !argv.includes('--json') ? { exitCode: 0, stdout: 'chief ● alive\nworkers: 1\n' } : undefined)
  const opened: any = await $.command.run({ command: 'chief', args: '' } as any)
  expect(out.opened).toContain('mother-chief')
  expect(opened.text).toMatch(/Chief pane opened/)
  const st: any = await $.command.run({ command: 'chief', args: 'status' } as any)
  expect(st.text).toBe('chief ● alive\nworkers: 1')
  await $.command.run({ command: 'chief', args: 'pause' } as any)
  await $.command.run({ command: 'chief', args: 'resume' } as any)
  expect(out.argv.filter(a => a[2] === 'chief' && ['pause', 'resume'].includes(a[3])).map(a => a.slice(3))).toEqual([['pause'], ['resume']])
})
