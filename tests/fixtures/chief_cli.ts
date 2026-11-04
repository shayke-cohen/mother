// Written by tests/test_cli_contract.py from the real CLI: don't edit by hand.
export const CLI = {
 "tick_nothing": {
  "exitCode": 0,
  "stdout": "{\n  \"paused\": false,\n  \"decided\": [],\n  \"wake\": null,\n  \"judge\": [],\n  \"dispatched\": [],\n  \"reconciled\": [],\n  \"batch\": false\n}\n",
  "stderr": ""
 },
 "batch_nothing": {
  "exitCode": 0,
  "stdout": "null\n",
  "stderr": ""
 },
 "propose_unsigned": {
  "exitCode": 1,
  "stdout": "",
  "stderr": "mother decide: this chat-post isn't signed, so the guard would refuse it even with the manager's yes. Sign it: put Agnes (AI) in the text, then file it again\n"
 },
 "propose_reply": {
  "exitCode": 0,
  "stdout": "{\"ok\": true, \"decision\": {\"id\": \"D1\", \"key\": \"PLAT-1\", \"kind\": \"chat-reply\", \"tool\": \"mcp__chat__slack__post_message\", \"input\": {\"channel_id\": \"C00000001\", \"thread_ts\": \"1700000000.000100\", \"text\": \"We ship Tuesday.\\n:sparkles: *Agnes (AI)* \\u00b7 on behalf of Dana\"}, \"target\": \"C00000001\", \"thread\": \"1700000000.000100\", \"text\": \"We ship Tuesday.\\n:sparkles: *Agnes (AI)* \\u00b7 on behalf of Dana\", \"draft_sha\": \"38096bc2a5f927a283c01db06f358f7137ad86d3550e27ed79a6b36fe348c386\", \"recommendation\": \"send\", \"why\": \"they <asked> @x\", \"urgent\": false, \"status\": \"pending\", \"created\": \"2026-11-02T09:00:00Z\", \"expires\": \"2026-11-02T09:00:00Z\", \"answer\": null}}\n",
  "stderr": ""
 },
 "propose_create": {
  "exitCode": 0,
  "stdout": "{\"ok\": true, \"decision\": {\"id\": \"D2\", \"key\": \"PLAT-1\", \"kind\": \"tracker-create\", \"tool\": \"mcp__tracker__jira__create-issue\", \"input\": {\"projectKey\": \"PLAT\", \"issueType\": \"Bug\", \"summary\": \"s\", \"description\": \"d\"}, \"target\": \"PLAT\", \"thread\": null, \"text\": \"\", \"draft_sha\": \"33c74ffb929d02ac00552932bd7ce4581c6bed1cf7ff008f4d568214e1e20bb6\", \"recommendation\": \"send\", \"why\": \"\", \"urgent\": true, \"status\": \"pending\", \"created\": \"2026-11-02T09:00:00Z\", \"expires\": \"2026-11-02T09:00:00Z\", \"answer\": null}}\n",
  "stderr": ""
 },
 "propose_mail": {
  "exitCode": 0,
  "stdout": "{\"ok\": true, \"decision\": {\"id\": \"D3\", \"key\": \"PLAT-1\", \"kind\": \"manual\", \"tool\": \"mcp__mail__gmail__send-gmail-message\", \"input\": {\"to\": [\"a@example.com\"], \"subject\": \"x\", \"body\": \"y\"}, \"target\": null, \"thread\": null, \"text\": \"y\", \"draft_sha\": \"9a2ec7f2439ce7d9880790031ba8339d3a57c97bf25d4f9f33950aaa3313b2e9\", \"recommendation\": \"send\", \"why\": \"\", \"urgent\": false, \"status\": \"pending\", \"created\": \"2026-11-02T09:00:00Z\", \"expires\": \"2026-11-02T09:00:00Z\", \"answer\": null}}\n",
  "stderr": ""
 },
 "tick_due": {
  "exitCode": 0,
  "stdout": "{\n  \"paused\": false,\n  \"decided\": [],\n  \"wake\": null,\n  \"judge\": [],\n  \"dispatched\": [],\n  \"reconciled\": [],\n  \"batch\": true\n}\n",
  "stderr": ""
 },
 "status_pending": {
  "exitCode": 0,
  "stdout": "{\n  \"version\": 1,\n  \"at\": \"2026-11-02T09:00:00Z\",\n  \"paused\": false,\n  \"chief\": {\n    \"alive\": true,\n    \"heartbeat_at\": \"2026-11-02T09:00:00Z\",\n    \"session_id\": null\n  },\n  \"budget\": {\n    \"spent\": 0.0,\n    \"limit\": 20.0,\n    \"unreadable\": false,\n    \"over\": false\n  },\n  \"workers\": [\n    {\n      \"key\": \"PLAT-1\",\n      \"title\": \"Plat one\",\n      \"status\": \"waiting_decision\",\n      \"held\": false,\n      \"running\": false,\n      \"progress\": \"\",\n      \"cost\": 0.0,\n      \"runs\": 0,\n      \"last_run_at\": null,\n      \"session_id\": \"00000000-0000-4000-8000-000000000001\",\n      \"queued\": 0,\n      \"resume\": \"claude --resume 00000000-0000-4000-8000-000000000001\",\n      \"unseen\": 0\n    }\n  ],\n  \"desktop\": [],\n  \"decisions\": [\n    {\n      \"id\": \"D1\",\n      \"key\": \"PLAT-1\",\n      \"kind\": \"chat-reply\",\n      \"target\": \"C00000001, thread_ts 1700000000.000100\",\n      \"recommendation\": \"send\",\n      \"why\": \"they <asked> @x\",\n      \"text\": \"We ship Tuesday.\\n:sparkles: *Agnes (AI)* · on behalf of Dana\",\n      \"full_text\": \"We ship Tuesday.\\n:sparkles: *Agnes (AI)* · on behalf of Dana\",\n      \"urgent\": false,\n      \"status\": \"pending\",\n      \"created\": \"2026-11-02T09:00:00Z\",\n      \"expires\": \"2026-11-02T09:00:00Z\",\n      \"edit_refusal\": null,\n      \"edit_text\": \"We ship Tuesday.\\n:sparkles: *Agnes (AI)* · on behalf of Dana\"\n    },\n    {\n      \"id\": \"D2\",\n      \"key\": \"PLAT-1\",\n      \"kind\": \"tracker-create\",\n      \"target\": \"PLAT, issueType Bug\",\n      \"recommendation\": \"send\",\n      \"why\": \"\",\n      \"text\": \"\",\n      \"full_text\": \"summary: s\\n\\ndescription: d\",\n      \"urgent\": true,\n      \"status\": \"pending\",\n      \"created\": \"2026-11-02T09:00:00Z\",\n      \"expires\": \"2026-11-02T09:00:00Z\",\n      \"edit_refusal\": \"this call sends summary and description, and an edit can only replace one. Tell the worker what to change: 2 <your words>\",\n      \"edit_text\": null\n    },\n    {\n      \"id\": \"D3\",\n      \"key\": \"PLAT-1\",\n      \"kind\": \"manual\",\n      \"target\": \"a@example.com\",\n      \"recommendation\": \"send\",\n      \"why\": \"\",\n      \"text\": \"y\",\n      \"full_text\": \"subject: x\\n\\nbody: y\",\n      \"urgent\": false,\n      \"status\": \"pending\",\n      \"created\": \"2026-11-02T09:00:00Z\",\n      \"expires\": \"2026-11-02T09:00:00Z\",\n      \"edit_refusal\": \"this call sends subject and body, and an edit can only replace one. Tell the worker what to change: 3 <your words>\",\n      \"edit_text\": null\n    }\n  ],\n  \"to_decide\": 3\n}\n",
  "stderr": ""
 },
 "sent_before_batch": {
  "exitCode": 2,
  "stdout": "",
  "stderr": "mother decide: D1, D2, D3 wasn't in the batch `mother decide batch` offered\n"
 },
 "batch_due": {
  "exitCode": 0,
  "stdout": "{\"text\": \"3 to decide · 0 done without you\\nD1 · PLAT-1 · chat-reply on C00000001, thread_ts 1700000000.000100: \\\"We ship Tuesday. :sparkles: *Agnes (AI)* · on behalf of Dana\\\" · why: they &lt;asked&gt; @​x · recommend: send\\nD2 · PLAT-1 · tracker-create on PLAT, issueType Bug: \\\"summary: s description: d\\\" · recommend: send\\nD3 · PLAT-1 · manual on a@​example.com: \\\"subject: x body: y\\\" · recommend: send\\nReply in this DM, not in a thread: 7 ok · 7 no · 7 edit: your text · ok all\", \"ids\": [\"D1\", \"D2\", \"D3\"]}\n",
  "stderr": ""
 },
 "sent_ok": {
  "exitCode": 0,
  "stdout": "{\"ok\": true}\n",
  "stderr": ""
 },
 "sent_again": {
  "exitCode": 2,
  "stdout": "",
  "stderr": "mother decide: D1, D2, D3 wasn't in the batch `mother decide batch` offered\n"
 },
 "sent_no_ids": {
  "exitCode": 0,
  "stdout": "{\"ok\": true}\n",
  "stderr": ""
 },
 "edit_several_fields": {
  "exitCode": 1,
  "stdout": "{\"ok\": false, \"message\": \"D2 is still open: this call sends summary and description, and an edit can only replace one. Tell the worker what to change: 2 <your words>.\"}\n",
  "stderr": ""
 },
 "answer_ok": {
  "exitCode": 0,
  "stdout": "{\"ok\": true, \"message\": \"D1 approved: PLAT-1 will make that one call.\"}\n",
  "stderr": ""
 },
 "answer_again": {
  "exitCode": 1,
  "stdout": "{\"ok\": false, \"message\": \"D1 is already approved.\"}\n",
  "stderr": ""
 },
 "answer_no": {
  "exitCode": 0,
  "stdout": "{\"ok\": true, \"message\": \"D2 rejected: PLAT-1 won't make that call.\"}\n",
  "stderr": ""
 },
 "answer_usage": {
  "exitCode": 2,
  "stdout": "",
  "stderr": "usage: mother decide answer <n> <ok|no|edit|say> --via pane|typed [-- <text>]\n"
 },
 "post_log": {
  "exitCode": 0,
  "stdout": "",
  "stderr": ""
 }
}
