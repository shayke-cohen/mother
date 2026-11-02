import { atom, read, update } from 'claude-code'
import type { Register, Timer } from 'claude-code'

import { ago, motherBin } from './shared'
import type { Dollar } from './shared'
import type { ChiefStatus, ChiefWorker } from '../types'

// The Chief pane: the chief's health, one card per headless worker with a chat box and take-over. Every write goes
// through `mother chief …`; the pane never edits a chief file itself.
export const chiefAtom = atom({ plugin: 'mother', key: 'chief' } as const, null)
export const chiefRoleAtom = atom({ plugin: 'mother', key: 'chiefRole' } as const, null)
export const chiefErrorAtom = atom({ plugin: 'mother', key: 'chiefError' } as const, null)

export const CHIEF_PANE = 'mother-chief'
export const CHIEF_COMMAND = 'chief'
const CONSOLE_POLL_MS = 60_000
const DEFAULT_TICK_SECONDS = 30   // config.json chief.tick_seconds, its default
const MIN_TICK_SECONDS = 5

type On = Parameters<Register>[0]
type Role = 'chief' | 'console'

// ` · chief ● N working · M queued` for the status line; ○ while the chief's heartbeat is old; '' with no status.
export function chiefStatusText(s: ChiefStatus | null, _role: Role | null): string {
  if (!s) return ''
  const workers = s.workers ?? []
  const working = workers.filter(w => w.status === 'working').length
  const queued = workers.reduce((n, w) => n + (w.queued ?? 0), 0)
  return ` · chief ${s.chief?.alive ? '●' : '○'} ${working} working · ${queued} queued`
}

// Attention order: failed, held, working, idle, then the rest (done); newest last run first within a group.
const rank = (w: ChiefWorker): number =>
  w.status === 'failed' ? 0 : w.held ? 1 : w.status === 'working' ? 2 : w.status === 'idle' ? 3 : 4
const runAt = (w: ChiefWorker): number => (w.last_run_at ? Date.parse(w.last_run_at) || 0 : 0)
export const byAttention = (ws: ChiefWorker[]): ChiefWorker[] =>
  [...ws].sort((a, b) => rank(a) - rank(b) || runAt(b) - runAt(a))

type Ran = { ok: boolean; stdout: string; stderr: string }

async function cli($: Dollar, bin: string, args: string[]): Promise<Ran> {
  try {
    const ran = await $.process.run(['python3', bin, 'chief', ...args])
    return { ok: ran.exitCode === 0, stdout: String(ran.stdout ?? ''), stderr: String(ran.stderr ?? '') }
  } catch (err) {
    return { ok: false, stdout: '', stderr: (err as Error).message ?? String(err) }
  }
}

const firstLine = (text: string): string => text.split('\n').find(l => l.trim())?.trim() ?? ''

// `mother chief status --json`. A failure keeps the last good status and sets chiefError.
async function reloadStatus($: Dollar, bin: string): Promise<void> {
  const ran = await cli($, bin, ['status', '--json'])
  let parsed: ChiefStatus | null = null
  let error: string | null = null
  if (!ran.ok) {
    error = firstLine(ran.stderr) || 'mother chief status failed'
  } else {
    try {
      const raw = JSON.parse(ran.stdout)
      if (!raw || typeof raw !== 'object' || raw.version !== 1) throw new Error('unsupported version')
      parsed = raw as ChiefStatus
    } catch (err) {
      error = `mother chief status unreadable: ${(err as Error).message}`
    }
  }
  if (parsed) await update($, chiefAtom, () => parsed)
  await update($, chiefErrorAtom, () => error)
}

// `chief` only when the CLI says so exactly (MOTHER_CHIEF_ROLE=chief in the keeper's session); anything else,
// a failure included, is a console that never ticks.
async function readRole($: Dollar, bin: string): Promise<Role> {
  const ran = await cli($, bin, ['role'])
  return ran.ok && ran.stdout.trim() === 'chief' ? 'chief' : 'console'
}

