import type { Color } from 'claude-code'

import { oneLine } from './shared'
import type { Dollar } from './shared'
import type { ChiefDesktop, ChiefOpen, ChiefStatus } from '../types'

// The Chief pane's drawing kit: the header strip, a card frame with a kind colour, badges, the source colour map and
// the suggested prompts an Inbox card offers. Nothing here reads state or runs an action: chief.tsx's sections pass
// what they know and get elements back. Every colour is a theme key, so both themes and both surfaces draw it.

export type Ui = ReturnType<Dollar['ui']['resolve']>

// ---- sources: an Inbox item's kind, read from its key's shape (docs/CONTRACT.md §5e "Formats"); no new data flows ----
export type Source = 'chat' | 'mail' | 'tracker' | 'code' | 'calendar' | 'manual' | 'error' | 'other'

export function sourceOf(key: string | null | undefined): Source {
  const k = String(key ?? '').trim()
  if (/^watcher:/.test(k)) return 'error'
  if (/^mail:/.test(k)) return 'mail'
  if (/^cal:/.test(k)) return 'calendar'
  if (/^manual:/.test(k)) return 'manual'
  if (/^wa:/.test(k)) return 'chat'
  if (/^[A-Z][A-Z0-9_]*-\d+$/.test(k)) return 'tracker'
  if (/^[\w.-]+\/[\w.-]+#\d+$/.test(k)) return 'code'
  if (/^[A-Z0-9]+\/(dm|\d+(\.\d+)?)$/.test(k)) return 'chat'
  return 'other'
}

export const SOURCE_COLOR: Record<Source, Color> = {
  chat: 'planMode', mail: 'merged', tracker: 'permission', code: 'success', calendar: 'autoAccept',
  manual: 'suggestion', error: 'error', other: 'subtle',
}
export const TOP_COLOR: Color = 'claude'        // a Top 3 entry of the Today tab

// A Now item's source, from its id's first part (the tick's kind), for the Today tab's badge and border.
export function itemSource(id: string | null | undefined): Source {
  const kind = String(id ?? '').split(':')[0]
  return kind === 'chat' || kind === 'mail' ? kind
    : kind === 'pr' || kind === 'repo' || kind === 'build' ? 'code'
    : kind === 'ticket' ? 'tracker' : kind === 'event' ? 'calendar' : 'other'
}
export const NEED_COLOR: Color = 'warning'      // a session stopped on a question
export const SESSION_COLOR: Color = 'ide'       // a pinned session
export const SELF_COLOR: Color = 'claude'       // the session whose pane this is

// Two suggested prompts per source, fixed copy; the first is the one the brief names.
const SUGGESTIONS: Record<Source, [string, string]> = {
  chat: ['Draft a reply in the thread', 'Summarise the thread and propose a next step'],
  mail: ['Draft a reply', 'Summarise the mail and propose a next step'],
  tracker: ['Summarise the ticket and propose a next step', 'Find the owner and draft a DM'],
  code: ['Review the PR', 'Why is CI red?'],
  calendar: ['Decide whether to accept', 'Prep me for it'],
  manual: ["What's this about?", 'Make it a todo'],
  error: ['Diagnose why the source is unreachable', 'Check the connector'],
  other: ["What's this about?", 'Propose a next step'],
}
export const suggestionsFor = (source: Source): readonly string[] => SUGGESTIONS[source]
// The same two as short button labels, for a compact card whose item has no actions of its own.
const SUGGESTION_LABELS: Record<Source, [string, string]> = {
  chat: ['Draft reply', 'Summarise'],
  mail: ['Draft reply', 'Summarise'],
  tracker: ['Summarise', 'DM the owner'],
  code: ['Review PR', 'Why is CI red?'],
  calendar: ['Decide', 'Prep me'],
  manual: ["What's this?", 'Make a todo'],
  error: ['Diagnose', 'Check connector'],
  other: ["What's this?", 'Next step'],
}
export const suggestionLabels = (source: Source): readonly string[] => SUGGESTION_LABELS[source]

// ---- the triage verdict on an Inbox item: kind, who it is on, urgency, and up to 3 actions (first = recommended) ----
export type ItemAction = { label: string; prompt: string }
export const KINDS = ['decide', 'reply', 'act', 'fyi'] as const
export const ONS = ['manager', 'team', 'other'] as const
export const URGENCIES = ['now', 'today', 'later'] as const
export type Kind = (typeof KINDS)[number]
export type Urgency = (typeof URGENCIES)[number]
export type Extra = { kind: Kind; on: (typeof ONS)[number]; urgency: Urgency; actions: ItemAction[] }
export const ACTIONS_SHOWN = 3
export const ACTION_LABEL_MAX = 24
export const ACTION_PROMPT_MAX = 300

// Model-written actions as a card may show them: at most 3 of {label, prompt}, each one safe line (24 and 300
// characters), both non-empty. Anything off-shape is dropped.
export function cleanActions(raw: unknown): ItemAction[] {
  const out: ItemAction[] = []
  for (const a of Array.isArray(raw) ? raw : []) {
    if (!a || typeof a !== 'object' || typeof (a as any).label !== 'string' || typeof (a as any).prompt !== 'string') continue
    const label = oneLine((a as any).label, ACTION_LABEL_MAX)
    const prompt = oneLine((a as any).prompt, ACTION_PROMPT_MAX)
    if (label && prompt) out.push({ label, prompt })
    if (out.length === ACTIONS_SHOWN) break
  }
  return out
}

// {kind, on, urgency, actions} from a verdict or an Inbox item that carries any of them, else null (the older
// four-field verdict, an item decided with none). An unknown kind reads `act`, `on` `manager`, urgency `today`.
export function verdictExtra(v: unknown): Extra | null {
  const o = v && typeof v === 'object' ? (v as Record<string, unknown>) : null
  if (!o || (o.kind == null && o.on == null && o.urgency == null && !(Array.isArray(o.actions) && o.actions.length))) return null
  const pick = <T extends string>(x: unknown, allowed: readonly T[], fallback: T): T => ((allowed as readonly unknown[]).includes(x) ? (x as T) : fallback)
  return { kind: pick(o.kind, KINDS, 'act'), on: pick(o.on, ONS, 'manager'), urgency: pick(o.urgency, URGENCIES, 'today'), actions: cleanActions(o.actions) }
}

// The Inbox order: urgency (`now`, `today`, `later`; an item with none reads `today`), then newest first.
const urgencyRank = (u: unknown): number => URGENCIES.indexOf(verdictExtra(u)?.urgency ?? 'today')
export function inboxOrder<T extends { at?: string | null }>(items: readonly T[]): T[] {
  return [...items].sort((a, b) => urgencyRank(a) - urgencyRank(b) || (Date.parse(b.at ?? '') || 0) - (Date.parse(a.at ?? '') || 0))
}

export const KIND_COLOR: Record<Exclude<Kind, 'fyi'>, Color> = { decide: 'warning', reply: 'suggestion', act: 'subtle' }
export const URGENT_COLOR: Color = 'error'

// The small badges after a card's source badge: its kind (`fyi` wears none), and `urgent` while its urgency is `now`.
export function kindBadges(ui: Ui, x: Extra | null, id: string) {
  if (!x) return []
  return [
    x.kind !== 'fyi' ? badge(ui, x.kind, KIND_COLOR[x.kind], 'kind:' + id) : null,
    x.urgency === 'now' ? badge(ui, 'urgent', URGENT_COLOR, 'urgent:' + id) : null,
  ]
}
// An expanded card's one dim line: `decide · on manager · now`.
export const verdictLine = (x: Extra | null): string => (x ? `${x.kind} · on ${x.on} · ${x.urgency}` : '')

// A card's action buttons: up to 3, short labels, the first (the recommended one) primary. `says` is what a press
// sends, shown dim under the buttons of an expanded card. chief.tsx and pane_tabs.tsx pass each button's `run`.
export type CardAct = { key: string; label: string; says?: string; run: () => unknown }
export function actButtons(ui: Ui, acts: readonly CardAct[], press: (run: () => unknown) => () => unknown) {
  const { Button } = ui
  return acts.slice(0, ACTIONS_SHOWN).map((a, n) => (
    <Button key={a.key} variant={n === 0 ? 'primary' : 'secondary'} onPress={press(a.run)}>{a.label}</Button>
  ))
}
export function actSays(ui: Ui, id: string, acts: readonly CardAct[]) {
  const { Text } = ui
  return acts.slice(0, ACTIONS_SHOWN).filter(a => a.says).map((a, n) => (
    <Text key={`says:${id}:${n}`} dimColor wrap="wrap">{`${a.label}: ${a.says}`}</Text>
  ))
}

// The brief a chosen suggestion makes for a new session: the item in full, as hooks/shared.ts askPrompt words it.
export function suggestedBrief(suggestion: string, title: string, why: string | null | undefined, link: string | null | undefined): string {
  return `${suggestion} for "${title}"` + (why ? ` — ${why}` : '') + (link ? ` (${link})` : '')
    + ". Draft anything that reaches another person; don't send."
}

// ---- open / collapsed cards: explicit choices over a per-kind default; /chief expand|collapse sets `all` ----
export type CardKind = 'need' | 'inbox' | 'sess' | 'today'

// ---- tabs: one section at a time, or `all` stacked ----
export type TabId = 'needs' | 'inbox' | 'sessions' | 'today' | 'team' | 'health' | 'all'
export const TAB_IDS: readonly TabId[] = ['needs', 'inbox', 'sessions', 'today', 'team', 'health', 'all']
// A tab is data: an id, a label, how many items it holds and what draws them. `all` has no section of its own.
// `text` replaces `<label> <count>` on the button (Health wears no count); `alert` adds a red dot.
export type Tab = { id: TabId; label: string; count: number; section: (heading: boolean) => unknown; text?: string; alert?: boolean }

// The tab shown: the manager's pick, else the first tab that holds something, else `all` (every empty line).
export function shownTab(picked: string | null | undefined, tabs: readonly { id: TabId; count: number }[]): TabId {
  if (picked && (TAB_IDS as readonly string[]).includes(picked)) return picked as TabId
  return tabs.find(t => t.id !== 'all' && t.count > 0)?.id ?? 'all'
}

// A tab holds something new when its count is above the count it had when it was last opened.
export const tabIsNew = (t: { id: TabId; count: number }, seen: Record<string, number> | null | undefined): boolean =>
  t.id !== 'all' && t.count > (seen?.[t.id] ?? 0)

// A tab that is not the shown one lights under the pointer: each is its own hover group, so only that tab does.
export const tabHover = (id: string) => ({ scope: 'mother-tab:' + id, color: SELF_COLOR, underline: true, dimColor: false })

// The tab row: a Button per tab with its count, the shown one primary; ` •` marks a tab with something new.
export function tabRow(ui: Ui, tabs: readonly Tab[], shown: TabId, seen: Record<string, number> | null | undefined, onPick: (id: TabId) => unknown) {
  const { Box, Button, Text } = ui
  return (
    <Box key="tabs" flexWrap="wrap" gap="xs" marginTop={1}>
      {[...tabs.map(t => ({ id: t.id, alert: !!t.alert, text: t.text ?? `${t.label} ${t.count}${t.id !== shown && tabIsNew(t, seen) ? ' •' : ''}` })), { id: 'all' as TabId, alert: false, text: 'All' }].map(t => (
        <Button key={'tab:' + t.id} variant={t.id === shown ? 'primary' : 'secondary'} dimColor={t.id === 'all' && shown !== 'all'}
          {...(t.id === shown ? {} : { hover: tabHover(t.id) })}
          onPress={() => onPick(t.id)}>{t.alert ? <Text>{t.text}<Text color="error">{' ●'}</Text></Text> : t.text}</Button>
      ))}
    </Box>
  )
}

// Needs you cards start open; Inbox and session cards closed, except the selected card. A card the manager toggled
// keeps its choice; `all` (from /chief expand|collapse) stands above the defaults and below a later toggle.
export function isOpen(open: ChiefOpen | null, id: string, kind: CardKind, selected: string | null): boolean {
  const chosen = open?.cards?.[id]
  if (typeof chosen === 'boolean') return chosen
  if (typeof open?.all === 'boolean') return open.all
  return kind === 'need' || (selected !== null && id === selected)
}

export const toggled = (prev: ChiefOpen | null, id: string, nowOpen: boolean): ChiefOpen =>
  ({ all: prev?.all ?? null, cards: { ...(prev?.cards ?? {}), [id]: nowOpen } })
export const allCards = (open: boolean): ChiefOpen => ({ all: open, cards: {} })

// ---- who leads, the budget, a session's state: short words and a colour each ----
export type Pill = { text: string; color: Color }

// Led, here or by another session, is healthy: green. Only nobody leading is red.
export function leadPill(s: ChiefStatus, self: string | null): Pill {
  const by = typeof s.led_by === 'string' && s.led_by ? s.led_by : null
  return !by ? { text: 'nobody leading', color: 'error' }
    : by === self ? { text: 'led here', color: 'success' } : { text: 'led by another session', color: 'success' }
}

export const BUDGET_CELLS = 8
export const money = (n: number | undefined): string => `$${(n ?? 0).toFixed(2)}`

// `▰▰▰▱▱▱▱▱` for today's spend against the limit, and its colour: green under half, amber under 90%, red at or past.
export function budgetBar(spent: number | undefined, limit: number | undefined): Pill {
  const lim = typeof limit === 'number' && limit > 0 ? limit : 0
  const ratio = lim ? Math.min(1, Math.max(0, (spent ?? 0) / lim)) : 1
  const filled = Math.round(ratio * BUDGET_CELLS)
  return { text: '▰'.repeat(filled) + '▱'.repeat(BUDGET_CELLS - filled), color: ratio >= 0.9 ? 'error' : ratio >= 0.5 ? 'warning' : 'success' }
}

export const ACTIVE_MINUTES = 15

// `asks` while its floor card asks; else `active 3m` within 15 minutes of its last activity, else `idle 2h`.
export function sessionPill(d: ChiefDesktop, asks: boolean, nowMs: number, ago: (iso: string | null | undefined, nowMs: number) => string): Pill {
  if (asks) return { text: 'asks', color: 'warning' }
  const ms = d.last_activity_at ? Date.parse(d.last_activity_at) : NaN
  const age = ago(d.last_activity_at, nowMs)
  if (!Number.isNaN(ms) && nowMs - ms < ACTIVE_MINUTES * 60_000) return { text: `active ${age}`, color: 'success' }
  return { text: age ? `idle ${age}` : 'idle', color: 'subtle' }
}

// ---- text: never cut mid-word ----
// `text` on one line of at most `cap` characters: a longer one is cut at the last word boundary that fits and ends
// with `…` (a single word longer than the cap is cut where the cap falls).
export function clip(text: string | null | undefined, cap: number): string {
  const t = String(text ?? '').split(/\s+/).filter(Boolean).join(' ')
  if (t.length <= cap) return t
  const head = t.slice(0, Math.max(1, cap - 1))
  const at = head.lastIndexOf(' ')
  return (at > 0 ? head.slice(0, at) : head).replace(/[\s.,;:!?-]+$/, '') + '…'
}

// A stored why as an expanded card shows it: whole, on one line. One that reaches a storage cap (300, or the 200 of
// an older item) without ending a sentence was cut there, mid-word: its last partial word goes and `…` says so.
export const WHY_MAX = 300          // what the hub stores of a line's summary
const WHY_OLD_MAX = 200
export function whyText(raw: unknown): string {
  const t = oneLine(raw, WHY_MAX)
  if ((t.length >= WHY_MAX - 1 || t.length === WHY_OLD_MAX) && !/[.!?…)\]"'”]$/.test(t)) return clip(t, t.length - 1)
  return t
}

// What a card's second row says in place of a raw key: the CLI's `label` (a channel's name, a person, `mail thread`…)
// when the status carries one, else the same words from the key's shape. Never a raw chat or mail id.
export function humanLabel(u: { key?: string | null; label?: string | null; who?: string | null }): string {
  const given = String(u.label ?? '').trim()
  if (given) return given
  const key = String(u.key ?? '').trim()
  const source = sourceOf(key)
  if (source === 'tracker' || source === 'code') return key
  if (source === 'chat') return String(u.who ?? '').trim() || 'chat thread'
  return source === 'mail' ? 'mail thread' : source === 'calendar' ? 'calendar' : source === 'manual' ? 'note'
    : source === 'error' ? 'source error' : 'update'
}

// ---- elements ----
// A small filled pill: the source or state of a card, the counts of the header strip.
export function badge(ui: Ui, text: string, color: Color, key?: string) {
  const { Text } = ui
  return <Text key={key} backgroundColor={color} color="inverseText" weight="semibold">{` ${text} `}</Text>
}

// One dim line where a section has nothing: never a blank.
export function emptyLine(ui: Ui, key: string, text: string) {
  const { Box, Text } = ui
  return <Box key={key} marginTop={1}><Text dimColor>{text}</Text></Box>
}

export function sectionHeading(ui: Ui, key: string, text: string) {
  const { Box, Text } = ui
  return <Box key={key} marginTop={1}><Text weight="semibold" size="heading">{text}</Text></Box>
}

// A small heading inside a tab: `Suggested`, `Top 3`, `On you (12)`.
export function subHeading(ui: Ui, key: string, text: string) {
  const { Box, Text } = ui
  return <Box key={key} marginTop={1}><Text weight="semibold" dimColor>{text}</Text></Box>
}

export type CardFrame = {
  id: string            // the card's id: `<kind>:<item>`; the Box is keyed `card:<id>`
  color: Color          // the kind's border colour
  selected: boolean     // the last card a button was pressed on: a bold, full-strength border
  active?: boolean      // this session's own card: the accent colour
  scope?: string        // a hover group shared with another card of the same session
}

// A bordered card with padding. At rest the border is dim and rounded; selected or hovered it is bold and lit, and a
// hover anywhere in its `scope` lights every card of that group.
export function card(ui: Ui, f: CardFrame, children: unknown) {
  const { Box } = ui
  const color = f.active ? SELF_COLOR : f.color
  const lit = f.selected || !!f.active
  const hover = { borderStyle: 'bold', borderDimColor: false, ...(f.scope ? { scope: f.scope } : {}) }
  return (
    <Box key={'card:' + f.id} flexDirection="column" marginTop={1} paddingX={1}
      borderStyle={f.selected ? 'bold' : 'round'} borderColor={color} borderDimColor={!lit} hover={hover}>
      {children as any}
    </Box>
  )
}

export const COMPACT_TITLE_MAX = 56

// The title row: a plain Button whose press toggles the card; `▾` open, `▸` closed. `actions` (a compact card's
// primary button, and a small second one) sit at the right end of the same row; the title is then one cut line.
export function titleRow(ui: Ui, id: string, title: string, open: boolean, onPress: () => unknown, trailing?: unknown, actions?: unknown) {
  const { Box, Button, Text } = ui
  const toggle = (text: string) => (
    <Button key={'toggle:' + id} plain onPress={onPress}><Text weight="semibold">{`${open ? '▾' : '▸'} ${text}`}</Text></Button>
  )
  if (!actions) {
    return (
      <Box key={'title:' + id} flexWrap="wrap" gap="xs">
        {toggle(title)}
        {trailing as any}
      </Box>
    )
  }
  return (
    <Box key={'title:' + id} gap="xs" justifyContent="space-between">
      <Box key={'title-text:' + id} gap="xs" flexShrink={1}>
        {toggle(clip(title, COMPACT_TITLE_MAX))}
        {trailing as any}
      </Box>
      <Box key={'title-acts:' + id} gap="xs" flexShrink={0}>{actions as any}</Box>
    </Box>
  )
}

// The pane's picture, from `mother chief portrait --json`, loaded once per session by chief.tsx (a draw is pure).
// `png` (the workspace's own .mother/portrait.png, else the bundled one) is what the terminal's `Image` draws. A
// surface with `Svg` only (the desktop app) draws `jpeg` (the workspace's small .mother/portrait.jpg) wrapped in an
// SVG, else `svg`, the bundled vector's own markup. null = not loaded, or the verb failed: no picture.
export type Portrait = {
  png: string; width: number; height: number
  jpeg?: { base64: string; width: number; height: number } | null
  svg?: string | null
}
export const PORTRAIT_ALT = 'Mother, Chief of Stuff'
export const WIDE_RATIO = 1.4
const BANNER_COLUMNS = 44
const BANNER_ROWS_MIN = 3
const BANNER_ROWS_MAX = 6          // the terminal keeps the picture small
const SPOT_COLUMNS = 10
const SPOT_ROWS = 5
export const BANNER_W = 590        // the desktop banner's shape: the pane's width by a slim band
export const BANNER_H = 110
const SPOT_PX = 64
const SVG_MAX = 131_072

// A wide picture (width / height >= 1.4) is a banner across the top of the pane; a squarer one a small spot at the
// left of the header strip. Rows follow the picture's shape: a terminal cell is about twice as tall as it is wide.
export function portraitLayout(p: { width: number; height: number }): { banner: boolean; columns: number; rows: number } {
  if (p.width / p.height < WIDE_RATIO) return { banner: false, columns: SPOT_COLUMNS, rows: SPOT_ROWS }
  const rows = Math.round(BANNER_COLUMNS * p.height / p.width / 2)
  return { banner: true, columns: BANNER_COLUMNS, rows: Math.min(BANNER_ROWS_MAX, Math.max(BANNER_ROWS_MIN, rows)) }
}

export const jpegSvg = (j: { base64: string; width: number; height: number }): string =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${j.width} ${j.height}">` +
  `<image href="data:image/jpeg;base64,${j.base64}" width="${j.width}" height="${j.height}"/></svg>`

const xml = (t: string): string => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;')

// The slim banner a wide picture makes on a surface with `Svg`: the picture's middle band (`slice` crops, never
// squeezes) in a 590 x 110 viewBox, a dark gradient over its lower part, and the title drawn inside at the
// bottom-left as SVG text (escaped; a generic font family, nothing fetched).
export const bannerSvg = (j: { base64: string }, title = 'Mother', subtitle = 'Chief of Stuff'): string =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${BANNER_W} ${BANNER_H}" width="${BANNER_W}" height="${BANNER_H}">` +
  '<defs><linearGradient id="shade" x1="0" y1="0" x2="0" y2="1"><stop offset="0.2" stop-color="#000" stop-opacity="0"/>' +
  '<stop offset="1" stop-color="#000" stop-opacity="0.8"/></linearGradient></defs>' +
  `<image href="data:image/jpeg;base64,${j.base64}" x="0" y="0" width="${BANNER_W}" height="${BANNER_H}" preserveAspectRatio="xMidYMid slice"/>` +
  `<rect x="0" y="0" width="${BANNER_W}" height="${BANNER_H}" fill="url(#shade)"/>` +
  `<text x="16" y="${BANNER_H - 30}" font-family="sans-serif" font-size="26" font-weight="700" fill="#fff">${xml(title)}</text>` +
  `<text x="16" y="${BANNER_H - 12}" font-family="sans-serif" font-size="13" fill="#fff" fill-opacity="0.85">${xml(subtitle)}</text></svg>`

// The picture, whether it is a banner, and whether that banner carries the title, by the surface's name (a surface
// without Image still resolves one, to an empty stand-in). Nothing where the surface has no element for it: the
// title says it.
function portraitOf(ui: Ui, p: Portrait, surface: string): { el: unknown; banner: boolean; titled?: boolean } | null {
  const { Image, Svg } = ui as { Image?: any; Svg?: any }
  if (surface === 'terminal') {
    if (!Image) return null
    const { banner, columns, rows } = portraitLayout(p)
    return { banner, el: <Image key="portrait" source={{ png: p.png }} columns={columns} rows={rows} alt={PORTRAIT_ALT} /> }
  }
  if (!Svg) return null
  if (p.jpeg && portraitLayout(p.jpeg).banner) {
    const slim = bannerSvg(p.jpeg)
    if (slim.length <= SVG_MAX) return { banner: true, titled: true, el: <Svg source={slim} alt={PORTRAIT_ALT} width={BANNER_W} height={BANNER_H} /> }
  }
  const wrapped = p.jpeg ? jpegSvg(p.jpeg) : ''
  if (p.jpeg && wrapped.length <= SVG_MAX && !portraitLayout(p.jpeg).banner) {
    return { banner: false, el: <Svg source={wrapped} alt={PORTRAIT_ALT} width={SPOT_PX} height={Math.round(SPOT_PX * p.jpeg.height / p.jpeg.width)} /> }
  }
  // no usable jpeg (a custom PNG alone cannot be drawn here): the bundled vector, as a spot
  return p.svg ? { banner: false, el: <Svg source={p.svg} alt={PORTRAIT_ALT} width={SPOT_PX} height={SPOT_PX} /> } : null
}

export type Strip = {
  needs: number; inbox: number; sessions: number   // the tabs carry these counts; the strip no longer draws them
  spent: number | undefined; limit: number | undefined; overBudget: boolean
  lead: Pill; beat: string
  portrait: Portrait | null
  surface: string       // `terminal` draws an Image; any other surface an Svg when it has one
  guard?: string | null // `off` and `paused` each draw a red pill; `on`, or absent, nothing
}

// The header: the picture (a slim banner above, which carries the title on a surface with Svg, or a spot at the
// left), "Mother" and "Chief of Stuff" unless the banner says them, then one line: the budget as `$1.50 / $20` with a
// mini bar, who leads, the heartbeat's age, and a red pill while the guard is off or paused.
export function headerStrip(ui: Ui, s: Strip) {
  const { Box, Text } = ui
  const bar = budgetBar(s.spent, s.limit)
  const limit = typeof s.limit === 'number' ? `$${Number.isInteger(s.limit) ? s.limit : s.limit.toFixed(2)}` : '$0'
  const drawn = s.portrait ? portraitOf(ui, s.portrait, s.surface) : null
  const banner = !!drawn?.banner
  const titled = banner && !!drawn?.titled
  const picture = (drawn?.el ?? null) as any
  return (
    <Box key="strip-all" flexDirection="column">
    {banner && picture ? <Box key="banner" marginBottom={titled ? 0 : 1}>{picture}</Box> : null}
    <Box key="strip" gap={1}>
      {banner ? null : picture}
      <Box key="strip-text" flexDirection="column" flexGrow={1}>
      {titled ? null : <Text weight="bold" size="title" color={SELF_COLOR}>Mother</Text>}
      {titled ? null : <Text dimColor size="caption">Chief of Stuff</Text>}
      <Box key="strip-line" flexWrap="wrap" gap="xs">
        <Text key="pill:budget" color={s.overBudget ? 'error' : undefined}>{`${money(s.spent)} / ${limit}`}</Text>
        <Text key="pill:bar" color={bar.color}>{bar.text}</Text>
        {badge(ui, s.lead.text, s.lead.color, 'pill:lead')}
        {s.guard === 'paused' ? badge(ui, 'paused', 'error', 'pill:guard') : s.guard === 'off' ? badge(ui, 'guard off', 'error', 'pill:guard') : null}
        <Text key="pill:beat" dimColor>{s.beat}</Text>
      </Box>
      </Box>
    </Box>
    </Box>
  )
}
