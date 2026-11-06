import type { Color } from 'claude-code'

import type { Dollar } from './shared'

// The Chief pane's skin: one palette and the primitives every section draws with (Panel, Card, Pill, Badge, Ink, Dim,
// SectionHeading, Stat). Two looks:
//   dark  - the desktop app's default: one dark rounded panel, dark cards, light text, filled pills, rounded badges.
//   plain - the app's own colours and native buttons: every other surface (a terminal draws `[ label ]` buttons and
//           honours theme colours itself), and the desktop after `/chief skin plain`.
// What the desktop surface draws decides the dark constructs (measured in a live pane):
//   - the pane's body colour is the app's, so the panel is one Box with a fill;
//   - a Text with no colour is the app's default (dark in light mode): every Text in the panel gets one;
//   - a native Button wears the app's chrome and its label takes no colour: a pill is a filled rounded Box around a
//     `plain` Button, its fill picked to read under the app's own label colour (dark by default; `dark-app` for light);
//   - a hover Box needs a `key`, or the whole pane fails to draw.
// Nothing here reads state or runs an action.

type Ui = ReturnType<Dollar['ui']['resolve']>

export const PALETTE = {
  panel: '#12141c', card: '#1c2030', cardHover: '#242a40', line: '#2a2f45', ink: '#f2efe9', dim: '#9aa0b4',
  coral: '#e8704f', violet: '#7c6cff', green: '#2bb673', amber: '#e0a030', red: '#e5484d', blue: '#4c8dff',
  purple: '#a66cff', teal: '#2bb6a8', slate: '#5b6078',
} as const
const P = PALETTE

// The three looks `/chief skin` names. `dark` and `dark-app` are the same dark panel; they differ in the pills, whose
// label is the app's own text colour and takes none of ours: `dark` (the default) fills them for a dark label (the
// app in light mode), `dark-app` for a light one (the app in dark mode). Nothing the mod can read says which the
// desktop app is in (the `theme` config row is the CLI's), so the manager says it.
export type SkinMode = 'dark' | 'dark-app' | 'plain'
export type Skin = { dark: boolean; labels: 'dark' | 'light' }
export const PLAIN_SKIN: Skin = { dark: false, labels: 'dark' }
export const SKIN_MODES: readonly SkinMode[] = ['dark', 'dark-app', 'plain']
export const SKIN_STORE_KEY = 'chiefSkin'      // the plugin's store: machine-local, outlasts the session

// The skin a draw uses: dark on the desktop app unless the manager chose plain; plain everywhere else.
export function skinFor(surface: string, mode: SkinMode | null | undefined): Skin {
  return { dark: surface === 'desktop' && mode !== 'plain', labels: mode === 'dark-app' ? 'light' : 'dark' }
}

// ---- colours: the pane's theme keys as the dark skin draws them ----
const FILL: Record<string, string> = {
  claude: P.coral, planMode: P.violet, merged: P.purple, permission: P.blue, success: P.green, autoAccept: P.teal,
  suggestion: P.blue, ide: P.blue, error: P.red, warning: P.amber, subtle: P.slate, inverseText: P.panel,
}
// A fill or border colour: a theme key's hex, a hex as it is.
export const fillOf = (c: Color | string | null | undefined): string => (c && FILL[String(c)]) || (typeof c === 'string' && c.startsWith('#') ? c : P.slate)
// A text colour on the dark panel: the neutral keys read as dim, never as the dark slate.
export const inkOf = (c: Color | string | null | undefined): string => (c === 'subtle' ? P.dim : c === 'inverseText' ? P.panel : fillOf(c))
// The text a filled badge wears: dark on a bright fill, light on the neutral one.
const onFill = (fill: string): string => (fill === P.slate || fill === P.line || fill === P.card ? P.ink : P.panel)

export type PillVariant = 'primary' | 'secondary' | 'danger'
// A pill's fill reads under its label: cream and coral under a dark label, slate and a darker coral under a light one.
export function pillFills(s: Skin): Record<PillVariant, { fill: string; hover: string }> {
  return s.labels === 'light'
    ? { primary: { fill: '#c9553a', hover: P.coral }, secondary: { fill: '#3a4060', hover: '#4a5278' }, danger: { fill: '#b23a3e', hover: P.red } }
    : { primary: { fill: P.coral, hover: '#f08a6c' }, secondary: { fill: '#e9e6df', hover: '#ffffff' }, danger: { fill: '#f08a8d', hover: '#f5a3a5' } }
}

