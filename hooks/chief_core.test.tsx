import { expect, mock, test } from 'claude-code/testing'
import { EVENT_PRIORITY, EVENT_SHOW_MS, WARN_COOLDOWN_MS, changes, pickEvent, snapshotOf } from './events'
import type { Snapshot } from './events'
import { NOTIFIED_KEY, NOTIFY_OFF_KEY, NOTIFY_PER_PASS, chaseNotice, noticesOf, planNotices } from './notify'
import { accessRows, accessTitle } from './access'
import { changed, withBeat, NO_SIGS } from './fastloop'

// The infrastructure half of the chief batch: desktop notifications once per thing, the "what just changed" line,
// sign-in and network cards with Retry, and the pane's fast refresh. (The watcher's rate-limit fallback is tested in
// Python, tests/test_hub_sources.py.)

const WS = '/work/ws'
const NOW = Date.parse('2026-11-02T10:00:00Z')
const SELF = 'sess-1'
const CLI_A = '11111111-1111-4111-8111-111111111111'
const CLI_B = '22222222-2222-4222-8222-222222222222'
const PINNED_A = { session_id: 'local_a', cli_session_id: CLI_A, title: 'Fix the login bug', cwd: '/work/example-api',
  repo: 'example-api', last_activity_at: '2026-11-02T09:50:00Z', last_report: 'Merged the fix', open_link: null }
const PINNED_B = { ...PINNED_A, session_id: 'local_b', cli_session_id: CLI_B, title: 'Review the docs' }
const ASK = { asks: true, question: 'Ship now or wait for review?', waiting_on: 'the manager',
  options: [{ label: 'Ship', message: 'Ship it now.' }, { label: 'Wait', message: 'Wait for review.' }] }
const asking = (over: Record<string, unknown> = {}) => ({ session_id: CLI_A, ...ASK, at: '2026-11-02T09:50:00Z', ask_id: 'a1', ...over })
const item = (id: string, over: Record<string, unknown> = {}) => ({ id, key: 'C1/' + id, title: 'Item ' + id, why: '', link: '', at: '2026-11-02T09:00:00Z',
  links: [], kind: null, on: null, urgency: null, actions: [], ...over })
const urgent = (id: string, over: Record<string, unknown> = {}) => item(id, { kind: 'act', on: 'manager', urgency: 'now', who: 'Sam', ...over })

function status(over: Record<string, unknown> = {}) {
  return {
    version: 1, at: '2026-11-02T10:00:00Z', paused: false,
    chief: { alive: true, heartbeat_at: '2026-11-02T09:59:40Z' },
    budget: { spent: 1.5, limit: 20, unreadable: false, over: false },
    desktop: [PINNED_A, PINNED_B], led_by: SELF, inbox: [], inbox_count: 0, ...over,
  }
}

const SHOWN = [{ id: 'mother-chief', title: 'Chief', isShown: true, isFocused: false, isPlaced: true }]
const PANE = {
  plugin: 'mother', component: 'Pane', requestId: 'mother-chief', surface: 'desktop',
  props: { title: 'Chief', isFocused: true, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} },
} as const
const texts = async (ui: any) => (await ui.findAll({ type: 'Text' })).map((t: any) => String(t.text))
const hasText = async (ui: any, re: RegExp) => (await texts(ui)).some((t: string) => re.test(t))
// the event line: the dot's colour and the text after it, or null when the header has none (no other Text is a dot
// on the plain skin)
async function eventLine(ui: any): Promise<{ text: string; color: unknown } | null> {
  const all = await ui.findAll({ type: 'Text' })
  const i = all.findIndex((t: any) => String(t.text) === '\u25cf')
  return i < 0 ? null : { text: String(all[i + 1]?.text), color: all[i].props?.color }
}

type World = {
  status: () => any; cards: () => any[]; shown: boolean
  files: Record<string, { text: string; mtimeMs: number }>
  hub: (argv: string[]) => { exitCode: number; stdout?: string; stderr?: string } | undefined
}
type Out = { argv: string[][]; notes: { text: string; title?: string }[]; toasts: string[]; fsCalls: string[] }

