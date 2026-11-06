import { atom, update } from 'claude-code'

import { oneLine } from './shared'
import type { Dollar } from './shared'
import { badge, card, toggled } from './pane_ui'
import type { Ui } from './pane_ui'
import { pillRow } from './skin'
import type { ChiefAccess, ChiefOpen, ChiefStatus } from '../types'

// Sign-in and network problems as Needs you cards, at the top: a source the watcher cannot read for a reason only the
// manager can fix (a sign-in or token that expired or was refused, the VPN or network, a timeout). lib/chief.py
// matches the errors against one list (ACCESS_PATTERNS) and hands the rows over as `health.access`: one per source,
// gone as soon as the watcher reads that source again. Each card: what is wrong, the error's short reason, Retry
// (coral: `mother hub check <source>`, then, when it answers, `mother hub refresh` so the watcher reads it now, then
// the status again) and How to fix (one or two lines, folded until pressed).
const openAtom = atom({ plugin: 'mother', key: 'chiefOpen' } as const, null)
const selectedAtom = atom({ plugin: 'mother', key: 'chiefSelected' } as const, null)

export const ACCESS_COLOR = 'error' as const
const CHECK_TIMEOUT_MS = 60_000
const REFRESH_TIMEOUT_MS = 120_000

export function accessRows(s: ChiefStatus | null): ChiefAccess[] {
  const rows = s?.health?.access
  return (Array.isArray(rows) ? rows : []).filter(a => !!a && typeof a.source === 'string' && (a.kind === 'signin' || a.kind === 'network'))
}

export const accessTitle = (a: ChiefAccess): string =>
  a.kind === 'signin' ? `Sign-in needed: ${oneLine(a.source, 40)}` : `Network: ${oneLine(a.source, 40)} unreachable`

const fixId = (a: ChiefAccess): string => 'fix:' + oneLine(a.source, 40)
// How to fix is folded unless pressed open (not by /chief expand: it is the card's detail, not the card)
const fixShown = (open: ChiefOpen | null, a: ChiefAccess): boolean => open?.cards?.[fixId(a)] === true

type Ran = { ok: boolean; out: string }
async function hubRun($: Dollar, bin: string, args: string[], timeoutMs: number): Promise<Ran> {
  try {
    const r = await $.process.run(['python3', bin, 'hub', ...args], { timeoutMs })
    return { ok: r.exitCode === 0, out: `${String(r.stdout ?? '')}\n${String(r.stderr ?? '')}` }
  } catch (err) {
    return { ok: false, out: (err as Error).message ?? String(err) }
  }
}

// Retry: check the one source; when it answers, ask the watcher to read every source now (so the card can go), then
// re-read the status either way. Each step says what happened in one toast.
export async function retryAccess($: Dollar, bin: string, a: ChiefAccess, reload: () => Promise<unknown>): Promise<void> {
  const src = oneLine(a.source, 40)
  await update($, selectedAtom, () => 'access:' + src)
  $.ui.toast(`Checking ${src}…`)
  const checked = await hubRun($, bin, ['check', src], CHECK_TIMEOUT_MS)
  const row = checked.out.split('\n').map(l => l.trim()).find(l => l.startsWith(src + ' |')) ?? ''
  if (!checked.ok) {
    const why = row.split('|').slice(2).join('|').trim() || checked.out.split('\n').map(l => l.trim()).find(Boolean) || 'no answer'
    $.ui.toast(`${src} still fails: ${oneLine(why, 120)}`)
    await reload()
    return
  }
  $.ui.toast(`${src} answers now; the watcher reads it again`)
  const refreshed = await hubRun($, bin, ['refresh'], REFRESH_TIMEOUT_MS)
  if (!refreshed.ok) $.ui.toast(`The watcher has not read ${src} yet; the card goes on its next pass`)
  await reload()
}

async function toggleFix($: Dollar, a: ChiefAccess, nowOpen: boolean): Promise<void> {
  await update($, openAtom, prev => toggled(prev, fixId(a), nowOpen))
}

// The cards, in the order lib/chief.py gave them; [] when nothing is wrong.
export function accessCards($: Dollar, ui: Ui, bin: string, s: ChiefStatus | null, open: ChiefOpen | null, selected: string | null,
                            reload: () => Promise<unknown>): unknown[] {
  const { Box, Text, Button } = ui
  return accessRows(s).map(a => {
    const src = oneLine(a.source, 40)
    const id = 'access:' + src
    const shown = fixShown(open, a)
    return card(ui, { id, color: ACCESS_COLOR, selected: selected === id }, [
      <Box key={'meta:' + id} {...pillRow(ui)}>
        {badge(ui, a.kind === 'signin' ? 'sign-in' : 'network', ACCESS_COLOR, 'badge:' + id)}
        <Text key={'title:' + id} color="error" weight="semibold">{accessTitle(a)}</Text>
      </Box>,
      a.reason ? <Text key={'why:' + id} dimColor wrap="wrap">{oneLine(a.reason, 120)}</Text> : null,
      <Box key={'acts:' + id} {...pillRow(ui, true)}>
        <Button key={'access-retry:' + src} variant="primary" onPress={() => retryAccess($, bin, a, reload)}>Retry</Button>
        <Button key={'access-fix:' + src} variant="secondary" onPress={() => toggleFix($, a, !shown)}>How to fix</Button>
      </Box>,
      shown ? <Text key={'fix:' + id} color="warning" wrap="wrap">{oneLine(a.help, 240)}</Text> : null,
    ])
  })
}
