"""Everything the plugin needs at runtime must be tracked by git — tests pass on a dev checkout even when a
file never reaches users (this happened: an over-broad .gitignore dropped templates/workspace/)."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIRS = ["agents", "bin", "lib", "hooks", "skills", "routines", "rules", "assets", "templates", ".claude-plugin"]


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
                    if rel not in tracked:
                        missing.append(rel)
        self.assertEqual(missing, [], "runtime files not tracked by git (ignored or never added)")


if __name__ == "__main__":
    unittest.main()
