"""Everything the plugin needs at runtime must be tracked by git — tests pass on a dev checkout even when a
file never reaches users (this happened: an over-broad .gitignore dropped templates/workspace/)."""
import os
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


def load_guard():
    import importlib.util
    spec = importlib.util.spec_from_file_location("guard_pkg", str(ROOT / "hooks" / "guard.py"))
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    return g


TOOL_NAME_RE = r"^(?:[A-Z][a-z]+(?:[A-Z][a-z]*)*|LS)$|^mcp__"


def guard_tool_names(g=None):
    """Every tool name hooks/guard.py has a rule for, found in its code: each comparison whose left side reads
    `tool` (`tool == "X"`, `tool in ("X", …)`, `tool in NAME`, `tool not in NAME[kind]`), with module-level names
    resolved to their values (sets, tuples, dicts by value)."""
    import ast
    import re
    g = g or load_guard()

    def strings(node):
        if isinstance(node, ast.Constant):
            return {node.value} if isinstance(node.value, str) else set()
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return set().union(*[strings(e) for e in node.elts]) if node.elts else set()
        if isinstance(node, ast.Subscript):
            node = node.value
        if isinstance(node, ast.Name) and hasattr(g, node.id):
            v = getattr(g, node.id)
            vals = v.values() if isinstance(v, dict) else v if isinstance(v, (set, frozenset, tuple, list)) else []
            out = set()
            for x in vals:
                out |= {x} if isinstance(x, str) else set(y for y in x if isinstance(y, str)) \
                    if isinstance(x, (set, frozenset, tuple, list)) else set()
            return out
        if isinstance(node, ast.BoolOp) or isinstance(node, ast.BinOp):
            return set().union(*[strings(e) for e in ast.iter_child_nodes(node)])
        return set()

    names = set()
    for node in ast.walk(ast.parse((ROOT / "hooks" / "guard.py").read_text())):
        if isinstance(node, ast.Compare) and any(isinstance(n, ast.Name) and n.id == "tool"
                                                 for n in ast.walk(node.left)):
            for c in node.comparators:
                names |= strings(c)
    return {n for n in names if re.match(TOOL_NAME_RE, n)}


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

    def test_every_tool_reaches_the_guard_exactly_once(self):
        entries = self.entries()
        g = load_guard()
        names = guard_tool_names(g)
        for must in ("Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "Read", "Grep", "Glob", "LS", "WebFetch",
                     "WebSearch", "Task", "Agent", "Skill", "Monitor", "CronCreate", "CronDelete", "TaskStop",
                     "SendMessage", "ListAgents", "ToolSearch", "CronList", "TodoWrite",
                     "mcp__ccd_session__hand_off_to_session", "mcp__ccd_session_mgmt__send_message"):
            self.assertIn(must, names)
        main = [m for m, c in entries if c.endswith('guard.py"')]
        self.assertEqual(len(main), 1)
        for tool in sorted(names) + ["mcp__x__post_message", "mcp__terminal__run_in_terminal", "Artifact",
                                     "RemoteTrigger", "PushNotification", "BashOutput", "TaskOutput", "xRead",
                                     "ReadMcp", "Bash2", "mcp_x", "", "MCP__x"]:
            hits = [m for m, _ in entries if matcher_hits(m, tool)]
            self.assertEqual(len(hits), 1, "%r matched by %s" % (tool, hits))
        # what guard.py has a rule for in every session goes to it directly; the fast path takes only the tools
        # whose rules are its four cases (Read/Grep/Glob/LS, the hub's and the sub-agents' allow-lists)
        fast_ok = {"Read", "Grep", "Glob", "LS"} | set(g.HUB_PASS_TOOLS) | set(g.SUBAGENT_TOOLS["reader"])
        for tool in sorted(names - fast_ok):
            self.assertTrue(matcher_hits(main[0], tool), tool)

    def test_the_fast_path_knows_the_guards_sub_agents_and_hub_files(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("guard_fast_pkg", str(ROOT / "hooks" / "guard_fast.py"))
        f = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(f)
        g = load_guard()
        self.assertEqual(set(f.SUBAGENTS), set(g.SUBAGENT_TOOLS))
        self.assertEqual(os.path.basename(g.hub_session_path("/w")), "hub-session.json")
        self.assertEqual(os.path.basename(g.hub_fed_path("/w")), "fed-sessions.json")
        self.assertEqual(set(f.HUB_FILES), {"hub-session.json", "fed-sessions.json", "sessions.json"})
        self.assertEqual(set(f.KEY_TOOLS), {"Read", "Grep", "Glob", "LS"})

    def test_the_main_entry_takes_only_the_guarded_tools(self):
        main = [m for m, c in self.entries() if c.endswith('guard.py"')][0]
        for tool in ("ToolSearch", "TodoWrite", "CronList", "BashOutput", "TaskOutput", "NotebookRead", "ReadMcp",
                     "Read", "Artifact"):
            self.assertFalse(matcher_hits(main, tool), tool)