// A fake host: `mother chief status` answers world.status(), `mother floor list` world.cards(), `mother hub …`
// world.hub(); files under the workspace are world.files; the store is `store` (kept across a reboot).
async function boot($: any, on: any, world: World, store: Record<string, unknown>, sessionId = SELF) {
  const clock = mock.clock(on, { now: NOW })
  mock.env(on, { HOME: '/home/m' })
  const out: Out = { argv: [], notes: [], toasts: [], fsCalls: [] }
  on('store.get', (_: any, e: any) => ({ value: e.key === 'chiefSkin' ? store[e.key] ?? 'plain' : store[e.key] }))
  on('store.set', (_: any, e: any) => { store[e.key] = e.value; return { value: undefined } })
  on('store.delete', (_: any, e: any) => { delete store[e.key]; return { value: undefined } })
  on('store.keys', () => ({ value: Object.keys(store) }))
  on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: sessionId }))
  on('session.surfaces', () => ({ value: ['desktop'] }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', (_: any, e: any) => { out.toasts.push(e.text); return { value: undefined } })
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.panes', () => ({ value: world.shown ? SHOWN : [] }))
  on('ui.notify', (_: any, e: any) => { out.notes.push({ text: e.text, title: e.title }); return { value: { isSent: true, channel: 'terminal_bell' } } })
  on('command.register', (_: any, e: any) => ({ value: { command: e.name } }))
  on('mcp.call', () => ({ deny: 'no connected MCP tool' }))
  on('model.complete', () => ({ value: { isAnswered: false, reason: 'api-error', status: 529, error: 'off', usage: { input_tokens: 0, output_tokens: 0 } } }))
  const file = (path: string) => world.files[path.startsWith(WS + '/') ? path.slice(WS.length + 1) : path]
  on('fs.stat', (_: any, e: any) => {
    out.fsCalls.push('stat:' + e.path)
    const f = file(e.path)
    return f ? { value: { kind: 'file', size: f.text.length, mtimeMs: f.mtimeMs, isLink: false } } : { deny: 'ENOENT' }
  })
  on('fs.list', (_: any, e: any) => {
    out.fsCalls.push('list:' + e.path)
    const dir = String(e.path).slice(WS.length + 1) + '/'
    const rows = Object.entries(world.files).filter(([p]) => p.startsWith(dir) && !p.slice(dir.length).includes('/'))
      .map(([p, f]) => ({ name: p.slice(dir.length), kind: 'file', size: f.text.length, mtimeMs: f.mtimeMs, isLink: false }))
    return { value: rows }
  })
  on('fs.read', (_: any, e: any) => { out.fsCalls.push('read:' + e.path); const f = file(e.path); return f ? { value: f.text } : { deny: 'ENOENT' } })
  on('fs.exists', (_: any, e: any) => ({ value: !!file(e.path) }))
  on('process.run', async (_: any, e: any) => {
    const argv = [...e.argv] as string[]
    out.argv.push(argv)
    const ok = (stdout = '', exitCode = 0, stderr = '') => ({ value: { exitCode, stdout, stderr, isStdoutTruncated: false, isStderrTruncated: false } })
    if (argv[2] === 'path') return ok(WS + '\n')
    if (argv[2] === 'floor') return ok(argv[3] === 'list' ? JSON.stringify({ version: 1, cards: world.cards() }) : '{}')
    if (argv[2] === 'hub') { const r = world.hub(argv.slice(3)); return r ? ok(r.stdout ?? '', r.exitCode, r.stderr ?? '') : ok() }
    if (argv[2] === 'chief' && argv[3] === 'status') return ok(JSON.stringify(world.status()))
    if (argv[2] === 'chief' && argv[3] === 'tick') return ok('{"skipped": "test"}')
    if (argv[2] === 'chief' && argv[3] === 'tail') return ok(JSON.stringify({ version: 1, model: 'sonnet', sessions: [] }))
    return ok()
  })
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await clock.settle()
  await $.command.run({ command: 'chief', args: 'tab all' } as any)
  return { clock, out }
}

const world = (over: Partial<World> = {}): World => ({
  status: () => status(), cards: () => [], shown: false, files: {}, hub: () => undefined, ...over,
})

// ---- 2. what just changed: priority, cooldown, no replay ----
const snap = (over: Partial<Snapshot> = {}): Snapshot => ({ asks: {}, urgent: {}, inbox: {}, down: {}, sessions: {}, ...over })

test('test_events_first_read_only_seeds', async () => {
  expect(changes(null, snap({ asks: { a1: 'Fix the login bug' }, urgent: { U1: 'Pager' } }))).toEqual([])
})

test('test_events_priority_order_wins', async () => {
  const before = snap({ down: { mail: 'x' }, sessions: { c: { title: 'Docs', state: 'working' } } })
  const after = snap({ asks: { a1: 'Fix the login bug' }, urgent: { U1: 'Pager' }, inbox: { U2: 'News' }, down: { chat: '401' },
                       sessions: { c: { title: 'Docs', state: 'done' } } })
  const found = changes(before, after)
  expect(found.map(c => c.kind)).toEqual(['ask', 'urgent', 'down', 'session', 'inbox', 'recovered'])
  expect([...EVENT_PRIORITY]).toEqual(['ask', 'urgent', 'down', 'session', 'inbox', 'recovered'])
  const cool = new Map<string, number>()
  expect(pickEvent(null, found, NOW, cool)?.text).toBe('Fix the login bug asks you something')
  // each lower one alone, in turn, is picked when nothing better is there
  for (const kind of EVENT_PRIORITY) {
    const only = found.filter(c => c.kind === kind)
    expect(pickEvent(null, only, NOW, new Map())?.kind).toBe(kind)
  }
  expect(found.find(c => c.kind === 'session')?.text).toBe('Docs finished')
  expect(found.find(c => c.kind === 'recovered')?.text).toBe('mail is back')
})

test('test_events_lower_priority_waits_and_the_event_expires', async () => {
  const cool = new Map<string, number>()
  const ask = pickEvent(null, changes(snap(), snap({ asks: { a1: 'S' } })), NOW, cool)!
  expect(ask.kind).toBe('ask')
  const inboxLater = changes(snap(), snap({ inbox: { U9: 'News' } }))
  expect(pickEvent(ask, inboxLater, NOW + 60_000, cool)).toBe(ask)                       // lower: the question stays
  expect(pickEvent(ask, inboxLater, NOW + EVENT_SHOW_MS + 1, cool)?.kind).toBe('inbox')  // after 15 min it can go
  expect(pickEvent(ask, [], NOW + EVENT_SHOW_MS + 1, cool)).toBeNull()                    // and with nothing new, it ends
  const urgent2 = pickEvent(ask, changes(snap(), snap({ asks: { a2: 'T' } })), NOW + 1000, cool)!
  expect(urgent2.subject).toBe('a2')                                                      // equal priority: the newer replaces
})

test('test_events_warning_cooldown_per_subject', async () => {
  const cool = new Map<string, number>()
  const down = changes(snap(), snap({ down: { mail: '401' } }))
  expect(pickEvent(null, down, NOW, cool)?.kind).toBe('down')
  // the source comes back and goes down again 10 minutes later: still cooling, no new event for mail
  expect(pickEvent(null, down, NOW + 10 * 60_000, cool)).toBeNull()
  const other = changes(snap(), snap({ down: { chat: 'VPN' } }))
  expect(pickEvent(null, other, NOW + 10 * 60_000, cool)?.subject).toBe('chat')           // another subject is not cooling
  expect(pickEvent(null, down, NOW + WARN_COOLDOWN_MS, cool)?.subject).toBe('mail')        // 30 minutes on, it may speak again
  // a stuck session is a warning too; finished is not
  const stuck = changes(snap({ sessions: { c: { title: 'Docs', state: 'working' } } }), snap({ sessions: { c: { title: 'Docs', state: 'stuck' } } }))
  expect(stuck[0]).toEqual({ kind: 'session', subject: 'c', text: 'Docs looks stuck', warn: true })
})

test('test_events_snapshot_reads_status_and_floor', async () => {
  const s: any = status({ inbox: [item('U1'), urgent('U2'), item('W1', { key: 'watcher:mail' })],
                          health: { watcher: { at: 'x', sources: { mail: { status: 'couldnt_check', reason: '401' }, chat: { status: 'ok' } } } } })
  const floor: any = { cards: [asking()], self: SELF, answered: [] }
  const got = snapshotOf(s, floor, { [CLI_B]: 'done' })
  expect(got.asks).toEqual({ a1: 'Fix the login bug' })
  expect(got.urgent).toEqual({ U2: 'Item U2' })
  expect(got.inbox).toEqual({ U1: 'Item U1' })                  // a watcher error is health, not inbox
  expect(got.down).toEqual({ mail: '401' })
  expect(got.sessions[CLI_B]).toEqual({ title: 'Review the docs', state: 'done' })
})

test('test_event_line_shows_under_the_mood_and_never_replays_after_a_reload', async ($, on) => {
  let inbox: any[] = [item('U1')]
  const w = world({ status: () => status({ inbox, inbox_count: inbox.length }) })
  const store: Record<string, unknown> = {}
  const { clock } = await boot($, on, w, store)
  let ui = await $.ui.mount(PANE)
  expect(await eventLine(ui)).toBeNull()                         // the first read seeds: U1 was already there
  await ui.unmount()
  inbox = [urgent('U2', { title: 'Prod is down' }), item('U1')]
  await clock.advance(60_000)
  ui = await $.ui.mount(PANE)
  expect(await eventLine(ui)).toEqual({ text: 'Urgent: Prod is down', color: 'error' })
  await ui.unmount()
  await clock.advance(EVENT_SHOW_MS + 60_000)                   // fifteen minutes on, the line is gone
  ui = await $.ui.mount(PANE)
  expect(await eventLine(ui)).toBeNull()
  await ui.unmount()
  // a reload: the same status (an urgent item and U1) is read again as a first read, and nothing is replayed
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await clock.settle()
  await clock.advance(60_000)
  ui = await $.ui.mount(PANE)
  expect(await eventLine(ui)).toBeNull()
  await ui.unmount()
})

// ---- 1. notifications once per thing ----
test('test_plan_notices_seeds_quiet_hours_and_cap', async () => {
  const n = (key: string, isUrgent = false) => ({ key, text: key, urgent: isUrgent })
  // a store that never held the key: everything there is remembered, nothing raised
  let plan = planNotices([n('ask:a'), n('urgent:U1', true)], undefined, false, NOW)
  expect(plan.raise).toEqual([])
  expect(Object.keys(plan.kept).sort()).toEqual(['ask:a', 'urgent:U1'])
  plan = planNotices([n('ask:a'), n('ask:b')], plan.kept, false, NOW)
  expect(plan.raise.map(r => r.key)).toEqual(['ask:b'])
  // quiet hours: only urgent ones; the rest is remembered as seen
  plan = planNotices([n('ask:c'), n('urgent:U2', true)], plan.kept, true, NOW)
  expect(plan.raise.map(r => r.key)).toEqual(['urgent:U2'])
  expect('ask:c' in plan.kept).toBe(true)
  // at most NOTIFY_PER_PASS a pass; the rest is not remembered, so it goes next pass
  const many = Array.from({ length: NOTIFY_PER_PASS + 2 }, (_, i) => n('ask:m' + i))
  plan = planNotices(many, plan.kept, false, NOW)
  expect(plan.raise.length).toBe(NOTIFY_PER_PASS)
  expect('ask:m' + (NOTIFY_PER_PASS + 1) in plan.kept).toBe(false)
  // an entry older than 14 days is forgotten
  expect('ask:a' in planNotices([], { 'ask:a': NOW - 15 * 86_400_000 }, false, NOW).kept).toBe(false)
})

test('test_notices_say_who_and_what_in_one_line', async () => {
  const s: any = status({ inbox: [urgent('U2', { title: 'Prod is down' }), item('U1')],
                          health: { watcher: { at: 'x', sources: { mail: { status: 'couldnt_check', reason: '401', since: '2026-11-02T09:00:00Z' } } },
                                    access: [{ source: 'mail', kind: 'signin', reason: '401', help: 'h' }] } })
  const got = noticesOf(s, { cards: [asking()], self: SELF, answered: [] } as any)
  expect(got.map(g => [g.key, g.text, g.urgent])).toEqual([
    ['ask:a1', 'Fix the login bug asks: Ship now or wait for review?', false],
    ['urgent:U2', 'Urgent from Sam: Prod is down', true],
    ['down:mail:2026-11-02T09:00:00Z', 'mail needs you to sign in again', false],
  ])
  for (const g of got) expect(g.text.includes('\n')).toBe(false)
})

test('test_a_first_run_with_nothing_waiting_does_not_swallow_the_first_question', async ($, on) => {
  let cards: any[] = []
  const store: Record<string, unknown> = {}
  const { clock, out } = await boot($, on, world({ cards: () => cards }), store)
  expect(store[NOTIFIED_KEY]).toEqual({})                        // the store now knows a first run happened
  cards = [asking()]
  await clock.advance(60_000)
  expect(out.notes.map(n => n.text)).toEqual(['Fix the login bug asks: Ship now or wait for review?'])
})

test('test_leader_notifies_once_and_never_again_after_a_reload', async ($, on) => {
  let cards: any[] = [asking()]
  let inbox: any[] = []
  const w = world({ status: () => status({ inbox, inbox_count: inbox.length }), cards: () => cards })
  const store: Record<string, unknown> = {}
  const { clock, out } = await boot($, on, w, store)
  expect(out.notes).toEqual([])                                  // first run ever: what is there is seeded, not raised
  expect(Object.keys(store[NOTIFIED_KEY] as object)).toEqual(['ask:a1'])
  cards = [asking(), asking({ session_id: CLI_B, ask_id: 'b1', question: 'Merge the docs PR?' })]
  inbox = [urgent('U2', { title: 'Prod is down' })]
  await clock.advance(60_000)
  expect(out.notes).toEqual([
    { text: 'Review the docs asks: Merge the docs PR?', title: 'Mother' },
    { text: 'Urgent from Sam: Prod is down', title: 'Mother' },
  ])
  await clock.advance(60_000)
  expect(out.notes.length).toBe(2)                               // nothing new: nothing raised
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })   // a reload of the mod
  await clock.settle()
  await clock.advance(60_000)
  expect(out.notes.length).toBe(2)                               // the store remembers: no repeat
})

