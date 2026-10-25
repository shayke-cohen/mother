"""Tests for assets/deck/build.py. python3 -m unittest tests.test_deck -v"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DECK = ROOT / "assets" / "deck"


def build(data, out):
    src = Path(out).with_suffix(".json")
    src.write_text(json.dumps(data))
    return subprocess.run([sys.executable, str(DECK / "build.py"), str(src), str(out)], capture_output=True, text=True)


class DeckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "deck.html"
        self.data = json.loads((DECK / "example.json").read_text())

    def tearDown(self):
        self.tmp.cleanup()

    def test_example_builds(self):
        r = build(self.data, self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        html = self.out.read_text()
        self.assertIn("<title>Platform weekly — 2026-11-02</title>", html)
        self.assertNotIn("/*DATA*/", html)

    def test_card_delta_renders(self):
        self.data["pipeline"]["cards"][0].update({"prev": "50", "prevLabel": "vs last month", "estimate": True})
        r = build(self.data, self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        html = self.out.read_text()
        self.assertIn("function delta(c)", html)
        self.assertIn('"prevLabel": "vs last month"', html)

    def test_missing_section_fails(self):
        del self.data["carryover"]
        r = build(self.data, self.out)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("carryover", r.stderr + r.stdout)

    def test_script_breakout_is_neutralised(self):
        self.data["meta"]["subtitle"] = "</script><script>alert(1)</script>"
        self.assertEqual(build(self.data, self.out).returncode, 0)
        html = self.out.read_text()
        payload = html.split('<script id="deck-data" type="application/json">', 1)[1].split("</script>", 1)[0]
        self.assertIn("alert(1)", payload)  # stayed inside the data block

    def test_template_is_vendor_and_company_neutral(self):
        t = (DECK / "template.html").read_text().lower()
        for word in ("atlassian.net", "claudy", "mobpub"):
            self.assertNotIn(word, t)
        self.assertIn("mother approve tracker", t)


if __name__ == "__main__":
    unittest.main()
