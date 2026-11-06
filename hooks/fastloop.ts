import type { Dollar } from './shared'
import type { ChiefStatus } from '../types'

// The pane's fast refresh. The console pass runs every minute and spawns Python for each read; while the Chief pane
// is shown, this loop re-reads the cheap files every FAST_MS with `$.fs` instead:
//   .mother/chief/status.json     the status the minute pass saved (`mother chief status --json --save`)
//   .mother/chief/heartbeat.json  who leads, and since when
//   .mother/floor/*.json          the sessions' cards (only listed here: a change re-reads them through the CLI,
//                                 which validates them and computes each ask id)
// Each file's size and mtime make a signature; a read whose signatures all match the last does nothing, so the pane
// redraws only when a file changed. No model call, no process unless a floor card changed. The loop stops itself
// when the pane is not shown, and the minute pass or `/chief open` starts it again.
export const FAST_MS = 5_000
export const STATUS_FILE = '.mother/chief/status.json'
export const BEAT_FILE = '.mother/chief/heartbeat.json'
export const FLOOR_DIR = '.mother/floor'
const ALIVE_MS = 120_000     // lib/chief.py ALIVE_SECONDS: a heartbeat this fresh leads

export type Sigs = { status: string; beat: string; floor: string }
export const NO_SIGS: Sigs = { status: '', beat: '', floor: '' }

async function statSig($: Dollar, path: string): Promise<string> {
  try {
    const st = await $.fs.stat(path)
    return st.kind === 'file' ? `${st.size}:${st.mtimeMs}` : ''
  } catch {
    return ''   // missing: an empty signature, so its appearing later is a change
  }
}

async function floorSig($: Dollar, dir: string): Promise<string> {
  try {
    const rows = await $.fs.list(dir)
    return rows.filter(e => e.kind === 'file' && e.name.endsWith('.json'))
      .map(e => `${e.name}:${e.size}:${e.mtimeMs}`).sort().join('|')
  } catch {
    return ''
  }
}

export async function readSigs($: Dollar, ws: string): Promise<Sigs> {
  return { status: await statSig($, `${ws}/${STATUS_FILE}`), beat: await statSig($, `${ws}/${BEAT_FILE}`),
           floor: await floorSig($, `${ws}/${FLOOR_DIR}`) }
}

async function readJson($: Dollar, path: string): Promise<any> {
  try { return JSON.parse(String(await $.fs.read(path))) } catch { return null }
}

// The saved status, when it is one (version 1); null otherwise, so a torn or odd file changes nothing.
export async function readStatusFile($: Dollar, ws: string): Promise<ChiefStatus | null> {
  const doc = await readJson($, `${ws}/${STATUS_FILE}`)
  return doc && typeof doc === 'object' && doc.version === 1 ? (doc as ChiefStatus) : null
}

// The status with the heartbeat file's word on who leads: the heartbeat moves every pass of the leader, the saved
// status only when some session's minute pass writes it.
export function withBeat(s: ChiefStatus, beat: any, nowMs: number): ChiefStatus {
  if (!beat || typeof beat !== 'object' || typeof beat.at !== 'string') return s
  const at = Date.parse(beat.at)
  if (Number.isNaN(at)) return s
  const alive = nowMs - at < ALIVE_MS
  const who = typeof beat.session_id === 'string' && beat.session_id ? beat.session_id : null
  return { ...s, chief: { alive, heartbeat_at: beat.at }, led_by: alive ? who : null }
}

export async function readBeat($: Dollar, ws: string): Promise<any> {
  return readJson($, `${ws}/${BEAT_FILE}`)
}

// What changed since `last`: which of the three to re-read.
export function changed(last: Sigs, now: Sigs): { status: boolean; beat: boolean; floor: boolean; any: boolean } {
  const status = last.status !== now.status, beat = last.beat !== now.beat, floor = last.floor !== now.floor
  return { status, beat, floor, any: status || beat || floor }
}