test('test_non_leader_and_switched_off_never_notify', async ($, on) => {
  let cards: any[] = []
  let led = 'another-session'
  const w = world({ status: () => status({ led_by: led }), cards: () => cards })
  const store: Record<string, unknown> = { [NOTIFIED_KEY]: {} }
  const { clock, out } = await boot($, on, w, store)
  cards = [asking()]
  await clock.advance(60_000)
  expect(out.notes).toEqual([])                                  // another session leads: it notifies, not this one
  expect(store[NOTIFIED_KEY]).toEqual({})
  expect(((await $.command.run({ command: 'chief', args: 'notify off' } as any)) as any).text).toContain('notifications: off')
  expect(store[NOTIFY_OFF_KEY]).toBe(true)
  led = SELF
  await clock.advance(60_000)
  expect(out.notes).toEqual([])                                  // off: nothing
  expect(((await $.command.run({ command: 'chief', args: 'notify on' } as any)) as any).text).toContain('notifications: on')
  await clock.advance(60_000)
  expect(out.notes.map(n => n.text)).toEqual(['Fix the login bug asks: Ship now or wait for review?'])
  expect(((await $.command.run({ command: 'chief', args: 'notify' } as any)) as any).text).toContain('notifications: on')
})

test('test_quiet_hours_notify_only_urgent', async ($, on) => {
  let cards: any[] = []
  let inbox: any[] = []
  const w = world({ status: () => status({ quiet: true, inbox, inbox_count: inbox.length }), cards: () => cards })
  const store: Record<string, unknown> = { [NOTIFIED_KEY]: {} }
  const { clock, out } = await boot($, on, w, store)
  cards = [asking()]
  inbox = [urgent('U2', { title: 'Prod is down' })]
  await clock.advance(60_000)
  expect(out.notes.map(n => n.text)).toEqual(['Urgent from Sam: Prod is down'])
})

