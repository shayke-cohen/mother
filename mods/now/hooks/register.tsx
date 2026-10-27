import { atom, read, update } from 'claude-code'
import type { Hook, Register, Timer } from 'claude-code'

import type { NowCoverage, NowDoc, NowItem, NowTop } from '../types'

// State the Now panel (Task 9) draws from. Keys are declared in types/index.d.ts.
export const docAtom = atom({ plugin: 'mother-now', key: 'doc' } as const, null)
export const errorAtom = atom({ plugin: 'mother-now', key: 'error' } as const, null)
export const workspaceAtom = atom({ plugin: 'mother-now', key: 'workspace' } as const, null)
export const snoozingAtom = atom({ plugin: 'mother-now', key: 'snoozing' } as const, null)

const PANE = 'mother-now'
const SNOOZES = ['1h', 'tomorrow', 'next-week'] as const
const TOP_MAX = 3

export const POLL_MS = 60_000
export const TOASTED_CAP = 500
const DEFAULT_BIN = '~/.claude/plugins/marketplaces/mother/bin/mother'

// The engine interface a hook receives; the helpers below take it so Task 9 can pass its own `$`.
export type Dollar = Parameters<Hook<'session.start'>>[0]

// What `load` remembers between polls: the mtime it last read, so an unchanged file is not re-read.
export type Loader = { mtimeMs: number | null; statFailed: boolean }
export const newLoader = (): Loader => ({ mtimeMs: null, statFailed: false })

export function statusText(doc: NowDoc | null, nowMs: number): string {
  if (!doc) return '◆ no tick here'
  if (nowMs > Date.parse(doc.stale_after)) return '◆ stale'
  let text = `◆ ${doc.counts?.on_you ?? 0} on you · ${doc.counts?.neglected ?? 0} neglected`
  for (const [source, row] of Object.entries(doc.coverage ?? {})) {
    if (row && row.status !== 'ok') text += ` · ${source} ✗`
  }
  return text
}

export async function motherBin($: Dollar, configured: string): Promise<string> {
  if (configured !== '~' && !configured.startsWith('~/')) return configured
  const home = (await $.env.get('HOME')) ?? ''
  return home + configured.slice(1)
}

async function workspace($: Dollar, bin: string): Promise<string | null> {
  const known = await read($, workspaceAtom)
  if (known) return known
  try {
    const ran = await $.process.run(['python3', bin, 'path'])
    const path = String(ran.stdout ?? '').trim()
    if (ran.exitCode !== 0 || !path) throw new Error(String(ran.stderr ?? '').trim() || 'no workspace')
    await update($, workspaceAtom, () => path)
    return path
  } catch (err) {
    await update($, errorAtom, () => `mother path failed: ${(err as Error).message}`)
    return null
  }
}

async function toastNew($: Dollar, doc: NowDoc): Promise<void> {
  if (doc.quiet) return
  const seen: string[] = ((await $.store.get('toasted')) as string[] | undefined) ?? []
  const fresh = (doc.items ?? []).filter(i => i.section === 'on_you' && i.state === 'new' && !seen.includes(i.id))
  if (fresh.length === 0) return
  for (const i of fresh) $.ui.toast(`◆ New on you: ${i.title}`)
  await $.store.set('toasted', [...seen, ...fresh.map(i => i.id)].slice(-TOASTED_CAP))
}

// One pass: resolve the workspace, re-read now.json when its mtime moved, toast what is new, draw the
// status line. A file that fails to parse (or has another version) sets `error` and keeps the old doc.
export async function load($: Dollar, bin: string, loader: Loader): Promise<void> {
  const ws = await workspace($, bin)
  if (!ws) {
    await update($, docAtom, () => null)
  } else {
    await readDoc($, `${ws}/.mother/now.json`, loader)
  }
  $.ui.status(statusText(await read($, docAtom), await $.clock.now()))
}

// Only a missing file means "no tick here"; any other failure keeps the previous doc and sets `error`.
const isMissing = (err: unknown): boolean => {
  const e = err as { code?: unknown; message?: unknown }
  return e?.code === 'ENOENT' || /ENOENT|no such file|not found/i.test(String(e?.message ?? err))
}

export async function readDoc($: Dollar, path: string, loader: Loader): Promise<void> {
  let mtimeMs: number
  try {
    mtimeMs = (await $.fs.stat(path)).mtimeMs
  } catch (err) {
    if (isMissing(err)) {
      loader.mtimeMs = null
      await update($, docAtom, () => null)
      await update($, errorAtom, () => null)
      loader.statFailed = false
    } else {
      loader.statFailed = true
      await update($, errorAtom, () => `now.json unreadable: ${(err as Error).message ?? String(err)}`)
    }
    return
  }
  // A stat that works again clears the error a failed stat set, even when the file did not change.
  // (A parse error stays until the file changes: the mtime check below is what retries it.)
  if (loader.statFailed) {
    loader.statFailed = false
    await update($, errorAtom, () => null)
  }
  if (mtimeMs === loader.mtimeMs) return
  loader.mtimeMs = mtimeMs
  let parsed: NowDoc
  try {
    const raw = JSON.parse(await $.fs.read(path))
    if (!raw || typeof raw !== 'object' || raw.version !== 1) throw new Error('unsupported version')
    parsed = raw as NowDoc
  } catch (err) {
    await update($, errorAtom, () => `now.json unreadable: ${(err as Error).message}`)
    return
  }
  await update($, docAtom, () => parsed)
  await update($, errorAtom, () => null)
  await toastNew($, parsed).catch(() => undefined)   // a toast or store failure never un-reads a good doc
}


