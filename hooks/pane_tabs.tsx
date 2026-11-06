import { atom, read, update } from 'claude-code'
import type { Color } from 'claude-code'

import { SNOOZES, age, ago, askPrompt, isUrl, oneLine, safe, todaysTop } from './shared'
import type { Dollar } from './shared'
import { THREAD_KEY, followArgs, ignoreArgs, ignoreOptions, nowVerb, openInspector } from './register'
import { ACTIONS_SHOWN, ACTION_LABEL_MAX, SOURCE_COLOR, badge, card, emptyLine, isOpen, itemSource, leadPill, linkRows, linksOf, money, sectionHeading,
         leadBadge, sourceBadge, sourceLink, subHeading, titleRow, toggled, whyText } from './pane_ui'
import { SectionHeading, Stat, pillRow } from './skin'
import type { Ui } from './pane_ui'
import { inboxRow, logAct, nowRow, picked, quietedOf } from './habits'
import { mentionSearchText } from './mention_search'
import type { ActVerb } from './habits'
import type { ChiefDetail, ChiefHealth, ChiefHealthState, ChiefInboxItem, ChiefOpen, ChiefStatus, NowAction, NowDoc, NowItem } from '../types'

// The Chief pane's later tabs: Team (the tick's neglected items by team), Health (is everything working), the
// Suggested cards at the top of Today, and Follow / Ignore on an Inbox card. chief.tsx registers each with one row
// of its tab list and passes what it read; nothing here reads a file. Every write is a `mother …` verb: the Now
// panel's own (`nowVerb`, its hub-update helpers) or the Inbox's (`goHub`, which re-reads the status).
// (The atoms are declared again here: the engine's scan reads an atom from a const of the file that uses it.)
const draftAtom = atom({ plugin: 'mother', key: 'chiefDraft' } as const, null)
const openAtom = atom({ plugin: 'mother', key: 'chiefOpen' } as const, null)
const selectedAtom = atom({ plugin: 'mother', key: 'chiefSelected' } as const, null)
const healthAtom = atom({ plugin: 'mother', key: 'chiefHealth' } as const, null)
const detailAtom = atom({ plugin: 'mother', key: 'chiefDetail' } as const, null)
const docAtom = atom({ plugin: 'mother', key: 'doc' } as const, null)       // now.json as register.tsx last read it; only read here

// What chief.tsx hands each section: the open and selected cards, the picker flags (`chiefDraft`), the Health state.
export type Details = Record<string, ChiefDetail | null> | null   // by Now item id; null inside = nothing on disk has it
export type TabCtx = { open: ChiefOpen | null; selected: string | null; health?: ChiefHealthState | null; details?: Details; people?: Record<string, string> | null }
type Drafts = Record<string, string> | null
type GoHub = (args: string[]) => Promise<boolean>

const TITLE_MAX = 200

// (function declarations, not arrows: past an import the engine follows `$` only into a declared function)
function pressing($: Dollar, sel: string, run: () => unknown) {
  return async () => { await update($, selectedAtom, () => sel); await run() }
}
function toggleCard($: Dollar, id: string, nowOpen: boolean) {
  return update($, openAtom, prev => toggled(prev, id, nowOpen))
}

// What a Now item is from, as words: a chat user id becomes the person's name through the config's people (`mother
// chief context --json`); an id nobody is named for is left out, anything else is shown as it is.
const CHAT_ID_RE = /^[UW][A-Z0-9]{6,}$/
export function fromWords(from: unknown, people: Record<string, string> | null | undefined): string {
  const f = oneLine(from, 80)
  if (!f) return ''
  if (people && people[f]) return oneLine(people[f], 40)
  return CHAT_ID_RE.test(f) ? '' : f
}

// A tick signal as words: the known ones have their own, an unknown one loses its underscores.
const SIGNAL_WORDS: Record<string, string> = {
  suggestion_waiting: 'waiting on your yes', unanswered_mention: 'unanswered mention', mail_needs_reply: 'mail needs a reply',
  outside_question: 'question from outside', review_requested: 'review requested', pr_no_reviewer: 'PR with no reviewer',
  pr_changes_silent: 'PR changes went quiet', pr_approved_unmerged: 'approved, not merged', default_branch_red: 'default branch is red',
  decision_open: 'decision open',
}
export const signalWords = (signal: unknown): string => {
  const sig = oneLine(signal, 40)
  return sig ? SIGNAL_WORDS[sig] ?? sig.replace(/_/g, ' ') : ''
}
// `17h ago`, or `just now` for an age under an hour.
export const agoWords = (a: string): string => (a === '0h' || a === 'now' ? 'just now' : `${a} ago`)
// A flag in `chiefDraft` (an open picker, a group drawn in full): set by one press, cleared by the next.
function flip($: Dollar, key: string) {
  return update($, draftAtom, prev => {
    const next = { ...(prev ?? {}) }
    if (next[key] !== undefined) delete next[key]; else next[key] = '1'
    return next
  })
}
function fill($: Dollar, text: string) {
  return $.prompt.fill({ text, mode: 'replace' }).catch(() => undefined)
}

