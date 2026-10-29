"""Model-free prompt-injection regression: the attack corpus × session type × permission mode.

tests/fixtures/redteam_corpus.jsonl holds messages a person who can reach the manager might send (chat, mail,
tracker, calendar, code), each with the tool calls a model that obeyed it would make. For every call, in every
session type and mode, the guard's decision must be the expected one: never `none` or `allow` for an attack
that reaches anyone, unless the row says so. No model runs; the guard is called in process, in a temp workspace.

Session types: hub (the pinned hub), card (any workspace session), channel_manager / channel_team (a channel
reply session), reader (the mother:reader sub-agent). A row may name one session instead, such as live (a card
the hub routes to: its id is in sessions.json, so it is hub-fed) or old_hub. Modes: default, acceptEdits, bypassPermissions (with
policy.bypass_passes_asks off, the default), paused (.mother/PAUSED), dryrun (MOTHER_DRY_RUN=1).

`want` is the decision in default mode. acceptEdits and bypass expect the same. paused and dryrun expect `deny`
for anything that isn't `none`, unless the row's `paused` / `dryrun` map says otherwise (an edit to the guard's
or the persona's files, and a session message, stay a question under PAUSED).

To see the attacks that succeeded before a fix, point MOTHER_REDTEAM_GUARD at an older hooks/guard.py.
Run: python3 -m unittest tests.test_redteam -v
"""
import contextlib
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, "tests", "fixtures", "redteam_corpus.jsonl")
GUARD = os.environ.get("MOTHER_REDTEAM_GUARD") or os.path.join(ROOT, "hooks", "guard.py")

SESSIONS = ("hub", "card", "channel_manager", "channel_team", "reader")
MODES = ("default", "acceptEdits", "bypassPermissions", "paused", "dryrun")
HUB, OLD_HUB, CARD = "local_hub-0000", "local_hub-old", "local_card-1111"
LIVE = "11111111-1111-1111-1111-111111111111"
LOOP = "local_channels-2222"  # the channels loop's session: it ran `mother channels plan`
ENV_KEYS = ("MOTHER_WORKSPACE", "MOTHER_STATE_DIR", "MOTHER_DRY_RUN", "MOTHER_CHANNEL_ROLE", "MOTHER_CHANNEL_SERVERS")


def load_guard():
    spec = importlib.util.spec_from_file_location("redteam_guard", GUARD)
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    return g


def corpus():
    with open(CORPUS) as f:
        return [json.loads(line) for line in f if line.strip()]


def config():
    return {"version": 1,
            "agent": {"name": "Agnes", "manager": "Dana Levi", "manager_short": "Dana"},
            "manager": {"email": "dana@example.com", "timezone": "Europe/London", "chat_dm": "U00000000"},
            "workspace": {"git_remote": None, "home_machine": None},
            "connectors": {"chat": "slack", "tracker": "jira", "mail": "gmail", "calendar": "google"},
            "teams": [{"slug": "platform", "name": "Platform", "tracker": {"project": "PLAT", "board": 1},
                       "channels": [{"id": "C00000001", "name": "#platform", "mode": "mention", "trigger": "team"}]}],
            "policy": {}}


def expected(call, session, mode):
    want = call["want"][session]
    if mode in ("paused", "dryrun"):
        over = call.get(mode) or {}
        if session in over:
            return over[session]
        return "none" if want == "none" else "deny"
    return want


