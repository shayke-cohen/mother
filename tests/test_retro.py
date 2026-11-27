"""Tests for lib/retro_signals.py and `mother retro`. Stdlib only: python3 -m unittest tests.test_retro -v"""
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import tempfile as _tf_spool
os.environ["MOTHER_FLOOR_SPOOL"] = _tf_spool.mkdtemp(prefix="mother-spool-test-")   # never fold the real spool


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import retro_signals as rs  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "retro" / "projects"
NOW = dt.datetime(2026, 11, 27, 12, 0, tzinfo=dt.timezone.utc)
HOME = "/home/agnes"
CLI = ROOT / "bin" / "mother"


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.projects = self.t / "projects"
        shutil.copytree(str(FIXTURE), str(self.projects))
        recent = (NOW - dt.timedelta(hours=1)).timestamp()
        for p in self.projects.rglob("*.jsonl"):
            os.utime(str(p), (recent, recent))

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, **kw):
        kw.setdefault("min_kb", 0)
        kw.setdefault("home", HOME)
        kw.setdefault("parallel", False)
        rows, files = rs.scan(self.projects, 7, now=NOW, **kw)
        return {r["session"]: r for r in rows}, files


class Signals(Tmp):
    def test_human_turns_pings_nudges_corrections_and_the_rest(self):
        rows, _ = self.scan()
        s = rows["s-main-0001"]
        self.assertEqual(s["human_turns"], 5)      # not the sidechain, the command, the stop hook, the interrupt
        self.assertEqual((s["status_pings"], s["nudges"], s["corrections"]), (2, 1, 1))
        self.assertEqual((s["interrupts"], s["stop_blocks"], s["api_errors"], s["compactions"]), (1, 1, 1, 1))
        self.assertEqual((s["tool_calls"], s["tool_errors"], s["denials"], s["subagent_calls"]), (3, 1, 1, 1))
        self.assertEqual(s["error_tools"], {"Bash": 1})
        self.assertEqual(s["wait_cmds"], 1)
        self.assertEqual(s["first_prompt"], "Fix the login bug on the settings page")
        self.assertEqual(s["title"], "Fix the login flow")
        self.assertEqual(s["friction"], 3 * 2 + 1 + 2 + 2 + 2 + 1 + 2 + 1)

    def test_only_events_inside_the_window_count(self):
        rows, _ = self.scan()
        s = rows["s-main-0001"]
        # the old "status?" and the old 1.8M-token turn are outside the window; a repeated message id counts once
        self.assertEqual(s["main_tok_weighted_m"], 0.008)
        self.assertEqual(s["ctx_max"], 13000)
        self.assertEqual(s["start"], "2026-11-25T09:00:00.000Z")
        self.assertNotIn("s-old-0004", rows)       # recent file, but every event before the window

    def test_subagent_transcripts_count_under_their_parent(self):
        rows, files = self.scan()
        s = rows["s-main-0001"]
        self.assertEqual((s["sub_files"], s["sub_tool_calls"], s["sub_tool_errors"]), (1, 1, 1))
        self.assertEqual(s["sub_tok_weighted_m"], 0.6)
        self.assertEqual(s["tok_weighted_m"], 0.608)
        self.assertTrue(s["subagents_dir"].endswith(os.path.join("s-main-0001", "subagents")))
        self.assertEqual(s["status_pings"], 2)     # the sub-agent's "?" is not the manager's
        self.assertNotIn("agent-a1", rows)
        self.assertEqual(files, 5)

    def test_a_subagent_only_session_still_counts(self):
        (self.projects / "-home-agnes-src-web" / "s-main-0001.jsonl").unlink()
        rows, _ = self.scan()
        s = rows["s-main-0001"]
        self.assertEqual((s["human_turns"], s["sub_files"]), (0, 1))
        self.assertTrue(s["file"].endswith("agent-a1.jsonl"))
        self.assertEqual(s["area"], "web")

    def test_fork_replay_lines_are_skipped(self):
        rows, _ = self.scan()
        f = rows["s-fork-0002"]
        self.assertEqual(f["forked_from"], "s-main-0001")
        self.assertEqual((f["human_turns"], f["status_pings"]), (1, 0))
        self.assertEqual(f["tok_weighted_m"], 0.003)   # not the parent's 30M replayed
        self.assertEqual(f["start"], "2026-11-26T08:00:00.000Z")

    def test_origin_decides_who_typed_it(self):
        rows, _ = self.scan()
        d = rows["s-dev3-0003"]
        self.assertEqual((d["human_turns"], d["status_pings"]), (1, 1))
        self.assertEqual((d["repeat_cmds"], d["repeats"]), ({"npm test": 5}, 2))

    def test_mtime_and_size_filters(self):
        old = (NOW - dt.timedelta(days=9)).timestamp()
        p = self.projects / "-home-agnes-src-web" / "s-fork-0002.jsonl"
        os.utime(str(p), (old, old))
        rows, _ = self.scan()
        self.assertNotIn("s-fork-0002", rows)
        rows, _ = self.scan(min_kb=4)              # main + sub-agent add up past 4 KB; the dev3 file doesn't
        self.assertEqual(sorted(rows), ["s-main-0001"])

    def test_areas_and_the_areas_map(self):
        rows, _ = self.scan(areas={"src-api": "api"})
        self.assertEqual({k: r["area"] for k, r in rows.items()},
                         {"s-main-0001": "web", "s-fork-0002": "web", "s-dev3-0003": "api"})

    def test_parallel_scan_gives_the_same_rows(self):
        # through the CLI: a pool started from under `python -m unittest` would re-run the test runner
        def run(floor):
            env = {k: v for k, v in os.environ.items() if not k.startswith("MOTHER_")}
            env.update(MOTHER_STATE_DIR=str(self.t / "state"), MOTHER_NOW="2026-11-27T12:00:00Z", TZ="UTC",
                       HOME=str(self.t / "home"), MOTHER_RETRO_PARALLEL_MIN=floor)
            r = subprocess.run([sys.executable, str(CLI), "retro", "signals", "--projects", str(self.projects),
                                "--min-kb", "0", "--out", str(self.t / ("out" + floor))],
                               capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            return (self.t / ("out" + floor) / "sessions.jsonl").read_text().splitlines()
        self.assertEqual(sorted(run("0")), sorted(run("999")))


PARENT, FORK = "s-zz-parent", "s-aa-fork"    # the fork sorts first, so path order alone would pick it as owner


def _line(uuid, parent, sid, ts, kind, content=None, **extra):
    o = {"type": kind, "uuid": uuid, "parentUuid": parent, "sessionId": sid, "timestamp": ts,
         "cwd": "/home/agnes/src/web"}
    if content is not None:
        o["message"] = content
    o.update(extra)
    return o


def _parent_lines():
    return [
        _line("p1", None, PARENT, "2026-11-25T09:00:00.000Z", "user", {"role": "user", "content": "Fix the build"}),
        _line("p2", "p1", PARENT, "2026-11-25T09:00:05.000Z", "assistant",
              {"id": "pm1", "role": "assistant", "usage": {"input_tokens": 1000000, "output_tokens": 0},
               "content": [{"type": "tool_use", "id": "pt1", "name": "Bash", "input": {"command": "make"}}]}),
        _line("p3", "p2", PARENT, "2026-11-25T09:00:09.000Z", "user",
              {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "pt1", "is_error": True,
                                            "content": "exit 2"}]}),
        {"type": "cost-state", "sessionId": PARENT, "totalLinesAdded": 40},
        _line("p4", "p3", PARENT, "2026-11-25T09:30:00.000Z", "user", {"role": "user", "content": "?"}),
        _line("p5", "p4", PARENT, "2026-11-25T09:30:05.000Z", "assistant",
              {"id": "pm2", "role": "assistant", "usage": {"input_tokens": 0, "output_tokens": 100000},
               "content": []}),
        _line("p6", "p5", PARENT, "2026-11-25T09:31:00.000Z", "user",
              {"role": "user", "content": "no, that's wrong"}),
    ]


