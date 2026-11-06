import { askIdOf, needsYou } from './floor'
import type { FloorState } from './floor'
import { oneLine } from './shared'
import type { Dollar } from './shared'
import { verdictExtra } from './pane_ui'
import type { ChiefStatus } from '../types'

// Desktop notifications, once per thing. Only the leading session notifies (several open sessions would each raise
// the same one otherwise), and what was raised is kept in the plugin's store, which every session of this machine
// shares and which outlasts a reload: nothing is raised twice, whichever session leads. Raised for: a new Needs you
// question (by its ask id), a new urgent Inbox item, a source going down (by source and since when), and a chase
// card (`notifyOnce` from the code that makes one). During the manager's quiet hours (`status.quiet`) only urgent
// things are raised; the rest is remembered as seen, so the morning does not replay the night.
// `/chief notify on|off|status` is the switch (the store's `chiefNotifyOff`), on by default.
export const NOTIFY_TITLE = 'Mother'
export const NOTIFIED_KEY = 'chiefNotified'
export const NOTIFY_OFF_KEY = 'chiefNotifyOff'
export const NOTIFIED_KEEP_MS = 14 * 86_400_000
export const NOTIFIED_MAX = 400
export const NOTIFY_PER_PASS = 3
const LINE_MAX = 120

export type Notice = { key: string; text: string; urgent: boolean }

const isWatcher = (key: unknown): boolean => /^watcher:/.test(String(key ?? ''))
const line = (t: string): string => {
  const one = oneLine(t, 400)
  return one.length > LINE_MAX ? one.slice(0, LINE_MAX - 1).trimEnd() + '…' : one
}

// What this status says the manager should hear about, each with the key it is remembered by.
export function noticesOf(s: ChiefStatus | null, floor: FloorState | null): Notice[] {
  if (!s) return []
  const out: Notice[] = []
  for (const n of needsYou(s, floor)) {
    const who = oneLine(n.d.title, 60) || 'A session'
    out.push({ key: 'ask:' + askIdOf(n.card), text: line(`${who} asks: ${oneLine(n.card.question, 200)}`), urgent: false })
  }
  for (const u of Array.isArray(s.inbox) ? s.inbox : []) {
    if (!u || typeof u.id !== 'string' || isWatcher(u.key) || verdictExtra(u)?.urgency !== 'now') continue
    const who = oneLine(u.who, 40)
    out.push({ key: 'urgent:' + u.id, text: line(`Urgent${who ? ' from ' + who : ''}: ${oneLine(u.title, 200) || oneLine(u.key, 80)}`), urgent: true })
  }
  const access = new Map((s.health?.access ?? []).map(a => [a.source, a]))
  const sources = s.health?.watcher?.sources
  for (const [name, v] of Object.entries(sources && typeof sources === 'object' ? sources : {})) {
    if (v?.status !== 'couldnt_check') continue
    const a = access.get(name)
    const what = a ? (a.kind === 'signin' ? 'needs you to sign in again' : 'is unreachable: check the VPN or network')
      : `cannot be read${v.reason ? ': ' + oneLine(v.reason, 80) : ''}`
    out.push({ key: `down:${name}:${v.since ?? ''}`, text: line(`${oneLine(name, 40)} ${what}`), urgent: false })
  }
  return out
}

// The chase card's line, for whoever makes one: who it waits on and what for.
export const chaseNotice = (id: string, who: string, what: string): Notice =>
  ({ key: 'chase:' + id, text: line(`Chase ${oneLine(who, 40) || 'someone'}: ${oneLine(what, 200)}`), urgent: false })

async function storeGet($: Dollar, key: string): Promise<unknown> {
  try { return await $.store.get(key) } catch { return undefined }
}

export async function notifyIsOn($: Dollar): Promise<boolean> {
  return (await storeGet($, NOTIFY_OFF_KEY)) !== true
}

// The switch: true when the store took it.
export async function setNotify($: Dollar, on: boolean): Promise<boolean> {
  try {
    if (on) await $.store.delete(NOTIFY_OFF_KEY)
    else await $.store.set(NOTIFY_OFF_KEY, true)
    return true
  } catch {
    return false
  }
}

const keptOf = (v: unknown): Record<string, number> => {
  const out: Record<string, number> = {}
  if (v && typeof v === 'object' && !Array.isArray(v)) {
    for (const [k, at] of Object.entries(v)) if (typeof at === 'number' && Number.isFinite(at)) out[k] = at
  }
  return out
}

// The ones to raise now, the ones to only remember, and the store's new value. A store that never held the key is a
// first run: everything already there is remembered without a word (a fresh install never replays the backlog).
export function planNotices(found: readonly Notice[], stored: unknown, quiet: boolean, nowMs: number):
    { raise: Notice[]; kept: Record<string, number> } {
  const first = stored === undefined
  const kept = keptOf(stored)
  for (const [k, at] of Object.entries(kept)) if (nowMs - at > NOTIFIED_KEEP_MS) delete kept[k]
  const raise: Notice[] = []
  for (const n of found) {
    if (n.key in kept) continue
    if (!first && (!quiet || n.urgent)) {
      if (raise.length >= NOTIFY_PER_PASS) continue   // the rest waits for the next pass
      raise.push(n)
    }
    kept[n.key] = nowMs
  }
  const keys = Object.keys(kept)
  if (keys.length > NOTIFIED_MAX) for (const k of keys.sort((a, b) => kept[a] - kept[b]).slice(0, keys.length - NOTIFIED_MAX)) delete kept[k]
  return { raise, kept }
}

// One pass, on the leading session only and while the switch is on. A notification that could not be sent is still
// remembered: a channel that is off would otherwise be asked again every minute.
export async function notifyPass($: Dollar, s: ChiefStatus | null, floor: FloorState | null, self: string | null,
                                 extra: readonly Notice[] = []): Promise<Notice[]> {
  if (!s || !self || s.led_by !== self) return []
  if (!(await notifyIsOn($))) return []
  const found = [...noticesOf(s, floor), ...extra]
  let stored: unknown
  try { stored = await $.store.get(NOTIFIED_KEY) } catch { return [] }   // unreadable is not a first run: raise nothing
  if (!found.length && stored !== undefined) return []                   // a first run with nothing there still writes
  const { raise, kept } = planNotices(found, stored, s.quiet === true, await $.clock.now())
  try { await $.store.set(NOTIFIED_KEY, kept) } catch { return [] }   // unremembered, it would repeat: raise nothing
  for (const n of raise) {
    try { await $.ui.notify(n.text, { title: NOTIFY_TITLE }) } catch { /* refused or no channel */ }
  }
  return raise
}

// For a caller with one thing of its own (a chase card): raised once, on the leading session, by the same rules.
export async function notifyOnce($: Dollar, s: ChiefStatus | null, self: string | null, n: Notice): Promise<boolean> {
  return (await notifyPass($, s, null, self, [n])).some(r => r.key === n.key)
}

export async function notifyStatusText($: Dollar): Promise<string> {
  const on = await notifyIsOn($)
  return `Chief notifications: ${on ? 'on' : 'off'}. ` +
    'The leading session notifies once per new question, urgent inbox item, source going down or chase; ' +
    'during quiet hours only urgent ones.'
}
