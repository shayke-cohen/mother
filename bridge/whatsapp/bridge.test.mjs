// The bridge over a real Unix socket with a stub WhatsApp client: nothing here connects to WhatsApp.
// Run: node --test bridge/whatsapp/*.test.mjs
import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";
import { createBridge, locations, workspace, verifyWithCli } from "./bridge.mjs";

const SELF = "972500000099@s.whatsapp.net";
const DM = "972500000001@s.whatsapp.net";
const OTHER = "972500000002@s.whatsapp.net";
const TEXT = "see you at 3";
const sha = (t) => createHash("sha256").update(t, "utf8").digest("hex");
const isoZ = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");

let tmp, env, ws, wa, client, logs, verified, server, sock;

function stubClient() {
  const c = { calls: [], isLinked: true, failSend: false, n: 0 };
  c.linked = () => c.isLinked;
  c.me = () => SELF;
  c.presence = async (jid, s) => { c.calls.push(["presence", jid, s]); };
  c.sendText = async (jid, text) => {
    c.calls.push(["send", jid, text]);
    if (c.failSend) throw Object.assign(new Error("boom"), { code: "ECONNRESET" });
    return "STANZA" + ++c.n;
  };
  return c;
}

function writeJson(p, v) {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(v));
}

function draft(id = "w3kx", text = TEXT, chat = DM) {
  const d = { id, chat, chat_name: "Dana", text, sha256: sha(text), created: isoZ(Date.now() - 7200_000),
              run: null, status: "open" };
  writeJson(path.join(wa, "drafts", id + ".json"), d);
  return d;
}

function approve(d, extra = {}) {
  writeJson(path.join(wa, "approved", d.id + ".json"),
    { id: d.id, chat: d.chat, sha256: d.sha256, path: "session", evidence: "session:u1", at: isoZ(Date.now()),
      ...extra });
}

function request(obj, raw) {
  return new Promise((resolve, reject) => {
    const c = net.connect(sock);
    let buf = "";
    c.setEncoding("utf8");
    c.on("data", (d) => { buf += d; });
    c.on("end", () => { try { resolve(JSON.parse(buf)); } catch (e) { reject(e); } });
    c.on("error", reject);
    c.end(raw !== undefined ? raw : JSON.stringify(obj) + "\n");
  });
}

const sentLines = () => {
  const p = path.join(tmp, "state", "whatsapp", "sent.jsonl");
  return fs.existsSync(p) ? fs.readFileSync(p, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
};

async function start(opts = {}) {
  const b = createBridge({ env, client, verify: async (id) => { verified.push(id); return opts.verify ?? true; },
                           sleep: async () => {}, rand: () => 0, log: (r) => logs.push(r), ...opts.bridge });
  server = await b.listen(sock);
  return b;
}

beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "mwa-"));
  ws = path.join(tmp, "ws");
  wa = path.join(ws, ".mother", "whatsapp");
  env = { MOTHER_STATE_DIR: path.join(tmp, "state"), MOTHER_WORKSPACE: ws, PATH: process.env.PATH,
          HOME: process.env.HOME };
  writeJson(path.join(ws, "config.json"), {
    workspace: { home_machine: "laptop-a" },
    whatsapp: { enabled: true, self_jid: SELF, allow: [{ jid: DM, name: "Dana" }], min_gap_seconds: 5 },
  });
  client = stubClient();
  logs = [];
  verified = [];
  sock = locations(env).sock;
});

afterEach(async () => {
  if (server) await new Promise((r) => server.close(r));
  server = null;
  fs.rmSync(tmp, { recursive: true, force: true });
});

test("socket is 0600 in a 0700 folder", async () => {
  await start();
  assert.equal(fs.statSync(sock).mode & 0o777, 0o600);
  assert.equal(fs.statSync(path.dirname(sock)).mode & 0o777, 0o700);
  assert.ok(fs.statSync(sock).isSocket());
});

test("status", async () => {
  await start();
  assert.deepEqual(await request({ op: "status" }),
                   { ok: true, linked: true, me: SELF, sent_today: 0, prompts_today: 0 });
  client.isLinked = false;
  assert.deepEqual(await request({ op: "status" }),
                   { ok: true, linked: false, me: null, sent_today: 0, prompts_today: 0 });
});