// ---- what an expanded Today / Team / Suggested card adds: everything the item carries ----
const DOTS_ID_RE = /^[AM]\d{1,3}$/
const DETAIL_TIMEOUT_MS = 30_000
const DETAIL_TEXT_MAX = 1500
const DETAIL_LINES_MAX = 30
export const TASK_SHOWN_MAX = 1500
export const DECISION_LABEL_MAX = 40

// The dots action (`A23`) or manager item (`M4`) a Now item stands for, from its actions' own words (the tick folds a
// dots action in as `dots do A23`, labelled `Run dots action A23`) or its title (`Dots A23: …`); null for any other.
export function dotsId(i: { title?: unknown; actions?: readonly Partial<NowAction>[] } | null | undefined): string | null {
  for (const a of i?.actions ?? []) {
    const m = /\bdots\s+(?:do\s+|action\s+)?([AM]\d{1,3})\b/i.exec(`${a?.task ?? ''} ${a?.prompt ?? ''} ${a?.label ?? ''}`)
    if (m) return m[1].toUpperCase()
  }
  const t = /^\s*dots\s+([AM]\d{1,3})\b/i.exec(String(i?.title ?? ''))
  return t ? t[1].toUpperCase() : null
}

// A pressed card loads what its dots action says, once (`mother chief dots-detail <id> --json`): never a draw. A
// failed or empty answer is kept as null, which the card says.
export async function loadDetail($: Dollar, bin: string, i: NowItem | null | undefined, loaded: Details | undefined): Promise<void> {
  const id = dotsId(i)
  if (!i || !id || !DOTS_ID_RE.test(id) || (loaded && i.id in loaded)) return
  let got: ChiefDetail | null = null
  try {
    const ran = await $.process.run(['python3', bin, 'chief', 'dots-detail', id, '--json'], { timeoutMs: DETAIL_TIMEOUT_MS })
    const d = ran.exitCode === 0 ? JSON.parse(String(ran.stdout ?? 'null')) : null
    if (d && typeof d === 'object' && typeof d.text === 'string' && d.text.trim()) {
      got = { id, text: safe(d.text).slice(0, DETAIL_TEXT_MAX), date: oneLine(d.date, 10), source_file: oneLine(d.source_file, 120),
              label: typeof d.label === 'string' ? oneLine(d.label, DECISION_LABEL_MAX) || null : null }
    }
  } catch { /* said on the card */ }
  await update($, detailAtom, prev => ({ ...(prev ?? {}), [i.id]: got }))
}

// A yes/no decision's primary button says what a yes does, once the detail gave a short verb phrase ("Annotate 7
// todos" in place of "Run dots action A23"); any other button keeps its label.
export function decisionLabel(a: Partial<NowAction>, n: number, detail: ChiefDetail | null | undefined, cap: number): string {
  const label = oneLine(a.label, cap)
  return n === 0 && detail?.label && /^run\b/i.test(label) ? oneLine(detail.label, DECISION_LABEL_MAX) : label
}

// Under an expanded card's buttons, one dim line per action: its label and the instruction it runs, in the routine's
// own words. Never the verb the button types (`mother now do S1`): the manager reads what happens, not how. A dots
// action's line says nothing either: "What this does" above it is the detail.
const VERB_RE = /^(mother\s+now\s+do\s+\S+|dots\s+(?:do\s+|action\s+)?[AM]\d{1,3})\s*$/i
export function actionSays(ui: Ui, id: string, acts: readonly NowAction[], detail: ChiefDetail | null | undefined, cap: number) {
  const { Text } = ui
  return acts.map((a, n) => {
    const task = oneLine(a.task || a.prompt, TASK_SHOWN_MAX)
    if (!task || VERB_RE.test(task)) return null
    return <Text key={`says:${id}:${n}`} dimColor wrap="wrap">{`${decisionLabel(a, n, detail, cap)}: ${task}`}</Text>
  })
}

// The detail of every open card that stands for a dots action, loaded once per item: on the console pass and when
// a card opens (`/chief expand`, a restored toggle), never in a draw. A press on a card loads its own as before.
export async function loadOpenDetails($: Dollar, bin: string): Promise<void> {
  const doc = await read($, docAtom)
  if (!doc) return
  const open = await read($, openAtom)
  const selected = await read($, selectedAtom)
  const nowMs = await $.clock.now()
  const items = (doc.items ?? []).filter(i => !!i)
  const byId = new Map(items.map(i => [i.id, i]))
  const cards: [string, NowItem | undefined][] = [
    ...todaysTop(doc, nowMs).filter(e => e && e.state !== 'done').map(e => ['today:top:' + e.rank, e.item_id ? byId.get(e.item_id) : undefined] as [string, NowItem | undefined]),
    ...items.map(i => [`${i.section === 'neglected' ? 'team' : i.section === 'suggestion' ? 'sug' : 'today'}:${i.id}`, i] as [string, NowItem | undefined]),
  ]
  for (const [id, i] of cards) {
    if (!i || !dotsId(i) || !isOpen(open, id, 'today', selected)) continue
    const loaded = await read($, detailAtom)
    if (loaded && i.id in loaded) continue
    await loadDetail($, bin, i, loaded)
  }
}

