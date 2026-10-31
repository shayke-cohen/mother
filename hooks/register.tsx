import { atom, read, update } from 'claude-code'
import type { Hook, Register, Timer } from 'claude-code'

import type { HubBundle, HubDoc, HubPending, HubSession, HubSessions, HubUpdate, HubWatch, InspectRef, NowCoverage, NowDoc, NowItem, NowTop, WatcherDoc } from '../types'

// State the Now panel (Task 9) draws from. Keys are declared in types/index.d.ts.
export const docAtom = atom({ plugin: 'mother', key: 'doc' } as const, null)
export const errorAtom = atom({ plugin: 'mother', key: 'error' } as const, null)
export const workspaceAtom = atom({ plugin: 'mother', key: 'workspace' } as const, null)
export const snoozingAtom = atom({ plugin: 'mother', key: 'snoozing' } as const, null)
export const selectedAtom = atom({ plugin: 'mother', key: 'selected' } as const, null)
export const expandedAtom = atom({ plugin: 'mother', key: 'expanded' } as const, null)
export const hubAtom = atom({ plugin: 'mother', key: 'hub' } as const, null)
export const inspectAtom = atom({ plugin: 'mother', key: 'inspect' } as const, null)

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

// One source in the hub's health line: ✓ / ~ / ✗, plus how old the reading is when it isn't ✓ (a ✗ from two
// hours ago, before a VPN came back, reads very differently from one a minute old), and `stale` as before.
export function sourceMark(cap: string, row: { status?: string; at?: string; every_seconds?: number } | null | undefined,
                           active: boolean | undefined, nowMs: number): string {
  const st = row?.status
  const m = st === 'ok' ? '✓' : st === 'partial' ? '~' : '✗'
  const old = st !== 'ok' && row?.at ? ` ${ago(row.at, nowMs)} ago` : ''
  return `${cap} ${m}${old}${sourceStale(row, active, nowMs) ? ' stale' : ''}`
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

// The Top 3 entry a focused element belongs to (`top:<rank>`), which then holds the hotkeys; null for any other
// element: Home rows open the Inspector, and the hotkeys of a page live there.
export function rowOfElement(key: string | undefined): string | null {
  const top = /^top-(?:row|done|doit|ask)-(\d+)$/.exec(key ?? '')
  return top ? 'top:' + top[1] : null
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

const SECTION_NAME: Record<NowItem['section'], string> = { on_you: 'On you', neglected: 'Neglected', suggestion: 'Suggestion' }
const RELATED_MAX = 5

// ---- what a row asks of the manager ----------------------------------------------------------------

// The verb an on-you item asks for, from its kind and signal alone. Decide rows fold into one row on Home and
// get the decision queue; the rest group under Reply, Review and On you.
export type Verb = 'decide' | 'reply' | 'review' | 'other'
const REPLY_SIGNALS = ['unanswered_mention', 'mail_needs_reply', 'outside_question']
export function verbOf(i: NowItem): Verb {
  const kind = i.id.split(':')[0]
  if (i.signal === 'decision_open') return 'decide'
  if (kind === 'chat' || kind === 'mail' || REPLY_SIGNALS.includes(i.signal)) return 'reply'
  if (kind === 'pr' || i.signal === 'review_requested') return 'review'
  return 'other'
}
const GROUPS: [Verb, string][] = [['reply', 'Reply'], ['review', 'Review'], ['other', 'On you']]

// Rows a group draws before "Show more"; the rest wait behind the toggle (`g-<verb>` / `g-suggestion` in expanded).
const LIMITS: Record<Verb | 'suggestion', number> = { decide: 0, reply: 5, review: 5, other: 5, suggestion: 3 }
const SESSIONS_ON_HOME = 3

// The live sessions with news first, then the most recently routed.
export function sessionsByNews(s: HubSessions | null | undefined): [string, HubSession][] {
  const t = (iso: string | null | undefined) => (iso ? Date.parse(iso) || 0 : 0)
  return liveSessions(s).sort(([, a], [, b]) => ((b.unseen ?? 0) - (a.unseen ?? 0)) || (t(b.last_routed_at ?? b.opened_at) - t(a.last_routed_at ?? a.opened_at)))
}

// Counts of a list by a label, largest first, as `label n · label n`: work counts, never people.
function tally(labels: string[], max = 3): string {
  const n = new Map<string, number>()
  for (const l of labels) n.set(l, (n.get(l) ?? 0) + 1)
  return [...n.entries()].sort((a, b) => b[1] - a[1]).slice(0, max).map(([l, c]) => `${l} ${c}`).join(' · ')
}
const signalText = (s: string) => s.replace(/_/g, ' ')
const teamName = (t: string | null | undefined) => t || 'No team'

// ---- the Inspector ---------------------------------------------------------------------------------

// One pane beside Home that shows one thing at a time. A row on Home starts a fresh stack; a link inside a page
// pushes onto it, and Back pops. The stack lives in `inspect` (types/index.d.ts).
export const INSPECTOR = 'mother-inspector'
const STACK_MAX = 20
const TITLES: Record<InspectRef['kind'], string> = {
  item: 'Item', update: 'Hub update', session: 'Session', sessions: 'Sessions', proposal: 'Proposed session',
  decisions: 'Decide', team: 'Team', system: 'Mother',
}
export const inspectTitle = (r: InspectRef): string => (r.kind === 'team' ? `Team · ${teamName(r.id)}` : TITLES[r.kind])
const sameRef = (a: InspectRef | undefined, b: InspectRef) => !!a && a.kind === b.kind && (a.id ?? null) === (b.id ?? null) && (a.filter ?? null) === (b.filter ?? null)

export async function openInspector($: Dollar, ref: InspectRef, mode: 'reset' | 'push' | 'replace' = 'push'): Promise<void> {
  await update($, inspectAtom, cur => {
    const stack = cur ?? []
    if (mode === 'reset') return [ref]
    if (mode === 'replace') return [...stack.slice(0, -1), ref]
    return sameRef(stack[stack.length - 1], ref) ? stack : [...stack, ref].slice(-STACK_MAX)
  })
  // A picker left open on the page before closes; a hub refresh still running keeps its marker.
  await update($, snoozingAtom, cur => (cur === 'hub:refresh' ? cur : null))
  await $.ui.open({ id: INSPECTOR, title: inspectTitle(ref) })
}

async function inspectorBack($: Dollar): Promise<void> {
  const stack = (await read($, inspectAtom)) ?? []
  const prev = stack[stack.length - 2]
  if (!prev) return
  await update($, inspectAtom, () => stack.slice(0, -1))
  await update($, snoozingAtom, cur => (cur === 'hub:refresh' ? cur : null))
  await $.ui.open({ id: INSPECTOR, title: inspectTitle(prev) })
}

// What every page and Home share: the files as last read, the open picker, the toggles, and the verbs.
type Ctx = {
  doc: NowDoc | null; hub: HubBundle; picking: string | null; expanded: Set<string>; nowMs: number; surface: string
  go: Go; goHub: Go; goInbox: Go
}

const todaysTop = (doc: NowDoc | null, nowMs: number): NowTop[] =>
  doc && doc.top_date === localDate(nowMs, doc.timezone) ? doc.top ?? [] : []

function toggler($: Dollar) {
  return (name: string) => update($, expandedAtom, cur => {
    const set = new Set(cur ?? [])
    if (set.has(name)) set.delete(name); else set.add(name)
    return [...set]
  })
}

// Home: Top 3, the asks, then one stream grouped by what the manager must do. Every row is one line that opens
// the Inspector; the actions live there.
function homeTree($: Dollar, ui: Ui, c: Ctx, error: string | null, selected: string | null) {
  const { Box, Text, Button, Link } = ui
  const { doc, hub, expanded, nowMs, go, goHub, goInbox } = c
  const inspect = (ref: InspectRef) => openInspector($, ref, 'reset')
  const sessionsNode = sessionsGroup($, ui, c)
  if (!doc) {
    return (
      <Box flexDirection="column">
        {error
          ? <Text color="warning">Couldn't read now.json.</Text>
          : <Text dimColor>Tick runs on the home machine; nothing here yet.</Text>}
        {sessionsNode}
        {homeFooter($, ui, c)}
      </Box>
    )
  }

  const tz = doc.timezone
  const at = (iso: string | undefined | null) => clock(iso, tz)
  const items = doc.items ?? []
  const toggle = toggler($)
  const fill = (text: string) => { void $.prompt.fill({ text, mode: 'replace' }) }
  const top = todaysTop(doc, nowMs)
  // Hotkeys belong to the selected Top 3 entry alone: the one the ring is on (`ui.focus`), else the first open one.
  const rows = top.filter(e => e.state !== 'done').map(e => 'top:' + e.rank)
  const current = selected && rows.includes(selected) ? selected : rows[0]
  const hot = (row: string, k: string): string | undefined => (row === current ? k : undefined)
  const inTop = new Set(top.map(e => e.item_id).filter(Boolean))
  const byId = new Map(items.map(i => [i.id, i]))
  const gaps = Object.entries(doc.coverage ?? {}).filter(([, cv]) => cv && cv.status !== 'ok') as [string, NowCoverage][]
  const routines = doc.routines ?? []
  const stale = nowMs > Date.parse(doc.stale_after)
  const heading = (text: string, count?: number | string) => (
    <Box key={'h-' + text} marginTop={1}>
      <Text weight="semibold" size="heading">{text}{count !== undefined ? <Text dimColor weight="regular">{` ${count}`}</Text> : null}</Text>
    </Box>
  )

  // Without a Top 3 entry to hold it, the first row of the stream takes the focus ring.
  const firstRow = rows.length ? null : GROUPS.map(([v]) => items.find(i => i.section === 'on_you' && verbOf(i) === v)).find(Boolean)?.id ?? null
  const row = (i: NowItem) => {
    const meta = [i.team, age(i.since, nowMs)].filter(Boolean).join(' · ')
    return (
      <Box key={'item-' + i.id}>
        <Button key={'row-' + i.id} plain autoFocus={i.id === firstRow ? true : undefined} onPress={() => inspect({ kind: 'item', id: i.id })}>{(inTop.has(i.id) ? '★ ' : '') + i.title}</Button>
        <Text dimColor>{' ' + meta + ' '}</Text>
        {isUrl(i.link) ? <Link href={i.link}>↗</Link> : null}
        {i.unverified_since ? <Text color="warning">{' unverified'}</Text> : null}
        {sessionForItem(hub.sessions, i) ? <Text dimColor>{' ◆ session'}</Text> : null}
      </Box>
    )
  }

  const group = (name: string, key: string, all: NowItem[], limit: number) => {
    if (all.length === 0) return null
    const open = expanded.has('g-' + key)
    const shown = open ? all : all.slice(0, limit)
    const mail = all.filter(isMail)
    const doneAll = async (list: NowItem[]) => { for (const x of list) await go(['done', x.id]) }
    return (
      <Box key={'g-' + key} flexDirection="column">
        {heading(name, all.length)}
        {all.length >= 2 ? (
          <Box key={'batch-' + key} flexWrap="wrap" gap="xs">
            {mail.length >= 2
              ? <Button key={'archive-all-' + key} plain dimColor onPress={async () => { if (await goInbox(['archive', '--', ...mail.map(x => x.link)])) await doneAll(mail) }}>{`Archive ${mail.length} mail`}</Button>
              : null}
            <Button key={'done-all-' + key} plain dimColor onPress={() => doneAll(all)}>Done all</Button>
          </Box>
        ) : null}
        {shown.map(row)}
        {all.length > limit
          ? <Button key={'more-' + key} plain dimColor onPress={() => toggle('g-' + key)}>{open ? 'Show fewer' : `Show ${all.length - limit} more`}</Button>
          : null}
      </Box>
    )
  }

  const topEntry = (e: NowTop) => {
    const done = e.state === 'done'
    const id = 'top:' + e.rank
    const action = e.item_id ? byId.get(e.item_id)?.actions?.[0] : undefined
    const item = e.item_id ? byId.get(e.item_id) : undefined
    return (
      <Box key={'top-' + e.rank} flexDirection="column">
        <Box>
          <Button key={'top-row-' + e.rank} plain dimColor={done}
            onPress={() => (item ? inspect({ kind: 'item', id: item.id }) : update($, selectedAtom, () => id))}>
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

  // One line for everything that makes the list less trustworthy; the detail is on the Mother page.
  const health = [
    ...(stale ? [`stale since ${at(doc.stale_after)}`] : []),
    ...(routines.length ? [`${routines.length} routine${routines.length === 1 ? '' : 's'} ${new Set(routines.map(r => r.state)).size === 1 ? routines[0]?.state : 'not ok'}`] : []),
    ...(gaps.some(([, cv]) => cv.status === 'couldnt_check') ? [`couldn't check ${gaps.filter(([, cv]) => cv.status === 'couldnt_check').map(([s]) => s).join(', ')}`] : []),
    ...(gaps.some(([, cv]) => cv.status === 'partial') ? [`partly checked ${gaps.filter(([, cv]) => cv.status === 'partial').map(([s]) => s).join(', ')}`] : []),
  ]
  const prompts = suggestedPrompts(doc, top, nowMs)
  const hint = doc.swap_hint
  const setBy = top[0]?.set_by === 'manager' ? 'you' : 'agent'
  const setAt = top[0]?.set_at ? ` at ${at(top[0].set_at)}` : ''
  const onYou = items.filter(i => i.section === 'on_you')
  const decisions = onYou.filter(i => verbOf(i) === 'decide')
  const neglected = items.filter(i => i.section === 'neglected')
  const suggestions = items.filter(i => i.section === 'suggestion')
  const noItems = onYou.length + neglected.length + suggestions.length === 0
  const teams = [...new Set(neglected.map(i => i.team ?? ''))]
    .map(t => [t, neglected.filter(i => (i.team ?? '') === t)] as [string, NowItem[]])
    .sort((a, b) => b[1].length - a[1].length)
  const updates = (hub.hub?.updates ?? []).filter(u => u.state === 'open')

  return (
    <Box flexDirection="column">
      <Box>
        <Text weight="semibold">{`Now · ${at(doc.generated_at)} · next ${at(doc.next_at)}`}</Text>
      </Box>
      {error ? <Text color="warning">{`Couldn't read now.json — showing the ${at(doc.generated_at)} list.`}</Text> : null}
      {health.length > 0
        ? <Button key="health-open" plain onPress={() => inspect({ kind: 'system' })}><Text color="warning">{'⚠ ' + health.join(' · ') + ' ›'}</Text></Button>
        : null}
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
      {prompts.length > 0 && !(noItems && top.length === 0 && health.length === 0) ? (
        <Box key="prompts" flexDirection="column" marginTop={1}>
          <Box flexWrap="wrap" gap="xs">
            {prompts.map((p, n) => <Button key={'prompt-' + p.id} variant={n === 0 ? 'primary' : undefined} onPress={() => fill(p.text)}>{p.label}</Button>)}
          </Box>
        </Box>
      ) : null}
      {decisions.length > 0 ? (
        <Box key="g-decide" flexDirection="column">
          {heading('Decide', decisions.length)}
          <Box>
            <Button key="decide-open" plain onPress={() => inspect({ kind: 'decisions' })}>{`${decisions.length} decision${decisions.length === 1 ? '' : 's'} waiting ›`}</Button>
            <Text dimColor>{' ' + tally(decisions.map(i => teamName(i.team)))}</Text>
          </Box>
        </Box>
      ) : null}
      {GROUPS.map(([verb, name]) => group(name, verb, onYou.filter(i => verbOf(i) === verb), LIMITS[verb]))}
      {updates.length > 0 ? (
        <Box key="g-updates" flexDirection="column">
          {heading('From the hub', updates.length)}
          {updates.map(u => (
            <Box key={'upd-' + u.id}>
              <Button key={'upd-row-' + u.id} plain onPress={() => inspect({ kind: 'update', id: u.id })}>{u.title}</Button>
              <Text dimColor>{` ${ago(u.at, nowMs)} `}</Text>
              {isUrl(u.link) ? <Link href={u.link}>↗</Link> : null}
            </Box>
          ))}
        </Box>
      ) : null}
      {sessionsNode}
      {group('Suggestions', 'suggestion', suggestions, LIMITS.suggestion)}
      {teams.length > 0 ? (
        <Box key="g-neglected" flexDirection="column">
          {heading('Neglected by teams', neglected.length)}
          {teams.map(([t, list]) => (
            <Box key={'team-' + t}>
              <Button key={'team-row-' + t} plain onPress={() => inspect({ kind: 'team', id: t })}>{`${teamName(t)} ${list.length}`}</Button>
              <Text dimColor>{' ' + tally(list.map(i => signalText(i.signal)))}</Text>
            </Box>
          ))}
        </Box>
      ) : null}
      {noItems && health.length === 0 && top.length === 0
        ? <Text>{`Nothing on you. Next check ${at(doc.next_at)}.`}</Text> : null}
      {homeFooter($, ui, c)}
    </Box>
  )
}

// Home's Sessions group: the live sessions the hub feeds (news first, three of them), the proposals waiting to be
// started, and View all. A hub that is not alive says so here with Restart hub, since nothing routes until it is.
function sessionsGroup($: Dollar, ui: Ui, c: Ctx) {
  const { Box, Text, Button } = ui
  const { hub: hb, nowMs } = c
  const health = hubHealth(hb, nowMs)
  if (health === 'none') return null
  const inspect = (ref: InspectRef) => openInspector($, ref, 'reset')
  const sessions = sessionsByNews(hb.sessions)
  const pending = hb.hub?.pending ?? []
  const label = health === 'alive' ? '●' : health === 'stale' ? '◌ stale' : '✗'
  const count = `${sessions.length} live` + (pending.length ? ` · ${pending.length} proposed` : '')
  return (
    <Box key="hub" flexDirection="column">
      <Box marginTop={1}>
        <Text weight="semibold" size="heading">{'Sessions'}<Text dimColor weight="regular">{` ${count} · hub ${label}`}</Text></Text>
      </Box>
      {health !== 'alive' ? (
        <Box key="hub-down">
          <Text color="warning">{health === 'stale' ? 'The hub is behind; updates wait. ' : 'The hub is down; nothing routes. '}</Text>
          <Button key="hub-restart" onPress={() => { void $.prompt.fill({ text: '/mother:hub', mode: 'replace' }) }}>Restart hub</Button>
        </Box>
      ) : null}
      {sessions.slice(0, SESSIONS_ON_HOME).map(([key, v]) => (
        <Box key={'sess-' + key}>
          <Button key={'sess-row-' + key} plain onPress={() => inspect({ kind: 'session', id: key })}>{'◆ ' + v.title}</Button>
          {(v.unseen ?? 0) > 0 ? <Text color="warning">{` ${v.unseen} new`}</Text> : null}
          <Text dimColor>{ago(v.last_routed_at, nowMs) ? ' ' + ago(v.last_routed_at, nowMs) : ''}</Text>
        </Box>
      ))}
      {pending.map(p => (
        <Box key={'pend-' + p.key}>
          <Button key={'pend-row-' + p.key} plain dimColor onPress={() => inspect({ kind: 'proposal', id: p.key })}>{'◇ Proposed: ' + p.title}</Button>
          <Text dimColor>{' ' + ago(p.proposed_at, nowMs)}</Text>
        </Box>
      ))}
      <Button key="sessions-all" plain dimColor onPress={() => inspect({ kind: 'sessions' })}>{sessions.length > SESSIONS_ON_HOME ? `View all ${sessions.length} ›` : 'All sessions ›'}</Button>
    </Box>
  )
}

// One line at the foot of Home: the way to the Mother page (sources, routines, hub, what was routed, what is ignored).
function homeFooter($: Dollar, ui: Ui, c: Ctx) {
  const { Box, Button } = ui
  const gaps = Object.values(c.doc?.coverage ?? {}).filter(cv => cv && cv.status !== 'ok').length
  const health = hubHealth(c.hub, c.nowMs)
  const hubPart = health === 'none' ? '' : health === 'alive' ? ' · hub ●' : health === 'stale' ? ' · hub ◌' : ' · hub ✗'
  const m = c.hub.watch?.mute
  const ignored = (m?.keys?.length ?? 0) + (m?.people?.length ?? 0) + (m?.channels?.length ?? 0) + (m?.topics?.length ?? 0)
  return (
    <Box key="home-footer" marginTop={1}>
      <Button key="home-system" plain dimColor onPress={() => openInspector($, { kind: 'system' }, 'reset')}>
        {`Mother${gaps ? ` · ${gaps} can't check` : ''}${hubPart}${ignored ? ` · ${ignored} ignored` : ''} ›`}
      </Button>
    </Box>
  )
}

// The Inspector pane: the page on top of the stack, under a Back line. Every action fills the prompt (the manager
// presses Enter) or runs the same `mother now|hub|inbox` verbs Home ran before.
function inspectorTree($: Dollar, ui: Ui, c: Ctx, stack: InspectRef[]) {
  const { Box, Text, Button, Link, Select } = ui
  const { doc, hub, picking, expanded, nowMs, go, goHub, goInbox } = c
  const ref = stack[stack.length - 1]
  if (!ref) return <Box flexDirection="column"><Text dimColor>Pick a row in the Now panel to see it here.</Text></Box>
  const at = (iso: string | undefined | null) => clock(iso, doc?.timezone)
  const fill = (text: string) => { void $.prompt.fill({ text, mode: 'replace' }) }
  const nav = (r: InspectRef) => openInspector($, r, 'push')
  const replace = (r: InspectRef) => openInspector($, r, 'replace')
  const toggle = toggler($)
  const pick = (key: string) => update($, snoozingAtom, cur => (cur === key ? null : key))
  const items = doc?.items ?? []
  const heading = (text: string, count?: number) => (
    <Box key={'ih-' + text} marginTop={1}>
      <Text weight="semibold">{text}{count !== undefined ? <Text dimColor weight="regular">{` ${count}`}</Text> : null}</Text>
    </Box>
  )
  const gone = (what: string) => <Text key="insp-gone" dimColor wrap="wrap">{`This ${what} is no longer on the list.`}</Text>
  const canLink = (l: string) => c.surface === 'terminal' || /^https?:\/\//.test(l)
  const top = todaysTop(doc, nowMs)
  const itemRow = (i: NowItem, key: string, onPress: () => unknown = () => nav({ kind: 'item', id: i.id })) => (
    <Box key={key}>
      <Button key={key + '-b'} plain onPress={onPress}>{'▸ ' + i.title}</Button>
      <Text dimColor>{' ' + age(i.since, nowMs)}</Text>
    </Box>
  )

  const navLine = (
    <Box key="insp-nav">
      {stack.length > 1 ? <Button key="insp-back" plain onPress={() => inspectorBack($)}>← Back</Button> : null}
      <Text dimColor>{(stack.length > 1 ? ' ' : '') + stack.slice(-3).map(inspectTitle).join(' › ')}</Text>
    </Box>
  )

  const itemPage = (i: NowItem) => {
    const inTop = top.some(e => e.item_id === i.id)
    const rank = lowestOpenRank(top)
    const sess = sessionForItem(hub.sessions, i)
    const moves = [...(i.actions ?? []).map(a => ({ label: a.label || a.id, text: a.prompt || `mother now do ${a.id}` })),
                   ...quickActions(i).map(q => ({ label: q.label, text: q.text }))]
    const [primary, ...rest] = moves
    const more = expanded.has('insp-more')
    const mine = new Set([i.link, ...(i.also ?? [])])
    const related = items.filter(x => x.id !== i.id && ((i.team && x.team === i.team) || [x.link, ...(x.also ?? [])].some(k => mine.has(k))))
      .slice(0, RELATED_MAX)
    const meta = [SECTION_NAME[i.section], i.team, signalText(i.signal)].filter(Boolean).join(' · ') + (inTop ? ' · ★ Top 3' : '')
    return (
      <Box key={'ip-' + i.id} flexDirection="column">
        <Text dimColor>{meta}</Text>
        <Text weight="semibold" size="heading" wrap="wrap">{i.title}</Text>
        {heading("Why it's here")}
        {i.why ? <Text wrap="wrap">{i.why}</Text> : null}
        <Text dimColor wrap="wrap">{`raised ${age(i.since, nowMs)} ago · first seen ${at(i.first_seen)} · checked ${at(i.last_seen)}`
          + (i.unverified_since ? ` · unverified since ${at(i.unverified_since)}` : '')}</Text>
        {heading('Next move')}
        <Box key="ip-moves" flexWrap="wrap" gap="xs">
          {primary ? <Button key="ip-primary" variant="primary" hotkey="x" onPress={() => fill(primary.text)}>{primary.label}</Button> : null}
          {rest.slice(0, 3).map((m, n) => <Button key={'ip-move' + n} onPress={() => fill(m.text)}>{m.label}</Button>)}
          {sess
            ? <Button key="ip-open-session" hotkey="a" onPress={() => openSession($, goHub, sess[0], sess[1])}>Open session</Button>
            : <Button key="ip-ask" hotkey="a" onPress={() => fill(askPrompt(i.title, i.why, i.link))}>Ask Mother</Button>}
          {rest.length > 3 ? <Button key="ip-more" plain dimColor onPress={() => toggle('insp-more')}>{more ? 'Fewer' : `${rest.length - 3} more`}</Button> : null}
        </Box>
        {more && rest.length > 3 ? (
          <Box key="ip-more-list" flexWrap="wrap" gap="xs">
            {rest.slice(3).map((m, n) => <Button key={'ip-extra' + n} onPress={() => fill(m.text)}>{m.label}</Button>)}
          </Box>
        ) : null}
        {heading('Linked')}
        <Box key="ip-linked" flexWrap="wrap" gap="xs">
          {linkChips(i.links, i.link, i.also).map((ch, n) => <Link key={'ip-chip-' + n} href={ch.url}>{ch.label}</Link>)}
          {sess
            ? <Button key="ip-session" onPress={() => nav({ kind: 'session', id: sess[0] })}>{'◆ ' + sess[1].title}</Button>
            : isUrl(i.link) ? <Button key="ip-tohub" onPress={() => goHub(['add', '--', i.link])}>Run in a session</Button> : null}
          {i.team ? <Button key="ip-team" onPress={() => nav({ kind: 'team', id: i.team as string })}>{'Team: ' + i.team}</Button> : null}
          {verbOf(i) === 'decide' ? <Button key="ip-decide" onPress={() => nav({ kind: 'decisions', id: i.id })}>Decision queue</Button> : null}
        </Box>
        {related.length > 0 ? heading('Related', related.length) : null}
        {related.map(r => itemRow(r, 'ip-rel-' + r.id))}
        <Box key="ip-life" marginTop={1} flexWrap="wrap" gap="xs">
          <Button key="ip-done" hotkey="d" onPress={() => go(['done', i.id])}>Done</Button>
          {isMail(i) ? <Button key="ip-archive" onPress={async () => { if (await goInbox(['archive', '--', i.link])) await go(['done', i.id]) }}>Archive</Button> : null}
          {isMail(i) ? <Button key="ip-read" onPress={() => goInbox(['read', '--', i.link])}>Mark read</Button> : null}
          <Button key="ip-snooze" hotkey="s" onPress={() => pick('fp-snooze')}>Snooze</Button>
          {isUrl(i.link) ? <Button key="ip-ignore" onPress={() => pick('fp-ignore')}>Ignore…</Button> : null}
          {rank !== null && !inTop ? <Button key="ip-maketop" hotkey="t" onPress={() => go(['top', 'swap', String(rank), i.id])}>Make top 3</Button> : null}
          <Button key="ip-tell" onPress={() => fill(`About "${i.title}"` + (i.link ? ` (${i.link})` : '') + ': ')}>Tell Mother…</Button>
        </Box>
        {picking === 'fp-snooze'
          ? <Select key="fp-snooze-pick" label="Snooze for" options={SNOOZES.map(value => ({ value }))} onSelect={value => go(['snooze', i.id, value], 'fp-snooze')} />
          : null}
        {picking === 'fp-ignore' && isUrl(i.link)
          ? <Select key="fp-ignore-pick" label="Ignore" options={ignoreOptions(i.id.startsWith('chat:'), i.from, i.similar)}
              onSelect={async value => { if (await goHub(ignoreArgs(value, i.link, i.from, i.similar), 'fp-ignore')) await go(['done', i.id]) }} />
          : null}
      </Box>
    )
  }

  const updatePage = (u: HubUpdate) => {
    const sess = liveSessions(hub.sessions).find(([k]) => k === u.key)
    return (
      <Box key={'iu-' + u.id} flexDirection="column">
        <Text dimColor>{`Hub update · ${u.key} · ${ago(u.at, nowMs)} ago`}</Text>
        <Text weight="semibold" size="heading" wrap="wrap">{u.title}</Text>
        {u.why ? <Text wrap="wrap">{u.why}</Text> : null}
        {isUrl(u.link) ? <Link href={u.link}>{chipLabel(u.link, 'Open source')}</Link> : null}
        {sess ? <Button key="iu-session" onPress={() => nav({ kind: 'session', id: sess[0] })}>{'◆ ' + sess[1].title}</Button> : null}
        <Box key="iu-life" marginTop={1} flexWrap="wrap" gap="xs">
          <Button key={'hub-done-' + u.id} hotkey="d" onPress={() => goHub(['done', u.id], 'hub:' + u.id)}>Done</Button>
          {/^mail:/.test(u.key)
            ? <Button key={'hub-archive-' + u.id} onPress={async () => { if (await goInbox(['archive', '--', u.key])) await goHub(['done', u.id], 'hub:' + u.id) }}>Archive</Button>
            : null}
          {followArgs(u.key) ? <Button key={'hub-follow-' + u.id} onPress={() => goHub(followArgs(u.key) as string[])}>Follow</Button> : null}
          <Button key={'hub-mute-' + u.id} onPress={() => pick('ignore:hub:' + u.id)}>Ignore…</Button>
          <Button key={'hub-snooze-' + u.id} hotkey="s" onPress={() => pick('hub:' + u.id)}>Snooze</Button>
          <Button key="iu-ask" onPress={() => fill(askPrompt(u.title, u.why, u.link))}>Ask Mother</Button>
        </Box>
        {picking === 'ignore:hub:' + u.id
          ? <Select key={'hub-ignore-pick-' + u.id} label="Ignore" options={ignoreOptions(THREAD_KEY.test(u.key))}
              onSelect={async value => { if (await goHub(ignoreArgs(value, u.key), 'ignore:hub:' + u.id)) await goHub(['done', u.id], 'hub:' + u.id) }} />
          : null}
        {picking === 'hub:' + u.id
          ? <Select key={'hub-snooze-pick-' + u.id} label="Snooze for" options={SNOOZES.map(value => ({ value }))}
              onSelect={value => goHub(['snooze', u.id, value], 'hub:' + u.id)} />
          : null}
      </Box>
    )
  }

  const sessionPage = (key: string, v: HubSession) => {
    const routed = (hub.hub?.routed ?? []).filter(r => r.key === key || (r.session_id && r.session_id === v.session_id)).slice(-ROUTED_SHOWN).reverse()
    const openUpdates = (hub.hub?.updates ?? []).filter(u => u.state === 'open' && u.key === key)
    const linked = items.filter(i => sessionForItem(hub.sessions, i)?.[0] === key)
    const live = v.state === 'live'
    return (
      <Box key={'is-' + key} flexDirection="column">
        <Text dimColor>{`Session · ${live ? 'live' : 'archived'}${v.opened_at ? ` · opened ${ago(v.opened_at, nowMs)} ago` : ''}`}</Text>
        <Text weight="semibold" size="heading" wrap="wrap">{v.title}</Text>
        <Box>
          {v.link && canLink(v.link) ? <Link href={v.link}>{v.name || 'Open'}</Link> : <Text dimColor>{v.name}</Text>}
          <Text dimColor>{` · ${key}`}</Text>
        </Box>
        {heading('Next move')}
        <Box key="is-moves" flexWrap="wrap" gap="xs">
          {live ? <Button key={'sess-open-' + key} variant="primary" hotkey="x" onPress={() => openSession($, goHub, key, v)}>Open session</Button> : null}
          {live ? <Button key="is-note" onPress={() => fill(`Tell my "${v.title}" session: `)}>Send a note…</Button> : null}
          <Button key="is-ask" onPress={() => fill(`Where does my "${v.title}" session stand, and what does it need from me?`)}>Where does it stand?</Button>
        </Box>
        {heading('Routed updates', (v.unseen ?? 0) > 0 ? v.unseen ?? 0 : undefined)}
        {(v.unseen ?? 0) > 0 ? <Text color="warning">{`${v.unseen} new since you last opened it`}</Text> : null}
        {routed.length === 0 ? <Text dimColor>Nothing routed into it yet.</Text> : null}
        {routed.map(r => <Text key={'is-r-' + r.line_id} dimColor wrap="wrap">{`${ago(r.at, nowMs)} · ${r.decision}${r.note ? ' — ' + r.note : ''}`}</Text>)}
        {(v.unseen ?? 0) > 0 ? <Button key="is-seen" onPress={() => goHub(['session', 'seen', key])}>Mark seen</Button> : null}
        {openUpdates.map(u => itemRowUpdate(u))}
        {heading('Watching')}
        <Box key="is-watch" flexWrap="wrap" gap="xs">
          {linkChips(null, v.link && isUrl(v.link) ? v.link : null, v.also).map((ch, n) => <Link key={'is-chip-' + n} href={ch.url}>{ch.label}</Link>)}
          {followArgs(key) ? <Button key={'sess-follow-' + key} onPress={() => goHub(followArgs(key) as string[])}>Follow more</Button> : null}
        </Box>
        {linked.length > 0 ? heading('On the Now list', linked.length) : null}
        {linked.map(i => itemRow(i, 'is-item-' + i.id))}
        <Box key="is-life" marginTop={1} flexWrap="wrap" gap="xs">
          <Button key={'sess-mute-' + key} onPress={() => pick('ignore:sess:' + key)}>Ignore…</Button>
          {live ? <Button key="is-drop" onPress={() => goHub(['session', 'drop', key])}>Stop routing here</Button> : null}
        </Box>
        {picking === 'ignore:sess:' + key
          ? <Select key={'sess-ignore-pick-' + key} label="Ignore" options={ignoreOptions(THREAD_KEY.test(key))}
              onSelect={value => goHub(ignoreArgs(value, key), 'ignore:sess:' + key)} />
          : null}
      </Box>
    )
  }
  const itemRowUpdate = (u: HubUpdate) => (
    <Box key={'is-u-' + u.id}>
      <Button key={'is-u-b-' + u.id} plain onPress={() => nav({ kind: 'update', id: u.id })}>{'▸ ' + u.title}</Button>
      <Text dimColor>{' ' + ago(u.at, nowMs)}</Text>
    </Box>
  )

  const sessionsPage = () => {
    const live = sessionsByNews(hub.sessions)
    const fresh = live.filter(([, v]) => (v.unseen ?? 0) > 0)
    const quiet = live.filter(([, v]) => !((v.unseen ?? 0) > 0))
    const pending = hub.hub?.pending ?? []
    const dropped = Object.entries(hub.sessions?.sessions ?? {}).filter(([, v]) => v && v.state === 'dropped')
    const sessRow = ([key, v]: [string, HubSession]) => (
      <Box key={'il-' + key} flexDirection="column">
        <Box>
          <Button key={'il-b-' + key} plain onPress={() => nav({ kind: 'session', id: key })}>{'◆ ' + v.title}</Button>
          {(v.unseen ?? 0) > 0 ? <Text color="warning">{` ${v.unseen} new`}</Text> : null}
          <Text dimColor>{ago(v.last_routed_at, nowMs) ? ' ' + ago(v.last_routed_at, nowMs) : ''}</Text>
        </Box>
        <Text dimColor>{'  ' + [key, ...(v.also ?? []).map(a => chipLabel(a, a))].slice(0, 3).join(' · ')}</Text>
      </Box>
    )
    const showDropped = expanded.has('insp-dropped')
    return (
      <Box key="il" flexDirection="column">
        <Text dimColor>{hub.hub?.last_wake ? `hub woke ${ago(hub.hub.last_wake, nowMs)} ago` : 'hub never woke'}</Text>
        {live.length === 0 && pending.length === 0 ? <Text dimColor>No sessions yet: the hub opens one when something needs work.</Text> : null}
        {fresh.length > 0 ? heading('New updates', fresh.length) : null}
        {fresh.map(sessRow)}
        {quiet.length > 0 ? heading('Live', quiet.length) : null}
        {quiet.map(sessRow)}
        {pending.length > 0 ? heading('Proposed', pending.length) : null}
        {pending.map(p => (
          <Box key={'il-p-' + p.key}>
            <Button key={'il-p-b-' + p.key} plain onPress={() => nav({ kind: 'proposal', id: p.key })}>{'◇ ' + p.title}</Button>
            <Text dimColor>{' ' + ago(p.proposed_at, nowMs)}</Text>
          </Box>
        ))}
        {dropped.length > 0
          ? <Button key="il-dropped" plain dimColor onPress={() => toggle('insp-dropped')}>{`${showDropped ? '▾' : '▸'} Archived ${dropped.length}`}</Button>
          : null}
        {showDropped ? dropped.map(([key, v]) => (
          <Button key={'il-d-' + key} plain dimColor onPress={() => nav({ kind: 'session', id: key })}>{'  ' + v.title}</Button>
        )) : null}
        <Box key="il-life" marginTop={1} flexWrap="wrap" gap="xs">
          {fresh.length > 0 ? <Button key="il-seen-all" onPress={async () => { for (const [k] of fresh) await goHub(['session', 'seen', k]) }}>Mark all seen</Button> : null}
          <Button key="il-log" onPress={() => nav({ kind: 'system' })}>Routing log</Button>
        </Box>
      </Box>
    )
  }

  const proposalPage = (p: HubPending) => (
    <Box key={'ipp-' + p.key} flexDirection="column">
      <Text dimColor>{`Proposed session · ${ago(p.proposed_at, nowMs)} ago · ${p.raised_at ? 'card raised in the hub session' : 'card not raised yet'}`}</Text>
      <Text weight="semibold" size="heading" wrap="wrap">{p.title}</Text>
      {isUrl(p.link) ? <Link href={p.link}>{chipLabel(p.link, 'Open source')}</Link> : null}
      {p.read ? heading('What Mother read') : null}
      {p.read ? <Text wrap="wrap">{p.read}</Text> : null}
      {p.next_step ? heading('The session would') : null}
      {p.next_step ? <Text wrap="wrap">{p.next_step}</Text> : null}
      {(p.also ?? []).length > 0 ? (
        <Box key="ipp-chips" flexWrap="wrap" gap="xs">
          {linkChips(null, null, p.also).map((ch, n) => <Link key={'ipp-chip-' + n} href={ch.url}>{ch.label}</Link>)}
        </Box>
      ) : null}
      <Text dimColor wrap="wrap">{p.raised_at ? 'Start it from its card in the hub session.' : 'The hub raises its card on its next wake.'}</Text>
      <Box key="ipp-life" marginTop={1} flexWrap="wrap" gap="xs">
        <Button key="ipp-ask" onPress={() => fill(askPrompt(p.title, p.next_step, p.link))}>Ask Mother</Button>
        <Button key="ipp-drop" onPress={() => goHub(['pending', 'drop', p.key])}>Dismiss</Button>
      </Box>
    </Box>
  )

  const decisionsPage = () => {
    const all = items.filter(i => i.section === 'on_you' && verbOf(i) === 'decide')
    const list = ref.filter !== undefined ? all.filter(i => (i.team ?? '') === ref.filter) : all
    const cur = list.find(i => i.id === ref.id) ?? list[0]
    const teams = [...new Set(all.map(i => i.team ?? ''))]
    const idx = cur ? list.indexOf(cur) : -1
    const goTo = (i: NowItem | undefined) => (i ? replace({ kind: 'decisions', id: i.id, filter: ref.filter }) : undefined)
    return (
      <Box key="id" flexDirection="column">
        <Box key="id-teams" flexWrap="wrap" gap="xs">
          <Button key="id-all" plain dimColor={ref.filter !== undefined} onPress={() => replace({ kind: 'decisions' })}>{`All ${all.length}`}</Button>
          {teams.length > 1 ? teams.map(t => (
            <Button key={'id-team-' + t} plain dimColor={ref.filter !== t} onPress={() => replace({ kind: 'decisions', filter: t })}>{`${teamName(t)} ${all.filter(i => (i.team ?? '') === t).length}`}</Button>
          )) : null}
        </Box>
        {!cur ? <Text dimColor>No decisions waiting.</Text> : (
          <Box key={'id-card-' + cur.id} flexDirection="column">
            <Text dimColor>{`${idx + 1} of ${list.length} · ${teamName(cur.team)} · asked ${age(cur.since, nowMs)} ago`}</Text>
            <Text weight="semibold" size="heading" wrap="wrap">{cur.title}</Text>
            {cur.why ? <Text wrap="wrap">{cur.why}</Text> : null}
            {isUrl(cur.link) ? <Link href={cur.link}>{chipLabel(cur.link, 'Source')}</Link> : null}
            {heading('Options')}
            {(cur.actions ?? []).length === 0 ? <Text dimColor wrap="wrap">No options from the tick: ask Mother, or answer in the digest thread.</Text> : null}
            {(cur.actions ?? []).slice(0, 9).map((a, n) => (
              <Button key={'id-opt' + n} variant={n === 0 ? 'primary' : undefined} hotkey={String(n + 1)}
                onPress={() => fill(a.prompt || `mother now do ${a.id}`)}>{a.label || a.id}</Button>
            ))}
            <Box key="id-life" marginTop={1} flexWrap="wrap" gap="xs">
              <Button key="id-ask" hotkey="a" onPress={() => fill(askPrompt(cur.title, cur.why, cur.link))}>Ask Mother first</Button>
              {idx + 1 < list.length ? <Button key="id-skip" hotkey="n" onPress={() => goTo(list[idx + 1])}>Skip</Button> : null}
              <Button key="id-done" hotkey="d" onPress={() => go(['done', cur.id])}>Done</Button>
              <Button key="id-item" plain dimColor onPress={() => nav({ kind: 'item', id: cur.id })}>Details ›</Button>
            </Box>
            {list.length > idx + 1 ? heading('Up next', list.length - idx - 1) : null}
            {list.slice(idx + 1, idx + 6).map(i => itemRow(i, 'id-next-' + i.id, () => goTo(i)))}
          </Box>
        )}
      </Box>
    )
  }

  const teamPage = (t: string) => {
    const mine = items.filter(i => (i.team ?? '') === t)
    const onYou = mine.filter(i => i.section === 'on_you')
    const decisions = onYou.filter(i => verbOf(i) === 'decide')
    const others = onYou.filter(i => verbOf(i) !== 'decide')
    const neglected = mine.filter(i => i.section === 'neglected')
    const bySignal = [...new Set(neglected.map(i => i.signal))].map(s => [s, neglected.filter(i => i.signal === s)] as [string, NowItem[]])
    const name = teamName(t)
    return (
      <Box key={'it-' + t} flexDirection="column">
        <Text weight="semibold" size="heading">{name}</Text>
        <Text dimColor>{`${onYou.length} on you · ${neglected.length} neglected`}</Text>
        {decisions.length > 0 || others.length > 0 ? heading('Needs you', onYou.length) : null}
        {decisions.length > 0
          ? <Button key="it-decide" plain onPress={() => nav({ kind: 'decisions', filter: t })}>{`▸ ${decisions.length} decision${decisions.length === 1 ? '' : 's'} ›`}</Button>
          : null}
        {others.map(i => itemRow(i, 'it-on-' + i.id))}
        {neglected.length > 0 ? heading('Neglected work', neglected.length) : null}
        {bySignal.map(([s, list]) => (
          <Box key={'it-sig-' + s} flexDirection="column">
            <Text dimColor>{`${signalText(s)} · ${list.length}`}</Text>
            {list.map(i => itemRow(i, 'it-ng-' + i.id))}
          </Box>
        ))}
        <Box key="it-life" marginTop={1} flexWrap="wrap" gap="xs">
          {neglected.length > 0
            ? <Button key="it-triage" variant="primary" onPress={() => fill(`Go through the ${neglected.length} neglected ${name} items in the Now list: ${listed(neglected.map(i => i.title))}. For each, propose drop, delegate (name who) or make it mine. Change nothing until I say yes.`)}>{`Triage ${neglected.length} with Mother`}</Button>
            : null}
          <Button key="it-update" onPress={() => fill(`Draft a short ${name} team update from the Now list and the latest digest: what moved, what is stuck, what I need from them. Draft only.`)}>Draft team update</Button>
        </Box>
        <Text dimColor>Stuck work only: no per-person counts.</Text>
      </Box>
    )
  }

  const systemPage = () => {
    const gaps = Object.entries(doc?.coverage ?? {})
    const health = hubHealth(hub, nowMs)
    const sources = Object.entries(hub.watcher?.sources ?? {})
    const refreshing = picking === 'hub:refresh'
    const routed = (hub.hub?.routed ?? []).slice(-ROUTED_SHOWN).reverse()
    const fixes = doc ? suggestedPrompts(doc, top, nowMs).filter(p => p.id === 'recheck' || p.id === 'routines') : []
    const sessionKeys = new Map(liveSessions(hub.sessions))
    return (
      <Box key="isys" flexDirection="column">
        {heading('Now list')}
        {doc
          ? <Text dimColor>{`tick ${at(doc.generated_at)} · next ${at(doc.next_at)} · ${doc.status}`}</Text>
          : <Text dimColor>No tick here: it runs on the home machine.</Text>}
        {gaps.map(([s, cv]) => (
          <Text key={'isys-src-' + s} color={cv.status === 'ok' ? undefined : 'warning'} dimColor={cv.status === 'ok'} wrap="wrap">
            {`${s} ${cv.status === 'ok' ? '✓' : cv.status === 'partial' ? '~ partly checked' : "✗ couldn't check"}${cv.since && cv.status !== 'ok' ? ' since ' + at(cv.since) : ''}${cv.reason ? ' — ' + cv.reason : ''}`}
          </Text>
        ))}
        {(doc?.routines ?? []).map(r => <Text key={'routine-' + r.id} color="warning">{`${r.id} ${r.state} at ${at(r.expected)}`}</Text>)}
        {fixes.length > 0 ? (
          <Box key="isys-fixes" flexWrap="wrap" gap="xs">
            {fixes.map(p => <Button key={'isys-fix-' + p.id} onPress={() => fill(p.text)}>{p.label}</Button>)}
          </Box>
        ) : null}
        {health !== 'none' ? heading('Hub') : null}
        {health !== 'none' ? (
          <Box key="isys-hub" flexDirection="column">
            <Text color={health === 'alive' ? undefined : 'warning'} dimColor={health === 'alive'}>
              {hub.watcher ? `watcher polled ${ago(hub.watcher.at, nowMs)} ago${hub.watcher.active === false ? ' (idle hours)' : ''}` : 'watcher not running'}
            </Text>
            {sources.map(([cap, row]) => <Text key={'isys-hs-' + cap} dimColor>{sourceMark(cap, row, hub.watcher?.active, nowMs)}</Text>)}
            <Text dimColor>{hub.hub?.last_wake ? `hub woke ${ago(hub.hub.last_wake, nowMs)} ago` : 'hub never woke'}</Text>
            <Box flexWrap="wrap" gap="xs">
              <Button key="hub-refresh" dimColor={refreshing} onPress={() => { if (!refreshing) void refreshHub($, goHub) }}>{refreshing ? 'Refreshing…' : 'Refresh'}</Button>
              {health !== 'alive'
                ? <Button key="hub-restart" onPress={() => { void $.prompt.fill({ text: '/mother:hub', mode: 'replace' }) }}>Restart hub</Button>
                : null}
            </Box>
          </Box>
        ) : null}
        {routed.length > 0 ? heading('Recently routed', routed.length) : null}
        {routed.map(r => {
          const s = sessionKeys.get(r.key)
          const text = `${ago(r.at, nowMs)} · ${r.key} ${r.decision}${r.note ? ' — ' + r.note : ''}`
          return s
            ? <Button key={'routed-' + r.line_id} plain dimColor onPress={() => nav({ kind: 'session', id: r.key })}>{text}</Button>
            : <Text key={'routed-' + r.line_id} dimColor wrap="wrap">{text}</Text>
        })}
        {ignoredSection(ui, hub.watch, expanded, toggle, at, goHub)}
      </Box>
    )
  }

  let page
  if (ref.kind === 'item') { const i = items.find(x => x.id === ref.id); page = i ? itemPage(i) : gone('item') }
  else if (ref.kind === 'update') { const u = (hub.hub?.updates ?? []).find(x => x.id === ref.id && x.state === 'open'); page = u ? updatePage(u) : gone('update') }
  else if (ref.kind === 'session') { const v = hub.sessions?.sessions?.[ref.id ?? '']; page = v ? sessionPage(ref.id as string, v) : gone('session') }
  else if (ref.kind === 'sessions') page = sessionsPage()
  else if (ref.kind === 'proposal') { const p = (hub.hub?.pending ?? []).find(x => x.key === ref.id); page = p ? proposalPage(p) : gone('proposal') }
  else if (ref.kind === 'decisions') page = decisionsPage()
  else if (ref.kind === 'team') page = teamPage(ref.id ?? '')
  else page = systemPage()
  return (
    <Box flexDirection="column">
      {navLine}
      {page}
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

// `mother hub refresh`: restart the watcher, reconnect every source and poll it now. It can take a minute, so the
// button reads "Refreshing…" until it returns (run() clears the row marker), and the result is one toast.
async function refreshHub($: Dollar, goHub: (args: string[], rowId?: string | null) => Promise<boolean>): Promise<void> {
  await update($, snoozingAtom, () => 'hub:refresh')
  $.ui.toast('Refreshing the hub: reconnecting every source…')
  const ok = await goHub(['refresh'], 'hub:refresh')
  $.ui.toast(ok ? 'Hub refreshed: every source answered' : 'Hub refreshed: some sources still fail (see the hub line)')
}

// One context for both panes: the files as last read, the open picker and toggles, and the verbs. A hub verb that
// changes what is watched (add, follow, mute, unmute) toasts its first line.
async function makeCtx($: Dollar, surface: string, configured: string, loader: Loader): Promise<Ctx> {
  const bin = await motherBin($, configured)
  return {
    doc: await read($, docAtom), hub: (await read($, hubAtom)) ?? NO_HUB, picking: await read($, snoozingAtom),
    expanded: new Set((await read($, expandedAtom)) ?? []), nowMs: await $.clock.now(), surface,
    go: (args, rowId = null) => run($, bin, loader, args, rowId),
    goHub: (args, rowId = null) => run($, bin, loader, args, rowId, 'hub', ['add', 'follow', 'mute', 'unmute'].includes(args[0])),
    goInbox: (args, rowId = null) => run($, bin, loader, args, rowId, 'inbox', true),
  }
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

  // The Inspector; the Focus pane id of 0.10 draws it too, so a pane left open across the update keeps working.
  for (const id of [INSPECTOR, FOCUS]) {
    on('ui.render', { component: 'Pane', requestId: id }, async ($, e) =>
      inspectorTree($, $.ui.resolve(e), await makeCtx($, e.surface, configured, loader), (await read($, inspectAtom)) ?? []))
  }

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) =>
    homeTree($, $.ui.resolve(e), await makeCtx($, e.surface, configured, loader), await read($, errorAtom), await read($, selectedAtom)))
}
