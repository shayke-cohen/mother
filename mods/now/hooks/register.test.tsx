import { expect, mock, test } from 'claude-code/testing'

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
      if (cli && e.argv[2] === 'now') {
        const r = cli([...e.argv])
        return { value: { exitCode: r.exitCode, stdout: r.stdout ?? '', stderr: r.stderr ?? '', isStdoutTruncated: false, isStderrTruncated: false } }
      }
      if (pathFails) return { value: { exitCode: 2, stdout: '', stderr: 'no workspace', isStdoutTruncated: false, isStderrTruncated: false } }
      return { value: { exitCode: 0, stdout: WS + '\n', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    })
    on('fs.stat', (_: any, e: any) => {
      out.stats++
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
    return { clock, out }
  }
  const last = (o: { status: (string | undefined)[] }) => o.status[o.status.length - 1]
  const PATH = WS + '/.mother/now.json'

  test(`status line counts and a failing source, on ${surface}`, async ($, on) => {
    const files = { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } }
    const { out } = await boot($, on, files)
    expect(last(out)).toBe('◆ 3 on you · 2 neglected · tracker ✗')
    expect(out.argv[0]).toEqual(['python3', '/home/m/.claude/plugins/marketplaces/mother/bin/mother', 'path'])
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
    plugin: 'mother-now', component: 'Pane', requestId: 'mother-now',
    props: { title: 'Now', isFocused: true, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} },
  } as const
  const texts = async (ui: any) => (await ui.findAll({ type: 'Text' })).map((t: any) => t.text as string)
  const hasText = async (ui: any, re: RegExp) => (await texts(ui)).some((t: string) => re.test(t))
  const nowCalls = (out: { argv: string[][] }) => out.argv.filter(a => a[2] === 'now').map(a => a.slice(3))
  const TOP1 = [{ rank: 1, title: 'T1', why: '', link: null, item_id: null, set_by: 'agent', state: 'open' }]
  const BIN = '/home/m/.claude/plugins/marketplaces/mother/bin/mother'
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

  test(`sections render in order and carry the header, on ${surface}`, async ($, on) => {
    const d: any = doc({
      top: [{ rank: 1, title: 'Ship it', why: '', link: null, item_id: 'a', set_by: 'agent', state: 'open' },
            { rank: 2, title: 'Done thing', why: '', link: null, item_id: null, set_by: 'agent', state: 'done' }],
      items: [item('a', 'on_you', 'open', 'Review A'), item('s1', 'suggestion', 'open', 'Nudge B'), item('d', 'neglected', 'open', 'Stalled D')],
      counts: { on_you: 1, neglected: 1, suggestion: 1 },
      routines: [{ id: 'dots', state: 'missed', expected: '2026-10-26T13:00:00Z' }],
    })
    d.items[1].actions = [{ id: 'S1', label: 'Nudge', prompt: 'mother now do S1', task: 't' }]
    await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    const all = await texts(ui)
    const at = (re: RegExp) => all.findIndex(t => re.test(t))
    const order = [/Top 3 today/, /On you \(1\)/, /Neglected \(1\)/, /Suggestions \(1\)/, /Couldn't check/, /Routines/].map(at)
    expect(order.every(i => i >= 0)).toBe(true)
    expect([...order].sort((a, b) => a - b)).toEqual(order)
    expect(all.some(t => /^Now · 09:05 · next 10:05$/.test(t))).toBe(true)
    expect(all.some(t => /set by agent/.test(t))).toBe(true)
    expect(all.some(t => /dots missed at 13:00/.test(t))).toBe(true)
    await ui.unmount()
  })

  test(`empty sections are not drawn, on ${surface}`, async ($, on) => {
    const d = doc({ items: [item('a', 'on_you', 'open', 'Review A')], counts: { on_you: 1, neglected: 0, suggestion: 0 }, coverage: { chat: { status: 'ok' } } })
    await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /On you \(1\)/)).toBe(true)
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

  test(`Done runs mother now done and re-reads at once, on ${surface}`, async ($, on) => {
    const files: Record<string, { text: string; mtimeMs: number }> = { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } }
    const after = doc({ items: [item('b', 'on_you', 'open', 'Only B')], counts: { on_you: 1, neglected: 0, suggestion: 0 }, coverage: {} })
    const { out } = await boot($, on, files, false, {}, 'ENOENT', () => {
      files[PATH] = { text: JSON.stringify(after), mtimeMs: 2 }
      return { exitCode: 0 }
    })
    const ui = await $.ui.mount({ ...PANE, surface })
    await ui.press({ key: 'done-a' })
    expect(out.argv.find(a => a[2] === 'now')).toEqual(['python3', BIN, 'now', 'done', 'a'])
    expect(out.reads).toBe(2)   // no timer tick: the press itself re-read the file
    expect(await hasText(ui, /Only B/)).toBe(true)
    expect(await ui.find({ key: 'done-a' })).toBeUndefined()
    expect(await ui.find({ key: 'done-b' })).toBeDefined()
    await ui.unmount()
  })

  test(`a failing CLI toasts its first stderr line and the pane still draws, on ${surface}`, async ($, on) => {
    const files = { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } }
    const { out } = await boot($, on, files, false, {}, 'ENOENT', () => ({ exitCode: 1, stderr: 'mother: unknown id\nTraceback...' }))
    const ui = await $.ui.mount({ ...PANE, surface })
    await ui.press({ key: 'done-a' })
    expect(out.toasts).toContain('mother: unknown id')
    expect(await hasText(ui, /On you \(3\)/)).toBe(true)
    await ui.unmount()
  })

  test(`Snooze opens a Select and runs mother now snooze, on ${surface}`, async ($, on) => {
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } }, false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    await ui.press({ key: 'snooze-a' })
    const sel = await ui.find({ type: 'Select', key: 'snooze-pick-a' })
    expect((sel?.props.options as any[]).map(o => o.value)).toEqual(['1h', 'tomorrow', 'next-week'])
    await ui.select({ key: 'snooze-pick-a', value: 'tomorrow' })
    expect(nowCalls(out)[0]).toEqual(['snooze', 'a', 'tomorrow'])
    await ui.unmount()
  })

  test(`Do it fills the prompt, replaces, never submits, on ${surface}`, async ($, on) => {
    const d: any = doc({ items: [item('s1', 'suggestion', 'open', 'Nudge B')], counts: { on_you: 0, neglected: 0, suggestion: 1 }, coverage: {} })
    d.items[0].actions = [{ id: 'S1', label: 'Nudge', prompt: 'mother now do S1', task: 't' }]
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    await ui.press({ key: 'doit-s1' })
    expect(out.fills).toEqual([{ text: 'mother now do S1', mode: 'replace' }])
    expect(out.submits).toBe(0)
    expect(out.argv.filter(a => a[2] === 'now').length).toBe(0)
    await ui.unmount()
  })

  test(`Do it is drawn for a tick-owned action on an on-you item, with x on the selected row, on ${surface}`, async ($, on) => {
    const d: any = doc({ coverage: {} })
    d.items[0].actions = [{ id: 'S2', label: 'Draft a nudge', prompt: 'mother now do S2', task: 'draft a nudge to the reviewer' }]
    const { out } = await boot($, on, { [PATH]: { text: JSON.stringify(d), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hotkeys(ui)).toEqual(['d:done-a', 's:snooze-a', 't:maketop-a', 'x:doit-a'])
    await ui.press({ key: 'doit-a' })
    expect(out.fills).toEqual([{ text: 'mother now do S2', mode: 'replace' }])
    expect(await ui.find({ key: 'doit-b' })).toBeUndefined()           // no action, no Do it
    await ui.unmount()
  })

  test(`yesterday's Top 3 is not shown, by the doc's timezone, on ${surface}`, async ($, on) => {
    const top = [{ rank: 1, title: 'Old one', why: '', link: null, item_id: null, set_by: 'manager', state: 'open' }]
    await boot($, on, { [PATH]: { text: JSON.stringify(doc({ top, top_date: '2026-10-25' })), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /Top 3 today|Old one/)).toBe(false)
    expect(await hotkeys(ui)).toEqual(['d:done-a', 's:snooze-a', 't:maketop-a'])
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
    expect(await hasText(ui, /Couldn't check/)).toBe(true)
    expect(await hasText(ui, /tracker.*since 08:05/i)).toBe(true)
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
    expect(await hasText(ui, /On you \(3\)/)).toBe(true)
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
    expect(await ui.find({ key: 'maketop-a' })).toBeUndefined()         // already in Top 3: no Make top 3
    await ui.press({ key: 'top-done-1' })
    await ui.press({ key: 'maketop-c' })
    expect(nowCalls(out)).toEqual([['top', 'done', '1'], ['top', 'swap', '2', 'c']])
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

  test(`with no focus event the first row holds the hotkeys, on ${surface}`, async ($, on) => {
    await boot($, on, { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hotkeys(ui)).toEqual(['d:done-a', 's:snooze-a', 't:maketop-a'])
    expect((await ui.find({ key: 'done-a' }))?.props.autoFocus).toBe(true)
    await ui.unmount()
  })

  test(`a ui.focus on another row moves the hotkeys to it, on ${surface}`, async ($, on) => {
    await boot($, on, { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    await $.ui.focus({ requestId: 'mother-now', key: 'snooze-b' } as any)
    expect(await hotkeys(ui)).toEqual(['d:done-b', 's:snooze-b', 't:maketop-b'])
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

  test(`the first open Top 3 entry holds the hotkeys until another row is focused, on ${surface}`, async ($, on) => {
    const top = [1, 2].map(r => ({ rank: r, title: 'T' + r, why: '', link: null, item_id: null, set_by: 'agent', state: 'open' }))
    await boot($, on, { [PATH]: { text: JSON.stringify(doc({ top })), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hotkeys(ui)).toEqual(['d:top-done-1'])
    await $.ui.focus({ requestId: 'mother-now', key: 'done-a' } as any)
    expect(await hotkeys(ui)).toEqual(['d:done-a', 's:snooze-a', 't:maketop-a'])
    await ui.unmount()
  })

  test(`a manager-set Top 3 says set by you with the time, on ${surface}`, async ($, on) => {
    const top = [{ rank: 1, title: 'T1', why: '', link: null, item_id: null, set_by: 'manager', set_at: '2026-10-26T09:40:00Z', state: 'open' }]
    await boot($, on, { [PATH]: { text: JSON.stringify(doc({ top })), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /^set by you at 09:40$/)).toBe(true)
    await ui.unmount()
  })

  test(`an agent-set Top 3 shows its set_at, on ${surface}`, async ($, on) => {
    await boot($, on, { [PATH]: { text: JSON.stringify(doc({ top: [{ ...TOP1[0], set_at: '2026-10-26T07:05:00Z' }] })), mtimeMs: 1 } })
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await hasText(ui, /^set by agent at 07:05$/)).toBe(true)
    await ui.unmount()
  })

  test(`a snooze only closes the Select of the row it acted on, on ${surface}`, async ($, on) => {
    await boot($, on, { [PATH]: { text: JSON.stringify(doc()), mtimeMs: 1 } }, false, {}, 'ENOENT', () => ({ exitCode: 0 }))
    const ui = await $.ui.mount({ ...PANE, surface })
    await ui.press({ key: 'snooze-b' })
    await ui.press({ key: 'done-a' })          // acts on another row while b's Select is open
    expect(await ui.find({ type: 'Select', key: 'snooze-pick-b' })).toBeDefined()
    await ui.select({ key: 'snooze-pick-b', value: '1h' })
    expect(await ui.find({ type: 'Select', key: 'snooze-pick-b' })).toBeUndefined()
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
}
