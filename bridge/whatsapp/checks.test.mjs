// Pure checks the bridge runs before every message. Run: node --test bridge/whatsapp
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  checkSend, checkPrompt, parseSent, sentLine, jitteredGap, typingMs, utcDay,
} from "./checks.mjs";

const SELF = "972500000099@s.whatsapp.net";
const DM = "972500000001@s.whatsapp.net";
const GROUP = "120363000000000001@g.us";
const OTHER = "972500000002@s.whatsapp.net";
const NOW = Date.parse("2026-10-27T12:00:00Z");
const sha = (t) => createHash("sha256").update(t, "utf8").digest("hex");

function cfg(extra = {}) {
  return {
    whatsapp: {
      enabled: true, self_jid: SELF,
      allow: [{ jid: DM, name: "Dana" }, { jid: GROUP, name: "PLAT team", group: true }],
      ...extra,
    },
  };
}

function draft(text = "see you at 3", chat = DM) {
  return { id: "w3kx", chat, chat_name: "Dana", text, sha256: sha(text), created: "2026-10-27T10:00:00Z" };
}

function approval(d = draft(), extra = {}) {
  return { id: d.id, chat: d.chat, sha256: d.sha256, path: "session", evidence: "session:u1",
           at: "2026-10-27T11:00:00Z", ...extra };
}

const at = (msAgo) => new Date(NOW - msAgo).toISOString().replace(/\.\d{3}Z$/, "Z");

function sends(n, kind = "send", msAgo = 3600_000) {
  return Array.from({ length: n }, (_, i) => ({
    at: at(msAgo), kind, chat: kind === "send" ? DM : SELF, draft: "w" + i, sha256: "x", stanza: "S" + i,
  }));
}

const ok = (args) => checkSend({ cfg: cfg(), draft: draft(), approval: approval(), sentLines: [], now: NOW, ...args });

test("allowlisted chat, approval and matching hash: ok", () => {
  assert.deepEqual(ok({}), { ok: true });
  const g = draft("ship it", GROUP);
  assert.deepEqual(ok({ draft: g, approval: approval(g) }), { ok: true });
});

test("a chat not in the allowlist: not_allowlisted", () => {
  const d = draft("hi", OTHER);
  assert.deepEqual(ok({ draft: d, approval: approval(d) }), { ok: false, error: "not_allowlisted" });
});

test("the self chat is never a send target", () => {
  const c = cfg();
  c.whatsapp.allow.push({ jid: SELF, name: "Me" });
  const d = draft("hi", SELF);
  assert.deepEqual(ok({ cfg: c, draft: d, approval: approval(d) }), { ok: false, error: "not_allowlisted" });
});

test("no approval record: no_approval", () => {
  assert.deepEqual(ok({ approval: null }), { ok: false, error: "no_approval" });
  assert.deepEqual(ok({ approval: approval(draft(), { id: "w9pa" }) }), { ok: false, error: "no_approval" });
  assert.deepEqual(ok({ approval: approval(draft(), { chat: GROUP }) }), { ok: false, error: "no_approval" });
  assert.deepEqual(ok({ approval: "nope" }), { ok: false, error: "no_approval" });
});

test("draft text edited after approval: hash_mismatch (Review Focus 3)", () => {
  const a = approval();
  const edited = draft("see you at 4");  // re-hashed consistently, but not what was approved
  assert.deepEqual(ok({ draft: edited, approval: a }), { ok: false, error: "hash_mismatch" });
  const sneaky = { ...draft(), text: "see you at 4" };  // text edited, stored hash left alone
  assert.deepEqual(ok({ draft: sneaky, approval: a }), { ok: false, error: "hash_mismatch" });
  assert.deepEqual(ok({ approval: approval(draft(), { sha256: undefined }) }), { ok: false, error: "hash_mismatch" });
});

test("a missing or malformed draft: no_draft", () => {
  assert.deepEqual(ok({ draft: null }), { ok: false, error: "no_draft" });
  assert.deepEqual(ok({ draft: { id: "w3kx", chat: DM } }), { ok: false, error: "no_draft" });
});

test("20 sends today: cap", () => {
  assert.deepEqual(ok({ sentLines: sends(19) }), { ok: true });
  assert.deepEqual(ok({ sentLines: sends(20) }), { ok: false, error: "cap" });
  const lower = cfg({ max_per_day: 3 });
  assert.deepEqual(ok({ cfg: lower, sentLines: sends(3) }), { ok: false, error: "cap" });
  const over = cfg({ max_per_day: 500 });  // never above the ceiling of 50
  assert.deepEqual(ok({ cfg: over, sentLines: sends(50) }), { ok: false, error: "cap" });
});