// ---- the skinned element table ----
// `skinUi` hands a section the surface's own table with Text, Button, Input and Select replaced: a Text gets a colour,
// a Button becomes a pill, an Input loses its native submit label, a Select's label is an ink Text before it. So a
// section written `<Text dimColor>` or `<Button variant="primary">` draws through Dim and Pill with no colour of its own.
const SKIN = '__skin'
const RAW = '__raw'
const NAMES = ['Box', 'Text', 'Button', 'Link', 'Input', 'Select', 'Svg', 'Image', 'Markdown'] as const

export const skinOf = (ui: Ui): Skin => ((ui as any)?.[SKIN] as Skin | undefined) ?? PLAIN_SKIN
export const rawOf = (ui: Ui): any => (ui as any)?.[RAW] ?? ui

const NB = ' '   // a pill's label is padded with these: only the label is the click target, and a plain space is trimmed

// The words of a Button's children: strings and numbers, through any nested element.
export function labelOf(children: unknown): string {
  if (children == null || typeof children === 'boolean') return ''
  if (typeof children === 'string' || typeof children === 'number') return String(children)
  if (Array.isArray(children)) return children.map(labelOf).join('')
  const el = children as { children?: unknown; props?: { children?: unknown } }
  return labelOf(el.children ?? el.props?.children)
}

// A drawn pill's label length, by element: the title row uses it to keep a compact card's first row on one line.
const PILL_LABEL = new WeakMap<object, number>()
export const pillLabel = (el: unknown): number => (el && typeof el === 'object' ? PILL_LABEL.get(el as object) ?? 0 : 0)
export const PILL_EXTRA = 6     // what a pill takes beside its label, in cells: padding, the label's two spaces, the border
export const pillCells = (el: unknown): number => (pillLabel(el) ? pillLabel(el) + PILL_EXTRA : 0)

// `tight` is a pill with no padding of its own (the label's two spaces are all of it): the tab row, seven on one line.
// `bare` has neither padding nor the spaces, for a glyph on a pill of a fixed `width` (a card's toggle).
type PillOpts = { key?: string; variant?: PillVariant; label: string; onPress: () => unknown; tight?: boolean; bare?: boolean; width?: number }
export const PILL_LABEL_MAX = 28   // a longer label is cut at a word boundary: a pill never takes half a card's row

// A label of at most `cap` characters, cut at the last word boundary that fits, ending in `…`.
export function clipLabel(text: string, cap = PILL_LABEL_MAX): string {
  const t = text.split(/\s+/).filter(Boolean).join(' ')
  if (t.length <= cap) return t
  const head = t.slice(0, Math.max(1, cap - 1))
  const at = head.lastIndexOf(' ')
  return (at > 0 ? head.slice(0, at) : head).replace(/[\s.,;:!?-]+$/, '') + '…'
}

function darkPill(raw: any, skin: Skin, o: PillOpts) {
  const { Box, Button } = raw
  const c = pillFills(skin)[o.variant ?? 'secondary']
  const pad = o.tight || o.bare ? 0 : 1
  const label = o.bare ? o.label : clipLabel(o.label)
  // every hover Box is keyed: one without a key makes the whole pane fail to draw
  const hover = o.key ? { key: 'pill:' + o.key, hover: { backgroundColor: c.hover, borderColor: c.hover } } : {}
  const size = o.width ? { width: o.width, justifyContent: 'center' as const } : {}
  const el = (
    <Box {...hover} {...size} borderStyle="round" borderColor={c.fill} backgroundColor={c.fill} paddingX={pad} paddingY={0} height={1} flexShrink={0}>
      <Button key={o.key} plain onPress={o.onPress}>{o.bare ? label : NB + label + NB}</Button>
    </Box>
  )
  if (el && typeof el === 'object') PILL_LABEL.set(el as object, label.length)
  return el
}

export function skinUi(ui: Ui, skin: Skin): Ui {
  if (!skin.dark) return ui
  const raw: any = {}
  for (const n of NAMES) raw[n] = (ui as any)[n]
  const out: any = { ...raw, [SKIN]: skin, [RAW]: raw }
  out.Text = (props: any) => {
    const { color, backgroundColor, dimColor, ...rest } = props ?? {}
    if (backgroundColor) {
      const fill = fillOf(backgroundColor)
      return raw.Text({ ...rest, backgroundColor: fill, color: onFill(fill) })
    }
    // no colour of its own: ink, or dim (`dimColor` alone is invisible here); a coloured one keeps its dim
    return color ? raw.Text({ ...rest, color: inkOf(color), ...(dimColor ? { dimColor: true } : {}) })
      : raw.Text({ ...rest, color: dimColor ? P.dim : P.ink })
  }
  out.Button = (props: any) => darkPill(raw, skin, {
    key: props?.key, label: labelOf(props?.children), onPress: props?.onPress,
    variant: props?.variant === 'primary' ? 'primary' : props?.variant === 'danger' ? 'danger' : 'secondary',
  })
  // the field's own submit button is dark on dark: the caller puts a pill beside the field
  out.Input = (props: any) => { const { submitLabel: _, ...rest } = props ?? {}; return raw.Input(rest) }
  // the dropdown's own label is dark on dark: an ink Text before it
  out.Select = (props: any) => {
    const { label, ...rest } = props ?? {}
    const { Box } = raw
    if (!label) return raw.Select(rest)
    return <Box gap={1} alignItems="center">{out.Text({ children: label })}{raw.Select(rest)}</Box>
  }
  return out as Ui
}

