// Pure checks the bridge runs before every message it sends. No I/O, no clock, no randomness of their own:
// the bridge passes `now` (ms since epoch) and random numbers in, so every rule is testable without WhatsApp.
// docs/CONTRACT.md §5d. Errors: not_allowlisted, no_approval, hash_mismatch, cap, gap (with waitMs), no_draft,
// already_sent, off, bad_request.
import { createHash } from "node:crypto";

export const MAX_PER_DAY = 20;
export const MAX_PER_DAY_CEILING = 50;
export const MIN_GAP_SECONDS = 5;
export const MAX_PROMPTS_PER_DAY = 10;
export const MAX_TEXT = 4096;

export const sha256 = (text) => createHash("sha256").update(text, "utf8").digest("hex");

/** YYYY-MM-DD of a time (ms) in UTC: the day the caps count, the same day the guard counts. */
export const utcDay = (ms) => new Date(ms).toISOString().slice(0, 10);

/** ISO-8601 UTC with a Z and whole seconds, as sent.jsonl and the guard write it. */
export const isoZ = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");

const fail = (error) => ({ ok: false, error });
const isInt = (v) => Number.isInteger(v) && typeof v !== "boolean";

function wa(cfg) {
  const w = cfg && typeof cfg === "object" ? cfg.whatsapp : null;
  return w && typeof w === "object" && !Array.isArray(w) ? w : null;
}

/** Sending is on only when whatsapp.enabled is true and whatsapp.send isn't false. */
function sendingOn(w) {
  return !!w && w.enabled === true && (w.send === undefined || w.send === true);
}

function count(lines, kind, now) {
  const day = utcDay(now);
  return lines.filter((r) => r.kind === kind && typeof r.at === "string" && r.at.slice(0, 10) === day).length;
}

function lastAt(lines) {
  let last = null;
  for (const r of lines) {
    const t = typeof r.at === "string" ? Date.parse(r.at) : NaN;
    if (!Number.isNaN(t) && (last === null || t > last)) last = t;
  }
  return last;
}

/** ms still to wait before the next message may go (0 = none). */
function gapLeft(lines, now, gapSeconds) {
  const last = lastAt(lines);
  return last === null ? 0 : Math.max(0, Math.ceil(last + gapSeconds * 1000 - now));
}

function gapCheck(lines, now, gapSeconds) {
  const waitMs = gapLeft(lines, now, gapSeconds);
  return waitMs > 0 ? { ok: false, error: "gap", waitMs } : null;
}

// The manager's approval reply (lib/whatsapp.py REPLY_RE): `ok <id>…`, `ok all`, `no <id>…`.
const REPLY_RE = /^(ok|no)((?:[ \t]+w[a-z0-9]+)+|[ \t]+all)$/i;

/** True when the text, or any line of it, would read as the manager's `ok`/`no` reply. */
export function looksLikeReply(text) {
  return [String(text), ...String(text).split(/\r?\n/)].some((l) => REPLY_RE.test(l.trim()));
}

/** whatsapp.min_gap_seconds, default 5. */
export function minGapSeconds(cfg) {
  const w = wa(cfg) || {};
  return isInt(w.min_gap_seconds) && w.min_gap_seconds >= 0 ? w.min_gap_seconds : MIN_GAP_SECONDS;
}

function sendCap(w) {
  const c = isInt(w.max_per_day) && w.max_per_day >= 1 ? w.max_per_day : MAX_PER_DAY;
  return Math.min(c, MAX_PER_DAY_CEILING);
}

/**
 * May the bridge send this draft now? {ok: true} or {ok: false, error}.
 * cfg: workspace config; draft: drafts/<id>.json; approval: approved/<id>.json (null if missing);
 * sentLines: parsed sent.jsonl (null if unreadable); now: ms; gapSeconds: the jittered gap (default
 * whatsapp.min_gap_seconds). The yes itself is re-proved separately (`mother whatsapp verify`).
 */