// The rest an item carries, for an expanded card: its signal, sender and ages; every labelled link (or `no source
// link`); its other keys; the "similar" rule; and for a dots item what the action does, as the dots file words it.
export function itemDetail(ui: Ui, id: string, i: NowItem, nowMs: number, details: Details | undefined, people?: Record<string, string> | null) {
  const { Box, Text } = ui
  const from = fromWords(i.from, people)
  const raised = age(i.since, nowMs)
  const first = i.first_seen ? age(i.first_seen, nowMs) : ''
  const facts = [
    signalWords(i.signal), from ? `from ${from}` : '', i.team ? `team ${oneLine(i.team, 40)}` : '',
    raised ? `raised ${agoWords(raised)}` : '', first && first !== raised ? `first seen ${agoWords(first)}` : '',
  ].filter(Boolean).join(' · ')
  const also = (Array.isArray(i.also) ? i.also : []).map(a => oneLine(a, 80)).filter(Boolean).slice(0, 8)
  const words = Array.isArray(i.similar?.words) ? i.similar!.words.map(w => oneLine(w, 30)).filter(Boolean) : []
  const dots = dotsId(i)
  const detail = details?.[i.id]
  return [
    facts ? <Text key={'facts:' + id} dimColor wrap="wrap">{facts}</Text> : null,
    linkRows(ui, id, linksOf(i.link, i.links, itemSource(i.id))),
    also.length ? <Text key={'also:' + id} dimColor wrap="wrap">{`also: ${also.join(', ')}`}</Text> : null,
    words.length ? <Text key={'similar:' + id} dimColor wrap="wrap">{`similar: ${words.join(', ')}${i.similar?.channel ? ` in ${oneLine(i.similar.channel, 40)}` : ''}`}</Text> : null,
    dots && detail
      ? <Box key={'detail:' + id} flexDirection="column">
          <Text key={'detail-head:' + id} weight="semibold" size="footnote">What this does</Text>
          {detail.text.split('\n').slice(0, DETAIL_LINES_MAX).map((line, n) => <Text key={`detail:${id}:${n}`} wrap="wrap">{line || ' '}</Text>)}
          <Text key={'detail-from:' + id} dimColor size="caption">{`${dots} · ${detail.source_file}${detail.date ? ` · ${detail.date}` : ''}`}</Text>
        </Box>
      : null,
    dots && detail === null ? <Text key={'detail-none:' + id} dimColor>{`What this does: no dots output on disk has ${dots}`}</Text> : null,
  ]
}

// ---- a Now item as a card: Team's and Suggested's, with the actions the Now panel's item page offers ----
// Compact, two rows as every Chief card: the title with its primary button (the tick's first action, else Ask Mother)
// and Done at the right end; the badge, age, source link. Expanded adds the whole why, Ask Mother, Open (the item's
// Inspector page), Snooze and Ignore… with the Now panel's own choices. The text is tick-written from
// other people's words: one cleaned line each, drawn as plain Text.
function nowCard($: Dollar, ui: Ui, bin: string, i: NowItem, nowMs: number, drafts: Drafts, ctx: TabCtx, kind: 'team' | 'sug') {
  const { Box, Text, Button, Select } = ui
  const id = `${kind}:${i.id}`
  const open = isOpen(ctx.open, id, 'today', ctx.selected)
  // a press selects the card, which opens it: what its dots action does is loaded then, once
  const press = (run: () => unknown) => pressing($, id, async () => { await run(); await loadDetail($, bin, i, ctx.details) })
  const go = (args: string[]) => nowVerb($, bin, args)
  const source = itemSource(i.id)
  const title = oneLine(i.title, TITLE_MAX)
  const why = whyText(i.why)
  const link = isUrl(i.link) ? i.link : null
  const detail = ctx.details?.[i.id]
  const acts = (i.actions ?? []).slice(0, ACTIONS_SHOWN)   // the tick's actions, the first primary; run as the Now panel runs them
  const action = acts[0]
  const snoozeKey = `${kind}-snooze:${i.id}`
  const ignoreKey = `${kind}-ignore:${i.id}`
  const label = kind === 'sug' ? 'suggested' : source === 'other' ? 'neglected' : source
  const note = (verb: ActVerb, more = {}) => logAct($, bin, verb, nowRow(i, source), more)   // what the manager did, for habits
  const doIt = action
    ? acts.map((a, n) => <Button key={n === 0 ? `${kind}-doit:${i.id}` : `${kind}-act:${i.id}:${n}`} variant={n === 0 ? 'primary' : 'secondary'}
        onPress={press(() => { note('action', picked(a.label, n)); return fill($, a.prompt || `mother now do ${a.id}`) })}>{decisionLabel(a, n, detail, n === 0 ? 40 : ACTION_LABEL_MAX) || 'Do it'}</Button>)
    : null
  const ask = <Button key={`${kind}-ask:${i.id}`} variant={action ? 'secondary' : 'primary'} onPress={press(() => { note('ask'); return fill($, askPrompt(title, why, i.link)) })}>Ask Mother</Button>
  const done = <Button key={`${kind}-done:${i.id}`} onPress={press(() => { note('done'); return go(['done', i.id]) })}>Done</Button>
  return card(ui, { id, color: SOURCE_COLOR[source], selected: ctx.selected === id }, [
    ...(titleRow(ui, id, title, open, press(() => toggleCard($, id, !open)), null, open ? undefined : [doIt ?? ask, done], (tail: unknown) => (
    <Box key={'meta:' + id} {...pillRow(ui)}>
      {sourceBadge(ui, source, label, 'badge:' + id)}
      <Text dimColor>{age(i.since, nowMs) + ' '}</Text>
      {open ? null : sourceLink(ui, id, linksOf(i.link, i.links, source))}
    {tail as any}</Box>)) as unknown[]),
    open && why ? <Text key={'why:' + id} wrap="wrap">{why}</Text> : null,
    ...(open ? itemDetail(ui, id, i, nowMs, ctx.details, ctx.people) : []),
    ...(open ? actionSays(ui, id, acts, detail, 40) : []),
    open
      ? <Box key={'acts:' + id} {...pillRow(ui, true)}>
          {doIt}
          {ask}
          {done}
          <Button key={`${kind}-open:${i.id}`} onPress={press(() => openInspector($, { kind: 'item', id: i.id }, 'reset'))}>Open</Button>
          <Button key={snoozeKey} onPress={press(() => flip($, snoozeKey))}>Snooze</Button>
          {link ? <Button key={ignoreKey} onPress={press(() => flip($, ignoreKey))}>Ignore…</Button> : null}
        </Box>
      : null,
    open && drafts?.[snoozeKey] !== undefined
      ? <Select key={`${kind}-snooze-pick:${i.id}`} label="Snooze for" options={SNOOZES.map(value => ({ value }))}
          onSelect={(value: string) => flip($, snoozeKey).then(() => { note('snooze'); return go(['snooze', i.id, value]) })} />
      : null,
    open && link && drafts?.[ignoreKey] !== undefined
      ? <Select key={`${kind}-ignore-pick:${i.id}`} label="Ignore" options={ignoreOptions(i.id.startsWith('chat:'), i.from, i.similar)}
          onSelect={async (value: string) => {
            await flip($, ignoreKey)
            // as the Now panel's item page: mute through the hub, then the item is done
            if (await hubVerb($, bin, ignoreArgs(value, link, i.from, i.similar))) { note('ignore'); await go(['done', i.id]) }
          }} />
      : null,
  ])
}

