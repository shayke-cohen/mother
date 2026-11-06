import { atom, read, update } from 'claude-code'

import { askIdOf, needsYou } from './floor'
import type { FloorState } from './floor'
import { oneLine } from './shared'
import type { Dollar } from './shared'
import { verdictExtra } from './pane_ui'
import type { ChiefCardState, ChiefEvent, ChiefStatus } from '../types'

// "What just changed": each status read is compared with the one before it, in code, and the most important change
// becomes the one current event, drawn as a line under the mood line (hooks/pane_ui.tsx headerStrip).
//   - Priority, highest first: a new question, an urgent inbox item, a source that went down, a session that
//     finished or got stuck, a new inbox item, a source that recovered.
//   - An event is shown for EVENT_SHOW_MS, unless a change of higher or equal priority replaces it first.
//   - A warning-level event (a source down, a session stuck) is not raised again for the same subject within
//     WARN_COOLDOWN_MS.
//   - The first read after a load only seeds the previous snapshot: a reload never replays what was already there.
// No model call, no process: the snapshot is built from the status and the floor already in hand.
export const eventAtom = atom({ plugin: 'mother', key: 'chiefEvent' } as const, null)

export const EVENT_SHOW_MS = 15 * 60_000
export const WARN_COOLDOWN_MS = 30 * 60_000
export const EVENT_PRIORITY = ['ask', 'urgent', 'down', 'session', 'inbox', 'recovered'] as const
export type EventKind = (typeof EVENT_PRIORITY)[number]
const TEXT_MAX = 90

export type Snapshot = {
  asks: Record<string, string>       // ask id -> who asks (the session's title)
  urgent: Record<string, string>     // inbox item id -> its title
  inbox: Record<string, string>      // every other open inbox item id -> its title
  down: Record<string, string>       // source -> why it cannot be read
  sessions: Record<string, { title: string; state: ChiefCardState | null }>
}
export type Change = { kind: EventKind; subject: string; text: string; warn: boolean }

const isWatcher = (key: unknown): boolean => /^watcher:/.test(String(key ?? ''))

// The parts of a status (and the floor) a change is read from. `states` is each pinned session's card state, as
// hooks/chief.tsx sessionStates computes it.
export function snapshotOf(s: ChiefStatus | null, floor: FloorState | null, states: Record<string, ChiefCardState> = {}): Snapshot {
  const snap: Snapshot = { asks: {}, urgent: {}, inbox: {}, down: {}, sessions: {} }
  if (!s) return snap
  for (const n of needsYou(s, floor)) snap.asks[askIdOf(n.card)] = oneLine(n.d.title, 80) || 'a session'
  for (const u of Array.isArray(s.inbox) ? s.inbox : []) {
    if (!u || typeof u.id !== 'string' || isWatcher(u.key)) continue
    const title = oneLine(u.title, 80) || oneLine(u.key, 80)
    if (verdictExtra(u)?.urgency === 'now') snap.urgent[u.id] = title
    else snap.inbox[u.id] = title
  }
  const sources = s.health?.watcher?.sources
  for (const [name, v] of Object.entries(sources && typeof sources === 'object' ? sources : {})) {
    if (v?.status === 'couldnt_check') snap.down[oneLine(name, 40)] = oneLine(v.reason, 80)
  }
  for (const d of Array.isArray(s.desktop) ? s.desktop : []) {
    if (!d || typeof d.cli_session_id !== 'string') continue
    snap.sessions[d.cli_session_id] = { title: oneLine(d.title, 80) || 'a session', state: states[d.cli_session_id] ?? null }
  }
  return snap
}

const clipText = (t: string): string => (t.length > TEXT_MAX ? t.slice(0, TEXT_MAX - 1).trimEnd() + '…' : t)

