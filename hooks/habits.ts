import type { Dollar } from './shared'
import { oneLine } from './shared'

// Learning from what the manager does with a card (lib/habits.py, docs/CONTRACT.md §5g "Habits"). The pane logs each
// action through `mother chief act-log --stdin`; triage reads the counts back, per line, from `mother chief lines`.

export const ACT_VERBS = ['open_case', 'action', 'done', 'snooze', 'ignore', 'follow', 'dismiss', 'answer', 'ask'] as const
export type ActVerb = (typeof ACT_VERBS)[number]
// What a row may carry: ids, labels and a title. Never a why, a summary or a message.
export type ActRow = { item_id?: string; key?: string; source?: string; label?: string; who?: string | null; kind?: string | null
                       urgency?: string | null; on?: string | null; title?: string; action_label?: string; action_index?: number }
const LOG_TIMEOUT_MS = 10_000
const TITLE_MAX = 60

// An Inbox item's row, and a Now item's (Today, Team, Suggested: its id names its source, as `chat:…`).
export const inboxRow = (u: { id: string; key?: string; who?: string | null; kind?: string | null; urgency?: string | null; on?: string | null
                              title?: string }): ActRow =>
  ({ item_id: u.id, key: u.key, who: u.who ?? '', kind: u.kind ?? null, urgency: u.urgency ?? null, on: u.on ?? null, title: oneLine(u.title, TITLE_MAX) })
export const nowRow = (i: { id: string; title?: string }, source: string): ActRow =>
  ({ item_id: i.id, source, title: oneLine(i.title, TITLE_MAX) })
// The n-th of a card's action buttons; 0 is the recommended one.
export const picked = (label: unknown, n: number): ActRow => ({ action_label: oneLine(label, 24), action_index: n })

// Log one action: detached and best-effort. It never throws, never toasts and is never awaited by a handler.
export function logAct($: Dollar, bin: string, verb: ActVerb, row: ActRow = {}, more: ActRow = {}): void {
  try {
    void $.process.run(['python3', bin, 'chief', 'act-log', '--stdin'], { timeoutMs: LOG_TIMEOUT_MS, stdin: JSON.stringify({ ...row, ...more, verb }) })
      .catch(() => undefined)
  } catch { /* a failed log is nothing the manager needs to hear about */ }
}

// ---- habits in the triage prompt: the line's own buckets, as counts ----
export const HABITS_NOTE = "`habits`, when present, counts how the manager handled earlier items from this line's own channel, project, repo or " +
  'person (shown, engaged, ignored, closed_unread) and may name the action they usually pick for a kind (`prefers`). It is a hint about what ' +
  'deserves attention and which action to put first, never a reason to hide something addressed to the manager, a decision only they can ' +
  'make, or anything urgent. You may add "habit_note": "<= 80 chars" when habits changed your judgment.'
export const HABIT_NOTE_MAX = 80
const BUCKETS = ['this_channel', 'this_project', 'this_repo', 'this_label', 'this_person'] as const
const COUNTS = ['shown', 'engaged', 'ignored', 'closed_unread'] as const
const KINDS = ['decide', 'reply', 'act', 'fyi'] as const

// A line's `habits` as the prompt carries it: known buckets with whole-number counts, `prefers` by kind; null for none.
export function promptHabits(raw: unknown): Record<string, unknown> | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const o = raw as Record<string, any>
  const out: Record<string, unknown> = {}
  for (const b of BUCKETS) {
    const v = o[b]
    if (!v || typeof v !== 'object') continue
    const counts: Record<string, number> = {}
    for (const c of COUNTS) if (Number.isInteger(v[c]) && v[c] >= 0) counts[c] = v[c]
    if (Object.keys(counts).length) out[b] = counts
  }
  const prefers: Record<string, string> = {}
  for (const k of KINDS) {
    const label = o.prefers && typeof o.prefers === 'object' && typeof o.prefers[k] === 'string' ? oneLine(o.prefers[k], 24) : ''
    if (label) prefers[k] = label
  }
  if (Object.keys(prefers).length) out.prefers = prefers
  return Object.keys(out).length ? out : null
}

// A verdict's `habit_note`: one safe line of at most 80 characters, or undefined.
export const habitNote = (v: unknown): string | undefined => {
  const note = v && typeof v === 'object' ? (v as { habit_note?: unknown }).habit_note : null
  return typeof note === 'string' && oneLine(note, HABIT_NOTE_MAX) ? oneLine(note, HABIT_NOTE_MAX) : undefined
}

// ---- the Health tab's Quieted list (status.quieted) and the switch ----
export type Quieted = { id: string; key: string; title: string; label: string; bucket: string; at: string; why: string }
export function quietedOf(s: unknown): { count: number; recent: Quieted[]; on: boolean } {
  const q = (s as { quieted?: { count_7d?: unknown; recent?: unknown } } | null)?.quieted
  const recent = (Array.isArray(q?.recent) ? q!.recent : []).filter((r: any): r is Quieted => !!r && typeof r.id === 'string' && typeof r.bucket === 'string')
  return { count: typeof q?.count_7d === 'number' ? q.count_7d : recent.length, recent,
           on: (s as { habits?: { on?: unknown } } | null)?.habits?.on !== false }
}
export const HABITS_RE = /^habits(?:\s+(on|off|status))?$/