// `mother hub …` for a Now item (its Ignore): the first line of what it says is toasted, as the Now panel does.
async function hubVerb($: Dollar, bin: string, args: string[]): Promise<boolean> {
  try {
    const ran = await $.process.run(['python3', bin, 'hub', ...args])
    const line = String((ran.exitCode === 0 ? ran.stdout : ran.stderr) ?? '').split('\n').find(l => l.trim())
    if (line) $.ui.toast(line.trim())
    else if (ran.exitCode !== 0) $.ui.toast(`mother hub ${args[0]} failed`)
    return ran.exitCode === 0
  } catch (err) {
    $.ui.toast(`mother hub ${args[0]} failed: ${(err as Error).message ?? String(err)}`)
    return false
  }
}

// ---- Team: the neglected items, a group per team ----
export const TEAM_SHOWN = 7
export const NO_TEAM = 'Other'

// The neglected items by team, largest group first, the items with no team last under "Other".
export function teamGroups(doc: NowDoc | null | undefined): [string, NowItem[]][] {
  const items = (doc?.items ?? []).filter(i => i && i.section === 'neglected')
  const names = [...new Set(items.map(i => oneLine(i.team, 40) || NO_TEAM))]
  return names.map(t => [t, items.filter(i => (oneLine(i.team, 40) || NO_TEAM) === t)] as [string, NowItem[]])
    .sort((a, b) => (a[0] === NO_TEAM ? 1 : 0) - (b[0] === NO_TEAM ? 1 : 0) || b[1].length - a[1].length)
}
export const teamCount = (doc: NowDoc | null | undefined): number => teamGroups(doc).reduce((n, [, list]) => n + list.length, 0)

export function teamSection($: Dollar, ui: Ui, bin: string, doc: NowDoc | null | undefined, nowMs: number, drafts: Drafts, ctx: TabCtx, heading: boolean) {
  const { Box, Text, Button } = ui
  const groups = teamGroups(doc)
  if (!groups.length) return [emptyLine(ui, 'team-empty', doc ? 'Nothing neglected in the teams' : 'No team list yet: the tick has not written now.json')]
  const out: unknown[] = []
  if (heading) out.push(sectionHeading(ui, 'team-heading', `Team ${teamCount(doc)}`))
  for (const [t, list] of groups) {
    const moreKey = 'team-more:' + t
    const all = drafts?.[moreKey] !== undefined
    out.push(
      <Box key={'team-group:' + t} flexDirection="column">
        {SectionHeading(ui, { key: 'team-name:' + t, text: t, count: list.length, level: 'group' })}
        {(all ? list : list.slice(0, TEAM_SHOWN)).map(i => nowCard($, ui, bin, i, nowMs, drafts, ctx, 'team'))}
        {list.length > TEAM_SHOWN
          ? <Button key={moreKey} plain dimColor onPress={() => flip($, moreKey)}>{all ? 'Show fewer' : `Show ${list.length - TEAM_SHOWN} more`}</Button>
          : null}
      </Box>)
  }
  return out
}

