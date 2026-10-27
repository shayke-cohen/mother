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

for (const surface of ['terminal', 'desktop'] as const) {
  // The files the fake host holds: path -> { text, mtimeMs }.
  async function boot($: any, on: any, files: Record<string, { text: string; mtimeMs: number }>, pathFails = false, held: Record<string, unknown> = {}, statErr = 'ENOENT') {
    const clock = mock.clock(on, { now: NOW })
    on('store.get', (_: any, e: any) => ({ value: held[e.key] }))
    on('store.set', (_: any, e: any) => { held[e.key] = e.value; return { value: undefined } })
    mock.env(on, { HOME: '/home/m' })
    const out = { stats: 0, reads: 0, errors: [] as unknown[], status: [] as (string | undefined)[], toasts: [] as string[], argv: [] as string[][] }
    on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
    on('ui.status', (_: any, e: any) => { out.status.push(e.text); return { value: undefined } })
    on('ui.toast', (_: any, e: any) => { out.toasts.push(e.text); return { value: undefined } })
    on('state.set', (_: any, e: any, next: any) => { if (e.key === 'error') out.errors.push(e.value); return next(e) })
    on('process.run', (_: any, e: any) => {
      out.argv.push([...e.argv])
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
    expect(stored.slice(-3)).toEqual(['x1', 'x2', 'x3'])
    expect(stored[0]).toBe('old2')
  })
}