// ---- primitives ----
// The whole pane's content: one dark rounded Box on the dark skin, a plain column otherwise.
export function Panel(ui: Ui, children: unknown) {
  const { Box } = rawOf(ui)
  if (!skinOf(ui).dark) return <Box flexDirection="column">{children as any}</Box>
  return (
    <Box key="panel" flexDirection="column" borderStyle="round" borderColor={P.line} backgroundColor={P.panel} padding={1}>
      {children as any}
    </Box>
  )
}

export type CardFrame = {
  id: string            // the card's id: `<kind>:<item>`; the Box is keyed `card:<id>`
  color: Color          // the kind's border colour
  selected: boolean     // the last card a button was pressed on: a bold border
  active?: boolean      // this session's own card: the accent colour
  scope?: string        // a hover group shared with another card of the same session
}

// A bordered card with padding. Plain: at rest the border is dim and rounded; selected or hovered it is bold and lit.
// Dark: a filled card with the kind's border; selected is a bold coral border, this session's a coral one; hovered,
// the fill lifts. A hover anywhere in its `scope` lights every card of that group.
export function Card(ui: Ui, f: CardFrame, children: unknown) {
  const { Box } = rawOf(ui)
  const scope = f.scope ? { scope: f.scope } : {}
  if (skinOf(ui).dark) {
    const border = f.selected || f.active ? P.coral : fillOf(f.color)
    return (
      <Box key={'card:' + f.id} flexDirection="column" marginTop={1} paddingX={1} backgroundColor={P.card}
        borderStyle={f.selected ? 'bold' : 'round'} borderColor={border} hover={{ backgroundColor: P.cardHover, borderColor: border, ...scope }}>
        {children as any}
      </Box>
    )
  }
  const color = f.active ? 'claude' : f.color
  const lit = f.selected || !!f.active
  return (
    <Box key={'card:' + f.id} flexDirection="column" marginTop={1} paddingX={1}
      borderStyle={f.selected ? 'bold' : 'round'} borderColor={color} borderDimColor={!lit} hover={{ borderStyle: 'bold', borderDimColor: false, ...scope }}>
      {children as any}
    </Box>
  )
}

// A row of pills (and the fields beside them): it wraps, and on the dark skin wrapped lines keep a gap so they
// never touch; `apart` also keeps it a row away from what is above it (one action row under another).
export function pillRow(ui: Ui, apart = false): Record<string, unknown> {
  return skinOf(ui).dark ? { flexWrap: 'wrap', gap: 1, rowGap: 1, alignItems: 'center', ...(apart ? { marginTop: 1 } : {}) }
    : { flexWrap: 'wrap', gap: 'xs', alignItems: 'center' }
}

// A button: a filled rounded pill on the dark skin (coral for the recommended action and the shown tab, quieter for
// the rest, red for danger), the surface's own Button otherwise.
export function Pill(ui: Ui, o: PillOpts) {
  if (skinOf(ui).dark) return darkPill(rawOf(ui), skinOf(ui), o)
  const { Button } = ui
  return <Button key={o.key as string} variant={o.variant === 'primary' ? 'primary' : 'secondary'} onPress={o.onPress}>{o.label}</Button>
}

// A small filled badge: a card's source, kind or state, who leads. `dim` is a quiet word with no fill.
export function Badge(ui: Ui, o: { tone: Color; text: string; key?: string; dim?: boolean }) {
  const { Text } = ui
  if (o.dim) return <Text key={o.key} dimColor>{o.text}</Text>
  if (!skinOf(ui).dark) return <Text key={o.key} backgroundColor={o.tone} color="inverseText" weight="semibold">{` ${o.text} `}</Text>
  const { Box, Text: Raw } = rawOf(ui)
  const fill = fillOf(o.tone)
  return (
    <Box borderStyle="round" borderColor={fill} backgroundColor={fill} paddingX={1} paddingY={0} height={1} flexShrink={0}>
      <Raw key={o.key} color={onFill(fill)} size="footnote" weight="semibold">{o.text}</Raw>
    </Box>
  )
}