// ---- 3. sign-in and network cards ----
const ACCESS_STATUS = (access: any[]) => status({ health: { watcher: { at: '2026-11-02T10:00:00Z', sources: {} }, access } })

test('test_access_rows_and_titles', async () => {
  const rows = accessRows(ACCESS_STATUS([{ source: 'mail', kind: 'signin', reason: '401', help: 'h' },
                                         { source: 'tracker', kind: 'network', reason: 'timed out', help: 'h' },
                                         { source: 'x', kind: 'other' }]) as any)
  expect(rows.map(accessTitle)).toEqual(['Sign-in needed: mail', 'Network: tracker unreachable'])
})

test('test_access_card_retry_runs_the_check_then_clears_when_healthy', async ($, on) => {
  let access: any[] = [{ source: 'mail', kind: 'signin', reason: '401 token expired', help: 'Sign in to this connector again, then Retry.' }]
  const hub: string[][] = []
  const w = world({ status: () => ACCESS_STATUS(access),
    hub: argv => { hub.push(argv); if (argv[0] === 'check') { access = []; return { exitCode: 0, stdout: 'mail | ok | tools/list: 9 tools\n' } } return { exitCode: 0 } } })
  const { out } = await boot($, on, w, { [NOTIFIED_KEY]: {} })
  let ui = await $.ui.mount(PANE)
  expect(await hasText(ui, /^Sign-in needed: mail$/)).toBe(true)
  expect(await hasText(ui, /^401 token expired$/)).toBe(true)
  expect((await ui.find({ key: 'access-retry:mail' }))?.props.variant).toBe('primary')
  expect(await hasText(ui, /Sign in to this connector again/)).toBe(false)   // How to fix is folded
  await ui.press({ key: 'access-fix:mail' })
  expect(await hasText(ui, /Sign in to this connector again/)).toBe(true)
  expect(await hasText(ui, /^Nothing needs you$/)).toBe(false)
  await ui.press({ key: 'access-retry:mail' })
  expect(hub).toEqual([['check', 'mail'], ['refresh']])
  expect(out.toasts).toContain('mail answers now; the watcher reads it again')
  await ui.unmount()
  ui = await $.ui.mount(PANE)
  expect(await hasText(ui, /Sign-in needed/)).toBe(false)         // the status read after Retry has no access row
  await ui.unmount()
})

