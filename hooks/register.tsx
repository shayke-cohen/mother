import { atom, read, update } from 'claude-code'
import type { Hook, Register, Timer } from 'claude-code'

import type { HubBundle, HubDoc, HubPending, HubSession, HubSessions, HubUpdate, HubWatch, NowCoverage, NowDoc, NowItem, NowTop, WatcherDoc } from '../types'

// State the Now panel (Task 9) draws from. Keys are declared in types/index.d.ts.
export const docAtom = atom({ plugin: 'mother', key: 'doc' } as const, null)
export const errorAtom = atom({ plugin: 'mother', key: 'error' } as const, null)
export const workspaceAtom = atom({ plugin: 'mother', key: 'workspace' } as const, null)
export const snoozingAtom = atom({ plugin: 'mother', key: 'snoozing' } as const, null)
export const selectedAtom = atom({ plugin: 'mother', key: 'selected' } as const, null)
export const expandedAtom = atom({ plugin: 'mother', key: 'expanded' } as const, null)
export const hubAtom = atom({ plugin: 'mother', key: 'hub' } as const, null)

const PANE = 'mother-now'
const FOCUS = 'mother-focus'
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
  ...newFileLoader(), hub: { watcher: newFileLoader(), hub: newFileLoader(), sessions: newFileLoader(), watch: newFileLoader() },
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

export type HubFile = 'watcher' | 'hub' | 'sessions' | 'watch'
const HUB_FILES: Record<HubFile, string> = { watcher: 'watcher.json', hub: 'hub.json', sessions: 'sessions.json', watch: 'watch.json' }
const NO_HUB: HubBundle = { hub: null, sessions: null, watcher: null, watch: null }
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

// The hub files, each re-read only when its mtime moved. A missing file is null; a file that will not
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
  if (next.hub === before.hub && next.sessions === before.sessions && next.watcher === before.watcher && next.watch === before.watch) return
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