def _fork_own_lines(marker=False):
    extra = {"forkedFrom": {"sessionId": PARENT, "messageUuid": "p6"}} if marker else {}
    return [
        _line("f1", "p6", FORK, "2026-11-26T08:00:00.000Z", "user", {"role": "user", "content": "add a test"},
              **extra),
        _line("f2", "f1", FORK, "2026-11-26T08:00:05.000Z", "assistant",
              {"id": "fm1", "role": "assistant", "usage": {"input_tokens": 2000, "output_tokens": 200},
               "content": [{"type": "tool_use", "id": "ft1", "name": "Bash", "input": {"command": "make test"}}]},
              **extra),
        _line("f3", "f2", FORK, "2026-11-26T08:00:09.000Z", "user",
              {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "ft1", "is_error": True,
                                            "content": "1 failed"}]}, **extra),
        {"type": "cost-state", "sessionId": FORK, "totalLinesAdded": 55},
    ]


class ForkReplay(unittest.TestCase):
    """A fork replays its parent's lines, with their uuids, then adds its own. Only its own count."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name) / "projects"
        self.d = self.projects / "-home-agnes-src-web"
        self.d.mkdir(parents=True)
        self.write(PARENT, _parent_lines())

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, lines, sub=None):
        p = self.d / (name + ".jsonl") if sub is None else self.d / name / "subagents" / (sub + ".jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps(o) + "\n" for o in lines))
        recent = (NOW - dt.timedelta(hours=1)).timestamp()
        os.utime(str(p), (recent, recent))
        return p

    def fork(self, replay_sid, marker=False, drop_first=False):
        replay = [dict(o, sessionId=replay_sid) for o in _parent_lines()]
        if drop_first:      # the fork opens with a summary instead of the parent's first line
            replay = [{"type": "summary", "summary": "Fixing the build", "leafUuid": "p6"}] + replay[1:]
        self.write(FORK, replay + _fork_own_lines(marker))

    def scan(self):
        rows, _ = rs.scan(self.projects, 7, min_kb=0, now=NOW, home=HOME, parallel=False)
        return {r["session"]: r for r in rows}

    def assert_fork_counts_only_its_own(self, f):
        self.assertEqual(f["forked_from"], PARENT)
        self.assertEqual((f["human_turns"], f["status_pings"], f["corrections"]), (1, 0, 0))
        self.assertEqual((f["tool_calls"], f["tool_errors"]), (1, 1))
        self.assertEqual(f["tok_weighted_m"], 0.003)           # 2000 in + 5 x 200 out, not the parent's 1.5M
        self.assertEqual(f["start"], "2026-11-26T08:00:00.000Z")
        self.assertEqual(f["first_prompt"], "add a test")
        self.assertEqual(f["lines_added"], 15)                  # 55 at the end, 40 of them the parent's
        self.assertEqual(f["friction"], 1)

    def assert_parent_unchanged(self, rows):
        p = rows[PARENT]
        self.assertEqual(p["forked_from"], "")
        self.assertEqual((p["human_turns"], p["status_pings"], p["corrections"], p["tool_errors"]), (3, 1, 1, 1))
        self.assertEqual(p["tok_weighted_m"], 1.5)
        self.assertEqual((p["lines_added"], p["replayed"]), (40, 0))

    def test_replay_under_the_parents_session_id(self):
        # the layout every real fork has shown so far
        self.fork(PARENT)
        rows = self.scan()
        self.assert_fork_counts_only_its_own(rows[FORK])
        self.assert_parent_unchanged(rows)

    def test_replay_under_the_forks_own_session_id_with_a_marker(self):
        self.fork(FORK, marker=True)
        rows = self.scan()
        self.assert_fork_counts_only_its_own(rows[FORK])
        self.assertEqual(rows[FORK]["replayed"], 6)
        self.assert_parent_unchanged(rows)

    def test_replay_under_the_forks_own_session_id_after_a_summary(self):
        self.fork(FORK, drop_first=True)       # no marker: the parent's earlier first event decides
        rows = self.scan()
        f = rows[FORK]
        self.assert_fork_counts_only_its_own(f)
        self.assertEqual(f["replayed"], 5)
        self.assert_parent_unchanged(rows)

    def test_the_parent_is_the_same_with_or_without_its_fork(self):
        alone = self.scan()[PARENT]
        self.fork(FORK, marker=True)
        with_fork = self.scan()[PARENT]
        self.assertEqual(alone, with_fork)

    def test_a_forks_copy_of_a_parent_subagent_is_not_counted(self):
        agent = [_line("a1", None, PARENT, "2026-11-25T09:10:00.000Z", "assistant",
                       {"id": "am1", "role": "assistant", "usage": {"input_tokens": 400000, "output_tokens": 0},
                        "content": [{"type": "tool_use", "id": "at1", "name": "Grep", "input": {}}]},
                       isSidechain=True)]
        mine = [_line("b1", None, FORK, "2026-11-26T08:10:00.000Z", "assistant",
                      {"id": "bm1", "role": "assistant", "usage": {"input_tokens": 3000, "output_tokens": 0},
                       "content": []}, isSidechain=True)]
        self.write(PARENT, agent, sub="agent-x")
        self.fork(PARENT)
        self.write(FORK, agent, sub="agent-x")                 # the verbatim copy a fork carries
        self.write(FORK, mine, sub="agent-y")
        rows = self.scan()
        f, p = rows[FORK], rows[PARENT]
        self.assertEqual((f["sub_files"], f["sub_tool_calls"], f["sub_tok_weighted_m"]), (1, 0, 0.003))
        self.assertEqual((p["sub_files"], p["sub_tool_calls"], p["sub_tok_weighted_m"]), (1, 1, 0.4))

    def test_fork_index_attributes_shared_uuids_to_the_parent(self):
        self.fork(FORK, marker=True)
        fp, pp = str(self.d / (FORK + ".jsonl")), str(self.d / (PARENT + ".jsonl"))
        idx = rs.fork_index({fp: FORK, pp: PARENT})
        self.assertEqual(sorted(idx), [fp])
        self.assertEqual(idx[fp], {u: PARENT for u in ("p1", "p2", "p3", "p4", "p5", "p6")})
        self.assertEqual(rs.fork_index({pp: PARENT}), {})


class Areas(unittest.TestCase):
    def test_worktree_paths_collapse_onto_the_repo(self):
        for cwd, want in (("/home/agnes/src/web", "web"),
                          ("/home/agnes/src/web/.claude/worktrees/feat-x", "web"),
                          ("/home/agnes/src/web/.claude/worktrees/feat-x/pkg/a", "web"),
                          ("/home/agnes/src/web/.dev3/worktrees/t1", "web"),
                          ("/home/agnes/.dev3.0/worktrees/home-agnes-web/0a1b2c3d/worktree", "web"),
                          ("/home/agnes/.dev3.0/worktrees/home-agnes-web/0a1b2c3d/worktree/sub", "web"),
                          ("-home-agnes--dev3-0-worktrees-home-agnes-web-00330810-worktree", "web"),
                          ("/home/agnes", "home"), ("", "other"), (None, "other")):
            self.assertEqual(rs.repo_of(cwd, HOME), want, cwd)

    def test_dev3_names_with_dashes_resolve_on_disk(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "src", "api-server"))
            enc = home.strip("/").replace("/", "-") + "-src-api-server"
            cwd = os.path.join(home, ".dev3.0", "worktrees", enc, "0a1b2c3d", "worktree")
            self.assertEqual(rs.repo_of(cwd, home), "api-server")
            self.assertEqual(rs.repo_of(os.path.join(home, ".dev3.0", "worktrees", enc, "notahash", "worktree"), home),
                             "worktree")

    def test_a_linked_worktree_names_its_repo(self):
        with tempfile.TemporaryDirectory() as d:
            wt = os.path.join(d, "api-wt-feature")
            os.makedirs(wt)
            with open(os.path.join(wt, ".git"), "w") as f:
                f.write("gitdir: /srv/repos/api/.git/worktrees/api-wt-feature\n")
            self.assertEqual(rs.repo_of(wt, "/nowhere"), "api")

    def test_areas_map_merges_and_renames(self):
        m = {"web": "frontend", "web-admin": "frontend", "blank": "  "}
        self.assertEqual(rs.area_of("/x/web", m), "frontend")
        self.assertEqual(rs.area_of("/x/web-admin/.claude/worktrees/a", m), "frontend")
        self.assertEqual(rs.area_of("/x/blank", m), "blank")
        self.assertEqual(rs.area_of("/x/api", m), "api")


class Pings(unittest.TestCase):
    def test_status_pings(self):
        for t in ("?", "??", "status?", "are we progressing?", "Are we done here?", "are we progressing? what next?",
                  "any update?", "what is the status?", "progressing? merged?", "is it merged?", "are we progreessing?",
                  "check status and progress", "❯ status?"):
            self.assertTrue(rs.is_status_ping(t), t)
        for t in ("merged", "done", "fix the login bug?", "what next", "why did the build fail?", "", "yes, merge it"):
            self.assertFalse(rs.is_status_ping(t), t)

    def test_nudges(self):
        for t in (".", "continue", "Continue.", "go on", "keep going", "try again", "continue / retry"):
            self.assertTrue(rs.is_nudge(t), t)
        for t in ("continue with the plan", "go merge it", "?"):
            self.assertFalse(rs.is_nudge(t), t)


class Summary(Tmp):
    def test_summary_areas_readers_and_top_lists(self):
        rows, files = rs.scan(self.projects, 7, min_kb=0, now=NOW, areas={"src-api": "api"}, home=HOME, parallel=False)
        s = rs.summarize(rows, 7, files, max_areas=1)
        self.assertEqual(s["sessions"], 3)
        self.assertEqual(list(s["areas"]), ["web", "api"])          # biggest token volume first
        self.assertEqual(s["areas"]["web"]["sessions"], 2)
        self.assertEqual(s["areas"]["web"]["forks"], 1)
        self.assertEqual(s["readers"], ["web"])
        self.assertEqual([r["session"] for r in s["areas_top"]["web"]["friction"]], ["s-main-0001"])  # 0 is no row
        self.assertEqual([r["session"] for r in s["areas_top"]["web"]["tokens"]], ["s-main-0001", "s-fork-0002"])
        self.assertEqual(s["areas_top"]["web"]["tokens"][0]["session"], "s-main-0001")
        self.assertEqual(s["repeated_commands"], {"api": {"npm test": 5}})
        self.assertEqual(s["top_status_pings"][0]["session"], "s-main-0001")

    def test_delta_against_the_previous_run(self):
        cur = {"areas": {"web": {"sessions": 2, "status_pings": 2}}}
        prev = {"areas": {"web": {"sessions": 5, "status_pings": 9}, "gone": {"sessions": 1}}}
        d = rs.delta(cur, prev)
        self.assertEqual(d["web"]["status_pings"], {"was": 9, "now": 2})
        self.assertEqual(d["gone"]["sessions"], {"was": 1, "now": 0})

    def test_output_paths_prefer_the_project(self):
        ws = self.t / "ws"
        ws.mkdir()
        self.assertEqual(rs.output_paths(ws, "2026-11-27")["report"], str(ws / "retro" / "2026-11-27.md"))
        (ws / "projects" / "session-retro").mkdir(parents=True)
        p = rs.output_paths(ws, "2026-11-27")
        self.assertEqual(p["report"], str(ws / "projects" / "session-retro" / "files" / "2026-11-27-retro.md"))
        self.assertEqual(p["ledger"], str(ws / "projects" / "session-retro" / "files" / "suggestions.jsonl"))


class Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.root = self.t / "retro"
        (self.root / "2026-11-20").mkdir(parents=True)
        (self.root / "2026-11-20" / "summary.json").write_text(json.dumps({"areas": {"web": {"stop_blocks": 40}}}))
        lines = [
            {"run": "2026-11-20", "item": 1, "title": "Branch-scoped stop gate", "status": "proposed"},
            {"run": "2026-11-20", "item": 1, "status": "applied", "decided": "2026-11-21",
             "measure_next_run": "stop blocks (was 40)", "metric": {"area": "web", "key": "stop_blocks"}},
            {"run": "2026-11-20", "item": 2, "title": "Smaller model for sub-agents", "status": "rejected",
             "decided": "2026-11-21"},
            {"run": "2026-11-20", "item": 3, "title": "Device bring-up CLI", "status": "queued", "decided": "2026-11-21"},
            {"run": "2026-11-20", "item": 4, "title": "Wait recipe", "status": "proposed"},
            {"run": "2026-08-01", "item": 1, "title": "An old applied fix", "status": "merged", "decided": "2026-08-02"},
        ]
        self.path = self.t / "suggestions.jsonl"
        self.path.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_fold_and_classify(self):
        cur = {"areas": {"web": {"stop_blocks": 12}}}
        d = rs.ledger(self.path, dt.date(2026, 11, 27), 8, self.root, cur)
        self.assertEqual((d["entries"], d["bad_lines"]), (5, 1))
        self.assertEqual([(x["item"], x["reason"]) for x in d["suppress"]],
                         [(1, "applied within 8 weeks"), (2, "rejected")])
        self.assertEqual(d["suppress"][0]["title"], "Branch-scoped stop gate")   # merged over the earlier line
        self.assertEqual([x["item"] for x in d["open"]], [3])
        self.assertEqual([x["item"] for x in d["proposed"]], [4])
        m = d["measure"][0]
        self.assertEqual((m["item"], m["was"], m["now"], m["metric"]), (1, 40, 12, {"area": "web", "key": "stop_blocks"}))
        self.assertNotIn("An old applied fix", json.dumps(d))                 # past 8 weeks: may come back

    def test_missing_ledger_is_empty(self):
        d = rs.ledger(self.t / "nope.jsonl", dt.date(2026, 11, 27))
        self.assertEqual((d["entries"], d["suppress"], d["measure"]), (0, [], []))


class Cli(Tmp):
    def run_cli(self, *args, ws=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("MOTHER_")}
        env.update(MOTHER_STATE_DIR=str(self.t / "state"), MOTHER_NOW="2026-11-27T12:00:00Z", TZ="UTC",
                   HOME=str(self.t / "home"))
        if ws:
            env["MOTHER_WORKSPACE"] = str(ws)
        return subprocess.run([sys.executable, str(CLI), "retro"] + list(args), capture_output=True, text=True,
                              env=env)

    def workspace(self, **retro):
        ws = self.t / "ws"
        ws.mkdir(exist_ok=True)
        cfg = json.loads((ROOT / "templates" / "workspace" / "config.example.json").read_text())
        cfg["routines"]["retro"].update(retro)
        (ws / "config.json").write_text(json.dumps(cfg))
        return ws

    def test_signals_writes_outside_git_and_prints_the_summary(self):
        ws = self.workspace(min_kb=0, areas={"src-api": "api"})
        prev = self.t / "state" / "retro" / "2026-11-20"
        prev.mkdir(parents=True)
        (prev / "summary.json").write_text(json.dumps({"areas": {"web": {"sessions": 4, "status_pings": 6}}}))
        r = self.run_cli("signals", "--projects", str(self.projects), ws=ws)
        self.assertEqual(r.returncode, 0, r.stderr)
        s = json.loads(r.stdout)
        out = self.t / "state" / "retro" / "2026-11-27"
        self.assertEqual(s["out"], str(out))
        self.assertTrue((out / "summary.json").is_file())
        self.assertEqual(len((out / "sessions.jsonl").read_text().splitlines()), s["sessions"])
        self.assertEqual(s["previous"]["date"], "2026-11-20")
        self.assertEqual(s["delta"]["web"]["status_pings"]["was"], 6)
        self.assertEqual(s["report"], str(ws / "retro" / "2026-11-27.md"))
        self.assertFalse(any(ws.rglob("sessions.jsonl")))

    def test_signals_without_a_workspace_and_usage_errors(self):
        r = self.run_cli("signals", "--projects", str(self.projects), "--min-kb", "0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(json.loads(r.stdout)["report"])
        self.assertEqual(self.run_cli("signals", "--days", "x").returncode, 2)
        self.assertEqual(self.run_cli("signals", "--nope", "1").returncode, 2)
        self.assertEqual(self.run_cli("ledger").returncode, 2)
        self.assertEqual(self.run_cli("bogus").returncode, 2)
        self.assertEqual(self.run_cli("help").returncode, 0)

    def test_ledger_verb(self):
        p = self.t / "l.jsonl"
        p.write_text(json.dumps({"run": "2026-11-20", "item": 1, "title": "x", "status": "rejected"}) + "\n")
        r = self.run_cli("ledger", str(p))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["suppress"][0]["reason"], "rejected")

    def test_config_is_validated(self):
        for bad, msg in (({"max_areas": 9}, "routines.retro.max_areas must be an integer 1-8"),
                         ({"window_days": True}, "routines.retro.window_days must be an integer 1-31"),
                         ({"areas": {"web": 3}}, "routines.retro.areas must map a repository name to an area name")):
            ws = self.workspace(**bad)
            env = {k: v for k, v in os.environ.items() if not k.startswith("MOTHER_")}
            env.update(MOTHER_STATE_DIR=str(self.t / "state"), MOTHER_WORKSPACE=str(ws))
            r = subprocess.run([sys.executable, str(CLI), "status"], capture_output=True, text=True, env=env)
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertIn(msg, r.stdout + r.stderr)
            self.assertEqual(self.run_cli("signals", "--projects", str(self.projects), ws=ws).returncode, 1)


class Registered(unittest.TestCase):
    def test_routine_is_registered_and_documented(self):
        cfg = json.loads((ROOT / "templates" / "workspace" / "config.example.json").read_text())
        self.assertEqual(cfg["routines"]["retro"], {"at": "17:30", "days": "Thu"})
        text = (ROOT / "routines" / "retro.md").read_text()
        self.assertIn("id: retro", text)
        self.assertIn("needs: [chat.post_dm]", text)
        self.assertIn("mother:retro-reader", text)
        for doc in ("README.md", "llms.txt", "docs/CONTRACT.md", "ARCHITECTURE.md"):
            self.assertIn("retro", (ROOT / doc).read_text(), doc)
        self.assertIn("`routines.retro`", (ROOT / "docs" / "CONTRACT.md").read_text())
        self.assertTrue((ROOT / "agents" / "retro-reader.md").is_file())

    def test_nothing_private_in_the_fixtures(self):
        for p in FIXTURE.rglob("*.jsonl"):
            text = p.read_text()
            self.assertNotIn(os.path.expanduser("~"), text, p)


if __name__ == "__main__":
    unittest.main()
