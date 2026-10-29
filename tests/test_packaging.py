"""Everything the plugin needs at runtime must be tracked by git — tests pass on a dev checkout even when a
file never reaches users (this happened: an over-broad .gitignore dropped templates/workspace/)."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIRS = ["agents", "bin", "lib", "hooks", "skills", "routines", "rules", "assets", "templates", ".claude-plugin", "mods",
                "bridge"]
SKIP_PARTS = {"__pycache__", "node_modules"}  # node_modules: installed by `npm ci`, never shipped
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
                if p.is_file() and p.name != ".DS_Store" and not SKIP_PARTS.intersection(p.parts):
                    rel = str(p.relative_to(ROOT))
                    if rel not in tracked and not rel.startswith(ENGINE_WRITTEN):
                        missing.append(rel)
        self.assertEqual(missing, [], "runtime files not tracked by git (ignored or never added)")

    def test_bridge_lockfile_pins_its_dependencies(self):
        import json
        pkg = json.loads((ROOT / "bridge" / "whatsapp" / "package.json").read_text())
        lock = json.loads((ROOT / "bridge" / "whatsapp" / "package-lock.json").read_text())
        for name, version in pkg.get("dependencies", {}).items():
            self.assertRegex(version, r"^\d+\.\d+\.\d+(-[\w.]+)?$", "%s must be an exact version" % name)
            self.assertEqual(lock["packages"]["node_modules/" + name]["version"], version, name)
        for where, entry in lock["packages"].items():  # no private mirror hostnames in a shipped file
            if "resolved" in entry:
                self.assertTrue(entry["resolved"].startswith("https://registry.npmjs.org/"), where)


def matcher_hits(matcher, tool):
    """Claude Code's matcher rule: a plain `A|B|C` list (letters, digits, `_`, `|`) is a list of exact names;
    anything else is a regular expression searched in the tool name."""
    import re
    if matcher in ("", "*"):
        return True
    if re.fullmatch(r"[A-Za-z0-9_|]+", matcher):
        return tool in matcher.split("|")
    return re.search(matcher, tool) is not None


def guard_tool_names():
    """Every non-MCP tool name hooks/guard.py has a rule for: the tool sets it names, plus every string that a
    `tool == …` / `tool in (…)` test compares against. HUB_PASS_TOOLS only ever pass, so they are left out."""
    import ast
    import importlib.util
    path = ROOT / "hooks" / "guard.py"
    spec = importlib.util.spec_from_file_location("guard_pkg", str(path))
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    names = set(g.HUB_TOOLS) | set(g.IN_APP_SESSION_TOOLS) | set(g.CHANNEL_NO_TOOLS) | set(g.HUB_FED_HANDOFFS)
    for s in g.SUBAGENT_TOOLS.values():
        names |= set(s)
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "tool":
            for c in node.comparators:
                vals = c.elts if isinstance(c, (ast.Tuple, ast.List, ast.Set)) else [c]
                names |= {v.value for v in vals if isinstance(v, ast.Constant) and isinstance(v.value, str)}
    return {n for n in names if not n.startswith("mcp__")} - set(g.HUB_PASS_TOOLS)


class HookRegistrationTest(unittest.TestCase):
    """The guard's rules only hold for tools Claude Code hands it: every tool name it has a rule for must be
    matched by exactly one PreToolUse entry that runs it unconditionally (CONTRACT §5)."""

    def entries(self):
        import json
        doc = json.loads((ROOT / "hooks" / "hooks.json").read_text())
        out = []
        for e in doc["hooks"]["PreToolUse"]:
            for h in e["hooks"]:
                cmd = h["command"]
                self.assertRegex(cmd, r'^python3 "\$\{CLAUDE_PLUGIN_ROOT\}/hooks/guard(_fast)?\.py"$',
                                 "a PreToolUse entry that doesn't run the guard unconditionally: %s" % cmd)
                out.append((e.get("matcher", ""), cmd))
        return out

    def test_every_guarded_tool_reaches_the_guard_exactly_once(self):
        entries = self.entries()
        names = guard_tool_names()
        for must in ("Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "Read", "Grep", "Glob", "LS", "WebFetch",
                     "WebSearch", "Task", "Agent", "Skill", "Monitor", "CronCreate", "CronDelete", "TaskStop",
                     "SendMessage", "ListAgents"):
            self.assertIn(must, names)
        for tool in sorted(names) + ["mcp__x__post_message", "mcp__ccd_session__hand_off_to_session",
                                     "mcp__terminal__run_in_terminal"]:
            hits = [m for m, _ in entries if matcher_hits(m, tool)]
            self.assertEqual(len(hits), 1, "%s matched by %s" % (tool, hits))

    def test_only_the_guarded_tools_reach_it(self):
        for tool in ("ToolSearch", "TodoWrite", "CronList", "BashOutput", "TaskOutput", "NotebookRead", "ReadMcp",
                     "xRead", "Reader"):
            self.assertEqual([m for m, _ in self.entries() if matcher_hits(m, tool)], [], tool)

    def test_the_fast_path_takes_only_reads(self):
        fast = [m for m, c in self.entries() if "guard_fast.py" in c]
        self.assertEqual(fast, ["Read|Grep|Glob|LS"])


if __name__ == "__main__":
    unittest.main()
