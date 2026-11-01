"""Tests for assets/plan/build.py. python3 -m unittest tests.test_plan -v"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / "assets" / "plan"


def build(data, out):
    src = Path(out).with_suffix(".json")
    src.write_text(json.dumps(data))
    return subprocess.run([sys.executable, str(PLAN / "build.py"), str(src), str(out)], capture_output=True, text=True)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "plan.html"
        self.data = json.loads((PLAN / "example.json").read_text())

    def tearDown(self):
        self.tmp.cleanup()

    def test_example_builds_static(self):
        r = build(self.data, self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        html = self.out.read_text()
        self.assertIn("<title>Sprint 12 plan — Platform team</title>", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("<!--BODY-->", html)
        self.assertIn('href="https://tracker.example.com/browse/PLAT-110"', html)

    def test_github_issue_key_links_to_github(self):
        self.data["next"]["goals"][0]["items"][0]["key"] = "acme/tools#42"
        r = build(self.data, self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('href="https://github.com/acme/tools/issues/42"', self.out.read_text())

    def test_missing_section_fails(self):
        del self.data["next"]
        r = build(self.data, self.out)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("next", r.stderr + r.stdout)

    def test_unknown_change_fails(self):
        self.data["next"]["goals"][0]["items"][0]["change"] = "maybe"
        r = build(self.data, self.out)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown change", r.stderr + r.stdout)

    def test_delta_colours(self):
        build(self.data, self.out)
        html = self.out.read_text()
        self.assertIn('<span class="pill p-bad">+14%</span> vs 36 on 10-25', html)   # 41 vs 36, lower is better
        self.assertIn('<span class="pill p-good">−25%</span> vs 12 on 10-25', html)  # 9 vs 12
        self.assertIn('<span class="pill p-good">+22%</span> vs 18 the month before', html)  # higher is better

    def test_counts_and_load(self):
        build(self.data, self.out)
        html = self.out.read_text()
        self.assertIn("<b>2</b><span>existing issues to add</span>", html)
        self.assertIn("<b>1</b><span>new issues to create</span>", html)
        self.assertIn("Lee <b>0</b> → <b>2</b>", html)
        self.assertIn("from Alex", html)

    def test_text_is_escaped(self):
        self.data["decisions"] = ["<img src=x onerror=alert(1)>"]
        build(self.data, self.out)
        self.assertIn("&lt;img src=x", self.out.read_text())

    def test_template_is_vendor_and_company_neutral(self):
        for f in ("template.html", "example.json", "build.py", "README.md"):
            t = (PLAN / f).read_text().lower()
            for word in ("atlassian.net", "mobpub", "wix", "stash", "jira"):
                self.assertNotIn(word, t, f)


if __name__ == "__main__":
    unittest.main()