export function checkSend({ cfg, draft, approval, sentLines, now, gapSeconds }) {
  const w = wa(cfg);
  if (!sendingOn(w)) return fail("off");
  if (!draft || typeof draft !== "object" || typeof draft.id !== "string" || typeof draft.chat !== "string"
      || typeof draft.text !== "string") return fail("no_draft");
  const allowed = new Set((Array.isArray(w.allow) ? w.allow : []).filter((a) => a && typeof a === "object")
    .map((a) => a.jid));
  if (!allowed.has(draft.chat) || draft.chat === w.self_jid) return fail("not_allowlisted");
  if (!approval || typeof approval !== "object" || approval.id !== draft.id || approval.chat !== draft.chat) {
    return fail("no_approval");
  }
  const sha = sha256(draft.text);
  if (draft.sha256 !== sha || approval.sha256 !== sha) return fail("hash_mismatch");
  if (!Array.isArray(sentLines)) return fail("cap");  // can't count today's sends: don't send
  if (sentLines.some((r) => r.kind === "send" && r.draft === draft.id && r.sha256 === sha)) {
    return fail("already_sent");
  }
  if (count(sentLines, "send", now) >= sendCap(w)) return fail("cap");
  return gapCheck(sentLines, now, gapSeconds ?? minGapSeconds(cfg)) || { ok: true };
}

/**
 * May the bridge send this approval prompt to the manager's own chat now? {ok: true, chat: self_jid} or
 * {ok: false, error}. Prompts count toward max_prompts_per_day only, never toward max_per_day.
 */
export function checkPrompt({ cfg, text, sentLines, now, gapSeconds }) {
  const w = wa(cfg);
  if (!sendingOn(w)) return fail("off");
  if (typeof text !== "string" || !text.trim() || text.length > MAX_TEXT || looksLikeReply(text)) {
    return fail("bad_request");  // a prompt must never read as the manager's own yes or no
  }
  if (typeof w.self_jid !== "string" || !/^\d+@s\.whatsapp\.net$/.test(w.self_jid)) return fail("not_allowlisted");
  if (!Array.isArray(sentLines)) return fail("cap");
  const cap = isInt(w.max_prompts_per_day) && w.max_prompts_per_day >= 1 ? w.max_prompts_per_day : MAX_PROMPTS_PER_DAY;
  if (count(sentLines, "prompt", now) >= cap) return fail("cap");
  return gapCheck(sentLines, now, gapSeconds ?? minGapSeconds(cfg)) || { ok: true, chat: w.self_jid };
}

/** sent.jsonl text → records; null when any non-empty line is not a JSON object (a torn write included). */
export function parseSent(text) {
  const out = [];
  for (const line of String(text).split("\n")) {
    if (!line.trim()) continue;
    let r;
    try {
      r = JSON.parse(line);
    } catch {
      return null;
    }
    if (!r || typeof r !== "object" || Array.isArray(r)) return null;
    out.push(r);
  }
  return out;
}

/** One sent.jsonl line, fields in contract order, ending in a newline. `draft` only for a send. A missing or
 * empty stanza id is written as null (readers skip such a line rather than treat the file as malformed). */
export function sentLine({ at, kind, chat, draft, sha256: hash, stanza }) {
  const r = { at, kind, chat };
  if (kind === "send") r.draft = draft;
  r.sha256 = hash;
  r.stanza = typeof stanza === "string" && stanza ? stanza : null;
  return JSON.stringify(r) + "\n";
}

/** min_gap_seconds with ±30 % jitter, never below min_gap_seconds. `r` is a random number in [0, 1). */
export function jitteredGap(seconds, r) {
  return Math.max(seconds, seconds * (0.7 + 0.6 * r));
}

/** "typing…" time before a send: min(8 s, 40 ms per character) plus up to 1 s of jitter. */
export function typingMs(text, r) {
  return Math.min(8000, 40 * [...String(text)].length) + Math.floor(r * 1000);
}
