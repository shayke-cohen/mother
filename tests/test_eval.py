"""Tests for lib/evals.py — the A/B harness for the brief's gather mode.

A fake `claude` (MOTHER_CLAUDE_BIN) reads the dry-run prompt, writes a canned brief for the gather mode it
names, and prints a `--output-format json` result. Nothing reaches a model or a connector.
Run: python3 -m unittest tests.test_eval -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import evals  # noqa: E402

MOTHER = os.path.join(ROOT, "bin", "mother")

INLINE = """## Today — Sun, 25 Oct
### Needs you
- Alex asks for the rollout date — https://mail.google.com/mail/u/0/#inbox/a1
- Decide the on-call swap
### Waiting on you (async)
- Review: acme/web#412 — tidy
⚠️ Couldn't check: chat — chat.read failed
"""

READERS = """## Today — Sun, 25 Oct
### Needs you
- Alex asks for the rollout date — https://mail.google.com/mail/u/0/#inbox/a1
- Sam in #platform — https://acme.slack.com/archives/C1/p2
### Waiting on you (async)
- Review: acme/web#412 — tidy
- Tracker: PLAT-12 In Progress — PLAT-12
"""

FAKE = r'''#!/usr/bin/env python3
import json, os, re, sys
prompt = sys.stdin.read()
gather = re.search(r"Gather mode: (\w+)", prompt).group(1)
path = re.search(r"Write the finished brief, markdown only, to: (\S+)", prompt).group(1)
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"gather": gather, "argv": sys.argv[1:], "dry": os.environ.get("MOTHER_DRY_RUN")}) + "\n")
if os.environ.get("FAKE_FAIL") == gather:
    sys.stderr.write("boom"); sys.exit(1)
open(path, "w").write(open(os.environ["FAKE_" + gather.upper()]).read())
cost = 0.40 if gather == "inline" else 0.90
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "DONE",
                  "total_cost_usd": cost, "duration_ms": 1000, "num_turns": 7,
                  "usage": {"input_tokens": 10, "output_tokens": 5},
                  "modelUsage": {"m": {"inputTokens": 1000, "outputTokens": 200, "cacheReadInputTokens": 50}}}))
'''


class TestScore(unittest.TestCase):
    def test_pair(self):
        s = evals.score_pair({"inline": INLINE, "readers": READERS})
        self.assertEqual(s["readers"]["recall"], 1.0)
        self.assertEqual(s["inline"]["recall"], 0.5)          # 2 of the 4 distinct citations
        self.assertEqual(s["inline"]["uncited"], 1)           # "Decide the on-call swap"
        self.assertEqual(s["readers"]["uncited"], 0)
        self.assertEqual(s["inline"]["couldnt"], ["chat"])
        # readers cited the tracker; inline neither cited nor flagged it. Chat was flagged, so not quiet.
        self.assertEqual(s["inline"]["false_quiet"], ["tracker"])
        self.assertEqual(s["readers"]["false_quiet"], [])

    def test_empty_pair(self):
        s = evals.score_pair({"inline": "", "readers": ""})
        self.assertEqual((s["inline"]["recall"], s["inline"]["cited"]), (1.0, 1.0))


def pair(winner, r_recall=1.0, i_recall=0.8, r_cost=0.9, i_cost=0.4, r_uncited=0, i_uncited=1, error=None):
    v = lambda rec, cost, unc, err=None: {"recall": rec, "uncited": unc, "false_quiet": [], "cost_usd": cost,
                                          "tokens": 100, "duration_ms": 1, "error": err}
    return {"variants": {"inline": v(i_recall, i_cost, i_uncited), "readers": v(r_recall, r_cost, r_uncited, error)},
            "verdict": {"winner": winner} if winner else None}


class TestShipRule(unittest.TestCase):
    def test_not_enough_data(self):
        self.assertEqual(evals.report([pair("readers")] * 3)["decision"], "not enough data")

    def test_ship(self):
        r = evals.report([pair("readers")] * 6 + [pair("same")] * 2 + [pair("inline")] * 2)
        self.assertEqual(r["decision"], "ship", r["reasons"])
        self.assertEqual(r["readers_or_same"], 0.8)

    def test_hold_on_cost_or_preference_or_recall(self):
        self.assertEqual(evals.report([pair("readers", r_cost=2.0)] * 10)["decision"], "hold")
        self.assertEqual(evals.report([pair("inline")] * 5 + [pair("readers")] * 5)["decision"], "hold")
        self.assertEqual(evals.report([pair("readers", r_recall=0.5)] * 10)["decision"], "hold")
        self.assertEqual(evals.report([pair("readers", r_uncited=3)] * 10)["decision"], "hold")

    def test_failed_runs_dont_count(self):
        r = evals.report([pair("readers")] * 9 + [pair("readers", error="timed out")])
        self.assertEqual((r["usable"], r["failed"], r["decision"]), (9, 1, "not enough data"))


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mother-eval-")
        self.ws = os.path.join(self.tmp, "ws")
        os.makedirs(self.ws)
        Path(self.ws, "config.json").write_text("{}")
        self.fake = os.path.join(self.tmp, "claude")
        Path(self.fake).write_text(FAKE)
        os.chmod(self.fake, 0o755)
        for name, text in (("inline", INLINE), ("readers", READERS)):
            Path(self.tmp, name + ".md").write_text(text)
        self.log = os.path.join(self.tmp, "calls.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def mother(self, *args, **env):
        e = dict(os.environ, MOTHER_WORKSPACE=self.ws, MOTHER_CLAUDE_BIN=self.fake, FAKE_LOG=self.log,
                 FAKE_INLINE=os.path.join(self.tmp, "inline.md"), FAKE_READERS=os.path.join(self.tmp, "readers.md"))
        e.update(env)
        return subprocess.run([sys.executable, MOTHER, "eval"] + list(args), capture_output=True, text=True, env=e)

    def calls(self):
        return [json.loads(l) for l in Path(self.log).read_text().splitlines()]

    def test_pair_verdict_report(self):
        p = self.mother("brief", "--first", "readers", MOTHER_NOW="1761379200")
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["errors"], {})
        self.assertTrue(Path(out["X"]).read_text().startswith("## Today"))
        calls = self.calls()
        self.assertEqual([c["gather"] for c in calls], ["readers", "inline"])
        self.assertTrue(all(c["dry"] == "1" for c in calls))      # the guard denies every outbound call
        self.assertNotIn("--dangerously-skip-permissions", calls[0]["argv"])

        pid = out["pair"]
        pj = json.loads(Path(self.ws, ".mother", "eval", pid[:10], pid[11:], "pair.json").read_text())
        self.assertEqual(pj["variants"]["readers"]["recall"], 1.0)
        self.assertEqual(pj["variants"]["inline"]["tokens"], 1250)  # modelUsage, subagents included
        self.assertEqual(sorted(pj["labels"].values()), ["inline", "readers"])

        x_is = pj["labels"]["X"]
        v = self.mother("verdict", pid, "x", "--note", "clearer")
        self.assertEqual(json.loads(v.stdout)["winner"], x_is)
        rec = json.loads(Path(self.ws, ".mother", "eval", "results.jsonl").read_text().splitlines()[-1])
        self.assertEqual((rec["winner"], rec["note"]), (x_is, "clearer"))

        r = json.loads(self.mother("report", "--min-pairs", "1").stdout)
        self.assertEqual((r["pairs"], r["judged"]), (1, 1))
        self.assertIn(r["decision"], ("ship", "hold"))

    def test_order_alternates(self):
        self.mother("brief", MOTHER_NOW="1761379200")
        self.mother("brief", MOTHER_NOW="1761379260")
        self.assertEqual([c["gather"] for c in self.calls()], ["inline", "readers", "readers", "inline"])

    def test_a_failed_variant_is_recorded(self):
        out = json.loads(self.mother("brief", MOTHER_NOW="1761379200", FAKE_FAIL="readers").stdout)
        self.assertIn("readers", out["errors"])
        r = json.loads(self.mother("report").stdout)
        self.assertEqual((r["failed"], r["decision"]), (1, "not enough data"))

    def test_bad_verdict(self):
        self.assertNotEqual(self.mother("verdict", "2026-10-25-000000", "maybe").returncode, 0)
        self.assertNotEqual(self.mother("verdict", "nope", "x").returncode, 0)


if __name__ == "__main__":
    unittest.main()
