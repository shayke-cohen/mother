# WhatsApp bridge (send-only)

The one process that sends WhatsApp messages for MOTHER. It links to the manager's WhatsApp as a device
(like WhatsApp Web, via [Baileys](https://github.com/WhiskeySockets/Baileys)) and sends one text to one
allowlisted chat after the manager's proven yes. It reads nothing: incoming messages are handled only as far
as the protocol needs and are never stored, logged or forwarded. Reading goes through the local database
(`mother whatsapp read`). The rules are in `docs/CONTRACT.md` §5d.

Optional. Nothing else in MOTHER depends on Node or on this folder. Node 20 or later, macOS, home machine only.

## Install

```bash
cd "${CLAUDE_PLUGIN_ROOT}/bridge/whatsapp" && npm ci --omit=dev
```

Versions are pinned exactly in `package.json` and `package-lock.json`. `node_modules/` is never committed.

## Run

| Command | Does |
|---|---|
| `node bridge.mjs --link` | Prints the QR in the terminal; scan it in WhatsApp > Settings > Linked devices. Exits once linked. |
| `node bridge.mjs` | Serves on `<state>/whatsapp/bridge.sock`. An unlinked bridge stays unlinked and refuses every send. |
| `node bridge.mjs --unlink` | Logs the device out and deletes `<state>/whatsapp/session/`: the kill switch. |

`<state>` is `$MOTHER_STATE_DIR` or `~/.mother`. The workspace is `$MOTHER_WORKSPACE`, else the
`<state>/workspace` pointer. Session keys live in `<state>/whatsapp/session/` (folder 0700, files 0600),
outside the workspace, never synced. The agent never runs these directly or talks to the socket: it goes
through `mother whatsapp …`, and the guard denies anything else that touches the socket or the state.

## Protocol

A Unix socket, mode 0600, no TCP. One JSON request per connection, one JSON reply, each ending in a newline.

| Request | Reply |
|---|---|
| `{"op": "status"}` | `{"ok": true, "linked", "me", "sent_today", "prompts_today"}` |
| `{"op": "send", "draft": "<id>"}` | `{"ok": true, "stanza"}` or `{"ok": false, "error"}` |
| `{"op": "prompt", "text": "…"}` | the same; goes to `whatsapp.self_jid` only |

Before every `send` the bridge checks, in order: sending is on and the workspace isn't PAUSED; the draft's
chat is in `whatsapp.allow` and isn't the self chat; `approved/<id>.json` exists for this draft and chat;
`sha256` of the draft text equals both the draft's stored hash and the approval's; not already sent; today's
`max_per_day` (UTC day of `sent.jsonl`'s `at`); the jittered `min_gap_seconds` since the last message; linked;
and then `python3 <plugin>/bin/mother whatsapp verify <id>` must exit 0 (it re-proves the yes from the
evidence the guard recorded). Then it shows "typing…" for `min(8 s, 40 ms × characters)` plus jitter, sends,
appends one line to `sent.jsonl`, and renames the approval to `.used` (`.failed` if the send threw).

Errors: `not_allowlisted`, `no_approval`, `hash_mismatch`, `cap`, `gap`, `unlinked`, plus `no_draft`,
`already_sent`, `off`, `paused`, `no_workspace`, `send_failed`, `bad_request`, `internal`. A refusal leaves
`approved/<id>.json` in place (a `gap` or `unlinked` send can be retried); only a send that threw marks it
`.failed`.

## Test

```bash
node --test bridge/whatsapp/*.test.mjs      # or: cd bridge/whatsapp && node --test
```

`checks.mjs` holds the pure rules; `bridge.test.mjs` drives the real socket with a stub WhatsApp client. No
test connects to WhatsApp.