// Run one `mother now …` verb (or `hub`, `inbox`), then re-read now.json at once. A failure toasts the first
// line of stderr. True when it exited 0.
async function run($: Dollar, bin: string, loader: Loader, args: string[], rowId: string | null = null, group = 'now', toastOut = false): Promise<boolean> {
  let ok = false
  try {
    const ran = await $.process.run(['python3', bin, group, ...args])
    ok = ran.exitCode === 0
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
  return ok
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
  if (!key || key.startsWith('snooze-pick-') || key.startsWith('ignore-pick-')) return null
  const top = /^top-(?:row|done|doit|ask)-(\d+)$/.exec(key)
  if (top) return 'top:' + top[1]
  return /^(?:row|done|snooze|maketop|doit|ask|act\d+|quick\d+|focus|ignore)-(.+)$/.exec(key)?.[1] ?? null
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

// ---- quick actions ---------------------------------------------------------------------------------

// What the panel offers to do about an item, from its kind (the id's first part) and signal alone: no model
// call. Each fills the prompt (never submits); the tick's own `actions` come first, above these.
export type QuickGroup = 'Respond' | 'Investigate' | 'Delegate' | 'Triage'
export type Quick = { group: QuickGroup; label: string; text: string }
const QUICK_IN_ROW = 3
const DRAFT = " Draft anything that reaches another person; don't send."

export function quickActions(i: NowItem): Quick[] {
  const kind = i.id.split(':')[0]
  const ref = `"${i.title}"` + (i.link ? ` (${i.link})` : '')
  const q = (group: QuickGroup, label: string, text: string): Quick => ({ group, label, text: text + DRAFT })
  const out: Quick[] = []
  if (kind === 'chat' || kind === 'mail' || ['unanswered_mention', 'mail_needs_reply', 'outside_question'].includes(i.signal)) {
    out.push(q('Respond', 'Draft a reply', `Read ${ref} in full and draft my reply, in my voice, as a draft for me to check.`))
    out.push(q('Respond', 'Summarize the thread', `Summarize ${ref}: who asked what, what was decided, and what is still open on me.`))
    out.push(q('Investigate', 'Find related tickets', `Find tracker tickets and PRs related to ${ref}, and say which one it belongs to, or that none does.`))
    out.push(q('Delegate', 'Turn into a ticket', `Propose a tracker ticket for ${ref}: title, description, team and who should own it, with the source linked. Create nothing until I say yes.`))
  } else if (kind === 'pr') {
    if (i.signal === 'review_requested') out.push(q('Respond', 'Review brief', `Give me a review brief for ${ref}: what changed, the risky parts, what to check first, and any open comments.`))
    out.push(q('Investigate', 'Why is CI red?', `Check the checks on ${ref}. If any fail, find the failing step and the likely cause from its log.`))
    if (i.signal === 'pr_no_reviewer') out.push(q('Delegate', 'Find a reviewer', `Suggest a reviewer for ${ref} from the code owners and who last changed these files, and draft the review request.`))
    if (i.signal === 'pr_changes_silent' || i.signal === 'pr_approved_unmerged') out.push(q('Delegate', 'Nudge the author', `Draft a short nudge to the author of ${ref} about what it is waiting on.`))
  } else if (kind === 'ticket') {
    out.push(q('Investigate', 'Find related PRs and commits', `Find the PRs, branches and commits that mention ${ref}, and say how far the work got.`))
    out.push(q('Investigate', 'Investigate in the code', `Investigate ${ref} in the team's repos: where the problem likely lives and what a fix would touch.`))
    out.push(q('Delegate', 'Find the owner and draft a DM', `Find who owns the code behind ${ref} (code owners, recent changes), propose assigning it to them, and draft the DM. Assign nothing until I say yes.`))
  } else if (kind === 'repo' || kind === 'build' || i.signal === 'default_branch_red') {
    out.push(q('Investigate', 'Find the breaking change', `Find the commit that turned ${ref} red, from the first failing run and its log.`))
    out.push(q('Delegate', 'Draft a heads-up to its author', `Draft a heads-up to the author of the change that broke ${ref}, with the failing check linked.`))
  } else if (kind === 'event') {
    out.push(q('Respond', 'Prep me for it', `Prep me for ${ref}: who is coming, what is open between us, and what I should bring.`))
  } else {
    out.push(q('Investigate', "What's this about?", `Tell me what ${ref} is about and what closes it.`))
  }
  // A ticket and a red branch already name their owner above; everything else can be handed on.
  if (!(kind === 'ticket' || kind === 'repo' || kind === 'build' || i.signal === 'default_branch_red')) {
    out.push(q('Delegate', 'Hand it to someone', `Who on my teams is best placed to take ${ref}, by what they own? Draft the hand-off message.`))
  }
  out.push(q('Triage', 'Make it a todo', `Add ${ref} to my todos with the link, and mark it done in the Now list.`))
  return out
}

// ---- the pane --------------------------------------------------------------------------------------

type Ui = ReturnType<Dollar['ui']['resolve']>

// A link the surface can open; a workspace path (`teams/x/intake.md#1`) stays in the Ask prompt instead.
const isUrl = (link: string | null | undefined): link is string => !!link && /^https?:\/\//.test(link)

export type Chip = { label: string; url: string }

// A label from the path shape alone, never the host: `…/browse/PLAT-2` -> `PLAT-2`, `…/owner/repo/pull/7`
// (or `/-/merge_requests/7`, `/issues/7`) -> `repo#7`; else the caller's fallback.
export function chipLabel(url: string, fallback: string): string {
  const path = url.split(/[?#]/)[0].replace(/\/+$/, '')
  let m = /\/browse\/([A-Z][A-Z0-9]+-\d+)$/.exec(path)
  if (m) return m[1]
  m = /\/[\w.-]+\/([\w.-]+)(?:\/-)?\/(?:pull|pulls|merge_requests|issues)\/(\d+)$/.exec(path)
  if (m) return m[1] + '#' + m[2]
  return fallback
}

// The Ignore picker: just this, this for a week, and (for a chat thread key or link) its whole channel.
// `x` is what `mother hub mute` is given: a hub key or an item's link.
const THREAD_KEY = /^[CG][A-Z0-9]+\/\d+\.\d+$/
// A Now item adds its sender (`from`) and the tick's "similar messages" rule (`similar`) when it has them.
export type Similar = { words: string[]; channel?: string }
const orWords = (words: string[]) => words.map(w => `"${w}"`).join(' or ')
export function ignoreOptions(chat: boolean, from?: string | null, similar?: Similar | null): { value: string; label: string }[] {
  return [{ value: 'item', label: 'Just this' }, { value: 'week', label: 'This, for a week' },
          ...(chat ? [{ value: 'channel', label: 'The whole channel' }] : []),
          ...(from ? [{ value: 'sender', label: 'Everything from this sender' }] : []),
          ...(similar?.words?.length ? [{ value: 'similar', label: `Similar: ${orWords(similar.words)}${similar.channel ? ', in this channel' : ''}` }] : [])]
}
export function ignoreArgs(scope: string, x: string, from?: string | null, similar?: Similar | null): string[] {
  if (scope === 'week') return ['mute', '--for', '1w', '--', x]
  if (scope === 'channel') return ['mute', '--scope', 'channel', '--', x]
  if (scope === 'sender' && from) return ['mute', '--', from]
  if (scope === 'similar' && similar?.words?.length) {
    return ['mute', '--words', similar.words.join(','), ...(similar.channel ? ['--channel', similar.channel] : [])]
  }
  return ['mute', '--', x]
}

// A Now item about a mail thread, which `mother inbox` can archive or mark read.
const isMail = (i: NowItem): boolean => i.id.startsWith('mail:') && isUrl(i.link)
type Go = (args: string[], rowId?: string | null) => Promise<boolean>

// Every source of a row as chips: the tick's labelled `links` first, then `link` and each http(s) `also`
// they miss, one chip per url.
export function linkChips(links: Chip[] | null | undefined, link: string | null | undefined, also: string[] | null | undefined): Chip[] {
  const out: Chip[] = []
  const seen = new Set<string>()
  const add = (url: string, label: string) => { if (!seen.has(url)) { seen.add(url); out.push({ label, url }) } }
  for (const l of links ?? []) if (l && isUrl(l.url) && l.label) add(l.url, l.label)
  if (isUrl(link)) add(link, chipLabel(link, 'Source'))
  for (const a of also ?? []) if (isUrl(a)) add(a, chipLabel(a, 'Link'))
  return out
}

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
                  expandedList: string[] | null, nowMs: number, go: (args: string[], rowId?: string | null) => Promise<boolean>,
                  hub: HubBundle, goHub: (args: string[], rowId?: string | null) => Promise<boolean>, surface: string, goInbox: Go) {
  const { Box, Text, Button, Link, Select } = ui
  const hubNode = hubSection($, ui, surface, hub, snoozing, new Set(expandedList ?? []), nowMs, goHub, goInbox)
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
    const chips = open ? linkChips(i.links, i.link, i.also) : []
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
            {chips.length > 1 || (chips.length === 1 && chips[0].url !== i.link) ? (
              <Box key={'chips-' + i.id} flexWrap="wrap" gap="xs">
                {chips.map((c, n) => <Link key={`chip-${i.id}-${n}`} href={c.url}>{c.label}</Link>)}
              </Box>
            ) : null}
            <Box flexWrap="wrap" gap="xs">
              {(i.actions ?? []).map((a, n) => (
                <Button key={n === 0 ? 'doit-' + i.id : `act${n}-` + i.id} variant={n === 0 ? 'primary' : undefined}
                  hotkey={n === 0 ? hot(i.id, 'x') : undefined}
                  onPress={() => fill(a.prompt || `mother now do ${a.id}`)}>{n === 0 && !a.label ? 'Do it' : a.label || a.id}</Button>
              ))}
              {quickActions(i).slice(0, QUICK_IN_ROW).map((qa, n) => (
                <Button key={`quick${n}-` + i.id} onPress={() => fill(qa.text)}>{qa.label}</Button>
              ))}
              <Button key={'focus-' + i.id} hotkey={hot(i.id, 'f')} onPress={() => { void select(i.id); void $.ui.open({ id: FOCUS, title: 'Focus' }) }}>Focus</Button>
              {sess
                ? <Button key={'ask-' + i.id} hotkey={hot(i.id, 'a')} onPress={() => openSession($, goHub, sess[0], sess[1])}>Open session</Button>
                : <Button key={'ask-' + i.id} hotkey={hot(i.id, 'a')} onPress={() => fill(askPrompt(i.title, i.why, i.link))}>Ask Mother</Button>}
              {isUrl(i.link) ? <Button key={'tohub-' + i.id} onPress={() => goHub(['add', '--', i.link])}>Send to hub</Button> : null}
              <Button key={'done-' + i.id} hotkey={hot(i.id, 'd')} onPress={() => go(['done', i.id], i.id)}>Done</Button>
              {isMail(i) ? <Button key={'archive-' + i.id} onPress={async () => { if (await goInbox(['archive', '--', i.link], i.id)) await go(['done', i.id], i.id) }}>Archive</Button> : null}
              {isMail(i) ? <Button key={'read-' + i.id} onPress={() => goInbox(['read', '--', i.link], i.id)}>Mark read</Button> : null}
              <Button key={'snooze-' + i.id} hotkey={hot(i.id, 's')} onPress={() => update($, snoozingAtom, cur => (cur === i.id ? null : i.id))}>Snooze</Button>
              {isUrl(i.link)
                ? <Button key={'ignore-' + i.id} onPress={() => update($, snoozingAtom, cur => (cur === 'ignore:' + i.id ? null : 'ignore:' + i.id))}>Ignore…</Button>
                : null}
              {rank !== null && !inTop.has(i.id)
                ? <Button key={'maketop-' + i.id} hotkey={hot(i.id, 't')} onPress={() => go(['top', 'swap', String(rank), i.id], i.id)}>Make top 3</Button>
                : null}
            </Box>
            {snoozing === i.id
              ? <Select key={'snooze-pick-' + i.id} label="Snooze for" options={SNOOZES.map(value => ({ value }))}
                  onSelect={value => go(['snooze', i.id, value], i.id)} />
              : null}
            {snoozing === 'ignore:' + i.id && isUrl(i.link)
              ? <Select key={'ignore-pick-' + i.id} label="Ignore" options={ignoreOptions(i.id.startsWith('chat:'), i.from, i.similar)}
                  onSelect={async value => { if (await goHub(ignoreArgs(value, i.link, i.from, i.similar), 'ignore:' + i.id)) await go(['done', i.id], i.id) }} />
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
    const mail = all.filter(isMail)
    const doneAll = async (list: NowItem[]) => { for (const x of list) await go(['done', x.id]) }
    return (
      <Box key={kind} flexDirection="column">
        {heading(name, all.length)}
        {all.length >= 2 ? (
          <Box key={'batch-' + kind} flexWrap="wrap" gap="xs">
            {mail.length >= 2
              ? <Button key={'archive-all-' + kind} plain dimColor onPress={async () => { if (await goInbox(['archive', '--', ...mail.map(x => x.link)])) await doneAll(mail) }}>{`Archive ${mail.length} mail`}</Button>
              : null}
            <Button key={'done-all-' + kind} plain dimColor onPress={() => doneAll(all)}>Done all</Button>
          </Box>
        ) : null}
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
      {ignoredSection(ui, hub.watch, expanded, toggle, at, goHub)}
      {noItems && health.length === 0 && top.length === 0
        ? <Text>{`Nothing on you. Next check ${at(doc.next_at)}.`}</Text> : null}
    </Box>
  )
}

// The focus pane: one item in full, beside the Now pane. It follows the selected row (a Top 3 entry shows
// its item), else the first item. Every action fills the prompt or runs the same verbs as the Now pane.
const QUICK_GROUPS: QuickGroup[] = ['Respond', 'Investigate', 'Delegate', 'Triage']
const SECTION_NAME: Record<NowItem['section'], string> = { on_you: 'On you', neglected: 'Neglected', suggestion: 'Suggestion' }
const RELATED_MAX = 5

function focusTree($: Dollar, ui: Ui, doc: NowDoc | null, picking: string | null, selected: string | null, nowMs: number,
                   go: (args: string[], rowId?: string | null) => Promise<boolean>, hub: HubBundle,
                   goHub: (args: string[], rowId?: string | null) => Promise<boolean>, goInbox: Go) {
  const { Box, Text, Button, Link, Select } = ui
  const items = doc?.items ?? []
  const topItem = /^top:(\d+)$/.exec(selected ?? '')
  const wanted = topItem ? (doc?.top ?? []).find(e => String(e.rank) === topItem[1])?.item_id : selected
  const i = items.find(x => x.id === wanted) ?? items[0]
  if (!i) return <Box flexDirection="column"><Text dimColor>Select an item in the Now panel.</Text></Box>
  const at = (iso: string | undefined | null) => clock(iso, doc?.timezone)
  const fill = (text: string) => { void $.prompt.fill({ text, mode: 'replace' }) }
  const select = (id: string) => update($, selectedAtom, () => id)
  const heading = (text: string) => <Box key={'fh-' + text} marginTop={1}><Text weight="semibold">{text}</Text></Box>
  const quick = quickActions(i)
  const sess = sessionForItem(hub.sessions, i)
  const mine = new Set([i.link, ...(i.also ?? [])])
  const related = items.filter(x => x.id !== i.id && ((i.team && x.team === i.team) || [x.link, ...(x.also ?? [])].some(k => mine.has(k))))
    .slice(0, RELATED_MAX)
  const rank = lowestOpenRank(doc?.top_date === localDate(nowMs, doc?.timezone) ? doc?.top ?? [] : [])
  const meta = [SECTION_NAME[i.section], i.team, i.signal.replace(/_/g, ' ')].filter(Boolean).join(' · ')
  return (
    <Box flexDirection="column">
      <Text weight="semibold" size="heading" wrap="wrap">{i.title}</Text>
      <Text dimColor>{meta}</Text>
      {i.why ? <Text wrap="wrap">{i.why}</Text> : null}
      <Text dimColor wrap="wrap">{`raised ${age(i.since, nowMs)} ago · first seen ${at(i.first_seen)} · checked ${at(i.last_seen)}`
        + (i.unverified_since ? ` · unverified since ${at(i.unverified_since)}` : '')}</Text>
      <Box key="fp-chips" flexWrap="wrap" gap="xs">
        {linkChips(i.links, i.link, i.also).map((c, n) => <Link key={'fp-chip-' + n} href={c.url}>{c.label}</Link>)}
      </Box>
      {(i.actions ?? []).length > 0 ? heading('Suggested') : null}
      {(i.actions ?? []).length > 0 ? (
        <Box key="fp-suggested" flexWrap="wrap" gap="xs">
          {(i.actions ?? []).map((a, n) => (
            <Button key={'fp-act' + n} variant={n === 0 ? 'primary' : undefined} onPress={() => fill(a.prompt || `mother now do ${a.id}`)}>{a.label || a.id}</Button>
          ))}
        </Box>
      ) : null}
      {QUICK_GROUPS.map(g => {
        const mineQ = quick.map((qa, n) => [qa, n] as [Quick, number]).filter(([qa]) => qa.group === g)
        return (
          <Box key={'fp-group-' + g} flexDirection="column">
            {heading(g)}
            <Box flexWrap="wrap" gap="xs">
              {mineQ.map(([qa, n]) => <Button key={'fp-q' + n} onPress={() => fill(qa.text)}>{qa.label}</Button>)}
              {g === 'Investigate'
                ? (sess
                    ? <Button key="fp-hub" onPress={() => openSession($, goHub, sess[0], sess[1])}>Open session</Button>
                    : isUrl(i.link) ? <Button key="fp-hub" onPress={() => goHub(['add', '--', i.link])}>Run in a session</Button> : null)
                : null}
              {g === 'Triage' ? (
                <Box key="fp-triage" flexWrap="wrap" gap="xs">
                  <Button key="fp-done" onPress={() => go(['done', i.id])}>Done</Button>
                  {isMail(i) ? <Button key="fp-archive" onPress={async () => { if (await goInbox(['archive', '--', i.link])) await go(['done', i.id]) }}>Archive</Button> : null}
                  {isMail(i) ? <Button key="fp-read" onPress={() => goInbox(['read', '--', i.link])}>Mark read</Button> : null}
                  <Button key="fp-snooze" onPress={() => update($, snoozingAtom, cur => (cur === 'fp-snooze' ? null : 'fp-snooze'))}>Snooze</Button>
                  {isUrl(i.link) ? <Button key="fp-ignore" onPress={() => update($, snoozingAtom, cur => (cur === 'fp-ignore' ? null : 'fp-ignore'))}>Ignore…</Button> : null}
                  {rank !== null ? <Button key="fp-maketop" onPress={() => go(['top', 'swap', String(rank), i.id])}>Make top 3</Button> : null}
                </Box>
              ) : null}
            </Box>
          </Box>
        )
      })}
      {picking === 'fp-snooze'
        ? <Select key="fp-snooze-pick" label="Snooze for" options={SNOOZES.map(value => ({ value }))} onSelect={value => go(['snooze', i.id, value], 'fp-snooze')} />
        : null}
      {picking === 'fp-ignore' && isUrl(i.link)
        ? <Select key="fp-ignore-pick" label="Ignore" options={ignoreOptions(i.id.startsWith('chat:'), i.from, i.similar)}
            onSelect={async value => { if (await goHub(ignoreArgs(value, i.link, i.from, i.similar), 'fp-ignore')) await go(['done', i.id]) }} />
        : null}
      <Box marginTop={1}>
        <Button key="fp-tell" onPress={() => fill(`About "${i.title}"` + (i.link ? ` (${i.link})` : '') + ': ')}>Tell Mother…</Button>
      </Box>
      {related.length > 0 ? heading('Related') : null}
      {related.map(r => (
        <Button key={'fp-rel-' + r.id} plain dimColor onPress={() => select(r.id)}>{`▸ ${r.title} · ${age(r.since, nowMs)}`}</Button>
      ))}
    </Box>
  )
}

// What the manager ignored (`mother hub mute`, in watch.json), collapsed at the foot of the pane: each value
// with what it is and its expiry, and Unignore (`unmute -- <value>`). Nothing muted draws nothing.
function ignoredSection(ui: Ui, watch: HubWatch | null, expanded: Set<string>, toggle: (name: string) => unknown,
                        at: (iso: string | null | undefined) => string, goHub: (args: string[], rowId?: string | null) => Promise<boolean>) {
  const { Box, Text, Button } = ui
  const m = watch?.mute
  const until = m?.until ?? {}
  const rows: [string, string][] = [...(m?.keys ?? []).map(v => [v, 'item'] as [string, string]),
                                    ...(m?.people ?? []).map(v => [v, 'sender'] as [string, string]),
                                    ...(m?.channels ?? []).map(v => [v, 'channel'] as [string, string]),
                                    ...(m?.topics ?? []).map(v => [v, 'similar'] as [string, string])]
  if (rows.length === 0) return null
  const open = expanded.has('ignored')
  return (
    <Box key="ignored" flexDirection="column" marginTop={1}>
      <Button key="ignored-toggle" plain dimColor onPress={() => toggle('ignored')}>{`${open ? '▾' : '▸'} Ignored ${rows.length}`}</Button>
      {open ? rows.map(([v, kind]) => (
        <Box key={'ign-' + v} paddingLeft={2} flexWrap="wrap">
          <Text dimColor>{`${topicText(v)} · ${until[v] ? 'until ' + at(until[v]) : kind} `}</Text>
          <Button key={'unignore-' + v} onPress={() => goHub(['unmute', '--', v])}>Unignore</Button>
        </Box>
      )) : null}
    </Box>
  )
}

// `topic:flaky|retry@C1` -> `"flaky" or "retry" in C1`; any other value as it is.
function topicText(v: string): string {
  const m = /^topic:([^@]+)(?:@(.+))?$/.exec(v)
  return m ? orWords(m[1].split('|')) + (m[2] ? ` in ${m[2]}` : '') : v
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
function openSession($: Dollar, goHub: (args: string[], rowId?: string | null) => Promise<boolean>, key: string, v: HubSession) {
  void $.prompt.fill({ text: `Open my "${v.title}" session`, mode: 'replace' })
  void goHub(['session', 'seen', key])
}

// The Hub section: first in the pane. Open by default when something waits; `hub` in `expanded` flips that.
function hubSection($: Dollar, ui: Ui, surface: string, hb: HubBundle, snoozing: string | null, expanded: Set<string>, nowMs: number,
                    goHub: (args: string[], rowId?: string | null) => Promise<boolean>, goInbox: Go) {
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
  const pickIgnore = (row: string) => update($, snoozingAtom, cur => (cur === 'ignore:' + row ? null : 'ignore:' + row))
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
                  <Button key={'sess-mute-' + key} onPress={() => pickIgnore('sess:' + key)}>Ignore…</Button>
                  {linkChips(null, null, v.also).filter(c => c.url !== v.link).map((c, n) => (
                    <Link key={`chip-sess-${key}-${n}`} href={c.url}>{c.label}</Link>
                  ))}
                  {snoozing === 'ignore:sess:' + key
                    ? <Select key={'sess-ignore-pick-' + key} label="Ignore" options={ignoreOptions(THREAD_KEY.test(key))}
                        onSelect={value => goHub(ignoreArgs(value, key), 'ignore:sess:' + key)} />
                    : null}
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
                    {/^mail:/.test(u.key)
                      ? <Button key={'hub-archive-' + u.id} onPress={async () => { if (await goInbox(['archive', '--', u.key])) await goHub(['done', u.id], 'hub:' + u.id) }}>Archive</Button>
                      : null}
                    {followArgs(u.key) ? <Button key={'hub-follow-' + u.id} onPress={() => goHub(followArgs(u.key) as string[])}>Follow</Button> : null}
                    <Button key={'hub-mute-' + u.id} onPress={() => pickIgnore('hub:' + u.id)}>Ignore…</Button>
                    <Button key={'hub-snooze-' + u.id} onPress={() => update($, snoozingAtom, cur => (cur === 'hub:' + u.id ? null : 'hub:' + u.id))}>Snooze</Button>
                  </Box>
                  {u.why ? <Text dimColor wrap="wrap">{u.why}</Text> : null}
                  {snoozing === 'ignore:hub:' + u.id
                    ? <Select key={'hub-ignore-pick-' + u.id} label="Ignore" options={ignoreOptions(THREAD_KEY.test(u.key))}
                        onSelect={async value => { if (await goHub(ignoreArgs(value, u.key), 'ignore:hub:' + u.id)) await goHub(['done', u.id], 'hub:' + u.id) }} />
                    : null}
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

  on('ui.render', { component: 'Pane', requestId: FOCUS }, async ($, e) => {
    const bin = await motherBin($, configured)
    const go = (args: string[], rowId: string | null = null) => run($, bin, loader, args, rowId)
    const goHub = (args: string[], rowId: string | null = null) => run($, bin, loader, args, rowId, 'hub', ['add', 'mute'].includes(args[0]))
    const goInbox = (args: string[], rowId: string | null = null) => run($, bin, loader, args, rowId, 'inbox', true)
    return focusTree($, $.ui.resolve(e), await read($, docAtom), await read($, snoozingAtom), await read($, selectedAtom),
      await $.clock.now(), go, (await read($, hubAtom)) ?? NO_HUB, goHub, goInbox)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const bin = await motherBin($, configured)
    const go = (args: string[], rowId: string | null = null) => run($, bin, loader, args, rowId)
    const goHub = (args: string[], rowId: string | null = null) => run($, bin, loader, args, rowId, 'hub', ['add', 'follow', 'mute', 'unmute'].includes(args[0]))
    return paneTree($, $.ui.resolve(e), await read($, docAtom), await read($, errorAtom), await read($, snoozingAtom),
      await read($, selectedAtom), await read($, expandedAtom), await $.clock.now(), go,
      (await read($, hubAtom)) ?? NO_HUB, goHub, e.surface, (args: string[], rowId: string | null = null) => run($, bin, loader, args, rowId, 'inbox', true))
  })
}