// ---- Suggested: the tick's suggestions, at the top of Today ----
export const SUGGESTED_SHOWN = 3
export const suggestedRows = (doc: NowDoc | null | undefined): NowItem[] => (doc?.items ?? []).filter(i => i && i.section === 'suggestion')

export function suggestedSection($: Dollar, ui: Ui, bin: string, doc: NowDoc | null | undefined, nowMs: number, drafts: Drafts, ctx: TabCtx) {
  const { Button } = ui
  const list = suggestedRows(doc)
  if (!list.length) return []
  const all = drafts?.['sug-more'] !== undefined
  return [
    subHeading(ui, 'suggested-heading', 'Suggested'),
    ...(all ? list : list.slice(0, SUGGESTED_SHOWN)).map(i => nowCard($, ui, bin, i, nowMs, drafts, ctx, 'sug')),
    list.length > SUGGESTED_SHOWN
      ? <Button key="sug-more" plain dimColor onPress={() => flip($, 'sug-more')}>{all ? 'Show fewer' : `Show ${list.length - SUGGESTED_SHOWN} more`}</Button>
      : null,
  ]
}

// ---- Follow and Ignore on an expanded Inbox card: the Now panel's hub-update actions ----
// Follow watches what the key belongs to (a tracker project, a repo, a chat thread, a group); a key with nothing to
// follow draws no button. Ignore… opens the Now panel's picker (just this, for a week, a thread's whole channel):
// `mother hub mute …`, then the item is done, so its card is gone on the next status read.
export function inboxFollowIgnore($: Dollar, ui: Ui, u: ChiefInboxItem, goHub: GoHub, press: (run: () => unknown) => () => Promise<void>,
                                  note?: (verb: ActVerb) => void) {
  const { Button } = ui
  const follow = followArgs(u.key)
  return [
    follow
      ? <Button key={'inbox-follow:' + u.id} onPress={press(async () => { if (await goHub(follow)) { note?.('follow'); $.ui.toast(`Following ${follow[follow.length - 1]}`) } })}>Follow</Button>
      : null,
    <Button key={'inbox-ignore:' + u.id} onPress={press(() => flip($, 'inbox-ignore:' + u.id))}>Ignore…</Button>,
  ]
}

export function inboxIgnorePick($: Dollar, ui: Ui, u: ChiefInboxItem, drafts: Drafts, goHub: GoHub, note?: (verb: ActVerb) => void) {
  const { Select } = ui
  const key = 'inbox-ignore:' + u.id
  if (drafts?.[key] === undefined) return null
  return <Select key={'inbox-ignore-pick:' + u.id} label="Ignore" options={ignoreOptions(THREAD_KEY.test(u.key))}
    onSelect={async (value: string) => { await flip($, key); if (await goHub(ignoreArgs(value, u.key))) { note?.('ignore'); await goHub(['done', u.id]) } }} />
}

// ---- Health: is everything working ----
export const SOURCES = ['workspace', 'chat', 'tracker', 'code', 'mail', 'calendar'] as const
export const REPORT_RE = /^[a-z0-9-]+-unsent-\d{4}-\d{2}-\d{2}\.md$/
export const DISMISSED_MAX = 50
export const DISMISSED_KEY = 'chiefDismissed'
const REPORT_LINES_MAX = 200
const REPORT_TIMEOUT_MS = 30_000

// An Inbox item about a source the watcher could not read: not work, so it is Health's, never the Inbox's.
export const isWatcherError = (u: { key?: unknown } | null | undefined): boolean => /^watcher:/.test(String(u?.key ?? ''))

const statusColor = (status: unknown): Color => (status === 'ok' ? 'success' : status === 'partial' ? 'warning' : 'error')
const statusWord = (status: unknown): string => oneLine(String(status ?? 'unknown').replace(/_/g, ' '), 24) || 'unknown'

const healthOf = (s: ChiefStatus | null): ChiefHealth => (s?.health && typeof s.health === 'object' ? s.health : {})

// `on`, `off` or `paused`: health.guard first, then the top-level `guard` (lib/chief.py writes both from one read);
// null = not said. The header strip's pill and the Health tab's Guard row both read this.
export function guardState(s: ChiefStatus | null): 'on' | 'off' | 'paused' | null {
  for (const g of [healthOf(s).guard, (s as { guard?: unknown } | null)?.guard]) {
    const word = typeof g === 'string' ? g : g && typeof g === 'object' ? (g as { state?: unknown }).state : null
    if (word === 'on' || word === 'off' || word === 'paused') return word
  }
  return null
}

// now.json's coverage as rows: the six sources in their fixed order, then any other the tick named.
export function sourceRows(doc: NowDoc | null | undefined): [string, { status?: string; reason?: string; since?: string }][] {
  const cov = doc?.coverage && typeof doc.coverage === 'object' ? doc.coverage : {}
  const names = [...SOURCES.filter(n => n in cov), ...Object.keys(cov).filter(n => !(SOURCES as readonly string[]).includes(n)).sort()]
  return names.map(n => [n, cov[n] && typeof cov[n] === 'object' ? cov[n] : {}])
}

