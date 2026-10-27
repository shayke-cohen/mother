# WhatsApp bridge (send-only)

The one process that sends WhatsApp messages for MOTHER. It links to the manager's WhatsApp as a device
(like WhatsApp Web, via [Baileys](https://github.com/WhiskeySockets/Baileys)) and sends one text to one
allowlisted chat after the manager's proven yes. It reads nothing: incoming messages are handled only as far
as the protocol needs and are never stored, logged or forwarded. Reading goes through the local database
(`mother whatsapp read`). The rules are in `docs/CONTRACT.md` §5d.

Optional. Nothing else in MOTHER depends on Node or on this folder. Node 20 or later, macOS, home machine only.

## Install

```bash
cd "${CLAUDE_PLUGIN_ROOT}/bridge/whatsapp" && npm ci --ignore-scripts --omit=dev
```

Versions are pinned exactly in `package.json` and `package-lock.json`. `node_modules/` is never committed.

Known risks of the dependency:
- Baileys is pinned to a **release candidate** (`7.0.0-rc14`, npm's `latest` at the time). It is an
  unofficial client of the WhatsApp Web protocol and can break when WhatsApp changes it.
- It has **install scripts** (Baileys' Node-version check, protobufjs' postinstall). They are not needed:
  `npm ci --ignore-scripts` installs a working bridge (checked on macOS arm64, Node 24), so skip them.
- It pulls **`sharp`**, which brings prebuilt native binaries (`@img/sharp-darwin-*`, libvips) for image
  handling the bridge never uses.

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

Before every `send` the bridge checks, in order: `approved/<id>.json` exists (else `no_approval`, nothing
else happens); sending is on and the workspace isn't PAUSED; the draft's chat is in `whatsapp.allow` and isn't
the self chat; the approval names this draft and chat; `sha256` of the draft text equals both the draft's
stored hash and the approval's; not already sent; today's `max_per_day` (UTC day of `sent.jsonl`'s `at`); the
jittered `min_gap_seconds` since the last message (waited out inside the request when 30 s or less, else
`gap`); linked; the linked account is `whatsapp.self_jid`; and then `python3 <plugin>/bin/mother whatsapp
verify <id> --sha256 <hash of the text about to go out>` must exit 0 (it re-proves the yes from the evidence
the guard recorded, on the path it recorded). Then it shows "typing…" for `min(8 s, 40 ms × characters)` plus
jitter, sends, and appends one line to `sent.jsonl` (`"stanza": null` if WhatsApp returned no id).

Once the approval file exists, every outcome settles it: `.used` after a send or on `already_sent`,
`.failed` on any refusal or failure, so a fresh `mother whatsapp send <id>` re-proves the manager's yes.
If the bridge dies between sending and logging, the approval stays `.json`: the guard then denies a resend
(the safe side) and the outcome is unrecorded.

A `prompt` must not read as the manager's reply (`ok <id>`, `ok all`, `no <id>` on any line: `bad_request`).
If its `sent.jsonl` line can't be written it returns `{"ok": false, "error": "log_failed", "stanza",
"detail"}`: the message went out but isn't recorded as the bridge's own.

Errors: `not_allowlisted`, `no_approval`, `hash_mismatch`, `cap`, `gap`, `unlinked`, plus `no_draft`,
`already_sent`, `off`, `paused`, `wrong_account`, `no_workspace`, `send_failed`, `log_failed` (prompt only),
`bad_request`, `internal`.

## Test

```bash
node --test bridge/whatsapp/*.test.mjs      # or: cd bridge/whatsapp && node --test
```

`checks.mjs` holds the pure rules; `bridge.test.mjs` drives the real socket with a stub WhatsApp client. No
test connects to WhatsApp.