type TextMore = Record<string, unknown>
// Body text, and its quiet form. (On the dark skin the table's Text gives each its colour.)
export function Ink(ui: Ui, text: string, more: TextMore = {}) {
  const { Text } = ui
  return <Text {...(more as any)}>{text}</Text>
}
export function Dim(ui: Ui, text: string, more: TextMore = {}) {
  const { Text } = ui
  return <Text dimColor {...(more as any)}>{text}</Text>
}

// A heading above a group, with an optional count. Dark: dim footnote text. `level` picks the plain look: a section's
// heading (`section`), a small dim one inside a tab (`sub`), or a group's name (`group`).
export function SectionHeading(ui: Ui, o: { key: string; text: string; count?: number; level?: 'section' | 'sub' | 'group'; icon?: unknown }) {
  const { Box, Text } = ui
  const dark = skinOf(ui).dark
  const look = dark ? { dimColor: true, weight: 'semibold', size: 'footnote' }
    : o.level === 'section' ? { weight: 'semibold', size: 'heading' } : o.level === 'sub' ? { weight: 'semibold', dimColor: true } : { weight: 'semibold' }
  const words = o.count !== undefined
    ? <Text {...(look as any)}>{o.text}<Text dimColor weight="regular">{` ${o.count}`}</Text></Text>
    : <Text {...(look as any)}>{o.text}</Text>
  if (!dark) return <Box key={o.key} marginTop={1}>{words}</Box>
  return <Box key={o.key} marginTop={1} gap={1} alignItems="center">{o.icon as any}{words}</Box>
}

// One dim line where a section has nothing: never a blank.
export function Empty(ui: Ui, key: string, text: string) {
  const { Box, Text } = ui
  return <Box key={key} marginTop={1}><Text dimColor {...(skinOf(ui).dark ? { size: 'footnote' as const } : {})}>{text}</Text></Box>
}

// A value with a quiet label after it: `$1.50 / $20`, `last poll 40s ago · outside active hours`.
export function Stat(ui: Ui, o: { key?: string; value: string; label?: string; tone?: Color }) {
  const { Text } = ui
  if (!o.label) return <Text key={o.key} color={o.tone}>{o.value}</Text>
  return <Text key={o.key} color={o.tone}>{o.value}<Text dimColor>{` ${o.label}`}</Text></Text>
}

// ---- small pictures (a surface with Svg; sizes in px) ----
const svg = (body: string, box = '0 0 16 16'): string => `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${box}">${body}</svg>`
const STROKE = 'fill="none" stroke-linecap="round" stroke-linejoin="round"'
// A 16 px mark per source, hard-coded, each under 300 bytes.
export const ICONS: Record<string, string> = {
  chat: svg(`<path d="M2 3h12v8H7l-3 3v-3H2z" fill="${P.violet}"/>`),
  mail: svg(`<rect x="1.5" y="3" width="13" height="10" rx="2" fill="${P.purple}"/><path d="M2.5 4.5l5.5 4 5.5-4" stroke="${P.panel}" stroke-width="1.4" ${STROKE}/>`),
  code: svg(`<path d="M6 4L2 8l4 4M10 4l4 4-4 4" stroke="${P.green}" stroke-width="2" ${STROKE}/>`),
  tracker: svg(`<rect x="2" y="2" width="12" height="12" rx="3" fill="${P.blue}"/><path d="M5 8.2l2 2 4-4.4" stroke="${P.panel}" stroke-width="1.6" ${STROKE}/>`),
  calendar: svg(`<rect x="2" y="3" width="12" height="11" rx="2" fill="${P.teal}"/><path d="M2 6.5h12M5.5 1.5v3M10.5 1.5v3" stroke="${P.panel}" stroke-width="1.4" ${STROKE}/>`),
}
export const ICON_PX = 14

// The mark before a source badge or a heading, on the dark skin where the surface has Svg; null otherwise.
export function Icon(ui: Ui, name: string, key?: string) {
  const { Svg } = rawOf(ui) as { Svg?: any }
  if (!skinOf(ui).dark || !Svg || !ICONS[name]) return null
  return <Svg key={key} source={ICONS[name]} alt={name} width={ICON_PX} height={ICON_PX} />
}

export const BAR_W = 120
export const BAR_H = 8
// Today's spend against the limit as a bar: coral on slate (red once it is spent).
export const barSvg = (ratio: number): string => {
  const w = Math.round(Math.min(1, Math.max(0, ratio)) * BAR_W)
  return svg(`<rect width="${BAR_W}" height="${BAR_H}" rx="4" fill="${P.slate}"/>` +
    (w ? `<rect width="${Math.max(w, BAR_H)}" height="${BAR_H}" rx="4" fill="${ratio >= 1 ? P.red : P.coral}"/>` : ''), `0 0 ${BAR_W} ${BAR_H}`)
}