test('test_access_retry_that_still_fails_says_why_and_keeps_the_card', async ($, on) => {
  const access = [{ source: 'tracker', kind: 'network', reason: 'connect ECONNREFUSED', help: 'Reconnect the VPN, then Retry.' }]
  const hub: string[][] = []
  const w = world({ status: () => ACCESS_STATUS(access),
    hub: argv => { hub.push(argv); return { exitCode: 1, stdout: 'tracker | ✗ | unreachable: no reply\n' } } })
  const { out } = await boot($, on, w, { [NOTIFIED_KEY]: {} })
  const ui = await $.ui.mount(PANE)
  expect(await hasText(ui, /^Network: tracker unreachable$/)).toBe(true)
  await ui.press({ key: 'access-retry:tracker' })
  expect(hub).toEqual([['check', 'tracker']])                     // no refresh while it still fails
  expect(out.toasts).toContain('tracker still fails: unreachable: no reply')
  expect(await hasText(ui, /^Network: tracker unreachable$/)).toBe(true)
  await ui.unmount()
})

// ---- 5. the fast refresh ----
test('test_fast_helpers', async () => {
  expect(changed(NO_SIGS, NO_SIGS).any).toBe(false)
  expect(changed(NO_SIGS, { status: '1:2', beat: '', floor: '' })).toEqual({ status: true, beat: false, floor: false, any: true })
  const s: any = status({ led_by: null })
  const beat = { at: '2026-11-02T09:59:50Z', session_id: 'sess-9' }
  expect(withBeat(s, beat, NOW).led_by).toBe('sess-9')
  expect(withBeat(s, beat, NOW + 5 * 60_000).led_by).toBeNull()   // an old heartbeat leads nobody
  expect(withBeat(s, { junk: 1 }, NOW)).toBe(s)
})