class RedTeam(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = load_guard()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mother-redteam-")
        self.ws = os.path.realpath(os.path.join(self.tmp, "ws"))
        os.makedirs(os.path.join(self.ws, ".mother", "hub"))
        os.makedirs(os.path.join(self.ws, "memory"))
        with open(os.path.join(self.ws, "config.json"), "w") as f:
            json.dump(config(), f)
        with open(os.path.join(self.ws, ".mother", "hub", "sessions.json"), "w") as f:
            json.dump({"version": 1, "sessions": {"PLAT-1": {"session_id": LIVE, "name": "plat-1", "title": "t",
                                                             "also": [], "state": "live"}}}, f)
        self.saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["MOTHER_WORKSPACE"] = self.ws
        os.environ["MOTHER_STATE_DIR"] = os.path.join(self.tmp, "state")
        # two hub sessions: the older one must stay fenced after the newer one ran `mother hub tail`
        for sid in (OLD_HUB, HUB):
            self.g.run(json.dumps({"tool_name": "Bash", "tool_input": {"command": "mother hub tail --once"},
                                   "session_id": sid, "cwd": self.ws, "permission_mode": "default"}))
        self.assertEqual(self.g.hub_session_ids(self.ws)[:2], [HUB, OLD_HUB])
        self.g.run(json.dumps({"tool_name": "Bash", "tool_input": {"command": "mother channels plan"},
                               "session_id": LOOP, "cwd": self.ws, "permission_mode": "default"}))
        if hasattr(self.g, "channels_session_ids"):  # MOTHER_REDTEAM_GUARD may point at an older guard
            self.assertEqual(self.g.channels_session_ids(self.ws), [LOOP])

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp)

    @contextlib.contextmanager
    def env(self, **kw):
        old = {k: os.environ.get(k) for k in kw}
        for k, v in kw.items():
            os.environ[k] = v
        try:
            yield
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def decide(self, call, session, mode):
        state = os.path.join(self.tmp, "state")
        inp = json.loads(json.dumps(call["input"]).replace("$W", self.ws).replace("$LIVE", LIVE)
                         .replace("$STATE", state))
        payload = {"tool_name": call["tool"], "tool_input": inp, "cwd": self.ws,
                   "permission_mode": mode if mode in ("acceptEdits", "bypassPermissions") else "default",
                   "session_id": {"hub": HUB, "old_hub": OLD_HUB, "live": LIVE, "channels": LOOP}.get(session, CARD)}
        env = {}
        if session == "reader":
            payload.update(agent_id="a1", agent_type="mother:reader")
        if session.startswith("channel_"):
            env = {"MOTHER_CHANNEL_ROLE": session[len("channel_"):], "MOTHER_CHANNEL_SERVERS": "mcp__tracker__jira"}
        if mode == "dryrun":
            env["MOTHER_DRY_RUN"] = "1"
        paused = os.path.join(self.ws, ".mother", "PAUSED")
        if mode == "paused":
            open(paused, "w").close()
        hubfile = os.path.join(self.ws, ".mother", "hub", "hub-session.json")
        fedfile = os.path.join(self.ws, ".mother", "hub", "fed-sessions.json")
        chanfile = os.path.join(self.ws, ".mother", "channels-session.json")
        with open(hubfile) as f:
            hub_before = f.read()  # a row that runs `mother hub tail` must not make later rows hub sessions
        fed_before = chan_before = None  # nor `mother hub …` hub-fed ones, nor `mother channels plan` loop ones
        if os.path.exists(fedfile):
            with open(fedfile) as f:
                fed_before = f.read()
        if os.path.exists(chanfile):
            with open(chanfile) as f:
                chan_before = f.read()
        try:
            with self.env(**env):
                r = self.g.run(json.dumps(payload))
        finally:
            if os.path.exists(paused):
                os.remove(paused)
            with open(hubfile, "w") as f:
                f.write(hub_before)
            if chan_before is None:
                if os.path.exists(chanfile):
                    os.remove(chanfile)
            else:
                with open(chanfile, "w") as f:
                    f.write(chan_before)
            if fed_before is None:
                if os.path.exists(fedfile):
                    os.remove(fedfile)
            else:
                with open(fedfile, "w") as f:
                    f.write(fed_before)
        return (r[0], r[1]) if r else ("none", "")

    def test_corpus_has_invented_data_only(self):
        with open(CORPUS) as f:
            text = f.read()
        for host in __import__("re").findall(r"[\w.-]+\.(?:com|net|org|io)\b", text):
            self.assertTrue(host.endswith("example.com"), host)

    def test_every_attack_is_stopped(self):
        rows = failures = 0
        bad = []
        for atk in corpus():
            for n, call in enumerate(atk["calls"]):
                sessions = [call["session"]] if call.get("session") else SESSIONS
                for session in sessions:
                    for mode in MODES:
                        want = expected(call, session, mode)
                        got, why = self.decide(call, session, mode)
                        rows += 1
                        if got != want:
                            failures += 1
                            bad.append("%s#%d %-16s %-17s want %-5s got %-5s fix=%s  %s" % (
                                atk["id"], n, session, mode, want, got, call.get("fix", "-"), why[:110]))
        self.assertGreater(rows, 400)
        self.assertEqual(bad, [], "\n%d of %d rows differ:\n%s" % (failures, rows, "\n".join(bad)))

    def test_routine_commands_run_without_a_question(self):
        """Every shell command the skills and routines run in a workspace session is on the allow-list: no question
        in default or bypass mode, and none under PAUSED (they reach no one). The channels loop's reply verbs run in
        its own session (the one that ran `mother channels plan`) and ask in any other."""
        path = os.path.join(ROOT, "tests", "fixtures", "routine_commands.txt")
        with open(path) as f:
            body = "".join(l for l in f if not l.startswith("#"))
        cmds = [c.strip("\n") for c in body.split("\n----\n") if c.strip()]
        self.assertGreater(len(cmds), 40)
        plugin = os.path.dirname(GUARD).rsplit(os.sep, 1)[0]
        bad = []
        with self.env(CLAUDE_PLUGIN_ROOT=plugin):
            for cmd in cmds:
                call = {"tool": "Bash", "input": {"command": cmd}}
                loop = self.g.channels_loop_shell(__import__("re").sub(r"[\"'\\]", "", cmd).casefold())
                for mode in ("default", "bypassPermissions", "paused"):
                    got, why = self.decide(call, "channels" if loop else "card", mode)
                    if got != "none":
                        bad.append("%s [%s] -> %s: %s" % (cmd.splitlines()[0], mode, got, why[:120]))
                if loop:
                    got, why = self.decide(call, "card", "default")
                    if got != "ask":
                        bad.append("%s [card] -> %s, want ask" % (cmd.splitlines()[0], got))
        self.assertEqual(bad, [], "\n" + "\n".join(bad))

    def test_no_attack_call_runs_unasked_in_a_workspace_session(self):
        """Whatever the table says, an attack call that reaches someone is never `allow` in any session or mode."""
        for atk in corpus():
            for call in atk["calls"]:
                for session in ([call["session"]] if call.get("session") else SESSIONS):
                    for mode in MODES:
                        got, _ = self.decide(call, session, mode)
                        self.assertNotEqual(got, "allow", "%s %s %s %s" % (atk["id"], call["tool"], session, mode))


if __name__ == "__main__":
    unittest.main()