export function watcherErrors(s: ChiefStatus | null): ChiefInboxItem[] {
  const own = healthOf(s).watcher_errors
  const rows = Array.isArray(own) ? own : (Array.isArray(s?.inbox) ? s!.inbox! : []).filter(isWatcherError)
  return rows.filter(u => !!u && typeof u.id === 'string')
}

export function undelivered(s: ChiefStatus | null, dismissed: readonly string[] | null | undefined) {
  const rows = healthOf(s).undelivered
  return (Array.isArray(rows) ? rows : []).filter(r => !!r && typeof r.name === 'string' && REPORT_RE.test(r.name) && !(dismissed ?? []).includes(r.name))
}

// What is wrong, counted: each source that is not ok, each watcher error, each undelivered report that was not
// dismissed, and a guard that is not on. Above 0 the Health tab wears a red dot.
export function healthProblems(s: ChiefStatus | null, doc: NowDoc | null | undefined, dismissed?: readonly string[] | null): number {
  const guard = guardState(s)
  const errors = Math.max(watcherErrors(s).length, Number(healthOf(s).watcher_error_count) || 0)
  return sourceRows(doc).filter(([, c]) => c.status !== 'ok').length + errors + undelivered(s, dismissed).length + (guard && guard !== 'on' ? 1 : 0)
}

// The dismissed list lives in the plugin's store, so it outlasts the session; the atom is what a draw reads.
export async function loadDismissed($: Dollar): Promise<void> {
  let kept: string[] = []
  try {
    const v = await $.store.get(DISMISSED_KEY)
    kept = Array.isArray(v) ? v.filter((n): n is string => typeof n === 'string' && REPORT_RE.test(n)).slice(-DISMISSED_MAX) : []
  } catch { /* an unreadable store: nothing dismissed */ }
  await update($, healthAtom, prev => ({ dismissed: kept, reports: prev?.reports ?? {} }))
}

// Dismiss deletes nothing: the name joins the list, and the row is no longer drawn or counted.
async function dismiss($: Dollar, name: string | null): Promise<void> {
  let kept: string[] = []
  await update($, healthAtom, prev => {
    kept = name === null ? [] : [...(prev?.dismissed ?? []).filter(n => n !== name), name].slice(-DISMISSED_MAX)
    const reports = { ...(prev?.reports ?? {}) }
    if (name !== null) delete reports[name]
    return { dismissed: kept, reports }
  })
  try { await $.store.set(DISMISSED_KEY, kept) } catch { /* kept for this session only */ }
}

// A report row was pressed. Opening reads the file through `mother chief report <name>` (never a draw, never a
// file read here); closing forgets the text. null = the verb refused or failed, which the row says.
async function toggleReport($: Dollar, bin: string, name: string, isLoaded: boolean): Promise<void> {
  if (isLoaded) {
    await update($, healthAtom, prev => {
      const reports = { ...(prev?.reports ?? {}) }
      delete reports[name]
      return { dismissed: prev?.dismissed ?? [], reports }
    })
    return
  }
  let text: string | null = null
  if (REPORT_RE.test(name)) {
    try {
      const ran = await $.process.run(['python3', bin, 'chief', 'report', name], { timeoutMs: REPORT_TIMEOUT_MS })
      if (ran.exitCode === 0) text = String(ran.stdout ?? '')
    } catch { /* said on the row */ }
  }
  await update($, healthAtom, prev => ({ dismissed: prev?.dismissed ?? [], reports: { ...(prev?.reports ?? {}), [name]: text } }))
}