// config.json -> chief.tick_seconds, through `mother path`; the default when anything is missing.
async function tickSeconds($: Dollar, bin: string): Promise<number> {
  try {
    const ran = await $.process.run(['python3', bin, 'path'])
    const ws = String(ran.stdout ?? '').trim()
    if (ran.exitCode !== 0 || !ws) return DEFAULT_TICK_SECONDS
    const n = JSON.parse(await $.fs.read(`${ws}/config.json`))?.chief?.tick_seconds
    return typeof n === 'number' && n > 0 ? Math.max(MIN_TICK_SECONDS, n) : DEFAULT_TICK_SECONDS
  } catch {
    return DEFAULT_TICK_SECONDS
  }
}

// One chief pass: `tick --json`, a wake prompt when it names one, then the status again.
async function tickOnce($: Dollar, bin: string): Promise<void> {
  const ran = await cli($, bin, ['tick', '--json'])
  if (ran.ok) {
    let wake: unknown = null
    try { wake = JSON.parse(ran.stdout)?.wake } catch { wake = null }
    if (typeof wake === 'string' && wake.trim()) void $.prompt.submit({ text: wake }).catch(() => undefined)
  } else {
    await update($, chiefErrorAtom, () => firstLine(ran.stderr) || 'mother chief tick failed')
  }
  await reloadStatus($, bin)
}

// A pane verb: run it, toast a failure's first stderr line, then reload.
async function act($: Dollar, bin: string, args: string[]): Promise<void> {
  const ran = await cli($, bin, args)
  if (!ran.ok) $.ui.toast(firstLine(ran.stderr) || `mother chief ${args[0]} failed`)
  await reloadStatus($, bin)
}

type Ui = ReturnType<Dollar['ui']['resolve']>

function paneTree($: Dollar, ui: Ui, bin: string, s: ChiefStatus | null, error: string | null, nowMs: number) {
  const { Box, Text, Button, Input } = ui
  const heading = (text: string) => (
    <Box marginTop={1}><Text weight="semibold" size="heading">{text}</Text></Box>
  )
  if (!s) {
    return (
      <Box flexDirection="column">
        {error ? <Text color="warning">{error}</Text> : <Text dimColor>No chief status yet.</Text>}
      </Box>
    )
  }
  const beat = s.chief?.heartbeat_at ? `heartbeat ${ago(s.chief.heartbeat_at, nowMs)} ago` : 'no heartbeat'
  const money = (n: number | undefined) => `$${(n ?? 0).toFixed(2)}`
  const card = (w: ChiefWorker) => {
    const state = w.status + (w.held ? ' · held' : '') + (w.running ? ' · running' : '')
    const meta = [w.last_run_at ? `last run ${ago(w.last_run_at, nowMs)} ago` : 'never run', money(w.cost),
                  w.queued ? `${w.queued} queued` : ''].filter(Boolean).join(' · ')
    return (
      <Box key={'w-' + w.key} flexDirection="column" marginTop={1}>
        <Text weight="semibold">{w.title}</Text>
        <Text dimColor>{`${w.key} · ${state}`}</Text>
        {w.progress ? <Text wrap="wrap">{w.progress}</Text> : null}
        <Text dimColor>{meta}</Text>
        {w.held
          ? (w.running
              ? <Text color="warning">finishing a turn — wait to resume</Text>
              : <Text>{w.resume}</Text>)
          : null}
        <Box flexWrap="wrap" gap="xs">
          <Input key={'say:' + w.key} placeholder="Tell this worker…" submitLabel="send"
            onSubmit={(value: string) => { if (value.trim()) void act($, bin, ['say', w.key, '--', value]) }} />
          {w.held
            ? <Button key={'release:' + w.key} onPress={() => act($, bin, ['release', w.key])}>Hand back</Button>
            : <Button key={'hold:' + w.key} onPress={() => act($, bin, ['hold', w.key])}>Take over</Button>}
        </Box>
      </Box>
    )
  }
  const workers = byAttention(s.workers ?? [])
  return (
    <Box flexDirection="column">
      {heading('Health')}
      <Text color={s.chief?.alive ? undefined : 'warning'}>{`Chief ${s.chief?.alive ? '● alive' : '○ down'} · ${beat}`}</Text>
      <Text>{`Budget ${money(s.budget?.spent)} of ${money(s.budget?.limit)} today`}</Text>
      {s.paused ? <Text color="warning">PAUSED: tick only writes the heartbeat; no run starts.</Text> : null}
      {error ? <Text color="warning">{error}</Text> : null}
      {heading(`Workers ${workers.length}`)}
      {workers.length === 0 ? <Text dimColor>No workers yet.</Text> : null}
      {workers.map(card)}
    </Box>
  )
}

