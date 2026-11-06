import { atom, update } from 'claude-code'

import { oneLine } from './shared'
import type { Dollar } from './shared'
import type { ChiefDesktop, ChiefStatus, ChiefUndo } from '../types'

// Archive on a Done card, and its Undo (docs/CONTRACT.md §5g "Done card"). The desktop app's own session tools do it:
// `ccd_sidebar set_pinned` (an archive refuses a pinned session, so a pinned one is unpinned first) and
// `ccd_session_mgmt archive_session`, the way the pane already reads `list_sessions`. When the archive is refused or
// the tool is not there, what was done stands: unpinned, else the session is only hidden from the floor (the plugin's
// store, `chiefHidden`). Undo reverses exactly that, for 10 seconds. Nothing here deletes anything.
// (chief.tsx declares the same atoms: the engine's scan reads an atom from a const of the reading file.)
const hiddenAtom = atom({ plugin: 'mother', key: 'chiefHidden' } as const, null)
const undoAtom = atom({ plugin: 'mother', key: 'chiefUndo' } as const, null)

export const SESSION_SERVER = 'ccd_session_mgmt'
export const SIDEBAR_SERVER = 'ccd_sidebar'
export const HIDDEN_STORE_KEY = 'chiefHidden'
export const UNDO_MS = 10_000
const CALL_MS = 10_000
const LOCAL_RE = /^local_[0-9A-Za-z-]{1,80}$/
const SESSION_ID_RE = /^[0-9a-f-]{36}$/
const HIDDEN_MAX = 200
const REFUSED_RE = /refus|denied|not allowed|cannot|can't|couldn't|unable|not archived|still working|is pinned|not found|no such/i

export type ArchiveHow = ChiefUndo['how']

// One app tool call with a timer: true only for an answer that is not an error and does not say it refused.
async function tool($: Dollar, server: string, name: string, args: Record<string, unknown>): Promise<boolean> {
  const stop = new AbortController()
  try {
    const timedOut = $.clock.sleep(CALL_MS, { signal: stop.signal }).then(() => 'timeout' as const, () => 'cancelled' as const)
    const r: any = await Promise.race([$.mcp.call(server, name, args), timedOut])
    if (r === 'timeout' || r === 'cancelled' || !r || r.isError) return false
    const text = (Array.isArray(r.content) ? r.content : []).filter((b: any) => b?.type === 'text').map((b: any) => String(b.text ?? '')).join(' ')
    return !REFUSED_RE.test(text)
  } catch {
    return false
  } finally {
    stop.abort()
  }
}

const hiddenOnly = (v: unknown): Record<string, true> => {
  const out: Record<string, true> = {}
  if (v && typeof v === 'object' && !Array.isArray(v)) for (const k of Object.keys(v).slice(-HIDDEN_MAX)) if (SESSION_ID_RE.test(k)) out[k] = true
  return out
}

export async function loadHidden($: Dollar): Promise<void> {
  let hidden: Record<string, true> = {}
  try { hidden = hiddenOnly(await $.store.get(HIDDEN_STORE_KEY)) } catch { /* nothing hidden */ }
  await update($, hiddenAtom, () => hidden)
}

// Hide or show one session on the floor. The store keeps it past the session; a store that fails keeps it here only.
export async function setHidden($: Dollar, cli: string, hide: boolean): Promise<void> {
  let next: Record<string, true> = {}
  await update($, hiddenAtom, (prev: Record<string, true> | null) => {
    next = { ...(prev ?? {}) }
    if (hide) next[cli] = true; else delete next[cli]
    return next
  })
  try {
    const stored = hiddenOnly(await $.store.get(HIDDEN_STORE_KEY))
    if (hide) stored[cli] = true; else delete stored[cli]
    await $.store.set(HIDDEN_STORE_KEY, stored)
  } catch { /* hidden in this pane for this session only */ }
}

// Archive, else unpin, else hide; says which. `pinned`: the session is in the sidebar's Pinned list.
export async function archiveSession($: Dollar, d: ChiefDesktop, pinned: boolean, nowMs: number): Promise<ChiefUndo> {
  const appId = LOCAL_RE.test(d.session_id) ? d.session_id : ''
  let unpinned = false
  if (appId && pinned) unpinned = await tool($, SIDEBAR_SERVER, 'set_pinned', { session_id: appId, pinned: false })
  let how: ArchiveHow = 'hidden'
  if (appId && (unpinned || !pinned) && await tool($, SESSION_SERVER, 'archive_session', { session_id: appId, reason: 'Done, from the Chief pane' })) how = 'archived'
  else if (unpinned) how = 'unpinned'
  if (how !== 'archived') await setHidden($, d.cli_session_id, true)   // off the floor either way until it is undone
  const undo: ChiefUndo = { how, cli: d.cli_session_id, session_id: appId, title: oneLine(d.title, 60), repin: unpinned, until: nowMs + UNDO_MS }
  await update($, undoAtom, () => undo)
  void $.clock.sleep(UNDO_MS).then(() => update($, undoAtom, (u: ChiefUndo | null) => (u && u.until <= nowMs + UNDO_MS ? null : u))).catch(() => undefined)
  return undo
}

// What the toast says of an archive.
export const archivedSays = (u: ChiefUndo): string =>
  u.how === 'archived' ? `Archived "${u.title}"` : u.how === 'unpinned' ? `Couldn't archive "${u.title}": unpinned it and hid it from the floor`
    : `Couldn't archive or unpin "${u.title}": hid it from the floor`

// Undo: unarchive (and pin again), or pin again, then show it on the floor. True when every step went through.
export async function undoArchive($: Dollar, u: ChiefUndo): Promise<boolean> {
  await update($, undoAtom, () => null)
  let ok = true
  if (u.how === 'archived' && u.session_id) ok = await tool($, SESSION_SERVER, 'unarchive_session', { session_id: u.session_id })
  if (u.repin && u.session_id) ok = (await tool($, SIDEBAR_SERVER, 'set_pinned', { session_id: u.session_id, pinned: true })) && ok
  await setHidden($, u.cli, false)
  return ok
}

// The status as the pane draws it: hidden sessions are off the floor (their rows and their cases' rows), nothing else.
export function withoutHidden(s: ChiefStatus | null, hidden: Record<string, true> | null | undefined): ChiefStatus | null {
  if (!s || !hidden || !Object.keys(hidden).length) return s
  return { ...s, desktop: (s.desktop ?? []).filter(d => !hidden[d.cli_session_id]), cases: (s.cases ?? []).filter(c => !hidden[c.cli_session_id]) }
}