test("an approved draft goes out once: typing, send, sent.jsonl, approval .used", async () => {
  const d = draft();
  approve(d);
  await start();
  const r = await request({ op: "send", draft: "w3kx" });
  assert.deepEqual(r, { ok: true, stanza: "STANZA1" });
  assert.deepEqual(verified, ["w3kx"]);
  assert.deepEqual(client.calls, [["presence", DM, "composing"], ["presence", DM, "paused"], ["send", DM, TEXT]]);
  const lines = sentLines();
  assert.equal(lines.length, 1);
  assert.deepEqual(Object.keys(lines[0]), ["at", "kind", "chat", "draft", "sha256", "stanza"]);
  assert.deepEqual({ ...lines[0], at: "x" },
                   { at: "x", kind: "send", chat: DM, draft: "w3kx", sha256: d.sha256, stanza: "STANZA1" });
  assert.match(lines[0].at, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
  assert.equal(fs.statSync(path.join(tmp, "state", "whatsapp", "sent.jsonl")).mode & 0o777, 0o600);
  assert.ok(fs.existsSync(path.join(wa, "approved", "w3kx.used")));
  assert.ok(!fs.existsSync(path.join(wa, "approved", "w3kx.json")));
  assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: false, error: "no_approval" });
  assert.equal(client.calls.filter((c) => c[0] === "send").length, 1);
  assert.equal((await request({ op: "status" })).sent_today, 1);
});

test("a slow send still answers a client that half-closed after its request", async () => {
  approve(draft());
  await start({ bridge: { sleep: (ms) => new Promise((r) => setTimeout(r, 200)) } });
  assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: true, stanza: "STANZA1" });
});

test("no approval record: no_approval, nothing sent", async () => {
  draft();
  await start();
  assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: false, error: "no_approval" });
  assert.deepEqual(client.calls, []);
  assert.deepEqual(verified, []);
});

test("the re-proof fails: no_approval, nothing sent, the approval stays in flight", async () => {
  approve(draft());
  await start({ verify: false });
  assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: false, error: "no_approval" });
  assert.deepEqual(client.calls, []);
  assert.ok(fs.existsSync(path.join(wa, "approved", "w3kx.json")));
  assert.deepEqual(sentLines(), []);
});

test("draft text edited after approval: hash_mismatch (Review Focus 3)", async () => {
  const d = draft();
  approve(d);
  draft("w3kx", "see you at 4");
  await start();
  assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: false, error: "hash_mismatch" });
  assert.deepEqual(client.calls, []);
});

test("a chat off the allowlist: not_allowlisted", async () => {
  approve(draft("w3kx", TEXT, OTHER));
  await start();
  assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: false, error: "not_allowlisted" });
  assert.deepEqual(client.calls, []);
});

test("unlinked: unlinked", async () => {
  approve(draft());
  client.isLinked = false;
  await start();
  assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: false, error: "unlinked" });
  assert.deepEqual(client.calls, []);
});

test("a send that throws: send_failed, approval .failed, no sent.jsonl line", async () => {
  approve(draft());
  client.failSend = true;
  await start();
  assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: false, error: "send_failed" });
  assert.ok(fs.existsSync(path.join(wa, "approved", "w3kx.failed")));
  assert.ok(!fs.existsSync(path.join(wa, "approved", "w3kx.json")));
  assert.deepEqual(sentLines(), []);
});

test("two sends back to back: the second waits out the gap", async () => {
  approve(draft("w3kx"));
  approve(draft("w9pa", "and bring the deck"));
  await start();
  const [a, b] = await Promise.all([request({ op: "send", draft: "w3kx" }), request({ op: "send", draft: "w9pa" })]);
  assert.deepEqual([a.ok, b], [true, { ok: false, error: "gap" }]);
  assert.ok(fs.existsSync(path.join(wa, "approved", "w9pa.json")));  // refused, not failed: still sendable later
});

