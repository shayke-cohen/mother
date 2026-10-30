import { atom, read, update } from 'claude-code'
import type { Hook, Register, Timer } from 'claude-code'

import type { HubBundle, HubDoc, HubPending, HubSession, HubSessions, HubUpdate, NowCoverage, NowDoc, NowItem, NowTop, WatcherDoc } from '../types'

// State the Now panel (Task 9) draws from. Keys are declared in types/index.d.ts.
export const docAtom = atom({ plugin: 'mother', key: 'doc' } as const, null)
export const errorAtom = atom({ plugin: 'mother', key: 'error' } as const, null)
export const workspaceAtom = atom({ plugin: 'mother', key: 'workspace' } as const, null)
export const snoozingAtom = atom({ plugin: 'mother', key: 'snoozing' } as const, null)
export const selectedAtom = atom({ plugin: 'mother', key: 'selected' } as const, null)
export const expandedAtom = atom({ plugin: 'mother', key: 'expanded' } as const, null)
export const hubAtom = atom({ plugin: 'mother', key: 'hub' } as const, null)

const PANE = 'mother-now'
const SNOOZES = ['1h', 'tomorrow', 'next-week'] as const
const TOP_MAX = 3

export const POLL_MS = 60_000
export const TOASTED_CAP = 500

// The engine interface a hook receives; the helpers below take it so Task 9 can pass its own `$`.
export type Dollar = Parameters<Hook<'session.start'>>[0]

// What `load` remembers between polls: the mtime it last read, so an unchanged file is not re-read.
export type FileLoader = { mtimeMs: number | null; statFailed: boolean }
export type Loader = FileLoader & { hub: Record<HubFile, FileLoader> }
const newFileLoader = (): FileLoader => ({ mtimeMs: null, statFailed: false })
export const newLoader = (): Loader => ({
  ...newFileLoader(), hub: { watcher: newFileLoader(), hub: newFileLoader(), sessions: newFileLoader() },
})

export function statusText(doc: NowDoc | null, nowMs: number): string {
  if (!doc) return '◆ no tick here'
  if (nowMs > Date.parse(doc.stale_after)) return '◆ stale'
  let text = `◆ ${doc.counts?.on_you ?? 0} on you · ${doc.counts?.neglected ?? 0} neglected`
  for (const [source, row] of Object.entries(doc.coverage ?? {})) {
    // ✗ = couldn't check, ~ = only partly checked
    if (row && row.status !== 'ok') text += ` · ${source} ${row.status === 'partial' ? '~' : '✗'}`
  }
  return text
}

// ---- the hub ---------------------------------------------------------------------------------------

export type HubFile = 'watcher' | 'hub' | 'sessions'
const HUB_FILES: Record<HubFile, string> = { watcher: 'watcher.json', hub: 'hub.json', sessions: 'sessions.json' }
const NO_HUB: HubBundle = { hub: null, sessions: null, watcher: null }
const WATCHER_DOWN_MS = 10 * 60_000
const ROUTED_SHOWN = 10

export const liveSessions = (s: HubSessions | null | undefined): [string, HubSession][] =>
  Object.entries(s?.sessions ?? {}).filter(([, v]) => v && v.state === 'live')

// The sessions with an update the manager has not looked at, plus the cards waiting to be started.
export const waitingCount = (h: HubBundle): number =>
  (h.hub?.pending ?? []).length + liveSessions(h.sessions).filter(([, v]) => (v.unseen ?? 0) > 0).length

export type HubHealth = 'none' | 'alive' | 'stale' | 'down'

// R4: judged without config. Down: no heartbeat, or one older than max(10 minutes, 2 x every_seconds), so a
// slow idle interval outside active hours is not down. Stale (inside active hours only): the heartbeat is older
// than 2 x every_seconds, or the watcher appended a line the hub must decide (last_append_at) after the hub last
// woke, and that was more than 2 x every_seconds ago. A quiet day's old last_wake is never stale on its own.
export function hubHealth(h: HubBundle, nowMs: number): HubHealth {
  if (!h.hub && !h.watcher) return 'none'
  const at = h.watcher ? Date.parse(h.watcher.at) : NaN
  const limit = 2 * (h.watcher?.every_seconds ?? 120) * 1000
  if (Number.isNaN(at) || nowMs - at > Math.max(WATCHER_DOWN_MS, limit)) return 'down'
  if (h.watcher?.active) {
    if (nowMs - at > limit) return 'stale'
    const appended = h.watcher.last_append_at ? Date.parse(h.watcher.last_append_at) : NaN
    const wake = h.hub?.last_wake ? Date.parse(h.hub.last_wake) : NaN
    if (!Number.isNaN(appended) && (Number.isNaN(wake) || appended > wake) && nowMs - appended > limit) return 'stale'
  }
  return 'alive'
}

// A source is stale (inside active hours only) when its own last poll is older than 2 x its own interval. A beat
// without the per-source fields (an older watcher) is never judged here; the heartbeat rules above still apply.
export function sourceStale(row: { every_seconds?: number; at?: string } | null | undefined, active: boolean | undefined, nowMs: number): boolean {
  if (!active || !row || !(typeof row.every_seconds === 'number' && row.every_seconds > 0) || !row.at) return false
  const at = Date.parse(row.at)
  return !Number.isNaN(at) && nowMs - at > 2 * row.every_seconds * 1000
}

