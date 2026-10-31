import { expect, mock, test } from 'claude-code/testing'
import { hubStatus, quickActions, sourceMark, sourceStale } from './register'

const WS = '/work/ws'
const NOW = Date.parse('2026-10-26T10:00:00Z')

function item(id: string, section: string, state: string, title = id) {
  return {
    id, section, signal: 's', title, why: '', link: 'https://example.com/' + id, also: [], team: null,
    since: '2026-10-26T08:00:00Z', first_seen: '2026-10-26T09:05:00Z', last_seen: '2026-10-26T09:05:00Z',
    state, snooze_until: null, actions: [],
  }
}

function doc(over: Record<string, unknown> = {}) {
  return {
    version: 1,
    generated_at: '2026-10-26T09:05:00Z',
    timezone: 'UTC',
    quiet: false,
    next_at: '2026-10-26T10:05:00Z',
    stale_after: '2026-10-26T11:05:00Z',
    status: 'partial',
    coverage: { chat: { status: 'ok' }, tracker: { status: 'couldnt_check', since: '2026-10-26T08:05:00Z' } },
    routines: [],
    counts: { on_you: 3, neglected: 2, suggestion: 0 },
    next_action: 1,
    dropped: 0,
    top: [],
    top_date: '2026-10-26',
    swap_hint: null,
    items: [item('a', 'on_you', 'open'), item('b', 'on_you', 'open'), item('c', 'on_you', 'open'),
            item('d', 'neglected', 'open'), item('e', 'neglected', 'open')],
    ...over,
  }
}

// Set by one test: the engine refuses the command name, as it does for a name another plugin's skill holds.
let refuseCommand = false
// The default mother_bin is empty: the panel runs this plugin's own bin/mother. boot() reads it off the first run.
let BIN = ''

