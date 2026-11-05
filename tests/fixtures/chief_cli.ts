// Written by tests/test_cli_contract.py from the real CLI: don't edit by hand.
export const CLI = {
 "tick_nothing": {
  "exitCode": 0,
  "stdout": "{\n  \"paused\": false,\n  \"decided\": [],\n  \"judge\": []\n}\n",
  "stderr": ""
 },
 "status_idle": {
  "exitCode": 0,
  "stdout": "{\n  \"version\": 1,\n  \"at\": \"2026-11-02T09:00:00Z\",\n  \"paused\": false,\n  \"chief\": {\n    \"alive\": true,\n    \"heartbeat_at\": \"2026-11-02T09:00:00Z\"\n  },\n  \"budget\": {\n    \"spent\": 0.0,\n    \"limit\": 20.0,\n    \"unreadable\": false,\n    \"over\": false\n  },\n  \"desktop\": [],\n  \"led_by\": null,\n  \"inbox\": [],\n  \"inbox_count\": 0,\n  \"cases\": [],\n  \"relay\": {\n    \"on\": true\n  },\n  \"guard\": \"on\",\n  \"health\": {\n    \"guard\": \"on\",\n    \"watcher\": null,\n    \"watcher_errors\": [],\n    \"watcher_error_count\": 0,\n    \"undelivered\": []\n  }\n}\n",
  "stderr": ""
 },
 "tick_skipped": {
  "exitCode": 0,
  "stdout": "{\"skipped\": \"another session leads\"}\n",
  "stderr": ""
 }
}
