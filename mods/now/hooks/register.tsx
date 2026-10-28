import { atom, read, update } from 'claude-code'
import type { Hook, Register, Timer } from 'claude-code'

import type { NowCoverage, NowDoc, NowItem, NowTop } from '../types'

// State the Now panel (Task 9) draws from. Keys are declared in types/index.d.ts.
export const docAtom = atom({ plugin: 'mother-now', key: 'doc' } as const, null)
export const errorAtom = atom({ plugin: 'mother-now', key: 'error' } as const, null)
export const workspaceAtom = atom({ plugin: 'mother-now', key: 'workspace' } as const, null)
export const snoozingAtom = atom({ plugin: 'mother-now', key: 'snoozing' } as const, null)
export const selectedAtom = atom({ plugin: 'mother-now', key: 'selected' } as const, null)
export const expandedAtom = atom({ plugin: 'mother-now', key: 'expanded' } as const, null)

const PANE = 'mother-now'
const SNOOZES = ['1h', 'tomorrow', 'next-week'] as const
const TOP_MAX = 3

export const POLL_MS = 60_000
export const TOASTED_CAP = 500
const DEFAULT_BIN = '~/.claude/plugins/marketplaces/mother/bin/mother'

// The engine interface a hook receives; the helpers below take it so Task 9 can pass its own `$`.
export type Dollar = Parameters<Hook<'session.start'>>[0]

// What `load` remembers between polls: the mtime it last read, so an unchanged file is not re-read.
export type Loader = { mtimeMs: number | null; statFailed: boolean }
export const newLoader = (): Loader => ({ mtimeMs: null, statFailed: false })

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

export async function motherBin($: Dollar, configured: string): Promise<string> {
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
  }
  $.ui.status(statusText(await read($, docAtom), await $.clock.now()))
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

// ---- writes ----------------------------------------------------------------------------------------

// Run one `mother now …` verb, then re-read now.json at once. A failure toasts the first line of stderr.
async function run($: Dollar, bin: string, loader: Loader, args: string[], rowId: string | null = null): Promise<void> {
  try {
    const ran = await $.process.run(['python3', bin, 'now', ...args])
    if (ran.exitCode !== 0) {
      const line = String(ran.stderr ?? '').split('\n').find(l => l.trim()) ?? `mother now ${args[0]} failed`
      $.ui.toast(line.trim())
    }
  } catch (err) {
    $.ui.toast(`mother now ${args[0]} failed: ${(err as Error).message ?? String(err)}`)
  }
  await load($, bin, loader).catch(() => undefined)
  if (rowId) await update($, snoozingAtom, cur => (cur === rowId ? null : cur))
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
  if (!key || key.startsWith('snooze-pick-')) return null
  const top = /^top-(?:row|done|doit|ask)-(\d+)$/.exec(key)
  if (top) return 'top:' + top[1]
  return /^(?:row|done|snooze|maketop|doit|ask|act\d+)-(.+)$/.exec(key)?.[1] ?? null
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

// ---- the pane --------------------------------------------------------------------------------------

type Ui = ReturnType<Dollar['ui']['resolve']>

// A link the surface can open; a workspace path (`teams/x/intake.md#1`) stays in the Ask prompt instead.
const isUrl = (link: string | null | undefined): link is string => !!link && /^https?:\/\//.test(link)

// Rows a section draws before "Show all"; the rest wait behind the toggle.
const LIMITS: Record<NowItem['section'], number> = { on_you: 5, neglected: 3, suggestion: 3 }
const SECTIONS: [NowItem['section'], string][] = [['on_you', 'On you'], ['neglected', 'Neglected'], ['suggestion', 'Suggestions']]

function paneTree($: Dollar, ui: Ui, doc: NowDoc | null, error: string | null, snoozing: string | null, selected: string | null,
                  expandedList: string[] | null, nowMs: number, go: (args: string[], rowId?: string | null) => Promise<void>) {
  const { Box, Text, Button, Link, Select } = ui
  if (!doc) {
    return error
      ? <Text color="warning">Couldn't read now.json.</Text>
      : <Text dimColor>Tick runs on the home machine; nothing here yet.</Text>
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

  const row = (i: NowItem) => {
    const open = i.id === current
    const meta = [i.team, age(i.since, nowMs)].filter(Boolean).join(' · ')
    return (
      <Box key={'item-' + i.id} flexDirection="column">
        <Box>
          <Button key={'row-' + i.id} plain dimColor={!open} autoFocus={i.id === rows[0] ? true : undefined}
            onPress={() => select(i.id)}>{(open ? '▾ ' : '▸ ') + (inTop.has(i.id) ? '★ ' : '') + i.title}</Button>
          <Text dimColor>{' ' + meta + ' '}</Text>
          {isUrl(i.link) ? <Link href={i.link}>↗</Link> : null}
          {i.unverified_since ? <Text color="warning">{' unverified'}</Text> : null}
        </Box>
        {open ? (
          <Box flexDirection="column" paddingLeft={2}>
            {i.why ? <Text dimColor wrap="wrap">{i.why}</Text> : null}
            <Box flexWrap="wrap" gap="xs">
              {(i.actions ?? []).map((a, n) => (
                <Button key={n === 0 ? 'doit-' + i.id : `act${n}-` + i.id} variant={n === 0 ? 'primary' : undefined}
                  hotkey={n === 0 ? hot(i.id, 'x') : undefined}
                  onPress={() => fill(a.prompt || `mother now do ${a.id}`)}>{n === 0 && !a.label ? 'Do it' : a.label || a.id}</Button>
              ))}
              <Button key={'ask-' + i.id} hotkey={hot(i.id, 'a')} onPress={() => fill(askPrompt(i.title, i.why, i.link))}>Ask Mother</Button>
              <Button key={'done-' + i.id} hotkey={hot(i.id, 'd')} onPress={() => go(['done', i.id], i.id)}>Done</Button>
              <Button key={'snooze-' + i.id} hotkey={hot(i.id, 's')} onPress={() => update($, snoozingAtom, cur => (cur === i.id ? null : i.id))}>Snooze</Button>
              {rank !== null && !inTop.has(i.id)
                ? <Button key={'maketop-' + i.id} hotkey={hot(i.id, 't')} onPress={() => go(['top', 'swap', String(rank), i.id], i.id)}>Make top 3</Button>
                : null}
            </Box>
            {snoozing === i.id
              ? <Select key={'snooze-pick-' + i.id} label="Snooze for" options={SNOOZES.map(value => ({ value }))}
                  onSelect={value => go(['snooze', i.id, value], i.id)} />
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
    return (
      <Box key={kind} flexDirection="column">
        {heading(name, all.length)}
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
      {noItems && health.length === 0 && top.length === 0
        ? <Text>{`Nothing on you. Next check ${at(doc.next_at)}.`}</Text> : null}
    </Box>
  )
}

export const COMMAND = 'now-panel'

export const register: Register = (on, options) => {
  const configured = String(options.mother_bin ?? DEFAULT_BIN)
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

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const bin = await motherBin($, configured)
    const go = (args: string[], rowId: string | null = null) => run($, bin, loader, args, rowId)
    return paneTree($, $.ui.resolve(e), await read($, docAtom), await read($, errorAtom), await read($, snoozingAtom),
      await read($, selectedAtom), await read($, expandedAtom), await $.clock.now(), go)
  })
}