for (const surface of ['terminal', 'desktop'] as const) {
  // The files the fake host holds: path -> { text, mtimeMs }.
  async function boot($: any, on: any, files: Record<string, { text: string; mtimeMs: number }>, pathFails = false, held: Record<string, unknown> = {}, statErr = 'ENOENT', cli?: (argv: string[]) => { exitCode: number; stderr?: string; stdout?: string }) {
    const clock = mock.clock(on, { now: NOW })
    on('store.get', (_: any, e: any) => ({ value: held[e.key] }))
    on('store.set', (_: any, e: any) => { held[e.key] = e.value; return { value: undefined } })
    mock.env(on, { HOME: '/home/m' })
    const out = { opened: [] as string[], commands: [] as string[], fills: [] as { text: string; mode?: string }[], submits: 0, open: { isPlaced: true } as Record<string, unknown>, stats: 0, reads: 0, errors: [] as unknown[], status: [] as (string | undefined)[], toasts: [] as string[], argv: [] as string[][] }
    on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
    on('ui.status', (_: any, e: any) => { out.status.push(e.text); return { value: undefined } })
    on('ui.toast', (_: any, e: any) => { out.toasts.push(e.text); return { value: undefined } })
    on('state.set', (_: any, e: any, next: any) => { if (e.key === 'error') out.errors.push(e.value); return next(e) })
    on('prompt.submit', (_: any, e: any, next: any) => { out.submits++; return next(e) })
    on('ui.focus', () => ({}))
    on('command.register', (_: any, e: any) => { if (refuseCommand) return { deny: `"/${e.name}" refused` }; out.commands.push(e.name); return { value: { command: e.name } } })
    on('ui.open', (_: any, e: any) => { out.opened.push(e.id); return { value: out.open } })
    on('prompt.fill', (_: any, e: any) => { out.fills.push({ text: e.text, mode: e.mode }); return { isFilled: true } })
    on('process.run', (_: any, e: any) => {
      out.argv.push([...e.argv])
      if (cli && ['now', 'hub', 'inbox'].includes(e.argv[2])) {
        const r = cli([...e.argv])
        return { value: { exitCode: r.exitCode, stdout: r.stdout ?? '', stderr: r.stderr ?? '', isStdoutTruncated: false, isStderrTruncated: false } }
      }
      if (pathFails) return { value: { exitCode: 2, stdout: '', stderr: 'no workspace', isStdoutTruncated: false, isStderrTruncated: false } }
      return { value: { exitCode: 0, stdout: WS + '\n', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    })
    on('fs.stat', (_: any, e: any) => {
      if (e.path === PATH) out.stats++   // hub files are stat'ed too; the tests below count now.json
      const f = files[e.path]
      if (!f) return { deny: statErr }
      return { value: { kind: 'file', size: f.text.length, mtimeMs: f.mtimeMs } }
    })
    on('fs.read', (_: any, e: any) => {
      out.reads++
      const f = files[e.path]
      if (!f) return { deny: 'ENOENT' }
      return { value: f.text }
    })
    await $.session.start({ cwd: '/', surface, isInteractive: true })
    await clock.settle()
    BIN = out.argv[0]?.[1] ?? ''   // the first run is always `mother path`
    return { clock, out }
  }
  const last = (o: { status: (string | undefined)[] }) => o.status[o.status.length - 1]
  // Link chips as [label, href]; a Link's text is its href then its label. The row's own ↗ is not a chip.
  const chipsOf = async (ui: any): Promise<[string, string][]> => (await ui.findAll({ type: 'Link' }))
    .map((l: any) => [String(l.text).slice(String(l.props.href).length), String(l.props.href)] as [string, string])
    .filter(([label]) => label !== '↗')
  const PATH = WS + '/.mother/now.json'

  test(`status line counts and a failing source, on ${surface}`, async ($, on) => {
    const files = { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } }
    const { out } = await boot($, on, files)
    expect(last(out)).toBe('◆ 3 on you · 2 neglected · tracker ✗')
    expect(out.argv[0]).toEqual(['python3', BIN, 'path'])
    // empty mother_bin = the plugin's own script, an absolute path inside this plugin
    expect(BIN.startsWith('/') && BIN.endsWith('/bin/mother') && !BIN.includes('~')).toBe(true)
  })

  test(`a configured ~ path for mother_bin is expanded, on ${surface}`, { options: { mother_bin: '~/src/mother/bin/mother' } }, async ($, on) => {
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } })
    expect(out.argv[0]).toEqual(['python3', '/home/m/src/mother/bin/mother', 'path'])
  })

  test(`a partly checked source shows ~, not ✗, on ${surface}`, async ($, on) => {
    const d = doc({ coverage: { chat: { status: 'ok' }, code: { status: 'partial' }, tracker: { status: 'couldnt_check' } } })
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    expect(last(out)).toBe('◆ 3 on you · 2 neglected · code ~ · tracker ✗')
  })

  test(`stale after stale_after, on ${surface}`, async ($, on) => {
    const files = { [PATH]: { text: JSON.stringify(doc({ stale_after: '2026-10-26T09:30:00Z' })), mtimeMs: 1 } }
    const { out } = await boot($, on, files)
    expect(last(out)).toBe('◆ stale')
  })

  test(`no tick here when the file is missing, on ${surface}`, async ($, on) => {
    const { out } = await boot($, on, {})
    expect(last(out)).toBe('◆ no tick here')
  })

  test(`no tick here when mother path fails, on ${surface}`, async ($, on) => {
    const { out } = await boot($, on, {}, true)
    expect(last(out)).toBe('◆ no tick here')
  })

  test(`a new on-you item toasts once across two reads, on ${surface}`, async ($, on) => {
    const d = doc({ items: [item('n1', 'on_you', 'new', 'Review needed')], counts: { on_you: 1, neglected: 0, suggestion: 0 } })
    const files = { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } }
    const { clock, out } = await boot($, on, files)
    expect(out.toasts.length).toBe(1)
    expect(out.toasts[0]).toContain('Review needed')
    files[PATH] = { text: JSON.stringify({ ...d, generated_at: '2026-10-26T10:05:00Z' }), mtimeMs: 2 }
    await clock.advance(60_000)
    expect(out.toasts.length).toBe(1)
  })

  test(`no toast while quiet, on ${surface}`, async ($, on) => {
    const d = doc({ quiet: true, items: [item('n1', 'on_you', 'new')] })
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    expect(out.toasts.length).toBe(0)
  })

  test(`a neglected new item never toasts, on ${surface}`, async ($, on) => {
    const d = doc({ items: [item('n2', 'neglected', 'new')] })
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    expect(out.toasts.length).toBe(0)
  })

  test(`a corrupt file keeps the previous status, on ${surface}`, async ($, on) => {
    const files = { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } }
    const { clock, out } = await boot($, on, files)
    const before = last(out)
    files[PATH] = { text: '{not json', mtimeMs: 2 }
    await clock.advance(60_000)
    expect(last(out)).toBe(before)
    expect(String(out.errors[out.errors.length - 1])).toContain('now.json')
    files[PATH] = { text: JSON.stringify(doc({ version: 2 })), mtimeMs: 3 }
    await clock.advance(60_000)
    expect(last(out)).toBe(before)
    expect(String(out.errors[out.errors.length - 1])).toContain('unsupported version')
  })

  test(`a corrupt file recovers once it is fixed, on ${surface}`, async ($, on) => {
    const files = { [PATH]: { text: '{not json', mtimeMs: 1 } }
    const { clock, out } = await boot($, on, files)
    expect(last(out)).toBe('◆ no tick here')
    expect(String(out.errors[out.errors.length - 1])).toContain('now.json')
    files[PATH] = { text: JSON.stringify(doc()), mtimeMs: 2 }
    await clock.advance(60_000)
    expect(last(out)).toBe('◆ 3 on you · 2 neglected · tracker ✗')
    expect(out.errors[out.errors.length - 1]).toBe(null)
  })

  test(`an unchanged mtime is not re-read, on ${surface}`, async ($, on) => {
    const files = { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } }
    const { clock, out } = await boot($, on, files)
    expect(out.reads).toBe(1)
    await clock.advance(60_000)
    await clock.advance(60_000)
    expect(out.reads).toBe(1)
    expect(out.stats).toBe(3)
  })

  test(`two session.starts give one poll per tick, on ${surface}`, async ($, on) => {
    const files = { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } }
    const { clock, out } = await boot($, on, files)
    await $.session.start({ cwd: '/', surface, isInteractive: true })
    await clock.settle()
    const before = out.stats
    await clock.advance(60_000)
    expect(out.stats - before).toBe(1)
  })

  test(`EACCES on stat keeps the previous doc and sets error, on ${surface}`, async ($, on) => {
    const files: Record<string, { text: string; mtimeMs: number }> = { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } }
    const { clock, out } = await boot($, on, files, false, {}, 'EACCES: permission denied')
    const before = last(out)
    delete files[PATH]
    await clock.advance(60_000)
    expect(last(out)).toBe(before)
    expect(String(out.errors[out.errors.length - 1])).toContain('EACCES')
  })

  test(`the toasted list keeps the last 500 ids, on ${surface}`, async ($, on) => {
    const old = Array.from({ length: 499 }, (_, i) => 'old' + i)
    const d = doc({ items: ['x1', 'x2', 'x3'].map(id => item(id, 'on_you', 'new')) })
    const held: Record<string, unknown> = { toasted: old }
    await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } }, false, held)
    const stored = held.toasted as string[]
    expect(stored.length).toBe(500)
    expect(stored.slice(-3)).toEqual(['x1', 'x2', 'x3'].map(id => id + '@2026-10-26T09:05:00Z'))
    expect(stored[0]).toBe('old2')
  })

  test(`an item asked for again after Done toasts again, on ${surface}`, async ($, on) => {
    const d = doc({ items: [item('n1', 'on_you', 'new', 'Review needed')] })
    const files = { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } }
    const { clock, out } = await boot($, on, files)
    expect(out.toasts.length).toBe(1)
    const again: any = doc({ items: [{ ...item('n1', 'on_you', 'new', 'Review needed'), first_seen: '2026-10-26T11:05:00Z' }] })
    files[PATH] = { text: JSON.stringify(again), mtimeMs: 2 }
    await clock.advance(60_000)
    expect(out.toasts.length).toBe(2)
  })

  const PANE = {
    plugin: 'mother', component: 'Pane', requestId: 'mother-now',
    props: { title: 'Now', isFocused: true, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} },
  } as const
  const texts = async (ui: any) => [...await ui.findAll({ type: 'Text' }), ...await ui.findAll({ type: 'Button' })].map((t: any) => t.text as string)
  const hasText = async (ui: any, re: RegExp) => (await texts(ui)).some((t: string) => re.test(t))
  const nowCalls = (out: { argv: string[][] }) => out.argv.filter(a => a[2] === 'now').map(a => a.slice(3))
  // The Inspector pane, and a press on Home that opens it: returns the mounted Inspector.
  const INSPECTOR = { ...PANE, requestId: 'mother-inspector', props: { ...PANE.props, title: 'Item' } } as const
  const inspectFrom = async ($: any, home: any, key: string) => {
    await home.press({ key })
    return $.ui.mount({ ...INSPECTOR, surface })
  }
  const TOP1 = [{ rank: 1, title: 'T1', why: '', link: null, item_id: null, set_by: 'agent', state: 'open' }]
  const hotkeys = async (ui: any) => {
    const all = await ui.findAll({ type: 'Button' })
    return all.filter((b: any) => b.props.hotkey).map((b: any) => `${b.props.hotkey}:${b.key}`)
  }

  test(`/now-panel registers and opens the pane, on ${surface}`, async ($, on) => {
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } })
    expect(out.commands).toContain('now-panel')
    const res: any = await $.command.run({ command: 'now-panel', args: '' } as any)
    expect(out.opened).toContain('mother-now')
    expect(res.text).toBe('Now panel opened.')
    expect(nowCalls(out)).toEqual([])
  })

  test(`a refused command name still starts the status line, on ${surface}`, async ($, on) => {
    refuseCommand = true
    try {
      const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } })
      expect(out.commands).toEqual([])
      expect(out.status.at(-1)).toMatch(/on you/)
    } finally {
      refuseCommand = false
    }
  })

  test(`/now-panel falls back to mother now show in chat when the pane is not placed, on ${surface}`, async ($, on) => {
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } }, false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    out.open = { isPlaced: false, reason: 'no surface places panes' }
    const res: any = await $.command.run({ command: 'now-panel', args: '' } as any)
    expect(nowCalls(out)).toEqual([['show']])
    expect(res.text).toMatch(/can't be drawn/)   // empty stdout counts as a failed show
  })

  test(`the chat fallback returns the stdout of mother now show, on ${surface}`, async ($, on) => {
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } }, false, {}, 'ENOENT',
      () => ({ exitCode: 0, stdout: 'Top 3 (2026-10-26)\nOn you (3)\n' }))
    out.open = { isPlaced: false, reason: 'narrow' }
    const res: any = await $.command.run({ command: 'now-panel', args: '' } as any)
    expect(res.text).toBe('Top 3 (2026-10-26)\nOn you (3)')
  })

  test(`the chat fallback says so when show fails, on ${surface}`, async ($, on) => {
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } }, false, {}, 'ENOENT', () => ({ exitCode: 2, stderr: 'boom' }))
    out.open = { isPlaced: false, reason: 'narrow' }
    const res: any = await $.command.run({ command: 'now-panel', args: '' } as any)
    expect(res.text).toMatch(/mother now show/)
  })

  test(`Home groups render in order and carry the header; the health line opens the Mother page, on ${surface}`, async ($, on) => {
    const d: any = doc({
      top: [{ rank: 1, title: 'Ship it', why: '', link: null, item_id: 'a', set_by: 'agent', state: 'open' },
            { rank: 2, title: 'Done thing', why: '', link: null, item_id: null, set_by: 'agent', state: 'done' }],
      items: [item('a', 'on_you', 'open', 'Review A'), item('s1', 'suggestion', 'open', 'Nudge B'), item('d', 'neglected', 'open', 'Stalled D')],
      counts: { on_you: 1, neglected: 1, suggestion: 1 },
      routines: [{ id: 'dots', state: 'missed', expected: '2026-10-26T13:00:00Z' }],
    })
    d.items[1].actions = [{ id: 'S1', label: 'Nudge', prompt: 'mother now do S1', task: 't' }]
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    const all = await texts(ui)
    const at = (re: RegExp) => all.findIndex(t => re.test(t))
    const order = [/^⚠ .*1 routine missed.*couldn't check tracker/, /Top 3 today/, /^On you 1$/, /^Suggestions 1$/, /^Neglected by teams 1$/].map(at)
    expect(order.every(i => i >= 0)).toBe(true)
    expect([...order].sort((a, b) => a - b)).toEqual(order)
    expect(all.some(t => /^Now · 09:05 · next 10:05$/.test(t))).toBe(true)
    expect(all.some(t => /set by agent/.test(t))).toBe(true)
    expect(all.some(t => /dots missed at 13:00/.test(t))).toBe(false)   // the detail is on the Mother page
    await ui.press({ key: 'health-open' })
    expect(out.opened).toContain('mother-inspector')
    const ip = await $.ui.mount({ ...INSPECTOR, surface })
    expect(await hasText(ip, /^dots missed at 13:00$/)).toBe(true)
    await ip.unmount()
    await ui.unmount()
  })

  test(`empty sections are not drawn, on ${surface}`, async ($, on) => {
    const d = doc({ items: [item('a', 'on_you', 'open', 'Review A')], counts: { on_you: 1, neglected: 0, suggestion: 0 }, coverage: { chat: { status: 'ok' } } })
    await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /^On you 1$/)).toBe(true)
    expect(await hasText(ui, /Neglected|Suggestions|Top 3|Couldn't check|Routines|Nothing on you/)).toBe(false)
    await ui.unmount()
  })

  test(`a row shows title, age, link and the unverified tag, on ${surface}`, async ($, on) => {
    const it: any = item('a', 'on_you', 'open', 'Review A')
    it.unverified_since = '2026-10-26T09:05:00Z'
    it.since = '2026-10-25T09:00:00Z'       // raised a day ago; the tick first saw it this morning
    const d = doc({ items: [it], counts: { on_you: 1, neglected: 0, suggestion: 0 }, coverage: {} })
    await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /Review A/)).toBe(true)
    expect(await hasText(ui, /^ ?1d ?$/)).toBe(true)
    expect(await hasText(ui, /unverified/)).toBe(true)
    expect((await ui.find({ type: 'Link' }))?.props.href).toBe('https://example.com/a')
    await ui.unmount()
  })

  test(`the item page draws a chip per source: links first, then link and http also, on ${surface}`, async ($, on) => {
    const it: any = item('a', 'on_you', 'open', 'Review A')
    it.link = 'https://example.com/acme/api/pull/7'
    it.links = [{ label: 'Thread', url: 'https://example.com/t/1' }, { label: 'Channel', url: 'https://example.com/c' }]
    it.also = ['https://example.com/browse/PLAT-2', 'PLAT-2', 'https://example.com/t/1', 'https://example.com/other']
    const d = doc({ items: [it], counts: { on_you: 1, neglected: 0, suggestion: 0 }, coverage: {} })
    await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'row-a')
    expect(await chipsOf(ip)).toEqual([
      ['Thread', 'https://example.com/t/1'],
      ['Channel', 'https://example.com/c'],
      ['api#7', 'https://example.com/acme/api/pull/7'],
      ['PLAT-2', 'https://example.com/browse/PLAT-2'],
      ['Link', 'https://example.com/other'],
    ])
    await ip.unmount()
    await ui.unmount()
  })

  test(`a row with only its link draws no chip line, on ${surface}`, async ($, on) => {
    const d = doc({ items: [item('a', 'on_you', 'open')], counts: { on_you: 1, neglected: 0, suggestion: 0 }, coverage: {} })
    await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await chipsOf(ui)).toEqual([])
    await ui.unmount()
  })

  const FOCUS = { ...PANE, requestId: 'mother-focus', props: { ...PANE.props, title: 'Focus' } } as const
  const kinded = (kind: string, signal: string, title: string, link: string) => {
    const it: any = item('x', 'on_you', 'open', title)
    it.id = `${kind}:${link}:${signal}`
    it.signal = signal
    it.link = link
    it.why = 'Asked 5 working hours ago'
    return it
  }

  test(`quick actions follow the item's kind and never send, on ${surface}`, async () => {
    const chat = quickActions(kinded('chat', 'unanswered_mention', 'Release question', 'https://example.com/t/1'))
    expect(chat.map(q => q.label)).toEqual(['Draft a reply', 'Summarize the thread', 'Find related tickets',
      'Turn into a ticket', 'Hand it to someone', 'Make it a todo'])
    for (const q of chat) {
      expect(q.text).toContain('"Release question" (https://example.com/t/1)')
      expect(q.text).toMatch(/don't send/)
    }
    expect(quickActions(kinded('pr', 'review_requested', 'Review api#7', 'https://example.com/acme/api/pull/7')).map(q => q.label))
      .toEqual(['Review brief', 'Why is CI red?', 'Hand it to someone', 'Make it a todo'])
    expect(quickActions(kinded('pr', 'pr_no_reviewer', 'api#8', 'https://example.com/acme/api/pull/8')).map(q => q.label))
      .toEqual(['Why is CI red?', 'Find a reviewer', 'Hand it to someone', 'Make it a todo'])
    expect(quickActions(kinded('ticket', 'bug_unowned', 'Login broken', 'https://example.com/browse/PLAT-2')).map(q => q.label))
      .toEqual(['Find related PRs and commits', 'Investigate in the code', 'Find the owner and draft a DM', 'Make it a todo'])
    expect(quickActions(kinded('repo', 'default_branch_red', 'api main red', 'https://example.com/acme/api/commits/main')).map(q => q.label))
      .toEqual(['Find the breaking change', 'Draft a heads-up to its author', 'Make it a todo'])
    expect(quickActions(kinded('todo', 'promise_due', 'Send the plan', 'https://example.com/x')).map(q => q.label))
      .toEqual(["What's this about?", 'Hand it to someone', 'Make it a todo'])
    expect(chat.map(q => q.group)).toEqual(['Respond', 'Respond', 'Investigate', 'Delegate', 'Delegate', 'Triage'])
  })

  test(`the item page offers one primary move and three more, the rest behind a toggle, on ${surface}`, async ($, on) => {
    const it = kinded('chat', 'unanswered_mention', 'Release question', 'https://example.com/t/1')
    const d = doc({ items: [it], counts: { on_you: 1, neglected: 0, suggestion: 0 }, coverage: {} })
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'row-' + it.id)
    const quick = quickActions(it)
    expect((await ip.find({ key: 'ip-primary' }))?.text).toBe('Draft a reply')
    expect((await ip.findAll({ type: 'Button' })).filter((b: any) => /^ip-move\d$/.test(String(b.key))).map((b: any) => b.text))
      .toEqual(['Summarize the thread', 'Find related tickets', 'Turn into a ticket'])
    await ip.press({ key: 'ip-move0' })
    expect(out.fills).toEqual([{ text: quick[1].text, mode: 'replace' }])
    expect(out.submits).toBe(0)
    expect((await ip.find({ key: 'ip-more' }))?.text).toBe('2 more')
    await ip.press({ key: 'ip-more' })
    expect((await ip.find({ key: 'ip-extra0' }))?.text).toBe('Hand it to someone')
    await ip.unmount()
    await ui.unmount()
  })

  test(`the item page: why, timeline, linked chips, lifecycle, and related items it pushes, on ${surface}`, async ($, on) => {
    const a = kinded('chat', 'unanswered_mention', 'Release question', 'https://example.com/t/1')
    a.links = [{ label: 'Channel', url: 'https://example.com/c' }]
    a.team = 'platform'
    a.actions = [{ id: 'S1', label: 'Draft the cutoff reply', prompt: 'mother now do S1', task: 'draft' }]
    const b: any = item('b', 'on_you', 'open', 'Other B')
    b.team = 'platform'
    const d = doc({ items: [a, b], counts: { on_you: 2, neglected: 0, suggestion: 0 }, coverage: {} })
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } }, false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'row-' + a.id)
    expect(await hasText(ip, /Release question/)).toBe(true)
    for (const h of [/^Why it's here$/, /^Next move$/, /^Linked$/, /^Related 1$/]) expect(await hasText(ip, h)).toBe(true)
    expect(await hasText(ip, /raised 2h ago/)).toBe(true)
    expect((await chipsOf(ip)).map(([l]) => l)).toEqual(['Channel', 'Source'])
    await ip.press({ key: 'ip-primary' })
    expect(out.fills[0]).toEqual({ text: 'mother now do S1', mode: 'replace' })
    await ip.press({ key: 'ip-tell' })
    expect(out.fills[1]).toEqual({ text: 'About "Release question" (https://example.com/t/1): ', mode: 'replace' })
    await ip.press({ key: 'ip-done' })
    expect(nowCalls(out)).toEqual([['done', a.id]])
    await ip.press({ key: 'ip-ignore' })
    await ip.select({ key: 'fp-ignore-pick', value: 'week' })
    expect(out.argv.filter(x => x[2] === 'hub').map(x => x.slice(3))).toEqual([['mute', '--for', '1w', '--', a.link]])
    await ip.press({ key: 'ip-rel-b-b' })               // a related item: pushed onto the stack
    expect(await hasText(ip, /Other B/)).toBe(true)
    await ip.press({ key: 'insp-back' })
    expect(await hasText(ip, /Release question/)).toBe(true)
    expect(await ip.find({ key: 'insp-back' })).toBeUndefined()
    await ip.unmount()
    await ui.unmount()
  })

  const mailItem = (tid: string, title: string) => {
    const it = kinded('mail', 'mail_needs_reply', title, 'https://mail.google.com/mail/u/0/#all/' + tid)
    it.id = 'mail:' + it.link + ':mail_needs_reply'
    return it
  }
  const mine = (out: { argv: string[][] }) => out.argv.filter(a => ['now', 'hub', 'inbox'].includes(a[2])).map(a => a.slice(2))

  test(`a mail item archives or marks read with no model, then Archive marks it done, on ${surface}`, async ($, on) => {
    const m = mailItem('T1', 'Budget question')
    const d = doc({ items: [m, item('b', 'on_you', 'open')], counts: { on_you: 2, neglected: 0, suggestion: 0 }, coverage: {} })
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } }, false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'row-' + m.id)
    await ip.press({ key: 'ip-read' })
    await ip.press({ key: 'ip-archive' })
    expect(mine(out)).toEqual([['inbox', 'read', '--', m.link], ['inbox', 'archive', '--', m.link], ['now', 'done', m.id]])
    await ui.press({ key: 'row-b' })
    expect(await ip.find({ key: 'ip-archive' })).toBeUndefined()
    await ip.unmount()
    await ui.unmount()
  })

  test(`a group with two or more mail rows offers Archive all, and Done all marks every row done, on ${surface}`, async ($, on) => {
    const m1 = mailItem('T1', 'One'), m2 = mailItem('T2', 'Two')
    const d = doc({ items: [m1, m2, item('c', 'on_you', 'open')], counts: { on_you: 3, neglected: 0, suggestion: 0 }, coverage: {} })
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } }, false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /^Reply 2$/)).toBe(true)
    expect((await ui.find({ key: 'archive-all-reply' }))?.text).toBe('Archive 2 mail')
    await ui.press({ key: 'archive-all-reply' })
    expect(mine(out)).toEqual([['inbox', 'archive', '--', m1.link, m2.link], ['now', 'done', m1.id], ['now', 'done', m2.id]])
    out.argv.length = 0
    await ui.press({ key: 'done-all-reply' })
    expect(mine(out)).toEqual([['now', 'done', m1.id], ['now', 'done', m2.id]])
    expect(await ui.find({ key: 'done-all-other' })).toBeUndefined()   // one row: no batch line
    await ui.unmount()
  })

  test(`a failed archive leaves the item on the list, on ${surface}`, async ($, on) => {
    const m = mailItem('T1', 'Budget question')
    const d = doc({ items: [m], counts: { on_you: 1, neglected: 0, suggestion: 0 }, coverage: {} })
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } }, false, {}, 'ENOENT',
      (argv: string[]) => argv[2] === 'inbox' ? { exitCode: 1, stderr: "mother inbox: couldn't archive mail:T1: 403\n" } : { exitCode: 0 })
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'row-' + m.id)
    await ip.press({ key: 'ip-archive' })
    expect(mine(out)).toEqual([['inbox', 'archive', '--', m.link]])
    expect(out.toasts).toContain("mother inbox: couldn't archive mail:T1: 403")
    await ip.unmount()
    await ui.unmount()
  })

  test(`a hub update about a mail thread can be archived from its page, then is done, on ${surface}`, async ($, on) => {
    const updates = [{ id: 'U1', key: 'mail:T1', title: 'New mail', why: 'w', link: null, at: iso(MIN), state: 'open' },
                     { id: 'U2', key: 'PLAT-5', title: 'Spec', why: 'w', link: null, at: iso(MIN), state: 'open' }]
    const { out } = await boot($, on, hubFiles(watcher(), hubDoc({ updates }), null), false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'upd-row-U2')
    expect(await ip.find({ key: 'hub-archive-U2' })).toBeUndefined()
    await ui.press({ key: 'upd-row-U1' })
    await ip.press({ key: 'hub-archive-U1' })
    expect(mine(out)).toEqual([['inbox', 'archive', '--', 'mail:T1'], ['hub', 'done', 'U1']])
    await ip.unmount()
    await ui.unmount()
  })

  test(`the Inspector with nothing picked says so, and the 0.10 Focus pane id draws it too, on ${surface}`, async ($, on) => {
    const d = doc({ items: [], counts: { on_you: 0, neglected: 0, suggestion: 0 }, coverage: {} })
    await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    for (const pane of [INSPECTOR, FOCUS]) {
      const ip = await $.ui.mount({ ...pane, surface })
      expect(await hasText(ip, /Pick a row in the Now panel/)).toBe(true)
      await ip.unmount()
    }
  })

  test(`Done runs mother now done and re-reads at once, on ${surface}`, async ($, on) => {
    const files: Record<string, { text: string; mtimeMs: number }> = { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } }
    const after = doc({ items: [item('b', 'on_you', 'open', 'Only B')], counts: { on_you: 1, neglected: 0, suggestion: 0 }, coverage: {} })
    const { out } = await boot($, on, files, false, {}, 'ENOENT', () => {
      files[PATH] = { text: JSON.stringify(after), mtimeMs: 2 }
      return { exitCode: 0 }
    })
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'row-a')
    await ip.press({ key: 'ip-done' })
    expect(out.argv.find(a => a[2] === 'now')).toEqual(['python3', BIN, 'now', 'done', 'a'])
    expect(out.reads).toBe(2)   // no timer tick: the press itself re-read the file
    expect(await hasText(ui, /Only B/)).toBe(true)
    expect(await ui.find({ key: 'row-a' })).toBeUndefined()
    expect(await hasText(ip, /no longer on the list/)).toBe(true)
    await ip.unmount()
    await ui.unmount()
  })

  test(`a failing CLI toasts its first stderr line and both panes still draw, on ${surface}`, async ($, on) => {
    const files = { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } }
    const { out } = await boot($, on, files, false, {}, 'ENOENT', () => ({ exitCode: 1, stderr: 'mother: unknown id\nTraceback...' }))
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'row-a')
    await ip.press({ key: 'ip-done' })
    expect(out.toasts).toContain('mother: unknown id')
    expect(await hasText(ui, /^On you 3$/)).toBe(true)
    expect(await ip.find({ key: 'ip-done' })).toBeDefined()
    await ip.unmount()
    await ui.unmount()
  })

  test(`Snooze opens a Select and runs mother now snooze, on ${surface}`, async ($, on) => {
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } }, false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'row-a')
    await ip.press({ key: 'ip-snooze' })
    const sel = await ip.find({ type: 'Select', key: 'fp-snooze-pick' })
    expect((sel?.props.options as any[]).map(o => o.value)).toEqual(['1h', 'tomorrow', 'next-week'])
    await ip.select({ key: 'fp-snooze-pick', value: 'tomorrow' })
    expect(nowCalls(out)[0]).toEqual(['snooze', 'a', 'tomorrow'])
    expect(await ip.find({ type: 'Select', key: 'fp-snooze-pick' })).toBeUndefined()
    await ip.unmount()
    await ui.unmount()
  })

  test(`a suggestion's action fills the prompt, replaces, never submits, on ${surface}`, async ($, on) => {
    const d: any = doc({ items: [item('s1', 'suggestion', 'open', 'Nudge B')], counts: { on_you: 0, neglected: 0, suggestion: 1 }, coverage: {} })
    d.items[0].actions = [{ id: 'S1', label: 'Nudge', prompt: 'mother now do S1', task: 't' }]
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /^Suggestions 1$/)).toBe(true)
    const ip = await inspectFrom($, ui, 'row-s1')
    expect((await ip.find({ key: 'ip-primary' }))?.text).toBe('Nudge')
    await ip.press({ key: 'ip-primary' })
    expect(out.fills).toEqual([{ text: 'mother now do S1', mode: 'replace' }])
    expect(out.submits).toBe(0)
    expect(out.argv.filter(a => a[2] === 'now').length).toBe(0)
    await ip.unmount()
    await ui.unmount()
  })

  test(`the item page's hotkeys: x the tick's action, a ask, d done, s snooze, t top 3, on ${surface}`, async ($, on) => {
    const d: any = doc({ coverage: {} })
    d.items[0].actions = [{ id: 'S2', label: 'Draft a nudge', prompt: 'mother now do S2', task: 'draft a nudge to the reviewer' }]
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'row-a')
    expect(await hotkeys(ip)).toEqual(['x:ip-primary', 'a:ip-ask', 'd:ip-done', 's:ip-snooze', 't:ip-maketop'])
    expect((await ip.find({ key: 'ip-primary' }))?.text).toBe('Draft a nudge')   // the action's own label
    await ip.press({ key: 'ip-primary' })
    expect(out.fills).toEqual([{ text: 'mother now do S2', mode: 'replace' }])
    await ui.press({ key: 'row-b' })                                           // no action: the first quick action leads
    expect((await ip.find({ key: 'ip-primary' }))?.text).toBe("What's this about?")
    await ip.unmount()
    await ui.unmount()
  })

  test(`yesterday's Top 3 is not shown, by the doc's timezone, on ${surface}`, async ($, on) => {
    const top = [{ rank: 1, title: 'Old one', why: '', link: null, item_id: null, set_by: 'manager', state: 'open' }]
    await boot($, on, { [PATH]: { text: JSON.stringify(doc({ top, top_date: '2026-10-25' })), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /Top 3 today|Old one/)).toBe(false)
    expect(await hotkeys(ui)).toEqual([])
    await ui.unmount()
  })

  test(`today is the manager's date: at UTC+14 the 26th's Top 3 is already yesterday's, on ${surface}`, async ($, on) => {
    const top = [{ rank: 1, title: 'Old one', why: '', link: null, item_id: null, set_by: 'agent', state: 'open' }]
    const far = doc({ top, top_date: '2026-10-26', timezone: 'Pacific/Kiritimati' })   // 10:00Z = 00:00 on the 27th
    await boot($, on, { [PATH]: { text: JSON.stringify(far), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /Top 3 today/)).toBe(false)
    await ui.unmount()
  })

  test(`Nothing on you appears only with full coverage, on ${surface}`, async ($, on) => {
    const ok = doc({ items: [], counts: { on_you: 0, neglected: 0, suggestion: 0 }, coverage: { chat: { status: 'ok' }, tracker: { status: 'ok' } } })
    await boot($, on, { [PATH]: { text: JSON.stringify(ok), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /^Nothing on you\. Next check 10:05\.$/)).toBe(true)
    await ui.unmount()
  })

  test(`a coverage gap never says Nothing on you, on ${surface}`, async ($, on) => {
    const gap = doc({ items: [], counts: { on_you: 0, neglected: 0, suggestion: 0 } })
    await boot($, on, { [PATH]: { text: JSON.stringify(gap), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /Nothing on you/)).toBe(false)
    expect(await hasText(ui, /couldn't check tracker/)).toBe(true)
    const ip = await inspectFrom($, ui, 'health-open')
    expect(await hasText(ip, /tracker.*since 08:05/i)).toBe(true)
    await ip.unmount()
    await ui.unmount()
  })

  test(`no doc says the tick runs on the home machine, on ${surface}`, async ($, on) => {
    await boot($, on, {})
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /^Tick runs on the home machine; nothing here yet\.$/)).toBe(true)
    await ui.unmount()
  })

  test(`an unreadable file shows the error line with the previous time, on ${surface}`, async ($, on) => {
    const files = { [PATH]: { text: JSON.stringify(doc({ generated_at: '2026-10-26T11:05:00Z' })), mtimeMs: 1 } }
    const { clock } = await boot($, on, files)
    files[PATH] = { text: '{nope', mtimeMs: 2 }
    await clock.advance(60_000)
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /^Couldn't read now\.json — showing the 11:05 list\.$/)).toBe(true)
    expect(await hasText(ui, /^On you 3$/)).toBe(true)
    await ui.unmount()
  })

  test(`an error with no previous doc says it could not read the file, on ${surface}`, async ($, on) => {
    await boot($, on, { [PATH]: { text: '{nope', mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /^Couldn't read now\.json\.$/)).toBe(true)
    expect(await hasText(ui, /Tick runs on the home machine/)).toBe(false)
    await ui.unmount()
  })

  test(`times use the doc's timezone, on ${surface}`, async ($, on) => {
    const d = doc({ timezone: 'Asia/Jerusalem' })   // UTC+2 on 2026-10-26: Israel's DST ended on the 25th
    await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    const header = (await texts(ui)).find(t => /^Now · /.test(t)) ?? ''
    expect(header).toMatch(/^Now · 11:05 · next 12:05$/)
    await ui.unmount()
  })

  test(`Top 3: Done on an entry runs top done <rank>; Make top 3 uses the lowest open rank, on ${surface}`, async ($, on) => {
    const top = [1, 2, 3].map(r => ({ rank: r, title: 'T' + r, why: '', link: null, item_id: r === 1 ? 'a' : null, set_by: 'agent', state: r === 3 ? 'done' : 'open' }))
    const d = doc({ top })
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } }, false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await ui.find({ key: 'top-done-3' })).toBeUndefined()      // a done entry has no Done
    const ip = await inspectFrom($, ui, 'row-a')
    expect(await ip.find({ key: 'ip-maketop' })).toBeUndefined()        // already in Top 3: no Make top 3
    expect(await hasText(ip, /★ Top 3/)).toBe(true)
    await ui.press({ key: 'top-done-1' })
    await ui.press({ key: 'row-c' })
    await ip.press({ key: 'ip-maketop' })
    expect(nowCalls(out)).toEqual([['top', 'done', '1'], ['top', 'swap', '2', 'c']])
    await ip.unmount()
    await ui.unmount()
  })

  test(`Make top 3 is hidden when every rank is done, on ${surface}`, async ($, on) => {
    const top = [1, 2, 3].map(r => ({ rank: r, title: 'T' + r, why: '', link: null, item_id: null, set_by: 'agent', state: 'done' }))
    await boot($, on, { [PATH]: { text: JSON.stringify(doc({ top })), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await ui.find({ key: 'maketop-a' })).toBeUndefined()
    await ui.unmount()
  })

  test(`Swap needs an item id in the hint, on ${surface}`, async ($, on) => {
    const withId = doc({ top: TOP1, swap_hint: { title: 'Better', why: 'w', link: null, item_id: 'd' } })
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(withId), mtimeMs: 1 } }, false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    await ui.press({ key: 'swap-hint' })
    expect(nowCalls(out)[0]).toEqual(['top', 'swap', '3', 'd'])
    await ui.unmount()
  })

  test(`a hint without an item id has no Swap button, on ${surface}`, async ($, on) => {
    const noId = doc({ top: TOP1, swap_hint: { title: 'Better', why: 'w', link: null } })
    await boot($, on, { [PATH]: { text: JSON.stringify(noId), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /Better/)).toBe(true)
    expect(await ui.find({ key: 'swap-hint' })).toBeUndefined()
    await ui.unmount()
  })

  test(`Home rows carry no hotkeys: the first row takes the ring and opens the Inspector, on ${surface}`, async ($, on) => {
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hotkeys(ui)).toEqual([])
    expect((await ui.find({ key: 'row-a' }))?.props.autoFocus).toBe(true)
    expect(await ui.find({ key: 'done-a' })).toBeUndefined()     // the actions live on the item's page
    await ui.press({ key: 'row-a' })
    expect(out.opened).toEqual(['mother-inspector'])
    expect(out.fills).toEqual([])
    await ui.unmount()
  })

  test(`a link inside a page pushes it; Back pops through the stack, on ${surface}`, async ($, on) => {
    const a: any = item('a', 'on_you', 'open', 'Alpha')
    a.team = 'platform'
    const b: any = item('b', 'on_you', 'open', 'Bravo')
    b.team = 'platform'
    const d = doc({ items: [a, b, { ...item('n', 'neglected', 'open', 'Stalled N'), team: 'platform', signal: 'pr_no_reviewer' }], counts: { on_you: 2, neglected: 1, suggestion: 0 }, coverage: {} })
    await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'row-a')
    await ip.press({ key: 'ip-team' })
    expect(await hasText(ip, /^platform$/)).toBe(true)
    expect(await hasText(ip, /^pr no reviewer · 1$/)).toBe(true)
    expect(await hasText(ip, /no per-person counts/)).toBe(true)
    await ip.press({ key: 'it-on-b-b' })
    expect(await hasText(ip, /Bravo/)).toBe(true)
    expect(await hasText(ip, / Item › Team · platform › Item$/)).toBe(true)
    await ip.press({ key: 'insp-back' })
    expect(await hasText(ip, /^platform$/)).toBe(true)
    await ip.press({ key: 'insp-back' })
    expect(await hasText(ip, /Alpha/)).toBe(true)
    await ui.press({ key: 'row-b' })                  // a Home row starts a fresh stack
    expect(await ip.find({ key: 'insp-back' })).toBeUndefined()
    await ip.unmount()
    await ui.unmount()
  })

  test(`no hotkey is drawn twice, even with Top 3 and suggestions, on ${surface}`, async ($, on) => {
    const top = [1, 2].map(r => ({ rank: r, title: 'T' + r, why: '', link: null, item_id: null, set_by: 'agent', state: 'open' }))
    const d: any = doc({ top, items: [...doc().items, item('s1', 'suggestion', 'open', 'Nudge')] })
    d.items[d.items.length - 1].actions = [{ id: 'S1', label: 'n', prompt: 'mother now do S1', task: 't' }]
    await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    for (const key of [undefined, 'top-done-2', 'doit-s1']) {
      if (key) await $.ui.focus({ requestId: 'mother-now', key } as any)
      const hk = (await hotkeys(ui)).map((x: string) => x[0])
      expect(new Set(hk).size).toBe(hk.length)
      expect(hk.length).toBeGreaterThan(0)
    }
    await ui.unmount()
  })

  test(`the first open Top 3 entry holds the hotkeys until another entry is focused, on ${surface}`, async ($, on) => {
    const top = [1, 2].map(r => ({ rank: r, title: 'T' + r, why: '', link: null, item_id: null, set_by: 'agent', state: 'open' }))
    await boot($, on, { [PATH]: { text: JSON.stringify(doc({ top })), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hotkeys(ui)).toEqual(['a:top-ask-1', 'd:top-done-1'])
    await $.ui.focus({ requestId: 'mother-now', key: 'row-a' } as any)       // a stream row holds no hotkeys
    expect(await hotkeys(ui)).toEqual(['a:top-ask-1', 'd:top-done-1'])
    await $.ui.focus({ requestId: 'mother-now', key: 'top-ask-2' } as any)
    expect(await hotkeys(ui)).toEqual(['a:top-ask-2', 'd:top-done-2'])
    await ui.unmount()
  })

  test(`a manager-set Top 3 says set by you with the time, on ${surface}`, async ($, on) => {
    const top = [{ rank: 1, title: 'T1', why: '', link: null, item_id: null, set_by: 'manager', set_at: '2026-10-26T09:40:00Z', state: 'open' }]
    await boot($, on, { [PATH]: { text: JSON.stringify(doc({ top })), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /^ set by you at 09:40$/)).toBe(true)
    await ui.unmount()
  })

  test(`an agent-set Top 3 shows its set_at, on ${surface}`, async ($, on) => {
    await boot($, on, { [PATH]: { text: JSON.stringify(doc({ top: [{ ...TOP1[0], set_at: '2026-10-26T07:05:00Z' }] })), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /^ set by agent at 07:05$/)).toBe(true)
    await ui.unmount()
  })

  test(`an open picker closes when the Inspector shows another page, on ${surface}`, async ($, on) => {
    await boot($, on, { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } }, false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'row-b')
    await ip.press({ key: 'ip-snooze' })
    expect(await ip.find({ type: 'Select', key: 'fp-snooze-pick' })).toBeDefined()
    await ui.press({ key: 'row-a' })
    expect(await hasText(ip, /^a$/)).toBe(true)
    expect(await ip.find({ type: 'Select', key: 'fp-snooze-pick' })).toBeUndefined()
    await ip.unmount()
    await ui.unmount()
  })

  test(`a good stat clears a stale EACCES error even with an unchanged mtime, on ${surface}`, async ($, on) => {
    const saved = { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } }
    const files: Record<string, { text: string; mtimeMs: number }> = { ...saved }
    const { clock, out } = await boot($, on, files, false, {}, 'EACCES: permission denied')
    delete files[PATH]
    await clock.advance(60_000)
    expect(String(out.errors[out.errors.length - 1])).toContain('EACCES')
    files[PATH] = saved[PATH]
    await clock.advance(60_000)
    expect(out.errors[out.errors.length - 1]).toBe(null)
  })

  test(`Ask Mother chips fill the prompt from the list, never submit, on ${surface}`, async ($, on) => {
    const d: any = doc({ routines: [{ id: 'brief', state: 'failed', expected: '2026-10-26T06:47:00Z' }] })
    d.items[0] = { ...d.items[0], id: 'mail:x', signal: 'mail_needs_reply', title: 'Reply to Dana' }
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    const chips = (await ui.findAll({ type: 'Button' })).filter((b: any) => String(b.key).startsWith('prompt-')).map((b: any) => b.key)
    expect(chips).toEqual(['prompt-next', 'prompt-routines', 'prompt-recheck', 'prompt-replies', 'prompt-neglected'])
    await ui.press({ key: 'prompt-routines' })
    await ui.press({ key: 'prompt-replies' })
    expect(out.fills[0].text).toMatch(/brief \(failed\).*mother:doctor/)
    expect(out.fills[1].mode).toBe('replace')
    expect(out.fills[1].text).toContain('"Reply to Dana"')
    expect(out.fills[1].text).toMatch(/never send/)
    expect(out.submits).toBe(0)
    expect(nowCalls(out)).toEqual([])
    await ui.unmount()
  })

  test(`Ask Mother on an item fills a prompt with its title, why and link, on ${surface}`, async ($, on) => {
    const d: any = doc()
    d.items[0].why = 'waiting since Monday'
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /^waiting since Monday$/)).toBe(false)    // Home rows are one line
    const ip = await inspectFrom($, ui, 'row-a')
    expect(await hasText(ip, /^waiting since Monday$/)).toBe(true)
    await ip.press({ key: 'ip-ask' })
    expect(out.fills[0].text).toBe('Help me close this out: "a" — waiting since Monday (https://example.com/a). What is the fastest next step? Draft anything that reaches another person; don\'t send.')
    expect(out.submits).toBe(0)
    await ip.unmount()
    await ui.unmount()
  })

  test(`every action of an item gets its own button, on ${surface}`, async ($, on) => {
    const d: any = doc({ coverage: {} })
    d.items[0].actions = [{ id: 'S1', label: 'Draft a reply', prompt: 'mother now do S1', task: 't' },
                          { id: 'S2', label: 'Nudge owner', prompt: 'mother now do S2', task: 't' }]
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'row-a')
    expect((await ip.find({ key: 'ip-move0' }))?.text).toBe('Nudge owner')
    await ip.press({ key: 'ip-move0' })
    expect(out.fills).toEqual([{ text: 'mother now do S2', mode: 'replace' }])
    await ip.unmount()
    await ui.unmount()
  })

  test(`a long group shows its first rows and a Show more toggle, on ${surface}`, async ($, on) => {
    const many = Array.from({ length: 8 }, (_, n) => item('m' + n, 'on_you', 'open', 'Item ' + n))
    await boot($, on, { [PATH]: { text: JSON.stringify(doc({ items: many })), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /^On you 8$/)).toBe(true)
    expect(await ui.find({ key: 'row-m4' })).toBeDefined()
    expect(await ui.find({ key: 'row-m5' })).toBeUndefined()
    expect((await ui.find({ key: 'more-other' }))?.text).toBe('Show 3 more')
    await ui.press({ key: 'more-other' })
    expect(await ui.find({ key: 'row-m7' })).toBeDefined()
    expect((await ui.find({ key: 'more-other' }))?.text).toBe('Show fewer')
    await ui.unmount()
  })

  test(`on-you rows group by verb; decisions fold into one row that opens the queue, on ${surface}`, async ($, on) => {
    const dec = (n: number, team: string) => ({ ...item('dec' + n, 'on_you', 'open', 'Decision ' + n), signal: 'decision_open', team,
      actions: [{ id: 'D' + n, label: 'Ticket it', prompt: `${n} ticket`, task: 't' }, { id: 'E' + n, label: 'Drop', prompt: `${n} drop`, task: 't' }] })
    const chat = kinded('chat', 'unanswered_mention', 'Question', 'https://example.com/t/1')
    const pr = kinded('pr', 'review_requested', 'Review api#7', 'https://example.com/acme/api/pull/7')
    const d = doc({ items: [dec(1, 'sub'), dec(2, 'sub'), dec(3, 'hima'), chat, pr], counts: { on_you: 5, neglected: 0, suggestion: 0 }, coverage: {} })
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } }, false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    const all = await texts(ui)
    const at = (re: RegExp) => all.findIndex(t => re.test(t))
    const order = [/^Decide 3$/, /^Reply 1$/, /^Review 1$/].map(at)
    expect(order.every(i => i >= 0)).toBe(true)
    expect([...order].sort((a, b) => a - b)).toEqual(order)
    expect(await ui.find({ key: 'row-dec1' })).toBeUndefined()
    expect(await hasText(ui, /^ sub 2 · hima 1$/)).toBe(true)
    const ip = await inspectFrom($, ui, 'decide-open')
    expect(await hasText(ip, /^1 of 3 · sub/)).toBe(true)
    expect(await hotkeys(ip)).toEqual(['1:id-opt0', '2:id-opt1', 'a:id-ask', 'n:id-skip', 'd:id-done'])
    await ip.press({ key: 'id-opt1' })
    expect(out.fills).toEqual([{ text: '1 drop', mode: 'replace' }])
    await ip.press({ key: 'id-skip' })
    expect(await hasText(ip, /^2 of 3/)).toBe(true)
    await ip.press({ key: 'id-team-hima' })
    expect(await hasText(ip, /^1 of 1 · hima/)).toBe(true)
    expect(await ip.find({ key: 'id-skip' })).toBeUndefined()
    await ip.press({ key: 'id-done' })
    expect(nowCalls(out)).toEqual([['done', 'dec3']])
    await ip.unmount()
    await ui.unmount()
  })

  test(`a Top 3 entry shows its why and the linked item's action, and opens the item, on ${surface}`, async ($, on) => {
    const d: any = doc({ top: [{ rank: 1, title: 'Ship it', why: 'due today', link: null, item_id: 'b', set_by: 'agent', state: 'open' }] })
    d.items[1].actions = [{ id: 'S9', label: 'Draft the post', prompt: 'mother now do S9', task: 't' }]
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /^due today$/)).toBe(true)
    expect(await hotkeys(ui)).toEqual(['x:top-doit-1', 'a:top-ask-1', 'd:top-done-1'])
    await ui.press({ key: 'top-doit-1' })
    expect(out.fills).toEqual([{ text: 'mother now do S9', mode: 'replace' }])
    expect(await hasText(ui, /^★ b$/)).toBe(true)                 // the item marks that it is in Top 3
    const ip = await inspectFrom($, ui, 'top-row-1')
    expect((await ip.find({ key: 'ip-primary' }))?.text).toBe('Draft the post')
    await ip.unmount()
    await ui.unmount()
  })

  // ---- the hub ------------------------------------------------------------------------------------
  const HUB = WS + '/.mother/hub'
  const iso = (msAgo: number) => new Date(NOW - msAgo).toISOString()
  const MIN = 60_000
  const watcher = (over: Record<string, unknown> = {}) => ({
    at: iso(MIN), every_seconds: 120, active: true,
    sources: { chat: { status: 'ok' }, tracker: { status: 'couldnt_check', reason: 'no connection' }, mail: { status: 'partial' } }, errors: [], ...over,
  })
  const hubDoc = (over: Record<string, unknown> = {}) => ({
    version: 1, last_wake: iso(2 * MIN), inbox_cursor: null, next_update: 2, routed: [], pending: [], updates: [], ...over,
  })
  const session = (over: Record<string, unknown> = {}) => ({
    session_id: 'sid-1', name: 'plat-1', title: 'Fix the login', link: 'claude://session/sid-1', also: [],
    opened_at: iso(48 * 60 * MIN), last_routed_at: iso(3 * 60 * MIN), unseen: 0, state: 'live', ...over,
  })
  const sessionsDoc = (map: Record<string, unknown>) => ({ version: 1, sessions: map })
  // texts plus Link labels (a session's title is a Link on the terminal)
  const withLinks = async (ui: any) => [...await texts(ui), ...(await ui.findAll({ type: 'Link' })).map((l: any) => l.text as string)]
  const hasAny = async (ui: any, re: RegExp) => (await withLinks(ui)).some((t: string) => re.test(t))
  const hubFiles = (w: unknown, h: unknown, ss: unknown, mt = 1) => {
    const f: Record<string, { text: string; mtimeMs: number }> = { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } }
    if (w) f[HUB + '/watcher.json'] = { text: JSON.stringify(w), mtimeMs: mt }
    if (h) f[HUB + '/hub.json'] = { text: JSON.stringify(h), mtimeMs: mt }
    if (ss) f[HUB + '/sessions.json'] = { text: JSON.stringify(ss), mtimeMs: mt }
    return f
  }
  const bundle = (w: any, h: any, ss: any) => ({ watcher: w, hub: h, sessions: ss })

  test(`sourceMark: a failed source says how old its reading is, on ${surface}`, () => {
    expect(sourceMark('code', { status: 'ok', at: iso(90 * MIN) }, false, NOW)).toBe('code ✓')
    expect(sourceMark('chat', { status: 'couldnt_check', at: iso(120 * MIN) }, false, NOW)).toMatch(/^chat ✗ 2h ago$/)
    expect(sourceMark('mail', { status: 'partial', at: iso(5 * MIN) }, false, NOW)).toMatch(/^mail ~ 5m ago$/)
    expect(sourceMark('tracker', null, false, NOW)).toBe('tracker ✗')
  })

  test(`hubStatus: absent, alive, stale, down and waiting, on ${surface}`, () => {
    expect(hubStatus(bundle(null, null, null), NOW)).toBe('')
    expect(hubStatus(bundle(watcher(), hubDoc(), null), NOW)).toBe(' · hub ●')
    // a quiet day: an old last_wake with nothing pending is normal
    expect(hubStatus(bundle(watcher(), hubDoc({ last_wake: iso(5 * 60 * MIN) }), null), NOW)).toBe(' · hub ●')
    // the watcher's own poll is older than 2 x every_seconds inside active hours
    expect(hubStatus(bundle(watcher({ at: iso(5 * MIN) }), hubDoc(), null), NOW)).toBe(' · hub ◌ stale')
    // outside active hours the watcher polls slowly: not stale
    expect(hubStatus(bundle(watcher({ at: iso(5 * MIN), active: false }), hubDoc(), null), NOW)).toBe(' · hub ●')
    // work pending and an old last_wake, but nothing appended since it woke: the hub is not behind
    const pending = [{ key: 'PLAT-1', title: 'T', link: null, proposed_at: iso(10 * MIN), raised_at: null }]
    expect(hubStatus(bundle(watcher(), hubDoc({ pending, last_wake: iso(25 * MIN) }), null), NOW)).toBe(' · hub ● · 1 waiting')
    // a line it must decide arrived after its last wake, more than 2 x every_seconds ago: stale
    expect(hubStatus(bundle(watcher({ last_append_at: iso(5 * MIN) }), hubDoc({ last_wake: iso(25 * MIN) }), null), NOW)).toBe(' · hub ◌ stale')
    expect(hubStatus(bundle(watcher({ last_append_at: iso(5 * MIN) }), hubDoc({ last_wake: null }), null), NOW)).toBe(' · hub ◌ stale')
    // ... but not yet 2 x every_seconds, or already woken since, or outside active hours: alive
    expect(hubStatus(bundle(watcher({ last_append_at: iso(3 * MIN) }), hubDoc({ last_wake: iso(25 * MIN) }), null), NOW)).toBe(' · hub ●')
    expect(hubStatus(bundle(watcher({ last_append_at: iso(5 * MIN) }), hubDoc({ last_wake: iso(4 * MIN) }), null), NOW)).toBe(' · hub ●')
    expect(hubStatus(bundle(watcher({ last_append_at: iso(5 * MIN), active: false }), hubDoc({ last_wake: iso(25 * MIN) }), null), NOW)).toBe(' · hub ●')
    // down: watcher.json missing, or its at older than max(10 minutes, 2 x every_seconds)
    expect(hubStatus(bundle(null, hubDoc(), null), NOW)).toBe(' · hub ✗')
    expect(hubStatus(bundle(watcher({ at: iso(11 * MIN) }), hubDoc(), null), NOW)).toBe(' · hub ✗')
    // outside active hours with a 30-minute idle interval, a 20-minute-old poll is not down
    expect(hubStatus(bundle(watcher({ at: iso(20 * MIN), active: false, every_seconds: 1800 }), hubDoc(), null), NOW)).toBe(' · hub ●')
    expect(hubStatus(bundle(watcher({ at: iso(61 * MIN), active: false, every_seconds: 1800 }), hubDoc(), null), NOW)).toBe(' · hub ✗')
    // waiting = pending + live sessions with unseen
    const ss = sessionsDoc({ 'PLAT-2': session({ unseen: 2 }), 'PLAT-3': session({ unseen: 0 }), 'PLAT-4': session({ unseen: 1, state: 'dropped' }) })
    expect(hubStatus(bundle(watcher(), hubDoc({ pending }), ss), NOW)).toBe(' · hub ● · 2 waiting')
  })

  test(`the status line carries the hub and the count, on ${surface}`, async ($, on) => {
    const ss = sessionsDoc({ 'PLAT-2': session({ unseen: 1 }) })
    const { out } = await boot($, on, hubFiles(watcher(), hubDoc(), ss))
    expect(last(out)).toBe('◆ 3 on you · 2 neglected · tracker ✗ · hub ● · 1 waiting')
  })

  test(`no hub files means no hub on the status line or in the pane, on ${surface}`, async ($, on) => {
    const { out } = await boot($, on, hubFiles(null, null, null))
    expect(last(out)).toBe('◆ 3 on you · 2 neglected · tracker ✗')
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /^Sessions/)).toBe(false)
    await ui.unmount()
  })

  test(`the Mother page carries the hub's health row and Refresh; Restart hub only when not alive, on ${surface}`, async ($, on) => {
    const { out } = await boot($, on, hubFiles(watcher(), hubDoc(), null))
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /^Sessions 0 live · hub ●$/)).toBe(true)
    expect((await ui.find({ key: 'home-system' }))?.text).toBe("Mother · 1 can't check · hub ● ›")
    expect(await ui.find({ key: 'hub-restart' })).toBeUndefined()
    const ip = await inspectFrom($, ui, 'home-system')
    expect((await ip.find({ key: 'hub-refresh' }))?.text).toBe('Refresh')
    expect(await hasText(ip, /polled 1m ago/)).toBe(true)
    expect(await hasText(ip, /^chat ✓$/)).toBe(true)
    expect(await hasText(ip, /^mail ~/)).toBe(true)
    expect(await hasText(ip, /^tracker ✗/)).toBe(true)
    expect(await ip.find({ key: 'hub-restart' })).toBeUndefined()
    expect(out.fills).toEqual([])
    await ip.unmount()
    await ui.unmount()
  })

  test(`a source is marked stale against its own interval, not the watcher's, on ${surface}`, async ($, on) => {
    const sources = {
      chat: { status: 'ok', every_seconds: 120, at: iso(MIN) },
      mail: { status: 'ok', every_seconds: 3600, at: iso(90 * MIN) },       // 90 min old, but hourly: fine
      tracker: { status: 'ok', every_seconds: 600, at: iso(25 * MIN) },     // older than 2 x 10 min: stale
      code: { status: 'ok' },                                                // no per-source fields: not judged
    }
    // the watcher itself polled a minute ago, so the hub stays alive: only the one source is marked
    expect(hubStatus(bundle(watcher({ sources }), hubDoc(), null), NOW)).toBe(' · hub ●')
    await boot($, on, hubFiles(watcher({ sources }), hubDoc(), null))
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'home-system')
    expect(await hasText(ip, /^chat ✓$/)).toBe(true)
    expect(await hasText(ip, /^mail ✓$/)).toBe(true)
    expect(await hasText(ip, /^tracker ✓ stale$/)).toBe(true)
    expect(await hasText(ip, /^code ✓$/)).toBe(true)
    expect(await hasText(ui, /hub ●/)).toBe(true)
    await ip.unmount()
    await ui.unmount()
  })

  test(`sourceStale: own interval, active hours only, ${surface}`, () => {
    const row = (every: number, ago: number) => ({ every_seconds: every, at: iso(ago) })
    expect(sourceStale(row(600, 19 * MIN), true, NOW)).toBe(false)
    expect(sourceStale(row(600, 21 * MIN), true, NOW)).toBe(true)
    expect(sourceStale(row(600, 21 * MIN), false, NOW)).toBe(false)   // outside active hours
    expect(sourceStale(row(0, 21 * MIN), true, NOW)).toBe(false)      // not polled in this period
    expect(sourceStale({ every_seconds: 600 }, true, NOW)).toBe(false)
    expect(sourceStale({ every_seconds: 600, at: 'garbage' }, true, NOW)).toBe(false)
    expect(sourceStale(null, true, NOW)).toBe(false)
  })

  test(`Restart hub shows on Home and the Mother page when watcher.json is missing, on ${surface}`, async ($, on) => {
    await boot($, on, hubFiles(null, hubDoc(), null))
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await ui.find({ key: 'hub-restart' })).toBeDefined()
    expect(await hasText(ui, /The hub is down/)).toBe(true)
    const ip = await inspectFrom($, ui, 'home-system')
    expect(await hasText(ip, /watcher not running/)).toBe(true)
    expect(await ip.find({ key: 'hub-restart' })).toBeDefined()
    await ip.unmount()
    await ui.unmount()
  })

  test(`Restart hub shows when the heartbeat is stale, on ${surface}`, async ($, on) => {
    await boot($, on, hubFiles(watcher({ at: iso(5 * MIN) }), hubDoc(), null))
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await ui.find({ key: 'hub-restart' })).toBeDefined()
    await ui.unmount()
  })

  test(`Restart hub fills /mother:hub and never submits, on ${surface}`, async ($, on) => {
    const { out } = await boot($, on, hubFiles(watcher({ at: iso(20 * MIN) }), hubDoc(), null))
    const ui = await $.ui.mount({ ...PANE, surface })
    await ui.press({ key: 'hub-restart' })
    expect(out.fills).toEqual([{ text: '/mother:hub', mode: 'replace' }])
    expect(out.submits).toBe(0)
    await ui.unmount()
  })

  test(`Home lists three sessions, news first, and View all opens the full list, on ${surface}`, async ($, on) => {
    const ss = sessionsDoc({
      'PLAT-1': session({ title: 'Quiet one', last_routed_at: iso(10 * MIN) }),
      'PLAT-2': session({ title: 'Has news', unseen: 2, last_routed_at: iso(60 * MIN) }),
      'PLAT-3': session({ title: 'Older', last_routed_at: iso(5 * 60 * MIN) }),
      'PLAT-4': session({ title: 'Oldest', last_routed_at: iso(9 * 60 * MIN) }),
      'PLAT-5': session({ title: 'Archived one', state: 'dropped' }),
    })
    const pending = [{ key: 'PLAT-9', title: 'Look at the rollout', link: null, proposed_at: iso(MIN), raised_at: null }]
    await boot($, on, hubFiles(watcher(), hubDoc({ pending }), ss))
    const ui = await $.ui.mount({ ...PANE, surface })
    const rows = (await ui.findAll({ type: 'Button' })).filter((b: any) => String(b.key).startsWith('sess-row-')).map((b: any) => b.text)
    expect(rows).toEqual(['◆ Has news', '◆ Quiet one', '◆ Older'])
    expect(await hasText(ui, /^ 2 new$/)).toBe(true)
    expect(await hasText(ui, /^Sessions 4 live · 1 proposed · hub ●$/)).toBe(true)
    expect((await ui.find({ key: 'pend-row-PLAT-9' }))?.text).toBe('◇ Proposed: Look at the rollout')
    expect((await ui.find({ key: 'sessions-all' }))?.text).toBe('View all 4 ›')
    const ip = await inspectFrom($, ui, 'sessions-all')
    for (const h of [/^New updates 1$/, /^Live 3$/, /^Proposed 1$/]) expect(await hasText(ip, h)).toBe(true)
    expect(await hasText(ip, /Archived one/)).toBe(false)
    await ip.press({ key: 'il-dropped' })
    expect(await hasText(ip, /Archived one/)).toBe(true)
    await ip.press({ key: 'il-b-PLAT-1' })
    expect(await hasText(ip, /^Quiet one$/)).toBe(true)
    await ip.unmount()
    await ui.unmount()
  })

  test(`the Hub section is open when a session has an update, on ${surface}`, async ($, on) => {
    await boot($, on, hubFiles(watcher(), hubDoc(), sessionsDoc({ 'PLAT-2': session({ unseen: 1 }) })))
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasAny(ui, /Fix the login/)).toBe(true)           // N > 0: open
    await ui.unmount()
  })

  test(`the session page draws a chip for each http also, on ${surface}`, async ($, on) => {
    const ss = sessionsDoc({ 'PLAT-2': session({ unseen: 1, link: null, also: ['https://example.com/acme/api/pull/9', 'PLAT-2'] }) })
    await boot($, on, hubFiles(watcher(), hubDoc(), ss))
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'sess-row-PLAT-2')
    expect((await chipsOf(ip)).filter(([, href]) => /^https:/.test(href))).toEqual([['api#9', 'https://example.com/acme/api/pull/9']])
    await ip.unmount()
    await ui.unmount()
  })

  test(`a session page shows its news and what was routed; a link opens it, Open runs seen, on ${surface}`, async ($, on) => {
    const ss = sessionsDoc({ 'PLAT-2': session({ unseen: 1 }) })
    const routed = [{ line_id: 'l1', key: 'PLAT-2', decision: 'route', session_id: 'sid-1', at: iso(3 * 60 * MIN), note: 'new comment' },
                    { line_id: 'l2', key: 'PLAT-7', decision: 'log', at: iso(MIN) }]
    const { out } = await boot($, on, hubFiles(watcher(), hubDoc({ routed }), ss), false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /^ 1 new$/)).toBe(true)
    expect(await hasText(ui, /^ 3h$/)).toBe(true)
    const ip = await inspectFrom($, ui, 'sess-row-PLAT-2')
    expect(await hasText(ip, /^Fix the login$/)).toBe(true)
    expect(await hasText(ip, /PLAT-2/)).toBe(true)
    expect(await hasText(ip, /^1 new since you last opened it$/)).toBe(true)
    expect(await hasText(ip, /^3h · route — new comment$/)).toBe(true)
    expect(await hasText(ip, /PLAT-7/)).toBe(false)
    const links = await ip.findAll({ type: 'Link' })
    // only the terminal is sent a claude:// link; the other surfaces get the Open button alone
    expect(links.some((l: any) => l.props.href === 'claude://session/sid-1')).toBe(surface === 'terminal')
    await ip.press({ key: 'is-seen' })
    await ip.press({ key: 'sess-open-PLAT-2' })
    expect(out.argv.filter(a => a[2] === 'hub').map(a => a.slice(3))).toEqual([['session', 'seen', 'PLAT-2'], ['session', 'seen', 'PLAT-2']])
    expect(out.fills).toEqual([{ text: 'Open my "Fix the login" session', mode: 'replace' }])
    await ip.unmount()
    await ui.unmount()
  })

  test(`a session with no link has no Link; Open fills the prompt; Stop routing drops it, on ${surface}`, async ($, on) => {
    const ss = sessionsDoc({ 'PLAT-2': session({ unseen: 1, link: null }) })
    const { out } = await boot($, on, hubFiles(watcher(), hubDoc(), ss), false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'sess-row-PLAT-2')
    expect((await ip.findAll({ type: 'Link' })).some((l: any) => /^claude:/.test(l.props.href))).toBe(false)
    expect((await ip.find({ key: 'sess-open-PLAT-2' }))?.text).toBe('Open session')
    await ip.press({ key: 'sess-open-PLAT-2' })
    expect(out.fills).toEqual([{ text: 'Open my "Fix the login" session', mode: 'replace' }])
    await ip.press({ key: 'is-drop' })
    expect(out.argv.filter(a => a[2] === 'hub').map(a => a.slice(3))).toEqual([['session', 'seen', 'PLAT-2'], ['session', 'drop', 'PLAT-2']])
    expect(out.submits).toBe(0)
    await ip.unmount()
    await ui.unmount()
  })

  test(`a dropped session is not shown, on ${surface}`, async ($, on) => {
    const ss = sessionsDoc({ 'PLAT-2': session({ state: 'dropped', unseen: 3, title: 'Gone one' }) })
    await boot($, on, hubFiles(watcher(), hubDoc(), ss))
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /Gone one/)).toBe(false)
    await ui.unmount()
  })

  test(`a rising unseen toasts once across two reads, on ${surface}`, async ($, on) => {
    const files = hubFiles(watcher(), hubDoc(), sessionsDoc({ 'PLAT-2': session({ unseen: 1 }) }))
    const { clock, out } = await boot($, on, files)
    expect(out.toasts.filter(t => t.includes('Update in'))).toEqual(['◆ Update in Fix the login'])
    files[HUB + '/watcher.json'] = { text: JSON.stringify(watcher({ at: iso(0) })), mtimeMs: 2 }
    files[HUB + '/sessions.json'] = { text: JSON.stringify(sessionsDoc({ 'PLAT-2': session({ unseen: 1, last_routed_at: iso(0) }) })), mtimeMs: 2 }
    await clock.advance(60_000)
    expect(out.toasts.filter(t => t.includes('Update in')).length).toBe(1)
    files[HUB + '/sessions.json'] = { text: JSON.stringify(sessionsDoc({ 'PLAT-2': session({ unseen: 2 }) })), mtimeMs: 3 }
    await clock.advance(60_000)
    expect(out.toasts.filter(t => t.includes('Update in')).length).toBe(2)   // it rose again
  })

  test(`a pending proposal toasts once, on ${surface}`, async ($, on) => {
    const pending = [{ key: 'PLAT-9', title: 'Review the rollout', link: null, read: 'r', next_step: 'n', name: 'plat-9', proposed_at: iso(MIN), raised_at: null }]
    const files = hubFiles(watcher(), hubDoc({ pending }), null)
    const { clock, out } = await boot($, on, files)
    expect(out.toasts.filter(t => t.includes('suggests'))).toEqual(['◆ MOTHER suggests a session: Review the rollout'])
    files[HUB + '/hub.json'] = { text: JSON.stringify(hubDoc({ pending, last_wake: iso(0) })), mtimeMs: 2 }
    await clock.advance(60_000)
    expect(out.toasts.filter(t => t.includes('suggests')).length).toBe(1)
  })

  test(`a proposed session's page says what Mother read and how to start it; Dismiss drops it, on ${surface}`, async ($, on) => {
    const pending = [
      { key: 'PLAT-9', title: 'Unraised one', link: null, read: 'Read the thread', next_step: 'Draft options', proposed_at: iso(5 * 60 * MIN), raised_at: null },
      { key: 'PLAT-8', title: 'Raised one', link: null, proposed_at: iso(30 * 60 * MIN), raised_at: iso(29 * 60 * MIN) },
    ]
    const { out } = await boot($, on, hubFiles(watcher(), hubDoc({ pending }), null), false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /Proposed: Unraised one/)).toBe(true)
    expect(await hasText(ui, /^ 5h$/)).toBe(true)
    expect(await hasText(ui, /^ 1d$/)).toBe(true)
    const ip = await inspectFrom($, ui, 'pend-row-PLAT-9')
    expect(await hasText(ip, /card not raised yet/)).toBe(true)
    for (const t of [/^What Mother read$/, /^Read the thread$/, /^The session would$/, /^Draft options$/]) expect(await hasText(ip, t)).toBe(true)
    await ui.press({ key: 'pend-row-PLAT-8' })
    expect(await hasText(ip, /Start it from its card in the hub session/)).toBe(true)
    await ip.press({ key: 'ipp-drop' })
    expect(out.argv.filter(a => a[2] === 'hub').map(a => a.slice(3))).toEqual([['pending', 'drop', 'PLAT-8']])
    await ip.unmount()
    await ui.unmount()
  })

  test(`Done on a hub update runs mother hub done, on ${surface}`, async ($, on) => {
    const updates = [{ id: 'U1', key: 'PLAT-5', title: 'Spec changed', why: 'Dana edited it', link: 'https://example.com/PLAT-5', at: iso(MIN), state: 'open' }]
    const { out } = await boot($, on, hubFiles(watcher(), hubDoc({ updates }), null), false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /^From the hub 1$/)).toBe(true)
    expect(await hasText(ui, /Spec changed/)).toBe(true)
    const ip = await inspectFrom($, ui, 'upd-row-U1')
    expect(await hasText(ip, /^Dana edited it$/)).toBe(true)
    await ip.press({ key: 'hub-done-U1' })
    expect(out.argv.filter(a => a[2] === 'hub')).toEqual([['python3', BIN, 'hub', 'done', 'U1']])
    await ip.unmount()
    await ui.unmount()
  })

  test(`Refresh on the Mother page runs mother hub refresh and toasts the result, on ${surface}`, async ($, on) => {
    const { out } = await boot($, on, hubFiles(watcher(), hubDoc(), null), false, {}, 'ENOENT', () => ({ exitCode: 2 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'home-system')
    await ip.press({ key: 'hub-refresh' })
    expect(out.argv.filter(a => a[2] === 'hub')).toEqual([['python3', BIN, 'hub', 'refresh']])
    expect(out.toasts.some((t: string) => /some sources still fail/.test(t))).toBe(true)
    await ip.unmount()
    await ui.unmount()
  })

  test(`Snooze on a hub update opens a Select and runs mother hub snooze, on ${surface}`, async ($, on) => {
    const updates = [{ id: 'U1', key: 'PLAT-5', title: 'Spec changed', why: 'w', link: null, at: iso(MIN), state: 'open' }]
    const { out } = await boot($, on, hubFiles(watcher(), hubDoc({ updates }), null), false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'upd-row-U1')
    await ip.press({ key: 'hub-snooze-U1' })
    expect(await ip.find({ key: 'hub-snooze-pick-U1' })).toBeDefined()
    await ip.select({ key: 'hub-snooze-pick-U1', value: 'tomorrow' })
    expect(out.argv.filter(a => a[2] === 'hub')).toEqual([['python3', BIN, 'hub', 'snooze', 'U1', 'tomorrow']])
    await ip.unmount()
    await ui.unmount()
  })

  test(`Run in a session on an item with a link runs mother hub add -- <link> and toasts its first line, on ${surface}`, async ($, on) => {
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } }, false, {}, 'ENOENT',
      () => ({ exitCode: 0, stdout: 'added a\nmore\n' }))
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'row-a')
    await ip.press({ key: 'ip-tohub' })
    expect(out.argv.filter(a => a[2] === 'hub')).toEqual([['python3', BIN, 'hub', 'add', '--', 'https://example.com/a']])
    expect(out.toasts).toContain('added a')
    await ip.unmount()
    await ui.unmount()
  })

  test(`Send to hub is absent without an http(s) link, on ${surface}`, async ($, on) => {
    const it = { ...item('a', 'on_you', 'open'), link: 'mailto:x@example.com' }
    await boot($, on, { [PATH]: { text: JSON.stringify(doc({ items: [it], counts: { on_you: 1, neglected: 0, suggestion: 0 } })), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await ui.find({ key: 'tohub-a' })).toBeUndefined()
    await ui.unmount()
  })

  test(`a failed hub verb toasts its first stderr line, on ${surface}`, async ($, on) => {
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } }, false, {}, 'ENOENT',
      () => ({ exitCode: 2, stderr: '\nmother hub add: bad\nusage' }))
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'row-a')
    await ip.press({ key: 'ip-tohub' })
    expect(out.toasts).toContain('mother hub add: bad')
    await ip.unmount()
    await ui.unmount()
  })

  test(`Follow on a session page follows what its key belongs to, and Ignore runs mute -- <key>, on ${surface}`, async ($, on) => {
    const ss = sessionsDoc({ 'PLAT-2': session({ unseen: 1 }), 'acme/web#12': session(), 'C00000001/1700000000.000100': session() })
    const { out } = await boot($, on, hubFiles(watcher(), hubDoc(), ss), false, {}, 'ENOENT', () => ({ exitCode: 0, stdout: 'follow projects PLAT\n' }))
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'sess-row-PLAT-2')
    await ip.press({ key: 'sess-follow-PLAT-2' })
    await ip.press({ key: 'sess-mute-PLAT-2' })
    await ip.select({ key: 'sess-ignore-pick-PLAT-2', value: 'item' })
    await ui.press({ key: 'sess-row-acme/web#12' })
    await ip.press({ key: 'sess-follow-acme/web#12' })
    await ui.press({ key: 'sess-row-C00000001/1700000000.000100' })
    await ip.press({ key: 'sess-follow-C00000001/1700000000.000100' })
    expect(out.argv.filter(a => a[2] === 'hub')).toEqual([
      ['python3', BIN, 'hub', 'follow', '--project', '--', 'PLAT'], ['python3', BIN, 'hub', 'mute', '--', 'PLAT-2'],
      ['python3', BIN, 'hub', 'follow', '--repo', '--', 'acme/web'],
      ['python3', BIN, 'hub', 'follow', '--thread', '--', 'C00000001/1700000000.000100']])
    expect(out.toasts).toContain('follow projects PLAT')
    await ip.unmount()
    await ui.unmount()
  })

  test(`Follow on a WhatsApp group session runs follow --whatsapp, a DM has none, Ignore runs mute -- <key>, on ${surface}`, async ($, on) => {
    const g = 'wa:15550000002-1700000000@g.us'
    const dm = 'wa:15550000001@s.whatsapp.net'
    const lid = 'wa:987654321012345@lid'
    const ss = sessionsDoc({ [g]: session({ unseen: 1 }), [dm]: session({ unseen: 1 }), [lid]: session({ unseen: 1 }) })
    const { out } = await boot($, on, hubFiles(watcher(), hubDoc(), ss), false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'sess-row-' + g)
    await ip.press({ key: 'sess-follow-' + g })
    await ui.press({ key: 'sess-row-' + lid })
    await ip.press({ key: 'sess-mute-' + lid })
    await ip.select({ key: 'sess-ignore-pick-' + lid, value: 'item' })
    expect(await ip.find({ key: 'sess-follow-' + lid })).toBeUndefined()
    await ui.press({ key: 'sess-row-' + dm })
    expect(await ip.find({ key: 'sess-follow-' + dm })).toBeUndefined()
    expect(out.argv.filter(a => a[2] === 'hub')).toEqual([
      ['python3', BIN, 'hub', 'follow', '--whatsapp', '--', '15550000002-1700000000@g.us'],
      ['python3', BIN, 'hub', 'mute', '--', lid]])
    await ip.unmount()
    await ui.unmount()
  })

  test(`a session whose key has nothing to follow shows no Follow, on ${surface}`, async ($, on) => {
    const ss = sessionsDoc({ 'mail:t1': session({ unseen: 1 }), 'cal:e1': session({ unseen: 1 }), 'manual:abc123': session({ unseen: 1 }) })
    await boot($, on, hubFiles(watcher(), hubDoc(), ss))
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await $.ui.mount({ ...INSPECTOR, surface })
    for (const k of ['mail:t1', 'cal:e1', 'manual:abc123']) {
      await ui.press({ key: 'sess-row-' + k })
      expect(await ip.find({ key: 'sess-follow-' + k })).toBeUndefined()
      expect(await ip.find({ key: 'sess-mute-' + k })).toBeDefined()
    }
    await ip.unmount()
    await ui.unmount()
  })

  test(`Follow on a hub update follows what its key belongs to, and Ignore runs mute -- <key>, on ${surface}`, async ($, on) => {
    const updates = [{ id: 'U1', key: 'PLAT-5', title: 'Spec changed', why: 'w', link: null, at: iso(MIN), state: 'open' },
                     { id: 'U2', key: 'acme/web#12', title: 'PR moved', why: 'w', link: null, at: iso(MIN), state: 'open' },
                     { id: 'U3', key: 'C00000001/1700000000.000100', title: 'Thread reply', why: 'w', link: null, at: iso(MIN), state: 'open' }]
    const { out } = await boot($, on, hubFiles(watcher(), hubDoc({ updates }), null), false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'upd-row-U1')
    await ip.press({ key: 'hub-follow-U1' })
    await ip.press({ key: 'hub-mute-U1' })
    await ip.select({ key: 'hub-ignore-pick-U1', value: 'item' })
    await ui.press({ key: 'upd-row-U2' })
    await ip.press({ key: 'hub-follow-U2' })
    await ui.press({ key: 'upd-row-U3' })
    await ip.press({ key: 'hub-follow-U3' })
    expect(out.argv.filter(a => a[2] === 'hub')).toEqual([
      ['python3', BIN, 'hub', 'follow', '--project', '--', 'PLAT'], ['python3', BIN, 'hub', 'mute', '--', 'PLAT-5'],
      ['python3', BIN, 'hub', 'done', 'U1'],
      ['python3', BIN, 'hub', 'follow', '--repo', '--', 'acme/web'],
      ['python3', BIN, 'hub', 'follow', '--thread', '--', 'C00000001/1700000000.000100']])
    await ip.unmount()
    await ui.unmount()
  })

  test(`Ignore on a session offers a week, and the whole channel only for a chat thread, on ${surface}`, async ($, on) => {
    const t = 'C00000001/1700000000.000100'
    const ss = sessionsDoc({ [t]: session({ unseen: 1 }), 'PLAT-2': session({ unseen: 1 }) })
    const { out } = await boot($, on, hubFiles(watcher(), hubDoc(), ss), false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'sess-row-PLAT-2')
    expect((await ip.find({ key: 'sess-mute-PLAT-2' }))?.text).toBe('Ignore…')
    await ip.press({ key: 'sess-mute-PLAT-2' })
    expect(((await ip.find({ type: 'Select', key: 'sess-ignore-pick-PLAT-2' }))?.props.options as any[]).map(o => o.value)).toEqual(['item', 'week'])
    await ip.select({ key: 'sess-ignore-pick-PLAT-2', value: 'week' })
    await ui.press({ key: 'sess-row-' + t })
    await ip.press({ key: 'sess-mute-' + t })
    expect(((await ip.find({ type: 'Select', key: 'sess-ignore-pick-' + t }))?.props.options as any[]).map(o => o.value)).toEqual(['item', 'week', 'channel'])
    await ip.select({ key: 'sess-ignore-pick-' + t, value: 'channel' })
    expect(out.argv.filter(a => a[2] === 'hub')).toEqual([
      ['python3', BIN, 'hub', 'mute', '--for', '1w', '--', 'PLAT-2'],
      ['python3', BIN, 'hub', 'mute', '--scope', 'channel', '--', t]])
    await ip.unmount()
    await ui.unmount()
  })

  test(`Ignore on an item mutes its link, then marks it done, on ${surface}`, async ($, on) => {
    const chat: any = item('a', 'on_you', 'open', 'Question')
    chat.id = 'chat:https://example.com/archives/C00000009/p1700000000123456:unanswered_mention'
    chat.link = 'https://example.com/archives/C00000009/p1700000000123456'
    const d = doc({ items: [chat, item('b', 'on_you', 'open')], counts: { on_you: 2, neglected: 0, suggestion: 0 }, coverage: {} })
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } }, false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'row-' + chat.id)
    await ip.press({ key: 'ip-ignore' })
    expect(((await ip.find({ type: 'Select', key: 'fp-ignore-pick' }))?.props.options as any[]).map(o => o.value)).toEqual(['item', 'week', 'channel'])
    await ip.select({ key: 'fp-ignore-pick', value: 'channel' })
    expect(out.argv.filter(a => a[2] === 'hub' || a[2] === 'now').map(a => a.slice(2))).toEqual([
      ['hub', 'mute', '--scope', 'channel', '--', chat.link], ['now', 'done', chat.id]])
    await ui.press({ key: 'row-b' })
    await ip.press({ key: 'ip-ignore' })
    expect(((await ip.find({ type: 'Select', key: 'fp-ignore-pick' }))?.props.options as any[]).map(o => o.value)).toEqual(['item', 'week'])
    await ip.unmount()
    await ui.unmount()
  })

  test(`Ignore offers the sender and the tick's similar rule when the item has them, on ${surface}`, async ($, on) => {
    const it: any = item('a', 'on_you', 'open', 'CI flaky again')
    it.from = 'U00000009'
    it.similar = { words: ['flaky', 'retry'], channel: 'C00000009' }
    const d = doc({ items: [it], counts: { on_you: 1, neglected: 0, suggestion: 0 }, coverage: {} })
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } }, false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await inspectFrom($, ui, 'row-a')
    await ip.press({ key: 'ip-ignore' })
    const opts = (await ip.find({ type: 'Select', key: 'fp-ignore-pick' }))?.props.options as any[]
    expect(opts.map(o => o.value)).toEqual(['item', 'week', 'sender', 'similar'])
    expect(opts[3].label).toBe('Similar: "flaky" or "retry", in this channel')
    await ip.select({ key: 'fp-ignore-pick', value: 'similar' })
    await ip.press({ key: 'ip-ignore' })
    await ip.select({ key: 'fp-ignore-pick', value: 'sender' })
    expect(out.argv.filter(a => a[2] === 'hub').map(a => a.slice(3))).toEqual([
      ['mute', '--words', 'flaky,retry', '--channel', 'C00000009'], ['mute', '--', 'U00000009']])
    await ip.unmount()
    await ui.unmount()
  })

  test(`the Mother page lists each mute with its expiry and Unignore runs unmute, on ${surface}`, async ($, on) => {
    const files = hubFiles(watcher(), hubDoc(), null)
    files[HUB + '/watch.json'] = { text: JSON.stringify({ version: 1, mute: { keys: ['PLAT-9'], people: ['U00000009'], channels: ['C00000009'], topics: ['topic:flaky|retry@C00000009'], until: { 'PLAT-9': '2026-11-02T10:00:00Z' } } }), mtimeMs: 1 }
    const { out } = await boot($, on, files, false, {}, 'ENOENT', () => ({ exitCode: 0, stdout: 'unmute keys PLAT-9\n' }))
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ key: 'home-system' }))?.text).toMatch(/4 ignored ›$/)
    const ip = await inspectFrom($, ui, 'home-system')
    expect((await ip.find({ key: 'ignored-toggle' }))?.text).toBe('▸ Ignored 4')
    await ip.press({ key: 'ignored-toggle' })
    expect(await hasText(ip, /PLAT-9 · until/)).toBe(true)
    expect(await hasText(ip, /U00000009 · sender/)).toBe(true)
    expect(await hasText(ip, /C00000009 · channel/)).toBe(true)
    expect(await hasText(ip, /"flaky" or "retry" in C00000009 · similar/)).toBe(true)
    await ip.press({ key: 'unignore-PLAT-9' })
    expect(out.argv.filter(a => a[2] === 'hub')).toEqual([['python3', BIN, 'hub', 'unmute', '--', 'PLAT-9']])
    expect(out.toasts).toContain('unmute keys PLAT-9')
    await ip.unmount()
    await ui.unmount()
  })

  test(`no Ignored list when nothing is muted, on ${surface}`, async ($, on) => {
    await boot($, on, hubFiles(watcher(), hubDoc(), null))
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await ui.find({ key: 'ignored-toggle' })).toBeUndefined()
    await ui.unmount()
  })

  test(`a hub update whose key has nothing to follow shows no Follow, on ${surface}`, async ($, on) => {
    const updates = [{ id: 'U1', key: 'mail:t1', title: 'Mail', why: 'w', link: null, at: iso(MIN), state: 'open' },
                     { id: 'U2', key: 'cal:e1', title: 'Meeting', why: 'w', link: null, at: iso(MIN), state: 'open' },
                     { id: 'U3', key: 'manual:abc123', title: 'Note', why: 'w', link: null, at: iso(MIN), state: 'open' }]
    await boot($, on, hubFiles(watcher(), hubDoc({ updates }), null))
    const ui = await $.ui.mount({ ...PANE, surface })
    const ip = await $.ui.mount({ ...INSPECTOR, surface })
    for (const id of ['U1', 'U2', 'U3']) {
      await ui.press({ key: 'upd-row-' + id })
      expect(await ip.find({ key: 'hub-follow-' + id })).toBeUndefined()
      expect(await ip.find({ key: 'hub-mute-' + id })).toBeDefined()
    }
    await ip.unmount()
    await ui.unmount()
  })

  test(`a done or snoozed hub update is not listed, on ${surface}`, async ($, on) => {
    const updates = [{ id: 'U1', key: 'K', title: 'Closed one', why: 'w', link: null, at: iso(MIN), state: 'done' },
                     { id: 'U2', key: 'K2', title: 'Later one', why: 'w', link: null, at: iso(MIN), state: 'snoozed', snooze_until: iso(-MIN) }]
    await boot($, on, hubFiles(watcher(), hubDoc({ updates }), null))
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /Closed one|Later one|Hub updates/)).toBe(false)
    await ui.unmount()
  })

  test(`the Mother page shows the last 10 routed lines; one with a live session opens it, on ${surface}`, async ($, on) => {
    const routed = Array.from({ length: 12 }, (_, n) => ({ line_id: 'l' + n, key: 'PLAT-' + n, decision: 'log', at: iso((12 - n) * MIN) }))
    await boot($, on, hubFiles(watcher(), hubDoc({ routed }), sessionsDoc({ 'PLAT-11': session() })))
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /PLAT-11 log/)).toBe(false)
    const ip = await inspectFrom($, ui, 'home-system')
    expect(await hasText(ip, /^Recently routed 10$/)).toBe(true)
    expect(await hasText(ip, /PLAT-2 log/)).toBe(true)
    expect(await hasText(ip, /PLAT-1 log/)).toBe(false)
    await ip.press({ key: 'routed-l11' })
    expect(await hasText(ip, /^Fix the login$/)).toBe(true)
    await ip.unmount()
    await ui.unmount()
  })

  test(`an item linked to a session shows the marker, Open session, and a link to the session page, on ${surface}`, async ($, on) => {
    const it: any = item('a', 'on_you', 'open', 'Review A')
    it.link = 'https://example.com/browse/PLAT-2'
    const d = doc({ items: [it, item('b', 'on_you', 'open', 'Other B')], counts: { on_you: 2, neglected: 0, suggestion: 0 }, coverage: {} })
    const files = hubFiles(watcher(), hubDoc(), sessionsDoc({ 'PLAT-2': session({ link: null }) }))
    files[PATH] = { text: JSON.stringify(d), mtimeMs: 1 }
    const { out } = await boot($, on, files)
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /◆ session/)).toBe(true)
    const ip = await inspectFrom($, ui, 'row-a')
    expect((await ip.find({ key: 'ip-open-session' }))?.text).toBe('Open session')
    await ip.press({ key: 'ip-open-session' })
    expect(out.fills).toEqual([{ text: 'Open my "Fix the login" session', mode: 'replace' }])
    expect(out.argv.filter(a => a[2] === 'hub')).toEqual([['python3', BIN, 'hub', 'session', 'seen', 'PLAT-2']])
    await ip.press({ key: 'ip-session' })
    expect(await hasText(ip, /^On the Now list 1$/)).toBe(true)
    await ui.press({ key: 'row-b' })
    expect((await ip.find({ key: 'ip-ask' }))?.text).toBe('Ask Mother')
    await ip.unmount()
    await ui.unmount()
  })

  test(`a hub file is re-read only when its mtime moves, on ${surface}`, async ($, on) => {
    const files = hubFiles(watcher(), hubDoc(), null)
    const { clock, out } = await boot($, on, files)
    const before = out.reads
    await clock.advance(60_000)
    expect(out.reads).toBe(before)
  })
}
