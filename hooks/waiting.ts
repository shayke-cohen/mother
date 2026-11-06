import { oneLine } from './shared'
import type { ChiefDesktop, ChiefStatus, ChiefTldr, FloorCard, FloorState, FloorWaiting } from '../types'

// What each session waits on (docs/CONTRACT.md §5g "Waiting on"). A pinned session's own mod asks it, through one
// system-prompt section, to end every turn with a `Waiting on:` line; lib/floor.py reads that line in code into the
// session's floor card as `waiting: {kind, who, what, since}`, keeping `since` while the same wait repeats. From it the
// pane draws three things, all computed here with no model call: `check due` on a session's card (waiting on someone
// else for over a day), a chase card in Needs you (for 3 days), and a Done card (it said it waits on nothing and has
// been quiet for 10 minutes).

export const WAITING_SECTION_ID = 'mother:waiting-on'
// Fixed copy: the one line a pinned session is asked to end each turn with. A case session gets the same from the
// cos skill, so it never gets this section too.
export const WAITING_SECTION = 'Close every turn with one final line that says what this session is now held up by, in one of three forms: ' +
  '`Waiting on: the manager — <what you need from them>`, `Waiting on: <person or thing> — <what>`, or `Waiting on: nothing — done`. ' +
  'Write nothing after it. The manager\'s overview of their sessions is built from that line.'
export const CHECK_DUE_MS = 24 * 3_600_000
export const CHASE_MS = 3 * 86_400_000
export const DONE_QUIET_MS = 10 * 60_000
const WHO_MAX = 80
const WHAT_MAX = 200

// The instruction a `Check now` press sends: read-only, and it ends on a fresh `Waiting on:` line.
export const CHECK_NOW_TEXT = 'Check, read-only, whether what you are waiting on has arrived (look at the thread, ticket or PR it is in; change ' +
  'nothing and send nothing), then end with an updated `Waiting on:` line.'
// The instruction `Draft a nudge` sends: a draft only, never a send.
export const nudgeText = (w: FloorWaiting): string =>
  `Draft a short, polite nudge to ${oneLine(w.who, WHO_MAX)} about ${oneLine(w.what, WHAT_MAX) || 'what you are waiting on'}. ` +
  'Show me the draft and do not send it or post it anywhere.'

// Is `cwd` one of the folders (or under one)? Folders come absolute from `status --json` -> floor.ignore_folders.
export function underFolder(cwd: string | null | undefined, folders: readonly unknown[] | null | undefined): boolean {
  const c = String(cwd ?? '').replace(/\/+$/, '')
  if (!c.startsWith('/')) return false
  return (folders ?? []).some(f => typeof f === 'string' && f.startsWith('/') && (c === f.replace(/\/+$/, '') || c.startsWith(f.replace(/\/+$/, '') + '/')))
}

// A session that started from a case brief runs under the cos skill, which asks for the line itself. A case row made
// from what the manager named to a pinned session (`origin: "mention"`) is not such a session.
export function runsCosSkill(s: ChiefStatus | null, cliId: string): boolean {
  return (Array.isArray(s?.cases) ? s!.cases! : []).some(c => c && c.cli_session_id === cliId && c.origin !== 'mention')
}

// Whether this session's system prompt gets the `Waiting on:` section: pinned, not a cos case, not under an ignored
// folder, and not switched off here (`/chief floor off`).
export function wantsWaitingSection(o: { pinned: boolean; cosCase: boolean; cwd: string | null; folders: readonly unknown[] | null | undefined; off: boolean }): boolean {
  return o.pinned && !o.cosCase && !o.off && !underFolder(o.cwd, o.folders)
}

// A card's wait, as a reader may trust it; null for none.
export function waitingOf(c: FloorCard | null | undefined): FloorWaiting | null {
  const w = c?.waiting
  if (!w || typeof w !== 'object' || !['manager', 'other', 'nothing'].includes(w.kind) || Number.isNaN(Date.parse(String(w.since)))) return null
  return { ...w, who: oneLine(w.who, WHO_MAX) || 'someone', what: oneLine(w.what, WHAT_MAX) }
}

const cardOf = (floor: FloorState | null, cli: string): FloorCard | undefined => floor?.cards.find(c => c.session_id === cli)

// `check due`: what the CLI's list read said; null when there is no wait to re-check.
export function checkDue(floor: FloorState | null, cli: string): FloorWaiting | null {
  const w = waitingOf(cardOf(floor, cli))
  return w && w.kind === 'other' && w.check_due === true && !w.dropped ? w : null
}

// Whole days since a wait started.
export const waitDays = (w: FloorWaiting, nowMs: number): number => Math.floor((nowMs - Date.parse(w.since)) / 86_400_000)

export type Chase = { d: ChiefDesktop; card: FloorCard; w: FloorWaiting; days: number }
export type DoneOne = { d: ChiefDesktop; card: FloorCard }

// Chase cards, longest wait first: waiting on someone other than the manager for 3 days or more, not dropped, not
// kept waiting (`snooze_until` in the future), never this session's own.
export function chaseCards(rows: readonly ChiefDesktop[], floor: FloorState | null, nowMs: number): Chase[] {
  const out: Chase[] = []
  for (const d of rows) {
    const card = cardOf(floor, d.cli_session_id)
    const w = waitingOf(card)
    if (!card || !w || w.kind !== 'other' || w.dropped || d.cli_session_id === floor?.self) continue
    if (nowMs - Date.parse(w.since) < CHASE_MS) continue
    if (w.snooze_until && Date.parse(w.snooze_until) > nowMs) continue
    out.push({ d, card, w, days: waitDays(w, nowMs) })
  }
  return out.sort((a, b) => Date.parse(a.w.since) - Date.parse(b.w.since))
}

// When the session last moved: its card's own time (a turn end, or a prompt typed there), or the app's last
// activity when that is later.
export function lastMoved(card: FloorCard, d: ChiefDesktop): number {
  return Math.max(Date.parse(card.at) || 0, (d.last_activity_at ? Date.parse(d.last_activity_at) : 0) || 0)
}

// Done cards: the session said it waits on nothing, is not running, has been quiet for 10 minutes, does not ask, and
// the manager did not press Keep since it last moved (`done_kept` is the card time Keep was pressed at).
export function doneCards(rows: readonly ChiefDesktop[], floor: FloorState | null, tldrs: Record<string, ChiefTldr> | null, nowMs: number): DoneOne[] {
  const out: DoneOne[] = []
  for (const d of rows) {
    const cli = d.cli_session_id
    const card = cardOf(floor, cli)
    const w = waitingOf(card)
    if (!card || !w || w.kind !== 'nothing' || card.asks || cli === floor?.self) continue
    if (card.done_kept && card.done_kept === card.at) continue
    if (tldrs?.[cli]?.running === true) continue
    if (nowMs - lastMoved(card, d) < DONE_QUIET_MS) continue
    out.push({ d, card })
  }
  return out
}