const kb = (bytes: unknown): string => (typeof bytes === 'number' && bytes >= 0 ? (bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`) : '')
const sub = (ui: Ui, key: string, text: string, count?: number) => SectionHeading(ui, { key, text, count, level: 'group' })

export function healthSection($: Dollar, ui: Ui, bin: string, s: ChiefStatus, doc: NowDoc | null | undefined, nowMs: number,
                              ctx: TabCtx, self: string | null, goHub: GoHub, heading: boolean, goChief?: GoHub) {
  const { Box, Text, Button } = ui
  const hd = healthOf(s)
  const out: unknown[] = []
  if (heading) out.push(sectionHeading(ui, 'health-heading', 'Health'))

  // Sources: what the tick could read (now.json coverage)
  const sources = sourceRows(doc)
  out.push(sub(ui, 'health-sources', 'Sources'))
  if (!sources.length) out.push(<Text key="health-sources-none" dimColor>{doc ? 'The tick named no source' : 'No source report yet: the tick has not written now.json'}</Text>)
  // every source that is fine on one row of badges; a source that is not on its own row, with its reason
  const fine = sources.filter(([, c]) => c.status === 'ok')
  if (fine.length) {
    out.push(
      <Box key="health-src-ok" {...pillRow(ui)}>
        {fine.map(([name]) => badge(ui, `${name} ok`, 'success', 'health-src-badge:' + name))}
      </Box>)
  }
  for (const [name, c] of sources) {
    if (c.status === 'ok') continue
    const since = c.since ? age(c.since, nowMs) : ''
    out.push(
      <Box key={'health-src:' + name} flexDirection="column">
        <Box flexWrap="wrap" gap="xs" alignItems="center">
          {badge(ui, statusWord(c.status), statusColor(c.status), 'health-src-badge:' + name)}
          <Text>{name}</Text>
          {since ? <Text dimColor>{since === '0h' ? 'just now' : `for ${since}`}</Text> : null}
        </Box>
        {c.reason ? <Text key={'health-src-why:' + name} wrap="wrap">{safe(String(c.reason)).trim()}</Text> : null}
      </Box>)
  }

  // Watcher: its last poll and each source it polls
  const w = hd.watcher
  out.push(sub(ui, 'health-watcher', 'Watcher'))
  if (!w || typeof w !== 'object') {
    out.push(<Text key="health-watcher-none" dimColor>The watcher never ran</Text>)
  } else {
    const polled = ago(w.at, nowMs)
    out.push(
      <Box key="health-watcher-poll" flexWrap="wrap" gap="xs" alignItems="center">
        <Text color={w.stale ? 'error' : undefined}>{polled ? (polled === 'now' ? 'last poll now' : `last poll ${polled} ago`) : 'last poll unknown'}</Text>
        {w.stale ? badge(ui, 'stale', 'error', 'health-watcher-stale') : null}
        {w.active === false ? <Text dimColor>outside active hours</Text> : null}
      </Box>)
    const polledSources = Object.entries(w.sources && typeof w.sources === 'object' ? w.sources : {})
    if (polledSources.length) {
      out.push(
        <Box key="health-watcher-sources" flexWrap="wrap" gap="xs">
          {polledSources.map(([name, v]) => badge(ui, `${oneLine(name, 40)} ${statusWord(v?.status)}`, statusColor(v?.status), 'health-watch:' + name))}
        </Box>)
    }
    for (const [name, v] of polledSources) {
      if (v?.status !== 'ok' && v?.reason) out.push(<Text key={'health-watch-why:' + name} wrap="wrap">{`${oneLine(name, 40)}: ${oneLine(v.reason, 300)}`}</Text>)
      // reading less than usual (the chat source while rate limited): said on its own line, in amber
      if (v?.degraded) out.push(<Text key={'health-watch-degraded:' + name} color="warning" wrap="wrap">{`${oneLine(name, 40)} degraded: ${oneLine(v.degraded, 200)}`}</Text>)
    }
  }
  // the chat source's other half: the leading desktop session's mention and DM search (hooks/mention_search.ts)
  const ms = hd.mention_search
  if (ms && typeof ms === 'object' && (ms.status !== 'never' || !!(w as any)?.sources?.chat)) {
    const bad = ms.status === 'error' || ms.status === 'rate_limited'
    out.push(<Text key="health-mention-search" color={bad ? 'warning' : undefined} dimColor={!bad && ms.status !== 'ok'} wrap="wrap">{`chat: ${oneLine(mentionSearchText(ms, nowMs), 300)}`}</Text>)
  }

  // Watcher errors: Inbox items keyed `watcher:`; Done is the Inbox's own verb
  const errors = watcherErrors(s)
  if (errors.length) {
    out.push(sub(ui, 'health-errors', 'Watcher errors', Math.max(errors.length, Number(hd.watcher_error_count) || 0)))
    if (errors.length >= 2) {
      out.push(<Button key="health-done-all" plain dimColor onPress={async () => { for (const u of errors) await goHub(['done', u.id]) }}>Done all</Button>)
    }
    for (const u of errors) {
      const id = 'health:err:' + u.id
      const why = whyText(u.why)
      out.push(card(ui, { id, color: 'error', selected: ctx.selected === id }, [
        <Box key={'meta:' + id} {...pillRow(ui)}>
          {badge(ui, 'error', 'error', 'badge:' + id)}
          <Text weight="semibold">{oneLine(u.title, TITLE_MAX) || oneLine(u.key, TITLE_MAX)}</Text>
          <Text dimColor>{`${oneLine(u.key, 60)} · ${ago(u.at, nowMs)}`}</Text>
          {sourceLink(ui, id, linksOf(u.link, u.links, 'error'))}
          <Button key={'health-done:' + u.id} onPress={pressing($, id, () => { logAct($, bin, 'done', inboxRow(u)); return goHub(['done', u.id]) })}>Done</Button>
        </Box>,
        why ? <Text key={'why:' + id} dimColor wrap="wrap">{why}</Text> : null,
      ]))
    }
  }

  // Undelivered reports: routine reports that did not reach the manager's DM
  const dismissed = ctx.health?.dismissed ?? []
  const reports = undelivered(s, dismissed)
  if (reports.length || dismissed.length) out.push(sub(ui, 'health-reports', 'Undelivered reports', reports.length))
  for (const r of reports) {
    const id = 'health:report:' + r.name
    const loaded = ctx.health?.reports?.[r.name]
    const open = loaded !== undefined
    const lines = typeof loaded === 'string' ? safe(loaded).split('\n') : []
    out.push(card(ui, { id, color: 'warning', selected: ctx.selected === id }, [
      titleRow(ui, id, `${oneLine(r.routine, 60)} · ${oneLine(r.date, 10)}`, open, pressing($, id, () => toggleReport($, bin, r.name, open)),
               <Text key={'size:' + id} dimColor>{kb(r.bytes)}</Text>),
      open && loaded === null ? <Text key={'report-failed:' + id} color="warning">Couldn't read this report</Text> : null,
      ...(open ? lines.slice(0, REPORT_LINES_MAX).map((line, n) => <Text key={`report:${r.name}:${n}`} wrap="wrap">{line || ' '}</Text>) : []),
      open && lines.length > REPORT_LINES_MAX ? <Text key={'report-more:' + id} dimColor>{`${lines.length - REPORT_LINES_MAX} more lines in .mother/${r.name}`}</Text> : null,
      open
        ? <Box key={'acts:' + id} {...pillRow(ui, true)}>
            <Button key={'health-dismiss:' + r.name} onPress={pressing($, id, () => { logAct($, bin, 'dismiss', { item_id: r.name, source: 'report', title: oneLine(r.routine, 60) }); return dismiss($, r.name) })}>Dismiss</Button>
          </Box>
        : null,
    ]))
  }
  if (dismissed.length) {
    out.push(
      <Box key="health-dismissed" flexWrap="wrap" gap="xs" alignItems="center">
        <Text dimColor>{`${dismissed.length} dismissed`}</Text>
        <Button key="health-restore" plain dimColor onPress={() => dismiss($, null)}>Show again</Button>
      </Box>)
  }

  // Budget and lead
  const b = s.budget
  const lead = leadPill(s, self, nowMs)
  const beat = s.chief?.heartbeat_at ? ago(s.chief.heartbeat_at, nowMs) : ''
  out.push(sub(ui, 'health-budget', 'Budget and lead'))
  out.push(
    <Box key="health-budget-row" flexWrap="wrap" gap="xs" alignItems="center">
      {Stat(ui, { value: b?.unreadable ? 'spend file unreadable' : `${money(b?.spent)} of ${money(b?.limit)} today`, tone: b?.over || b?.unreadable ? 'error' : undefined })}
      {leadBadge(ui, lead, 'health-lead')}
      <Text dimColor>{!beat ? 'no heartbeat' : beat === 'now' ? 'heartbeat now' : `heartbeat ${beat} ago`}</Text>
    </Box>)

  // Guard
  const guard = guardState(s)
  out.push(sub(ui, 'health-guard', 'Guard'))
  out.push(
    <Box key="health-guard-row" flexWrap="wrap" gap="xs" alignItems="center">
      {guard ? badge(ui, guard, guard === 'on' ? 'success' : 'error', 'health-guard-badge') : <Text dimColor>not reported</Text>}
      {guard === 'off' ? <Text color="error">every guard rule is off: the model alone decides</Text> : null}
      {guard === 'paused' ? <Text color="error">outbound actions are paused</Text> : null}
    </Box>)
  out.push(...quietedSection($, ui, s, nowMs, goChief))
  return out
}

// ---- Health: Quieted by habit (status.quieted, lib/habits.py) ----
// The lines the quiet rule logged instead of showing, newest first. Show makes one a normal Inbox item (`mother chief
// habits show <id>`); Stop quieting this, once per bucket, puts its bucket on the exceptions list (`habits except`).
// Nothing is drawn while habits are on and nothing was quieted. The text is model- or line-written: plain Text.
export function quietedSection($: Dollar, ui: Ui, s: ChiefStatus, nowMs: number, goChief?: GoHub) {
  const { Box, Text, Button } = ui
  const q = quietedOf(s)
  if (q.on && !q.count && !q.recent.length) return []
  const out: unknown[] = [sub(ui, 'health-quieted', 'Quieted by habit', q.count)]
  if (!q.on) out.push(<Text key="health-quieted-off" dimColor>Habits are off: nothing is quieted (/chief habits on)</Text>)
  const seen = new Set<string>()
  for (const r of q.recent) {
    const first = !seen.has(r.bucket)
    seen.add(r.bucket)
    out.push(
      <Box key={'quieted:' + r.id} flexDirection="column">
        <Box flexWrap="wrap" gap="xs">
          <Text weight="semibold">{oneLine(r.title, 60) || oneLine(r.key, 60)}</Text>
          <Text dimColor>{`${oneLine(r.label, 40)} · ${ago(r.at, nowMs)}`}</Text>
        </Box>
        <Box flexWrap="wrap" gap="xs" alignItems="center">
          <Text key={'quieted-why:' + r.id} dimColor>{oneLine(r.why, 120)}</Text>
          {goChief ? <Button key={'quieted-show:' + r.id} onPress={() => goChief(['habits', 'show', r.id])}>Show</Button> : null}
          {goChief && first ? <Button key={'quieted-stop:' + r.bucket} plain dimColor onPress={() => goChief(['habits', 'except', r.bucket])}>Stop quieting this</Button> : null}
        </Box>
      </Box>)
  }
  return out
}
