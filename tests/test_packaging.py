"""Everything the plugin needs at runtime must be tracked by git — tests pass on a dev checkout even when a
file never reaches users (this happened: an over-broad .gitignore dropped templates/workspace/)."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIRS = ["agents", "bin", "lib", "hooks", "skills", "routines", "rules", "assets", "templates", ".claude-plugin", "mods"]
# Written by the Claude Code engine when it loads a mod (its API types and a tsconfig that extends them);
# local to each machine, never shipped.
ENGINE_WRITTEN = ("mods/now/tsconfig.json", "mods/now/.claude-plugin/types/")


class PackagingTest(unittest.TestCase):
    def test_runtime_files_are_tracked(self):
        r = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True)
        if r.returncode:
            self.skipTest("not a git checkout")
        tracked = set(r.stdout.splitlines())
        missing = []
        for d in RUNTIME_DIRS:
            for p in (ROOT / d).rglob("*"):
                if p.is_file() and p.name != ".DS_Store" and "__pycache__" not in p.parts:
                    rel = str(p.relative_to(ROOT))
                    if rel not in tracked and not rel.startswith(ENGINE_WRITTEN):
                        missing.append(rel)
        self.assertEqual(missing, [], "runtime files not tracked by git (ignored or never added)")


if __name__ == "__main__":
    unittest.main()