test('test_fast_loop_redraws_on_change_only_and_stops_when_hidden', async ($, on) => {
  const saved = (over: Record<string, unknown>) => JSON.stringify(status(over))
  const w = world({ shown: true, files: {
    '.mother/chief/status.json': { text: saved({ inbox: [item('U1')], inbox_count: 1 }), mtimeMs: 1 },
    '.mother/chief/heartbeat.json': { text: JSON.stringify({ at: '2026-11-02T09:59:40Z', session_id: SELF }), mtimeMs: 1 },
  } })
  const { clock, out } = await boot($, on, w, { [NOTIFIED_KEY]: {} })
  await $.command.run({ command: 'chief', args: 'open' } as any)   // the pane opens: the fast loop starts
  const statusCalls = () => out.argv.filter(a => a[2] === 'chief' && a[3] === 'status').length
  const reads = () => out.fsCalls.filter(c => c.startsWith('read:')).length
  const before = statusCalls()
  await clock.advance(5_000)
  const ui = await $.ui.mount(PANE)
  expect(await hasText(ui, /Item U1$/)).toBe(true)               // read from the file, no CLI call
  expect(statusCalls()).toBe(before)
  const afterFirst = reads()
  await clock.advance(5_000)
  await clock.advance(5_000)
  expect(reads()).toBe(afterFirst)                               // same size and mtime: nothing read, nothing redrawn
  w.files['.mother/chief/status.json'] = { text: saved({ inbox: [item('U1'), item('U3', { title: 'Fresh news' })], inbox_count: 2 }), mtimeMs: 2 }
  await clock.advance(5_000)
  expect(await hasText(ui, /Fresh news$/)).toBe(true)
  expect(statusCalls()).toBe(before)
  expect(out.argv.filter(a => a[2] === 'chief' && a[3] === 'tick').length).toBe(0)   // and no tick, no model call
  await ui.unmount()
  w.shown = false
  await clock.advance(5_000)                                     // hidden: the loop stops itself
  const calls = out.fsCalls.length
  await clock.advance(5_000)
  await clock.advance(5_000)
  expect(out.fsCalls.length).toBe(calls)
})

