"""The agent-facing docs (AGENTS.md, ARCHITECTURE.md) stay true: every repo path they name exists, and the
code map names every skill and routine. A stale map sends the next agent to a file that isn't there."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ["AGENTS.md", "ARCHITECTURE.md"]
# A backticked token that looks like a path inside this repo: starts with a top-level dir or file we ship.
TOP = ("agents/", "bin/", "lib/", "hooks/", "skills/", "routines/", "rules/", "assets/", "templates/",
       "tests/", "docs/", ".claude-plugin/")
# Named in the docs as untracked personal folders; absent on a fresh clone.
UNTRACKED = ("docs/drafts/", "docs/superpowers/")


def named_paths(text):
    for tok in re.findall(r"`([^`\s]+)`", text):
        if tok.startswith(TOP) and not tok.startswith(UNTRACKED) and "<" not in tok and "*" not in tok and "…" not in tok:
            yield tok.rstrip(",.")


class DocsTest(unittest.TestCase):
    def test_named_paths_exist(self):
        missing = []
        for doc in DOCS:
            for p in named_paths((ROOT / doc).read_text()):
                if not (ROOT / p).exists():
                    missing.append(f"{doc}: {p}")
        self.assertEqual(missing, [], "docs name paths that don't exist")

    def test_architecture_lists_every_skill_and_routine(self):
        text = (ROOT / "ARCHITECTURE.md").read_text()
        names = [p.name for p in (ROOT / "skills").iterdir() if (p / "SKILL.md").is_file()]
        names += [p.stem for p in (ROOT / "routines").glob("*.md")]
        unlisted = [n for n in sorted(set(names)) if f"`{n}`" not in text]
        self.assertEqual(unlisted, [], "skills or routines missing from ARCHITECTURE.md")

    def test_claude_md_points_at_agents_md(self):
        self.assertIn("@AGENTS.md", (ROOT / "CLAUDE.md").read_text())


if __name__ == "__main__":
    unittest.main()