test("sends on an earlier UTC day don't count toward today's cap", () => {
  assert.deepEqual(ok({ sentLines: sends(20, "send", 13 * 3600_000) }), { ok: true });
});

test("prompts don't count toward the send cap", () => {
  assert.deepEqual(ok({ sentLines: sends(30, "prompt") }), { ok: true });
});

test("last send 2 s ago: gap", () => {
  assert.deepEqual(ok({ sentLines: sends(1, "send", 2000) }), { ok: false, error: "gap" });
  assert.deepEqual(ok({ sentLines: sends(1, "prompt", 2000) }), { ok: false, error: "gap" });  // any message
  assert.deepEqual(ok({ sentLines: sends(1, "send", 6000) }), { ok: true });
  assert.deepEqual(ok({ sentLines: sends(1, "send", 6000), gapSeconds: 6.5 }), { ok: false, error: "gap" });
  assert.deepEqual(ok({ cfg: cfg({ min_gap_seconds: 0 }), sentLines: sends(1, "send", 10) }), { ok: true });
});

test("unreadable sent.jsonl blocks: the cap can't be checked", () => {
  assert.deepEqual(ok({ sentLines: null }), { ok: false, error: "cap" });
});

test("already sent with this text: already_sent", () => {
  const d = draft();
  const line = { at: at(3600_000 * 30), kind: "send", chat: DM, draft: d.id, sha256: d.sha256, stanza: "B1" };
  assert.deepEqual(ok({ sentLines: [line] }), { ok: false, error: "already_sent" });
});

test("sending off: off", () => {
  assert.deepEqual(ok({ cfg: cfg({ enabled: false }) }), { ok: false, error: "off" });
  assert.deepEqual(ok({ cfg: cfg({ send: false }) }), { ok: false, error: "off" });
  assert.deepEqual(ok({ cfg: {} }), { ok: false, error: "off" });
});

test("prompt: only to self_jid, its own cap, the gap", () => {
  const p = (args) => checkPrompt({ cfg: cfg(), text: "WhatsApp -> Dana: \"hi\" - reply ok w3kx", sentLines: [],
                                    now: NOW, ...args });
  assert.deepEqual(p({}), { ok: true, chat: SELF });
  assert.deepEqual(p({ sentLines: sends(30, "send") }), { ok: true, chat: SELF });  // sends don't count
  assert.deepEqual(p({ sentLines: sends(10, "prompt") }), { ok: false, error: "cap" });
  assert.deepEqual(p({ cfg: cfg({ max_prompts_per_day: 2 }), sentLines: sends(2, "prompt") }),
                   { ok: false, error: "cap" });
  assert.deepEqual(p({ sentLines: sends(1, "prompt", 1000) }), { ok: false, error: "gap" });
  assert.deepEqual(p({ cfg: cfg({ self_jid: undefined }) }), { ok: false, error: "not_allowlisted" });
  assert.deepEqual(p({ text: "" }), { ok: false, error: "bad_request" });
  assert.deepEqual(p({ text: 7 }), { ok: false, error: "bad_request" });
  assert.deepEqual(p({ cfg: cfg({ enabled: false }) }), { ok: false, error: "off" });
});

test("parseSent: one record per line; a torn or malformed line makes it unreadable", () => {
  assert.deepEqual(parseSent(""), []);
  const l = sentLine({ at: "2026-10-27T11:00:00Z", kind: "send", chat: DM, draft: "w3kx", sha256: "ab", stanza: "S1" });
  assert.deepEqual(parseSent(l + l), [JSON.parse(l), JSON.parse(l)]);
  assert.equal(parseSent(l + '{"at": "2026-10-27'), null);
  assert.equal(parseSent(l + "[1]\n"), null);
});

test("sentLine: exact fields, one line ending in a newline, no draft key for a prompt", () => {
  const s = sentLine({ at: "2026-10-27T11:00:00Z", kind: "send", chat: DM, draft: "w3kx", sha256: "ab", stanza: "S1" });
  assert.equal(s, '{"at":"2026-10-27T11:00:00Z","kind":"send","chat":"' + DM + '","draft":"w3kx","sha256":"ab","stanza":"S1"}\n');
  const p = sentLine({ at: "2026-10-27T11:00:00Z", kind: "prompt", chat: SELF, sha256: "cd", stanza: "S2" });
  assert.deepEqual(Object.keys(JSON.parse(p)), ["at", "kind", "chat", "sha256", "stanza"]);
  assert.equal(s.split("\n").length, 2);
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
