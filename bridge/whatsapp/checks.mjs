// Pure checks the bridge runs before every message it sends. No I/O, no clock, no randomness of their own:
// the bridge passes `now` (ms since epoch) and random numbers in, so every rule is testable without WhatsApp.
// docs/CONTRACT.md §5d. Errors: off, bad_request, sent_log_unreadable, cap, gap (with waitMs).
import { createHash } from "node:crypto";

export const MAX_PER_DAY = 20;
export const MAX_PER_DAY_CEILING = 50;
export const MIN_GAP_SECONDS = 5;
export const MAX_TEXT = 4096;
export const USER_JID_RE = /^\d{6,15}@s\.whatsapp\.net$/;
export const GROUP_JID_RE = /^\d{6,30}(-\d+)?@g\.us$/;

export const sha256 = (text) => createHash("sha256").update(text, "utf8").digest("hex");

/** YYYY-MM-DD of a time (ms) in UTC: the day the cap counts. */
export const utcDay = (ms) => new Date(ms).toISOString().slice(0, 10);

/** ISO-8601 UTC with a Z and whole seconds, as sent.jsonl writes it. */
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

function sendsToday(lines, now) {
  const day = utcDay(now);
  return lines.filter((r) => r.kind === "send" && typeof r.at === "string" && r.at.slice(0, 10) === day).length;
}

function lastAt(lines) {
  let last = null;
  for (const r of lines) {
    const t = typeof r.at === "string" ? Date.parse(r.at) : NaN;
    if (!Number.isNaN(t) && (last === null || t > last)) last = t;
  }
  return last;
}

/** whatsapp.min_gap_seconds, default 5. */
export function minGapSeconds(cfg) {
  const w = wa(cfg) || {};
  return isInt(w.min_gap_seconds) && w.min_gap_seconds >= 0 ? w.min_gap_seconds : MIN_GAP_SECONDS;
}

/** whatsapp.max_per_day, default 20, never above 50. */
export function sendCap(cfg) {
  const w = wa(cfg) || {};
  const c = isInt(w.max_per_day) && w.max_per_day >= 1 ? w.max_per_day : MAX_PER_DAY;
  return Math.min(c, MAX_PER_DAY_CEILING);
}

/** A JID the bridge may address: a user (`<digits>@s.whatsapp.net`) or a group (`<digits>@g.us`). */
export function sendableJid(jid) {
  return typeof jid === "string" && (USER_JID_RE.test(jid) || GROUP_JID_RE.test(jid));
}

/**
 * May the bridge send `text` to `jid` now? {ok: true} or {ok: false, error}.
 * cfg: workspace config; sentLines: parsed sent.jsonl (null if unreadable); now: ms; gapSeconds: the jittered
 * gap (default whatsapp.min_gap_seconds). The manager's yes was asked by the guard before the CLI called.
 */
export function checkSend({ cfg, jid, text, sentLines, now, gapSeconds }) {
  if (!sendableJid(jid)) return fail("bad_request");
  if (typeof text !== "string" || !text.trim() || text.length > MAX_TEXT) return fail("bad_request");
  if (!sendingOn(wa(cfg))) return fail("off");
  if (!Array.isArray(sentLines)) return fail("sent_log_unreadable");  // can't count today's sends: don't send
  if (sendsToday(sentLines, now) >= sendCap(cfg)) return fail("cap");
  const last = lastAt(sentLines);
  const waitMs = last === null ? 0 : Math.max(0, Math.ceil(last + (gapSeconds ?? minGapSeconds(cfg)) * 1000 - now));
  return waitMs > 0 ? { ok: false, error: "gap", waitMs } : { ok: true };
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

/** One sent.jsonl line, fields in contract order, ending in a newline. A missing or empty message id is null. */
export function sentLine({ at, chat, sha256: hash, stanza }) {
  return JSON.stringify({ at, kind: "send", chat, sha256: hash,
                          stanza: typeof stanza === "string" && stanza ? stanza : null }) + "\n";
}

/** min_gap_seconds with ±30 % jitter, never below min_gap_seconds. `r` is a random number in [0, 1). */
export function jitteredGap(seconds, r) {
  return Math.max(seconds, seconds * (0.7 + 0.6 * r));
}

/** "typing…" time before a send: min(8 s, 40 ms per character) plus up to 1 s of jitter. */
export function typingMs(text, r) {
  return Math.min(8000, 40 * [...String(text)].length) + Math.floor(r * 1000);
}