export function hubStatus(h: HubBundle, nowMs: number): string {
  const health = hubHealth(h, nowMs)
  if (health === 'none') return ''
  const n = waitingCount(h)
  return (health === 'alive' ? ' · hub ●' : health === 'stale' ? ' · hub ◌ stale' : ' · hub ✗') + (n > 0 ? ` · ${n} waiting` : '')
}

// Empty (the default) means this plugin's own bin/mother: the panel ships inside the mother plugin.
export async function motherBin($: Dollar, configured: string): Promise<string> {
  if (!configured) return $.plugin.root + '/bin/mother'
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

// A toast is keyed on the item and when it first showed: a done item that is asked for again comes back
// with a new first_seen, so it toasts again.
export const toastKey = (i: NowItem): string => `${i.id}@${i.first_seen}`

async function toastNew($: Dollar, doc: NowDoc): Promise<void> {
  if (doc.quiet) return
  const seen: string[] = ((await $.store.get('toasted')) as string[] | undefined) ?? []
  const fresh = (doc.items ?? []).filter(i => i.section === 'on_you' && i.state === 'new' && !seen.includes(toastKey(i)))
  if (fresh.length === 0) return
  for (const i of fresh) $.ui.toast(`◆ New on you: ${i.title}`)
  await $.store.set('toasted', [...seen, ...fresh.map(toastKey)].slice(-TOASTED_CAP))
}

// One pass: resolve the workspace, re-read now.json when its mtime moved, toast what is new, draw the
// status line. A file that fails to parse (or has another version) sets `error` and keeps the old doc.
export async function load($: Dollar, bin: string, loader: Loader): Promise<void> {
  const ws = await workspace($, bin)
  if (!ws) {
    await update($, docAtom, () => null)
  } else {
    await readDoc($, `${ws}/.mother/now.json`, loader)
    await readHub($, `${ws}/.mother/hub`, loader).catch(() => undefined)   // the hub never un-reads the list
  }
  const nowMs = await $.clock.now()
  $.ui.status(statusText(await read($, docAtom), nowMs) + hubStatus((await read($, hubAtom)) ?? NO_HUB, nowMs))
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

// The three hub files, each re-read only when its mtime moved. A missing file is null; a file that will not
// parse (or has another version, for hub.json and sessions.json) keeps the previous value.
export async function readHub($: Dollar, dir: string, loader: Loader): Promise<void> {
  const before = (await read($, hubAtom)) ?? NO_HUB
  const next: HubBundle = { ...before }
  for (const name of Object.keys(HUB_FILES) as HubFile[]) {
    const path = `${dir}/${HUB_FILES[name]}`
    const fl = loader.hub[name]
    let mtimeMs: number
    try {
      mtimeMs = (await $.fs.stat(path)).mtimeMs
    } catch (err) {
      if (isMissing(err)) { fl.mtimeMs = null; (next as Record<string, unknown>)[name] = null }
      continue   // any other failure keeps the previous value
    }
    if (mtimeMs === fl.mtimeMs) continue
    fl.mtimeMs = mtimeMs
    try {
      const raw = JSON.parse(await $.fs.read(path))
      if (!raw || typeof raw !== 'object') throw new Error('not an object')
      if (name !== 'watcher' && raw.version !== 1) throw new Error('unsupported version')
      ;(next as Record<string, unknown>)[name] = raw
    } catch {
      // keep the previous value; the next change of the file retries it
    }
  }
  if (next.hub === before.hub && next.sessions === before.sessions && next.watcher === before.watcher) return
  await update($, hubAtom, () => next)
  await toastHub($, before, next).catch(() => undefined)   // a store failure never un-reads a good file
}

// Per-device de-dup in the store, like toastKey: a session toasts when its `unseen` rises above what this
// device last saw; a proposal toasts once per (key, proposed_at).
async function toastHub($: Dollar, before: HubBundle, next: HubBundle): Promise<void> {
  const seenUnseen: Record<string, number> = ((await $.store.get('hubUnseen')) as Record<string, number> | undefined) ?? {}
  const sessions = liveSessions(next.sessions)
  const unseenNow: Record<string, number> = {}
  let changed = false
  for (const [key, v] of sessions) {
    const n = v.unseen ?? 0
    unseenNow[key] = n
    if (n > (seenUnseen[key] ?? 0)) $.ui.toast(`◆ Update in ${v.title}`)
    if (n !== seenUnseen[key]) changed = true
  }
  if (changed || Object.keys(seenUnseen).length !== sessions.length) await $.store.set('hubUnseen', unseenNow)
  const toasted: string[] = ((await $.store.get('hubToasted')) as string[] | undefined) ?? []
  const fresh = (next.hub?.pending ?? []).filter(p => !toasted.includes(pendingKey(p)))
  if (fresh.length === 0) return
  for (const p of fresh) $.ui.toast(`◆ MOTHER suggests a session: ${p.title}`)
  await $.store.set('hubToasted', [...toasted, ...fresh.map(pendingKey)].slice(-TOASTED_CAP))
}
const pendingKey = (p: HubPending): string => `${p.key}@${p.proposed_at}`

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

// YYYY-MM-DD of an instant in the doc's timezone (the manager's local date); UTC when the zone is unknown.
export function localDate(nowMs: number, tz: string | null | undefined): string {
  if (tz) {
    try {
      const parts = new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(nowMs)
      const part = (t: string) => parts.find(p => p.type === t)?.value
      const y = part('year'), m = part('month'), d = part('day')
      if (y && m && d) return `${y}-${m}-${d}`
    } catch {
      // fall through to UTC
    }
  }
  return new Date(nowMs).toISOString().slice(0, 10)
}

// How long an item has waited: from `since`, when it was raised, not from when the tick first saw it.
export function age(since: string, nowMs: number): string {
  const hours = Math.max(0, Math.floor((nowMs - Date.parse(since)) / 3_600_000))
  return Number.isNaN(hours) ? '' : hours < 24 ? `${hours}h` : `${Math.floor(hours / 24)}d`
}

// A coarse age for the hub rows: minutes matter there.
export function ago(iso: string | undefined | null, nowMs: number): string {
  const ms = iso ? Date.parse(iso) : NaN
  if (Number.isNaN(ms)) return ''
  const mins = Math.max(0, Math.floor((nowMs - ms) / 60_000))
  return mins < 1 ? 'now' : mins < 60 ? `${mins}m` : mins < 1440 ? `${Math.floor(mins / 60)}h` : `${Math.floor(mins / 1440)}d`
}

// ---- writes ----------------------------------------------------------------------------------------

// Run one `mother now …` verb, then re-read now.json at once. A failure toasts the first line of stderr.
async function run($: Dollar, bin: string, loader: Loader, args: string[], rowId: string | null = null, group = 'now', toastOut = false): Promise<void> {
  try {
    const ran = await $.process.run(['python3', bin, group, ...args])
    if (ran.exitCode === 0 && toastOut) {
      const first = String(ran.stdout ?? '').split('\n').find(l => l.trim())
      if (first) $.ui.toast(first.trim())
    }
    if (ran.exitCode !== 0) {
      const line = String(ran.stderr ?? '').split('\n').find(l => l.trim()) ?? `mother ${group} ${args[0]} failed`
      $.ui.toast(line.trim())
    }
  } catch (err) {
    $.ui.toast(`mother ${group} ${args[0]} failed: ${(err as Error).message ?? String(err)}`)
  }
  await load($, bin, loader).catch(() => undefined)
  if (rowId) await update($, snoozingAtom, cur => (cur === rowId ? null : cur))
}

// The rank a new Top 3 entry takes: the next free one, else the highest rank not done; null = none.
export function lowestOpenRank(top: NowTop[]): number | null {
  if (top.length < TOP_MAX) return top.length + 1
  for (let r = top.length; r >= 1; r--) if (top[r - 1].state !== 'done') return r
  return null
}

// The row a focused element belongs to: an item id, or `top:<rank>` for a Top 3 entry; null for the
// controls that are not a row's own (the snooze Select, the swap hint).
export function rowOfElement(key: string | undefined): string | null {
  if (!key || key.startsWith('snooze-pick-')) return null
  const top = /^top-(?:row|done|doit|ask)-(\d+)$/.exec(key)
  if (top) return 'top:' + top[1]
  return /^(?:row|done|snooze|maketop|doit|ask|act\d+)-(.+)$/.exec(key)?.[1] ?? null
}

// ---- suggested prompts -----------------------------------------------------------------------------

// A prompt the panel offers to put in the prompt box. The panel only fills; the manager presses Enter.
export type Suggested = { id: string; label: string; text: string }

const PROMPTS_MAX = 5
const STALE_DAYS = 3
const LIST_MAX = 8

const listed = (titles: string[]): string =>
  titles.slice(0, LIST_MAX).map(t => `"${t}"`).join(', ') + (titles.length > LIST_MAX ? `, and ${titles.length - LIST_MAX} more` : '')

// What to ask MOTHER next, built from the list alone: no model call, no network.
export function suggestedPrompts(doc: NowDoc, top: NowTop[], nowMs: number): Suggested[] {
  const items = doc.items ?? []
  const onYou = items.filter(i => i.section === 'on_you')
  const neglected = items.filter(i => i.section === 'neglected')
  const out: Suggested[] = []
  const openTop = top.filter(e => e.state !== 'done')
  out.push({
    id: 'next', label: "What's next?",
    text: `Looking at my Now list (${onYou.length} on me, ${neglected.length} neglected`
      + (openTop.length ? `; Top 3: ${listed(openTop.map(e => e.title))}` : '')
      + '), what should I do in the next hour, and what can you take off my plate? Draft anything that reaches another person; don\'t send.',
  })
  const routines = doc.routines ?? []
  if (routines.length > 0) {
    out.push({
      id: 'routines', label: `Fix ${routines.length} routine${routines.length === 1 ? '' : 's'}`,
      text: `These MOTHER routines did not run cleanly: ${routines.map(r => `${r.id} (${r.state})`).join(', ')}. Run mother:doctor, tell me why each one failed, and fix what you can.`,
    })
  }
  const gaps = Object.entries(doc.coverage ?? {}).filter(([, c]) => c && c.status !== 'ok')
  if (gaps.length > 0) {
    out.push({
      id: 'recheck', label: `Re-check ${gaps.map(([s]) => s).join(', ')}`,
      text: `The last tick couldn't fully check ${gaps.map(([s, c]) => s + (c.reason ? ` (${c.reason})` : '')).join('; ')}. Re-check ${gaps.map(([s]) => s).join(' and ')} now and tell me what's new on me.`,
    })
  }
  const mail = onYou.filter(i => i.signal.startsWith('mail') || i.id.startsWith('mail:'))
  if (mail.length > 0) {
    out.push({
      id: 'replies', label: `Draft ${mail.length} repl${mail.length === 1 ? 'y' : 'ies'}`,
      text: `Draft replies (Gmail drafts only, never send) for the mail on me in the Now list: ${listed(mail.map(i => i.title))}.`,
    })
  }
  if (neglected.length > 0) {
    out.push({
      id: 'neglected', label: `Triage ${neglected.length} neglected`,
      text: `Go through the ${neglected.length} neglected items in the Now list. For each, propose one of: drop, delegate (name who), or make it mine. Change nothing until I say yes.`,
    })
  }
  const stale = onYou.filter(i => nowMs - Date.parse(i.since) >= STALE_DAYS * 86_400_000)
  if (stale.length >= 2) {
    out.push({
      id: 'stale', label: `Clear ${stale.length} stale`,
      text: `${stale.length} items have been on me for ${STALE_DAYS}+ days: ${listed(stale.map(i => i.title))}. For each, tell me whether it's still real and what closes it; I'll mark the dead ones done.`,
    })
  }
  return out.slice(0, PROMPTS_MAX)
}

// The prompt an item's or a Top 3 entry's "Ask" button fills: the item in full, ready to send as is.
export function askPrompt(title: string, why: string | null | undefined, link: string | null | undefined): string {
  return `Help me close this out: "${title}"` + (why ? ` — ${why}` : '') + (link ? ` (${link})` : '')
    + '. What is the fastest next step? Draft anything that reaches another person; don\'t send.'
}

// ---- the pane --------------------------------------------------------------------------------------

type Ui = ReturnType<Dollar['ui']['resolve']>

// A link the surface can open; a workspace path (`teams/x/intake.md#1`) stays in the Ask prompt instead.
const isUrl = (link: string | null | undefined): link is string => !!link && /^https?:\/\//.test(link)

// What Follow watches for a row key: a tracker item follows its project, a pull request its repo, a chat
// thread itself, a WhatsApp group its JID. Mail, calendar and manual keys have nothing to follow, so the row shows no Follow button.
export function followArgs(key: string): string[] | null {
  let m = /^([A-Z][A-Z0-9]+)-\d+$/.exec(key)
  if (m) return ['follow', '--project', '--', m[1]]
  m = /^([\w.-]+\/[\w.-]+)#\d+$/.exec(key)
  if (m) return ['follow', '--repo', '--', m[1]]
  if (/^[CG][A-Z0-9]+\/\d+\.\d+$/.test(key)) return ['follow', '--thread', '--', key]
  m = /^wa:(\d{6,30}(?:-\d+)?@g\.us)$/.exec(key)
  if (m) return ['follow', '--whatsapp', '--', m[1]]
  return null
}

// Rows a section draws before "Show all"; the rest wait behind the toggle.
const LIMITS: Record<NowItem['section'], number> = { on_you: 5, neglected: 3, suggestion: 3 }
const SECTIONS: [NowItem['section'], string][] = [['on_you', 'On you'], ['neglected', 'Neglected'], ['suggestion', 'Suggestions']]

function paneTree($: Dollar, ui: Ui, doc: NowDoc | null, error: string | null, snoozing: string | null, selected: string | null,
                  expandedList: string[] | null, nowMs: number, go: (args: string[], rowId?: string | null) => Promise<void>,
                  hub: HubBundle, goHub: (args: string[], rowId?: string | null) => Promise<void>, surface: string) {
  const { Box, Text, Button, Link, Select } = ui
  const hubNode = hubSection($, ui, surface, hub, snoozing, new Set(expandedList ?? []), nowMs, goHub)
  if (!doc) {
    return (
      <Box flexDirection="column">
        {hubNode}
        {error
          ? <Text color="warning">Couldn't read now.json.</Text>
          : <Text dimColor>Tick runs on the home machine; nothing here yet.</Text>}
      </Box>
    )
  }

  const tz = doc.timezone
  const at = (iso: string | undefined | null) => clock(iso, tz)
  const items = doc.items ?? []
  const expanded = new Set(expandedList ?? [])
  const toggle = (name: string) => update($, expandedAtom, cur => {
    const set = new Set(cur ?? [])
    if (set.has(name)) set.delete(name); else set.add(name)
    return [...set]
  })
  const fill = (text: string) => { void $.prompt.fill({ text, mode: 'replace' }) }
  // Top 3 is today's list only: yesterday's (or Friday's) is not shown, and the next day starts fresh.
  const top = doc.top_date === localDate(nowMs, tz) ? doc.top ?? [] : []
  const visible = (kind: NowItem['section']) => {
    const list = items.filter(i => i.section === kind)
    return expanded.has(kind) ? list : list.slice(0, LIMITS[kind])
  }
  // Hotkeys and the open detail belong to the selected row alone: the one the ring is on (`ui.focus`), else the first.
  const rows = [...top.filter(e => e.state !== 'done').map(e => 'top:' + e.rank),
                ...SECTIONS.flatMap(([k]) => visible(k).map(i => i.id))]
  const current = selected && rows.includes(selected) ? selected : rows[0]
  const hot = (row: string, k: string): string | undefined => (row === current ? k : undefined)
  const select = (row: string) => update($, selectedAtom, () => row)
  const inTop = new Set(top.map(e => e.item_id).filter(Boolean))
  const byId = new Map(items.map(i => [i.id, i]))
  const rank = lowestOpenRank(top)
  const gaps = Object.entries(doc.coverage ?? {}).filter(([, c]) => c && c.status !== 'ok') as [string, NowCoverage][]
  const routines = doc.routines ?? []
  const stale = nowMs > Date.parse(doc.stale_after)
  const heading = (text: string, count?: number) => (
    <Box key={'h-' + text} marginTop={1}>
      <Text weight="semibold" size="heading">{text}{count !== undefined ? <Text dimColor weight="regular">{` ${count}`}</Text> : null}</Text>
    </Box>
  )

  const sessionOf = (i: NowItem) => sessionForItem(hub.sessions, i)
  const row = (i: NowItem) => {
    const open = i.id === current
    const sess = sessionOf(i)
    const meta = [i.team, age(i.since, nowMs)].filter(Boolean).join(' · ')
    return (
      <Box key={'item-' + i.id} flexDirection="column">
        <Box>
          <Button key={'row-' + i.id} plain dimColor={!open} autoFocus={i.id === rows[0] ? true : undefined}
            onPress={() => select(i.id)}>{(open ? '▾ ' : '▸ ') + (inTop.has(i.id) ? '★ ' : '') + i.title}</Button>
          <Text dimColor>{' ' + meta + ' '}</Text>
          {isUrl(i.link) ? <Link href={i.link}>↗</Link> : null}
          {i.unverified_since ? <Text color="warning">{' unverified'}</Text> : null}
          {sess ? <Text dimColor>{' ◆ session'}</Text> : null}
        </Box>
        {open ? (
          <Box flexDirection="column" paddingLeft={2}>
            {i.why ? <Text dimColor wrap="wrap">{i.why}</Text> : null}
            <Box flexWrap="wrap" gap="xs">
              {(i.actions ?? []).map((a, n) => (
                <Button key={n === 0 ? 'doit-' + i.id : `act${n}-` + i.id} variant={n === 0 ? 'primary' : undefined}
                  hotkey={n === 0 ? hot(i.id, 'x') : undefined}
                  onPress={() => fill(a.prompt || `mother now do ${a.id}`)}>{n === 0 && !a.label ? 'Do it' : a.label || a.id}</Button>
              ))}
              {sess
                ? <Button key={'ask-' + i.id} hotkey={hot(i.id, 'a')} onPress={() => openSession($, goHub, sess[0], sess[1])}>Open session</Button>
                : <Button key={'ask-' + i.id} hotkey={hot(i.id, 'a')} onPress={() => fill(askPrompt(i.title, i.why, i.link))}>Ask Mother</Button>}
              {isUrl(i.link) ? <Button key={'tohub-' + i.id} onPress={() => goHub(['add', '--', i.link])}>Send to hub</Button> : null}
              <Button key={'done-' + i.id} hotkey={hot(i.id, 'd')} onPress={() => go(['done', i.id], i.id)}>Done</Button>
              <Button key={'snooze-' + i.id} hotkey={hot(i.id, 's')} onPress={() => update($, snoozingAtom, cur => (cur === i.id ? null : i.id))}>Snooze</Button>
              {rank !== null && !inTop.has(i.id)
                ? <Button key={'maketop-' + i.id} hotkey={hot(i.id, 't')} onPress={() => go(['top', 'swap', String(rank), i.id], i.id)}>Make top 3</Button>
                : null}
            </Box>
            {snoozing === i.id
              ? <Select key={'snooze-pick-' + i.id} label="Snooze for" options={SNOOZES.map(value => ({ value }))}
                  onSelect={value => go(['snooze', i.id, value], i.id)} />
              : null}
          </Box>
        ) : null}
      </Box>
    )
  }

  const section = (kind: NowItem['section'], name: string) => {
    const all = items.filter(i => i.section === kind)
    if (all.length === 0) return null
    const hidden = all.length - visible(kind).length
    return (
      <Box key={kind} flexDirection="column">
        {heading(name, all.length)}
        {visible(kind).map(row)}
        {all.length > LIMITS[kind]
          ? <Button key={'more-' + kind} plain dimColor onPress={() => toggle(kind)}>{hidden > 0 ? `Show ${hidden} more` : 'Show fewer'}</Button>
          : null}
      </Box>
    )
  }

  const topEntry = (e: NowTop) => {
    const done = e.state === 'done'
    const id = 'top:' + e.rank
    const action = e.item_id ? byId.get(e.item_id)?.actions?.[0] : undefined
    return (
      <Box key={'top-' + e.rank} flexDirection="column">
        <Box>
          <Button key={'top-row-' + e.rank} plain dimColor={done} onPress={() => select(id)}>
            <Text dimColor={done} strikethrough={done}>{`${e.rank}. ${e.title}`}</Text>
          </Button>
          {isUrl(e.link) ? <Link href={e.link}>{' ↗'}</Link> : null}
        </Box>
        {done ? null : (
          <Box flexDirection="column" paddingLeft={3}>
            {e.why ? <Text dimColor wrap="wrap">{e.why}</Text> : null}
            <Box flexWrap="wrap" gap="xs">
              {action
                ? <Button key={'top-doit-' + e.rank} variant="primary" hotkey={hot(id, 'x')}
                    onPress={() => fill(action.prompt || `mother now do ${action.id}`)}>{action.label || 'Do it'}</Button>
                : null}
              <Button key={'top-ask-' + e.rank} hotkey={hot(id, 'a')} onPress={() => fill(askPrompt(e.title, e.why, e.link))}>Ask Mother</Button>
              <Button key={'top-done-' + e.rank} hotkey={hot(id, 'd')} autoFocus={id === rows[0] ? true : undefined}
                onPress={() => go(['top', 'done', String(e.rank)])}>Done</Button>
            </Box>
          </Box>
        )}
      </Box>
    )
  }

  // One line for everything that makes the list less trustworthy; the detail waits behind a toggle.
  const health = [
    ...(stale ? [`stale since ${at(doc.stale_after)}`] : []),
    ...(routines.length ? [`${routines.length} routine${routines.length === 1 ? '' : 's'} ${new Set(routines.map(r => r.state)).size === 1 ? routines[0]?.state : 'not ok'}`] : []),
    ...(gaps.some(([, c]) => c.status === 'couldnt_check') ? [`couldn't check ${gaps.filter(([, c]) => c.status === 'couldnt_check').map(([s]) => s).join(', ')}`] : []),
    ...(gaps.some(([, c]) => c.status === 'partial') ? [`partly checked ${gaps.filter(([, c]) => c.status === 'partial').map(([s]) => s).join(', ')}`] : []),
  ]
  const healthOpen = expanded.has('health')
  const prompts = suggestedPrompts(doc, top, nowMs)
  const hint = doc.swap_hint
  const setBy = top[0]?.set_by === 'manager' ? 'you' : 'agent'
  const setAt = top[0]?.set_at ? ` at ${at(top[0].set_at)}` : ''
  const noItems = items.every(i => !SECTIONS.some(([k]) => k === i.section))

  return (
    <Box flexDirection="column">
      {hubNode}
      <Box>
        <Text weight="semibold">{`Now · ${at(doc.generated_at)} · next ${at(doc.next_at)}`}</Text>
      </Box>
      {error ? <Text color="warning">{`Couldn't read now.json — showing the ${at(doc.generated_at)} list.`}</Text> : null}
      {health.length > 0 ? (
        <Box key="health" flexDirection="column">
          <Box>
            <Text color="warning">{'⚠ ' + health.join(' · ') + ' '}</Text>
            <Button key="health-toggle" plain dimColor onPress={() => toggle('health')}>{healthOpen ? 'Hide' : 'Details'}</Button>
          </Box>
          {healthOpen ? (
            <Box flexDirection="column" paddingLeft={2}>
              {gaps.map(([source, c]) => (
                <Text key={'gap-' + source} dimColor wrap="wrap">{`${source}: ${c.status === 'partial' ? 'partly checked' : "couldn't check"}${c.since ? ' since ' + at(c.since) : ''}${c.reason ? ' — ' + c.reason : ''}`}</Text>
              ))}
              {routines.map(r => <Text key={'routine-' + r.id} dimColor>{`${r.id} ${r.state} at ${at(r.expected)}`}</Text>)}
            </Box>
          ) : null}
        </Box>
      ) : null}
      {prompts.length > 0 && !(noItems && top.length === 0 && health.length === 0) ? (
        <Box key="prompts" flexDirection="column">
          {heading('Ask Mother')}
          <Box flexWrap="wrap" gap="xs">
            {prompts.map((p, n) => <Button key={'prompt-' + p.id} variant={n === 0 ? 'primary' : undefined} onPress={() => fill(p.text)}>{p.label}</Button>)}
          </Box>
        </Box>
      ) : null}
      {top.length > 0 ? (
        <Box key="top" flexDirection="column">
          <Box marginTop={1}>
            <Text weight="semibold" size="heading">Top 3 today</Text>
            <Text dimColor>{` set by ${setBy}${setAt}`}</Text>
          </Box>
          {top.map(topEntry)}
          {hint && hint.title ? (
            <Box>
              <Text dimColor>{`Consider swapping: ${hint.title} `}</Text>
              {hint.item_id
                ? <Button key="swap-hint" onPress={() => go(['top', 'swap', String(TOP_MAX), String(hint.item_id)])}>Swap</Button>
                : null}
            </Box>
          ) : null}
        </Box>
      ) : null}
      {SECTIONS.map(([kind, name]) => section(kind, name))}
      {noItems && health.length === 0 && top.length === 0
        ? <Text>{`Nothing on you. Next check ${at(doc.next_at)}.`}</Text> : null}
    </Box>
  )
}

// The live session an item belongs to: its link (or the key it names) is a session key, the session's own
// link, or one of its `also` keys; item.also counts the same way. Exact matches, or the last path segment of
// a URL (`…/browse/PLAT-2` -> `PLAT-2`).
export function sessionForItem(sessions: HubSessions | null, i: NowItem): [string, HubSession] | null {
  const tail = (u: string) => u.split(/[?#]/)[0].replace(/\/+$/, '').split('/').pop() ?? ''
  const mine = new Set<string>()
  for (const l of [i.link, ...(i.also ?? [])]) if (l) { mine.add(l); mine.add(tail(l)) }
  for (const [key, v] of liveSessions(sessions)) {
    const theirs = [key, v.link, ...(v.also ?? [])].filter(Boolean) as string[]
    if (theirs.some(t => mine.has(t))) return [key, v]
  }
  return null
}

// Open a session: fill the prompt (the way in on a surface that cannot open the link) and run `session seen`.
function openSession($: Dollar, goHub: (args: string[], rowId?: string | null) => Promise<void>, key: string, v: HubSession) {
  void $.prompt.fill({ text: `Open my "${v.title}" session`, mode: 'replace' })
  void goHub(['session', 'seen', key])
}

// The Hub section: first in the pane. Open by default when something waits; `hub` in `expanded` flips that.
function hubSection($: Dollar, ui: Ui, surface: string, hb: HubBundle, snoozing: string | null, expanded: Set<string>, nowMs: number,
                    goHub: (args: string[], rowId?: string | null) => Promise<void>) {
  const { Box, Text, Button, Link, Select } = ui
  const health = hubHealth(hb, nowMs)
  if (health === 'none') return null
  const waiting = waitingCount(hb)
  const updates = (hb.hub?.updates ?? []).filter(u => u.state === 'open')
  // Open by default when something waits on the manager: cards to start, sessions with news, open updates.
  const open = (waiting > 0 || updates.length > 0) !== expanded.has('hub')
  const toggle = (name: string) => update($, expandedAtom, cur => {
    const set = new Set(cur ?? [])
    if (set.has(name)) set.delete(name); else set.add(name)
    return [...set]
  })
  const heading = (text: string, count?: number) => (
    <Box key={'hh-' + text} marginTop={1}>
      <Text weight="semibold">{text}{count !== undefined ? <Text dimColor weight="regular">{` ${count}`}</Text> : null}</Text>
    </Box>
  )
  // The terminal opens any scheme (a claude:// link); the other surfaces are sent https links only, so the
  // Open button (it fills the prompt) is the way in there.
  const canLink = (l: string) => surface === 'terminal' || /^https?:\/\//.test(l)
  const sources = Object.entries(hb.watcher?.sources ?? {})
  const mark = (st: string) => (st === 'ok' ? '✓' : st === 'partial' ? '~' : '✗')
  const healthLine = [
    hb.watcher ? `watcher polled ${ago(hb.watcher.at, nowMs)} ago` : 'watcher not running',
    ...sources.map(([cap, row]) => `${cap} ${mark(row?.status)}${sourceStale(row, hb.watcher?.active, nowMs) ? ' stale' : ''}`),
    hb.hub?.last_wake ? `hub woke ${ago(hb.hub.last_wake, nowMs)} ago` : 'hub never woke',
  ].join(' · ')
  const sessions = liveSessions(hb.sessions)
  const pending = hb.hub?.pending ?? []
  const routed = (hb.hub?.routed ?? []).slice(-ROUTED_SHOWN).reverse()
  const label = health === 'alive' ? '●' : health === 'stale' ? '◌ stale' : '✗'
  return (
    <Box key="hub" flexDirection="column">
      <Box>
        <Button key="hub-toggle" plain onPress={() => toggle('hub')}>{`${open ? '▾' : '▸'} Hub ${label}`}</Button>
        {waiting > 0 ? <Text dimColor>{` ${waiting} waiting `}</Text> : null}
        {health !== 'alive'
          ? <Button key="hub-restart" onPress={() => { void $.prompt.fill({ text: '/mother:hub', mode: 'replace' }) }}>Restart hub</Button>
          : null}
      </Box>
      <Box paddingLeft={2}>
        <Text color={health === 'alive' ? undefined : 'warning'} dimColor={health === 'alive'} wrap="wrap">{healthLine}</Text>
      </Box>
      {open ? (
        <Box flexDirection="column" paddingLeft={2}>
          {sessions.length > 0 ? (
            <Box key="hub-sessions" flexDirection="column">
              {heading('Sessions', sessions.length)}
              {sessions.map(([key, v]) => (
                <Box key={'sess-' + key} flexWrap="wrap">
                  {v.link && canLink(v.link) ? <Link href={v.link}>{v.title}</Link> : <Text>{v.title}</Text>}
                  <Text dimColor>{` ${key} `}</Text>
                  {(v.unseen ?? 0) > 0 ? <Text color="warning">{'new update '}</Text> : null}
                  <Text dimColor>{ago(v.last_routed_at, nowMs) ? ago(v.last_routed_at, nowMs) + ' ' : ''}</Text>
                  <Button key={'sess-open-' + key} onPress={() => openSession($, goHub, key, v)}>Open</Button>
                  {followArgs(key) ? <Button key={'sess-follow-' + key} onPress={() => goHub(followArgs(key) as string[])}>Follow</Button> : null}
                  <Button key={'sess-mute-' + key} onPress={() => goHub(['mute', '--', key])}>Mute</Button>
                </Box>
              ))}
            </Box>
          ) : null}
          {pending.length > 0 ? (
            <Box key="hub-pending" flexDirection="column">
              {heading('Waiting for you to start', pending.length)}
              {pending.map(p => (
                <Box key={'pend-' + p.key} flexWrap="wrap">
                  <Text>{p.title}</Text>
                  <Text dimColor>{` ${p.key} · ${p.raised_at ? 'open the hub to start it' : 'card not raised yet'} · ${ago(p.proposed_at, nowMs)} `}</Text>
                  {isUrl(p.link) ? <Link href={p.link}>↗</Link> : null}
                </Box>
              ))}
            </Box>
          ) : null}
          {updates.length > 0 ? (
            <Box key="hub-updates" flexDirection="column">
              {heading('Hub updates', updates.length)}
              {updates.map((u: HubUpdate) => (
                <Box key={'upd-' + u.id} flexDirection="column">
                  <Box flexWrap="wrap">
                    <Text>{u.title}</Text>
                    <Text dimColor>{` ${u.key} · ${ago(u.at, nowMs)} `}</Text>
                    {isUrl(u.link) ? <Link href={u.link}>↗</Link> : null}
                    <Button key={'hub-done-' + u.id} onPress={() => goHub(['done', u.id], 'hub:' + u.id)}>Done</Button>
                    {followArgs(u.key) ? <Button key={'hub-follow-' + u.id} onPress={() => goHub(followArgs(u.key) as string[])}>Follow</Button> : null}
                    <Button key={'hub-mute-' + u.id} onPress={() => goHub(['mute', '--', u.key])}>Mute</Button>
                    <Button key={'hub-snooze-' + u.id} onPress={() => update($, snoozingAtom, cur => (cur === 'hub:' + u.id ? null : 'hub:' + u.id))}>Snooze</Button>
                  </Box>
                  {u.why ? <Text dimColor wrap="wrap">{u.why}</Text> : null}
                  {snoozing === 'hub:' + u.id
                    ? <Select key={'hub-snooze-pick-' + u.id} label="Snooze for" options={SNOOZES.map(value => ({ value }))}
                        onSelect={value => goHub(['snooze', u.id, value], 'hub:' + u.id)} />
                    : null}
                </Box>
              ))}
            </Box>
          ) : null}
          {routed.length > 0 ? (
            <Box key="hub-routed" flexDirection="column">
              <Button key="routed-toggle" plain dimColor onPress={() => toggle('routed')}>{`${expanded.has('routed') ? '▾' : '▸'} Recently routed ${routed.length}`}</Button>
              {expanded.has('routed')
                ? routed.map(r => <Text key={'routed-' + r.line_id} dimColor>{`${r.key} ${r.decision} ${ago(r.at, nowMs)}`}</Text>)
                : null}
            </Box>
          ) : null}
        </Box>
      ) : null}
    </Box>
  )
}

export const COMMAND = 'now-panel'

export const register: Register = (on, options) => {
  const configured = String(options.mother_bin ?? '').trim()
  const loader = newLoader()
  let timer: Timer | undefined

  on('session.start', async ($, e, next) => {
    const bin = await motherBin($, configured)
    // `/now` itself belongs to MOTHER's mother:now skill, so the engine refuses it for a plugin command.
    // A refused name must not stop the poll, the status line or the toasts below.
    try {
      await $.command.register({ name: COMMAND, description: 'Open the Now panel beside the chat' })
    } catch {
      // carry on without the command
    }
    const poll = () => load($, bin, loader).catch(() => undefined)
    await poll()
    timer?.cancel()          // a repeated session.start must not stack a second poll
    timer = $.clock.every(POLL_MS, poll)
    return next(e)
  })

  on('command.run', { command: COMMAND }, async $ => {
    const opened = await $.ui.open({ id: PANE, title: 'Now' })
    if (opened.isPlaced) return { text: 'Now panel opened.' }
    // No surface draws the pane here (a narrow terminal, a remote surface): say the list in chat instead.
    try {
      const ran = await $.process.run(['python3', await motherBin($, configured), 'now', 'show'])
      const text = String(ran.stdout ?? '').trim()
      if (ran.exitCode === 0 && text) return { text }
    } catch {
      // fall through to the short message
    }
    return { text: "The Now panel can't be drawn here and `mother now show` failed; run it in a terminal." }
  })

  on('ui.focus', async ($, e, next) => {
    // The engine names the element in `element`; the test kit's `$.ui.focus` passes its `key` through as is.
    const row = rowOfElement(e.element ?? (e as { key?: string }).key)
    if (row) await update($, selectedAtom, () => row)
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const bin = await motherBin($, configured)
    const go = (args: string[], rowId: string | null = null) => run($, bin, loader, args, rowId)
    const goHub = (args: string[], rowId: string | null = null) => run($, bin, loader, args, rowId, 'hub', args[0] === 'add' || args[0] === 'follow' || args[0] === 'mute')
    return paneTree($, $.ui.resolve(e), await read($, docAtom), await read($, errorAtom), await read($, snoozingAtom),
      await read($, selectedAtom), await read($, expandedAtom), await $.clock.now(), go,
      (await read($, hubAtom)) ?? NO_HUB, goHub, e.surface)
  })
}