test("prompt: to the manager's own chat only, no typing, logged as a prompt", async () => {
  await start();
  const text = 'WhatsApp -> Dana: "see you at 3" - reply ok w3kx';
  assert.deepEqual(await request({ op: "prompt", text }), { ok: true, stanza: "STANZA1" });
  assert.deepEqual(client.calls, [["send", SELF, text]]);
  const [line] = sentLines();
  assert.deepEqual({ ...line, at: "x" }, { at: "x", kind: "prompt", chat: SELF, sha256: sha(text), stanza: "STANZA1" });
  assert.deepEqual(await request({ op: "prompt", text: "" }), { ok: false, error: "bad_request" });
  const st = await request({ op: "status" });
  assert.deepEqual([st.sent_today, st.prompts_today], [0, 1]);
});

test("PAUSED stops every send", async () => {
  approve(draft());
  fs.writeFileSync(path.join(ws, ".mother", "PAUSED"), "");
  await start();
  assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: false, error: "paused" });
  assert.deepEqual(await request({ op: "prompt", text: "hi" }), { ok: false, error: "paused" });
  assert.deepEqual(client.calls, []);
});

test("bad requests", async () => {
  await start();
  assert.deepEqual(await request(null, "not json\n"), { ok: false, error: "bad_request" });
  assert.deepEqual(await request({ op: "read" }), { ok: false, error: "bad_request" });
  assert.deepEqual(await request({ op: "send", draft: "../x" }), { ok: false, error: "bad_request" });
  assert.deepEqual(await request({ op: "send", draft: ["w3kx"] }), { ok: false, error: "bad_request" });
  assert.deepEqual(await request([1, 2]), { ok: false, error: "bad_request" });
});

test("logs carry ids and error codes, never text or a phone number", async () => {
  approve(draft());
  await start();
  await request({ op: "send", draft: "w3kx" });
  await request({ op: "prompt", text: "secret prompt body" });
  client.failSend = true;
  approve(draft("w9pa", "another secret"));
  await new Promise((r) => setTimeout(r, 10));
  await request({ op: "send", draft: "w9pa" });
  const all = JSON.stringify(logs);
  assert.ok(logs.length >= 3);
  for (const bad of [TEXT, "secret prompt body", "another secret", DM, SELF, "972500000"]) {
    assert.ok(!all.includes(bad), bad);
  }
});

test("a stale socket is replaced; a live one is not taken over", async () => {
  fs.mkdirSync(path.dirname(sock), { recursive: true });
  fs.writeFileSync(sock, "");
  await start();
  const second = createBridge({ env, client });
  await assert.rejects(second.listen(sock), /already running/);
});

test("workspace: $MOTHER_WORKSPACE, else the state pointer", () => {
  assert.equal(workspace(env), ws);
  const st = path.join(tmp, "state");
  fs.mkdirSync(st, { recursive: true });
  fs.writeFileSync(path.join(st, "workspace"), ws + "\n");
  assert.equal(workspace({ MOTHER_STATE_DIR: st }), ws);
  assert.equal(workspace({ MOTHER_STATE_DIR: path.join(tmp, "nowhere") }), null);
});

test("verifyWithCli runs `mother whatsapp verify`: yes for an approved draft, no once its text changes", async () => {
  const cfg = JSON.parse(fs.readFileSync(path.join(ws, "config.json"), "utf8"));
  cfg.whatsapp.approve_via = ["session"];  // no database needed
  writeJson(path.join(ws, "config.json"), cfg);
  const d = draft();
  const transcript = path.join(tmp, "t.jsonl");
  fs.writeFileSync(transcript, JSON.stringify({
    type: "user", uuid: "u1", timestamp: new Date(Date.now() - 600_000).toISOString(), origin: { kind: "human" },
    message: { role: "user", content: "ok w3kx" },
  }) + "\n");
  approve(d, { transcript });
  const venv = { ...env, MOTHER_HOSTNAME: "laptop-a", MOTHER_WHATSAPP_DB: path.join(tmp, "none.sqlite") };
  assert.equal(await verifyWithCli("w3kx", { env: venv }), true);
  draft("w3kx", "see you at 4");
  assert.equal(await verifyWithCli("w3kx", { env: venv }), false);
  assert.equal(await verifyWithCli("w9pa", { env: venv }), false);
});
