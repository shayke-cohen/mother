// Pure checks the bridge runs before every message. Run: node --test 'bridge/whatsapp/*.test.mjs'
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  checkSend, parseSent, sentLine, jitteredGap, typingMs, utcDay, sendableJid, sendCap,
} from "./checks.mjs";

const SELF = "972500000099@s.whatsapp.net";
const DM = "972500000001@s.whatsapp.net";
const GROUP = "120363000000000001@g.us";
const NOW = Date.parse("2026-10-27T12:00:00Z");

function cfg(extra = {}) {
  return { whatsapp: { enabled: true, self_jid: SELF, ...extra } };
}

const at = (msAgo) => new Date(NOW - msAgo).toISOString().replace(/\.\d{3}Z$/, "Z");

function sends(n, msAgo = 3600_000) {
  return Array.from({ length: n }, (_, i) => ({ at: at(msAgo), kind: "send", chat: DM, sha256: "x", stanza: "S" + i }));
}

const check = (args) => checkSend({ cfg: cfg(), jid: DM, text: "see you at 3", sentLines: [], now: NOW, ...args });

test("a user or group JID with text: ok; the manager's own chat too", () => {
  assert.deepEqual(check({}), { ok: true });
  assert.deepEqual(check({ jid: GROUP }), { ok: true });
  assert.deepEqual(check({ jid: SELF }), { ok: true });
});

test("only user and group JIDs are sendable", () => {
  for (const bad of ["123456789012@lid", "status@broadcast", "972500000001", "x@s.whatsapp.net", "",
                     "972500000001:3@s.whatsapp.net", null, 7]) {
    assert.equal(sendableJid(bad), false, String(bad));
    assert.deepEqual(check({ jid: bad }), { ok: false, error: "bad_request" }, String(bad));
  }
});

test("empty, blank, overlong or non-string text: bad_request", () => {
  for (const text of ["", "  \n ", "x".repeat(4097), null, 5]) {
    assert.deepEqual(check({ text }), { ok: false, error: "bad_request" }, String(text).slice(0, 10));
  }
  assert.deepEqual(check({ text: "x".repeat(4096) }), { ok: true });
});

test("sending off: off", () => {
  assert.deepEqual(check({ cfg: cfg({ enabled: false }) }), { ok: false, error: "off" });
  assert.deepEqual(check({ cfg: cfg({ send: false }) }), { ok: false, error: "off" });
  assert.deepEqual(check({ cfg: {} }), { ok: false, error: "off" });
});

test("20 sends today: cap; max_per_day lowers it, never above 50", () => {
  assert.deepEqual(check({ sentLines: sends(19) }), { ok: true });
  assert.deepEqual(check({ sentLines: sends(20) }), { ok: false, error: "cap" });
  assert.deepEqual(check({ cfg: cfg({ max_per_day: 3 }), sentLines: sends(3) }), { ok: false, error: "cap" });
  assert.deepEqual(check({ cfg: cfg({ max_per_day: 500 }), sentLines: sends(50) }), { ok: false, error: "cap" });
  assert.equal(sendCap(cfg({ max_per_day: 500 })), 50);
  assert.equal(sendCap({}), 20);
});

test("sends on an earlier UTC day don't count toward today's cap", () => {
  assert.deepEqual(check({ sentLines: sends(20, 13 * 3600_000) }), { ok: true });
});

test("old prompt lines in sent.jsonl don't count toward the cap", () => {
  const prompts = sends(30).map((r) => ({ ...r, kind: "prompt", chat: SELF }));
  assert.deepEqual(check({ sentLines: prompts.map((r) => ({ ...r, at: at(13 * 3600_000) })) }), { ok: true });
});

test("last send 2 s ago: gap, with the time left to wait", () => {
  assert.deepEqual(check({ sentLines: sends(1, 2000) }), { ok: false, error: "gap", waitMs: 3000 });
  assert.deepEqual(check({ sentLines: sends(1, 6000) }), { ok: true });
  assert.deepEqual(check({ sentLines: sends(1, 6000), gapSeconds: 6.5 }), { ok: false, error: "gap", waitMs: 500 });
  assert.deepEqual(check({ cfg: cfg({ min_gap_seconds: 0 }), sentLines: sends(1, 10) }), { ok: true });
});

test("unreadable sent.jsonl blocks with its own code: the cap can't be checked", () => {
  assert.deepEqual(check({ sentLines: null }), { ok: false, error: "sent_log_unreadable" });
});

test("parseSent: one record per line; a torn or malformed line makes it unreadable", () => {
  assert.deepEqual(parseSent(""), []);
  const l = sentLine({ at: "2026-10-27T11:00:00Z", chat: DM, sha256: "ab", stanza: "S1" });
  assert.deepEqual(parseSent(l + l), [JSON.parse(l), JSON.parse(l)]);
  assert.equal(parseSent(l + '{"at": "2026-10-27'), null);
  assert.equal(parseSent(l + "[1]\n"), null);
});

test("sentLine: exact fields, one line ending in a newline, null for a missing id", () => {
  const s = sentLine({ at: "2026-10-27T11:00:00Z", chat: DM, sha256: "ab", stanza: "S1" });
  assert.equal(s, '{"at":"2026-10-27T11:00:00Z","kind":"send","chat":"' + DM + '","sha256":"ab","stanza":"S1"}\n');
  assert.equal(s.split("\n").length, 2);
  assert.equal(JSON.parse(sentLine({ at: "x", chat: DM, sha256: "ab", stanza: "" })).stanza, null);
});

test("utcDay and the jitter helpers", () => {
  assert.equal(utcDay(Date.parse("2026-10-27T23:59:59Z")), "2026-10-27");
  for (const r of [0, 0.25, 0.5, 0.99]) {
    const g = jitteredGap(5, r);
    assert.ok(g >= 5 && g <= 6.5, String(g));  // ±30 %, never below min_gap_seconds
  }
  assert.equal(jitteredGap(0, 0.9), 0);
  assert.ok(typingMs("hi", 0) >= 80 && typingMs("hi", 0.99) < 80 + 1000);
  assert.ok(typingMs("x".repeat(1000), 0.5) >= 8000 && typingMs("x".repeat(1000), 0.99) < 9000);
});
