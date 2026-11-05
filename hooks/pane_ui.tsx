import type { Color } from 'claude-code'

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

// The tab row: a Button per tab with its count, the shown one primary; ` •` marks a tab with something new.
export function tabRow(ui: Ui, tabs: readonly Tab[], shown: TabId, seen: Record<string, number> | null | undefined, onPick: (id: TabId) => unknown) {
  const { Box, Button, Text } = ui
  return (
    <Box key="tabs" flexWrap="wrap" gap="xs" marginTop={1}>
      {[...tabs.map(t => ({ id: t.id, alert: !!t.alert, text: t.text ?? `${t.label} ${t.count}${t.id !== shown && tabIsNew(t, seen) ? ' •' : ''}` })), { id: 'all' as TabId, alert: false, text: 'All' }].map(t => (
        <Button key={'tab:' + t.id} variant={t.id === shown ? 'primary' : 'secondary'} dimColor={t.id === 'all' && shown !== 'all'}
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

export function leadPill(s: ChiefStatus, self: string | null): Pill {
  const by = typeof s.led_by === 'string' && s.led_by ? s.led_by : null
  return !by ? { text: 'nobody leading', color: 'error' }
    : by === self ? { text: 'led here', color: 'success' } : { text: 'led by another session', color: 'warning' }
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

// The title row: a plain Button whose press toggles the card; `▾` open, `▸` closed.
export function titleRow(ui: Ui, id: string, title: string, open: boolean, onPress: () => unknown, trailing?: unknown) {
  const { Box, Button, Text } = ui
  return (
    <Box key={'title:' + id} flexWrap="wrap" gap="xs">
      <Button key={'toggle:' + id} plain onPress={onPress}><Text weight="semibold">{`${open ? '▾' : '▸'} ${title}`}</Text></Button>
      {trailing as any}
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
const BANNER_ROWS_MIN = 6
const BANNER_ROWS_MAX = 14
const SPOT_COLUMNS = 10
const SPOT_ROWS = 5
const BANNER_PX = 352
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

// The picture and whether it is a banner, by the surface's name (a surface without Image still resolves one, to an
// empty stand-in). Nothing where the surface has no element for it: the title says it.
function portraitOf(ui: Ui, p: Portrait, surface: string): { el: unknown; banner: boolean } | null {
  const { Image, Svg } = ui as { Image?: any; Svg?: any }
  if (surface === 'terminal') {
    if (!Image) return null
    const { banner, columns, rows } = portraitLayout(p)
    return { banner, el: <Image key="portrait" source={{ png: p.png }} columns={columns} rows={rows} alt={PORTRAIT_ALT} /> }
  }
  if (!Svg) return null
  const wrapped = p.jpeg ? jpegSvg(p.jpeg) : ''
  if (p.jpeg && wrapped.length <= SVG_MAX) {
    const banner = portraitLayout(p.jpeg).banner
    const width = banner ? BANNER_PX : SPOT_PX
    return { banner, el: <Svg source={wrapped} alt={PORTRAIT_ALT} width={width} height={Math.round(width * p.jpeg.height / p.jpeg.width)} /> }
  }
  // no usable jpeg (a custom PNG alone cannot be drawn here): the bundled vector, as a spot
  return p.svg ? { banner: false, el: <Svg source={p.svg} alt={PORTRAIT_ALT} width={SPOT_PX} height={SPOT_PX} /> } : null
}

export type Strip = {
  needs: number; inbox: number; sessions: number
  spent: number | undefined; limit: number | undefined; overBudget: boolean
  lead: Pill; beat: string
  portrait: Portrait | null
  surface: string       // `terminal` draws an Image; any other surface an Svg when it has one
}

// The header: the picture (a banner above, or a spot at the left), "Mother" and "Chief of Stuff", then the counts as pills, the budget as
// `$1.50 / $20` with a mini bar, who leads, the heartbeat's age.
export function headerStrip(ui: Ui, s: Strip) {
  const { Box, Text } = ui
  const bar = budgetBar(s.spent, s.limit)
  const limit = typeof s.limit === 'number' ? `$${Number.isInteger(s.limit) ? s.limit : s.limit.toFixed(2)}` : '$0'
  const drawn = s.portrait ? portraitOf(ui, s.portrait, s.surface) : null
  const banner = !!drawn?.banner
  const picture = (drawn?.el ?? null) as any
  return (
    <Box key="strip-all" flexDirection="column">
    {banner && picture ? <Box key="banner" marginBottom={1}>{picture}</Box> : null}
    <Box key="strip" gap={1}>
      {banner ? null : picture}
      <Box key="strip-text" flexDirection="column" flexGrow={1}>
      <Text weight="bold" size="title" color={SELF_COLOR}>Mother</Text>
      <Text dimColor size="caption">Chief of Stuff</Text>
      <Box flexWrap="wrap" gap="xs">
        {badge(ui, `${s.needs} need you`, s.needs ? NEED_COLOR : 'subtle', 'pill:needs')}
        {badge(ui, `${s.inbox} inbox`, s.inbox ? 'permission' : 'subtle', 'pill:inbox')}
        {badge(ui, `${s.sessions} session${s.sessions === 1 ? '' : 's'}`, s.sessions ? SESSION_COLOR : 'subtle', 'pill:sessions')}
      </Box>
      <Box flexWrap="wrap" gap="xs">
        <Text key="pill:budget" color={s.overBudget ? 'error' : undefined}>{`${money(s.spent)} / ${limit}`}</Text>
        <Text key="pill:bar" color={bar.color}>{bar.text}</Text>
        {badge(ui, s.lead.text, s.lead.color, 'pill:lead')}
        <Text key="pill:beat" dimColor>{s.beat}</Text>
      </Box>
      </Box>
    </Box>
    </Box>
  )
}