// The module's own timer; a reload of the mod starts it over, a repeated session.start replaces it.
let timer: Timer | undefined
let busy = false

// The chief's part of session.start. The engine takes one unmatched session.start hook per module, so register.tsx's
// hook calls this after its own poll. Role `chief` ticks every tick_seconds and submits the wake text; role `console`
// only reloads the status every minute, and never ticks or submits.
export async function startChief($: Dollar, configured: string): Promise<void> {
  const bin = await motherBin($, configured)
  try {
    await $.command.register({ name: CHIEF_COMMAND, description: 'Open the Chief pane: headless workers, chat, take-over' })
  } catch {
    // carry on without the command
  }
  const role = await readRole($, bin)
  await update($, chiefRoleAtom, () => role)
  await reloadStatus($, bin)
  // One pass at a time: a slow tick never overlaps the next one.
  const pass = async (work: () => Promise<void>) => {
    if (busy) return
    busy = true
    try { await work() } catch { /* the next period retries */ } finally { busy = false }
  }
  timer?.cancel()          // a repeated session.start must not stack a second timer
  if (role === 'chief') {
    const every = (await tickSeconds($, bin)) * 1000
    timer = $.clock.every(every, () => pass(() => tickOnce($, bin)))
  } else {
    timer = $.clock.every(CONSOLE_POLL_MS, () => pass(() => reloadStatus($, bin)))
  }
}

export function registerChief(on: On, configured: string): void {
  // /chief [open] opens the pane; /chief status says `mother chief status`; /chief pause|resume run the chief-only
  // pause (.mother/chief/PAUSED, which the CLI writes and removes; the guard's .mother/PAUSED is never touched).
  on('command.run', { command: CHIEF_COMMAND }, async ($, e) => {
    const bin = await motherBin($, configured)
    const verb = String(e.args ?? '').trim()
    const say = async (args: string[], fallback: string) => {
      const ran = await cli($, bin, args)
      const text = (ran.ok ? ran.stdout : ran.stderr || ran.stdout).trim()
      return text || fallback
    }
    if (verb === '' || verb === 'open') {
      const opened = await $.ui.open({ id: CHIEF_PANE, title: 'Chief' })
      if (opened.isPlaced) return { text: 'Chief pane opened.' }
      return { text: await say(['status'], "The Chief pane can't be drawn here and `mother chief status` failed.") }
    }
    if (verb === 'status') return { text: await say(['status'], '`mother chief status` failed.') }
    if (verb === 'pause' || verb === 'resume') {
      const text = await say([verb], `mother chief ${verb} done.`)
      await reloadStatus($, bin)
      return { text }
    }
    return { text: 'Usage: /chief [open | status | pause | resume]' }
  })

  on('ui.render', { component: 'Pane', requestId: CHIEF_PANE }, async ($, e) =>
    paneTree($, $.ui.resolve(e), await motherBin($, configured), await read($, chiefAtom), await read($, chiefErrorAtom), await $.clock.now()))
}