// ---- time ------------------------------------------------------------------------------------------

// HH:MM of an ISO instant in the doc's timezone. Falls back to the HH:MM written in the ISO string
// (the instant's own clock, which is UTC for what lib/now.py writes) when Intl or the zone is missing.
export function clock(iso: string | undefined | null, tz: string | null | undefined): string {
  if (!iso) return '--:--'
  const ms = Date.parse(iso)
  if (tz && !Number.isNaN(ms)) {
    try {
      const parts = new Intl.DateTimeFormat('en-GB', { timeZone: tz, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(ms)
      const part = (t: string) => parts.find(p => p.type === t)?.value
      const h = part('hour'), m = part('minute')
      if (h && m) return `${h === '24' ? '00' : h}:${m}`
    } catch {
      // fall through to the ISO text
    }
  }
  return /T(\d\d:\d\d)/.exec(iso)?.[1] ?? '--:--'
}

export function age(firstSeen: string, nowMs: number): string {
  const hours = Math.max(0, Math.floor((nowMs - Date.parse(firstSeen)) / 3_600_000))
  return Number.isNaN(hours) ? '' : hours < 24 ? `${hours}h` : `${Math.floor(hours / 24)}d`
}

// ---- writes ----------------------------------------------------------------------------------------

// Run one `mother now …` verb, then re-read now.json at once. A failure toasts the first line of stderr.
async function run($: Dollar, bin: string, loader: Loader, args: string[]): Promise<void> {
  try {
    const ran = await $.process.run(['python3', bin, 'now', ...args])
    if (ran.exitCode !== 0) {
      const line = String(ran.stderr ?? '').split('\n').find(l => l.trim()) ?? `mother now ${args[0]} failed`
      $.ui.toast(line.trim())
    }
  } catch (err) {
    $.ui.toast(`mother now ${args[0]} failed: ${(err as Error).message ?? String(err)}`)
  }
  await load($, bin, loader).catch(() => undefined)
  await update($, snoozingAtom, () => null)
}

// The rank a new Top 3 entry takes: the next free one, else the highest rank not done; null = none.
export function lowestOpenRank(top: NowTop[]): number | null {
  if (top.length < TOP_MAX) return top.length + 1
  for (let r = top.length; r >= 1; r--) if (top[r - 1].state !== 'done') return r
  return null
}

// ---- the pane --------------------------------------------------------------------------------------

type Ui = ReturnType<Dollar['ui']['resolve']>

function paneTree($: Dollar, ui: Ui, doc: NowDoc | null, error: string | null, snoozing: string | null, nowMs: number,
                  go: (args: string[]) => Promise<void>) {
  const { Box, Text, Button, Link, Select } = ui
  if (!doc) return <Text dimColor>Tick runs on the home machine; nothing here yet.</Text>

  const tz = doc.timezone
  const at = (iso: string | undefined | null) => clock(iso, tz)
  const items = doc.items ?? []
  const top = doc.top ?? []
  const used = new Set<string>()
  // A hotkey belongs to the first row that offers the action (two on one hotkey: the later wins).
  const hot = (k: string): string | undefined => (used.has(k) ? undefined : (used.add(k), k))
  const inTop = new Set(top.map(e => e.item_id).filter(Boolean))
  const rank = lowestOpenRank(top)
  const gaps = Object.entries(doc.coverage ?? {}).filter(([, c]) => c && c.status !== 'ok') as [string, NowCoverage][]
  const heading = (text: string) => <Text bold>{text}</Text>

  const row = (i: NowItem) => (
    <Box key={'row-' + i.id} flexDirection="column">
      <Box>
        <Text>{'• ' + i.title}</Text>
        <Text dimColor>{' ' + age(i.first_seen, nowMs) + ' '}</Text>
        {i.link ? <Link href={i.link}>↗</Link> : null}
        {i.unverified_since ? <Text color="yellow">{' unverified'}</Text> : null}
      </Box>
      <Box>
        <Button key={'done-' + i.id} hotkey={hot('d')} onPress={() => go(['done', i.id])}>Done</Button>
        <Button key={'snooze-' + i.id} hotkey={hot('s')} onPress={() => update($, snoozingAtom, cur => (cur === i.id ? null : i.id))}>Snooze</Button>
        {rank !== null && !inTop.has(i.id)
          ? <Button key={'maketop-' + i.id} hotkey={hot('t')} onPress={() => go(['top', 'swap', String(rank), i.id])}>Make top 3</Button>
          : null}
        {i.section === 'suggestion' && i.actions?.[0]
          ? <Button key={'doit-' + i.actions[0].id} hotkey={hot('x')}
              onPress={() => { void $.prompt.fill({ text: i.actions[0].prompt || `mother now do ${i.actions[0].id}`, mode: 'replace' }) }}>Do it</Button>
          : null}
      </Box>
      {snoozing === i.id
        ? <Select key={'snooze-pick-' + i.id} label="Snooze for" options={SNOOZES.map(value => ({ value }))}
            onSelect={value => go(['snooze', i.id, value])} />
        : null}
    </Box>
  )

  const section = (name: string, kind: NowItem['section']) => {
    const list = items.filter(i => i.section === kind)
    return list.length === 0 ? null : <Box key={kind} flexDirection="column">{heading(`${name} (${list.length})`)}{list.map(row)}</Box>
  }

  const topEntry = (e: NowTop) => {
    const done = e.state === 'done'
    return (
      <Box key={'top-row-' + e.rank} flexDirection="column">
        <Box>
          <Text dimColor={done} strikethrough={done}>{`${e.rank}. ${e.title}`}</Text>
          {e.link ? <Link href={e.link}>{' ↗'}</Link> : null}
        </Box>
        {done ? null : <Button key={'top-done-' + e.rank} hotkey={hot('d')} onPress={() => go(['top', 'done', String(e.rank)])}>Done</Button>}
      </Box>
    )
  }

  const hint = doc.swap_hint
  const setBy = top[0]?.set_by === 'manager' ? 'you' : 'agent'
  const setAt = top[0]?.set_at ? ` at ${at(top[0].set_at)}` : ''
  const noItems = items.every(i => !['on_you', 'neglected', 'suggestion'].includes(i.section))

  return (
    <Box flexDirection="column">
      <Text bold>{`Now · ${at(doc.generated_at)} · next ${at(doc.next_at)}`}</Text>
      {error ? <Text color="yellow">{`Couldn't read now.json — showing the ${at(doc.generated_at)} list.`}</Text> : null}
      {top.length > 0 ? (
        <Box key="top" flexDirection="column">
          {heading('Top 3 today')}
          <Text dimColor>{`set by ${setBy}${setAt}`}</Text>
          {top.map(topEntry)}
          {hint && hint.title ? (
            <Box>
              <Text dimColor>{`Consider swapping: ${hint.title}`}</Text>
              {hint.item_id
                ? <Button key="swap-hint" onPress={() => go(['top', 'swap', String(TOP_MAX), String(hint.item_id)])}>Swap</Button>
                : null}
            </Box>
          ) : null}
        </Box>
      ) : null}
      {section('On you', 'on_you')}
      {section('Neglected', 'neglected')}
      {section('Suggestions', 'suggestion')}
      {gaps.length > 0 ? (
        <Box key="gaps" flexDirection="column">
          {heading("Couldn't check")}
          {gaps.map(([source, c]) => (
            <Text key={'gap-' + source}>{`• ${source}: ${c.status === 'partial' ? 'partly checked' : "couldn't check"}${c.since ? ' since ' + at(c.since) : ''}`}</Text>
          ))}
        </Box>
      ) : null}
      {(doc.routines ?? []).length > 0 ? (
        <Box key="routines" flexDirection="column">
          {heading('Routines')}
          {doc.routines.map(r => <Text key={'routine-' + r.id}>{`• ${r.id} ${r.state} at ${at(r.expected)}`}</Text>)}
        </Box>
      ) : null}
      {noItems && gaps.length === 0 && top.length === 0 && (doc.routines ?? []).length === 0
        ? <Text>{`Nothing on you. Next check ${at(doc.next_at)}.`}</Text> : null}
    </Box>
  )
}

export const register: Register = (on, options) => {
  const configured = String(options.mother_bin ?? DEFAULT_BIN)
  const loader = newLoader()
  let timer: Timer | undefined

  on('session.start', async ($, e, next) => {
    const bin = await motherBin($, configured)
    await $.command.register({ name: 'now', description: 'Open the Now panel' })
    const poll = () => load($, bin, loader).catch(() => undefined)
    await poll()
    timer?.cancel()          // a repeated session.start must not stack a second poll
    timer = $.clock.every(POLL_MS, poll)
    return next(e)
  })

  on('command.run', { command: 'now' }, async $ => {
    await $.ui.open({ id: PANE, title: 'Now' })
    return { text: 'Now panel opened.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const bin = await motherBin($, configured)
    const go = (args: string[]) => run($, bin, loader, args)
    return paneTree($, $.ui.resolve(e), await read($, docAtom), await read($, errorAtom), await read($, snoozingAtom),
      await $.clock.now(), go)
  })
}