test('test_fast_loop_rereads_floor_cards_only_when_a_card_file_moves', async ($, on) => {
  let cards: any[] = []
  const w = world({ shown: true, cards: () => cards, files: { '.mother/floor/x.json': { text: '{}', mtimeMs: 1 } } })
  const { clock, out } = await boot($, on, w, { [NOTIFIED_KEY]: {} })
  await $.command.run({ command: 'chief', args: 'open' } as any)
  const floorLists = () => out.argv.filter(a => a[2] === 'floor' && a[3] === 'list').length
  await clock.advance(5_000)
  const n = floorLists()
  await clock.advance(5_000)
  expect(floorLists()).toBe(n)
  cards = [asking()]
  w.files['.mother/floor/x.json'] = { text: '{"a": 1}', mtimeMs: 2 }
  await clock.advance(5_000)
  expect(floorLists()).toBe(n + 1)
  const ui = await $.ui.mount(PANE)
  expect(await hasText(ui, /Ship now or wait for review\?/)).toBe(true)
  await ui.unmount()
})

// ---- a chase card's notice (hooks/waiting.ts, the session half, calls notifyOnce), and the drawing rules ----
// notifyOnce is notifyPass with one more notice; the test's `$` is not a hook's, so the plan is what is read here
test('test_a_chase_notice_is_planned_once', async () => {
  const n = chaseNotice('c1', 'Sam', 'the API review')
  expect(n).toEqual({ key: 'chase:c1', text: 'Chase Sam: the API review', urgent: false })
  const first = planNotices([n], {}, false, NOW)
  expect(first.raise).toEqual([n])
  expect(planNotices([n], first.kept, false, NOW).raise).toEqual([])
  expect(planNotices([n], {}, true, NOW).raise).toEqual([])      // quiet hours: a chase waits with the rest
})

test('test_health_tab_shows_the_degraded_source', async ($, on) => {
  const w = world({ status: () => status({ health: { watcher: { at: '2026-11-02T10:00:00Z', sources: {
    chat: { status: 'partial', reason: 'rate limited, backing off until 10:30', degraded: 'rate limited since 09:58, reading known conversations only' } } } } }) })
  await boot($, on, w, { [NOTIFIED_KEY]: {} })
  const ui = await $.ui.mount(PANE)
  expect(await hasText(ui, /^chat degraded: rate limited since 09:58, reading known conversations only$/)).toBe(true)
  await ui.unmount()
})

test('test_dark_skin_every_new_text_has_a_colour_and_the_retry_pill_is_coral', async ($, on) => {
  let inbox: any[] = []
  const access = [{ source: 'mail', kind: 'signin', reason: '401', help: 'Sign in again.' }]
  const w = world({ status: () => status({ inbox, inbox_count: inbox.length, health: { watcher: { at: '2026-11-02T10:00:00Z', sources: {} }, access } }) })
  const { clock } = await boot($, on, w, { chiefSkin: 'dark', [NOTIFIED_KEY]: {} })
  inbox = [urgent('U2', { title: 'Prod is down' })]
  await clock.advance(60_000)
  const ui = await $.ui.mount(PANE)
  await ui.press({ key: 'access-fix:mail' })
  const all = await ui.findAll({ type: 'Text' })
  const shown = all.filter((t: any) => /Sign-in needed|^401$|Sign in again|Urgent: Prod|\u25cf/.test(String(t.text)))
  expect(shown.length).toBeGreaterThanOrEqual(5)
  for (const t of shown) expect(typeof t.props?.color).toBe('string')
  expect(all.find((t: any) => String(t.text) === '\u25cf')?.props?.color).toBe('#e5484d')
  expect(String((await ui.find({ key: 'access-retry:mail' }))?.text).trim()).toBe('Retry')
  await ui.unmount()
})