// Every change from `prev` to `next`; [] when there is no `prev` (the first read seeds, it never emits).
export function changes(prev: Snapshot | null, next: Snapshot): Change[] {
  if (!prev) return []
  const out: Change[] = []
  const fresh = (now: Record<string, string>, before: Record<string, string>) => Object.keys(now).filter(k => !(k in before))
  for (const id of fresh(next.asks, prev.asks)) out.push({ kind: 'ask', subject: id, text: `${next.asks[id]} asks you something`, warn: false })
  // an item that only turned urgent is new as urgent too
  for (const id of fresh(next.urgent, prev.urgent)) out.push({ kind: 'urgent', subject: id, text: `Urgent: ${next.urgent[id]}`, warn: false })
  for (const src of fresh(next.down, prev.down)) {
    out.push({ kind: 'down', subject: src, text: `${src} went down${next.down[src] ? ': ' + next.down[src] : ''}`, warn: true })
  }
  for (const [cli, now] of Object.entries(next.sessions)) {
    const before = prev.sessions[cli]
    if (!before || before.state === now.state) continue
    if (now.state === 'done') out.push({ kind: 'session', subject: cli, text: `${now.title} finished`, warn: false })
    if (now.state === 'stuck') out.push({ kind: 'session', subject: cli, text: `${now.title} looks stuck`, warn: true })
  }
  for (const id of fresh(next.inbox, prev.inbox)) {
    if (!(id in prev.urgent)) out.push({ kind: 'inbox', subject: id, text: `New in the inbox: ${next.inbox[id]}`, warn: false })
  }
  for (const src of fresh(prev.down, next.down)) out.push({ kind: 'recovered', subject: src, text: `${src} is back`, warn: false })
  return out.map(c => ({ ...c, text: clipText(c.text) }))
}

const rank = (k: string): number => {
  const i = (EVENT_PRIORITY as readonly string[]).indexOf(k)
  return i < 0 ? EVENT_PRIORITY.length : i
}
const EVENT_COLOR: Record<EventKind, string> = { ask: 'warning', urgent: 'error', down: 'error', session: 'claude', inbox: 'suggestion', recovered: 'success' }

// The event to show after this read: the highest-priority change that is not cooling down replaces the current
// event when it ranks at least as high or the current one has run its time; else the current one stays until then.
// `cooled` is each warning subject's last raise (`<kind>:<subject>` -> ms), updated in place.
export function pickEvent(current: ChiefEvent | null, found: readonly Change[], nowMs: number,
                          cooled: Map<string, number>): ChiefEvent | null {
  const live = current && nowMs - current.at < EVENT_SHOW_MS ? current : null
  const ok = found.filter(c => {
    if (!c.warn) return true
    const last = cooled.get(`${c.kind}:${c.subject}`)
    return last === undefined || nowMs - last >= WARN_COOLDOWN_MS
  })
  const top = [...ok].sort((a, b) => rank(a.kind) - rank(b.kind))[0]
  for (const c of ok) if (c.warn) cooled.set(`${c.kind}:${c.subject}`, nowMs)
  if (!top) return live
  if (live && rank(top.kind) > rank(live.kind)) return live
  return { kind: top.kind, subject: top.subject, text: top.text, color: EVENT_COLOR[top.kind], at: nowMs }
}

// The previous snapshot and the cooldowns live in this module: a reload of the mod starts them over, and its first
// read only seeds (no replay).
let prevSnap: Snapshot | null = null
const cooldowns = new Map<string, number>()

export function resetEvents(): void {
  prevSnap = null
  cooldowns.clear()
}

// One read's turn: compare with the previous snapshot and set the current event. A read with no status keeps both.
export async function observeEvents($: Dollar, s: ChiefStatus | null, floor: FloorState | null,
                                    states: Record<string, ChiefCardState>): Promise<void> {
  if (!s) return
  const next = snapshotOf(s, floor, states)
  const found = changes(prevSnap, next)
  prevSnap = next
  const nowMs = await $.clock.now()
  const prev = await read($, eventAtom)
  const picked = pickEvent(prev, found, nowMs, cooldowns)
  if (picked !== prev) await update($, eventAtom, () => picked)
}

// What the header draws: the current event while it is under EVENT_SHOW_MS old, else nothing.
export function shownEvent(e: ChiefEvent | null | undefined, nowMs: number): ChiefEvent | null {
  return e && typeof e.text === 'string' && nowMs - e.at < EVENT_SHOW_MS ? e : null
}
