"""Minimal MCP stdio client for the hub watcher (no model, Python 3.9 stdlib only).

`Client` speaks newline-delimited JSON-RPC to a server process. `ReadOnlyClient` wraps it with an allow-list so
the watcher can only ever call read tools; adapters only receive a `ReadOnlyClient`.
"""
import json
import os
import queue
import subprocess
import threading
import time
from typing import Any, Dict, List, Set

import health

PROTOCOL = "2025-06-18"


class McpError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__("%s: %s" % (kind, message))
        self.kind = kind
        self.message = message


class ForbiddenTool(Exception):
    """A tool name outside the read allow-list; a programming error, raised before anything is sent."""


def _kind(message: str) -> str:
    return health.classify(message)[1]


class Client:
    def __init__(self, command: str, args: List[str], env: Dict[str, str], timeout: float = 30):
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.timeout = timeout
        self.tools = []  # type: List[str]
        self._proc = None
        self._q = None  # type: Any
        self._id = 0

    # -- process plumbing
    def _spawn(self) -> None:
        env = dict(os.environ)
        env.update(self.env)
        self._proc = subprocess.Popen([self.command] + self.args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, env=env, universal_newlines=True, bufsize=1)
        self._q = queue.Queue()
        threading.Thread(target=self._pump, args=(self._proc, self._q), daemon=True).start()

    @staticmethod
    def _pump(proc, q) -> None:
        for line in proc.stdout:
            line = line.strip()
            if line:
                try:
                    q.put(json.loads(line))
                except ValueError:
                    pass
        q.put(None)  # EOF

    def _send(self, method: str, params=None, notify: bool = False):
        if self._proc is None:
            raise EOFError("server not running")  # closed after a timeout or crash; the caller respawns
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        try:
            self._proc.stdin.write(json.dumps(msg) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            raise EOFError("server closed")
        if notify:
            return None
        want = self._id
        deadline = time.monotonic() + self.timeout  # one deadline per call: notifications don't extend it
        while True:
            try:
                r = self._q.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                self.close()
                raise McpError("unreachable", "no reply to %s within %ss" % (method, self.timeout))
            if r is None:
                raise EOFError("server closed")
            if r.get("id") == want:
                if "error" in r:
                    m = r["error"].get("message", "") if isinstance(r["error"], dict) else str(r["error"])
                    raise McpError(_kind(m), m)
                return r.get("result")

    def _handshake(self) -> None:
        self._send("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                  "clientInfo": {"name": "mother-hub", "version": "0"}})
        self._send("notifications/initialized", notify=True)
        # The gateway refuses tools/call until tools/list has been seen.
        res = self._send("tools/list") or {}
        self.tools = [t["name"] for t in res.get("tools", [])]

    # -- public
    def start(self) -> None:
        self._spawn()
        try:
            self._handshake()
        except EOFError:
            self.close()
            raise McpError("dropped", "server closed during start")

    def call(self, name: str, arguments: dict) -> Any:
        params = {"name": name, "arguments": arguments or {}}
        try:
            res = self._send("tools/call", params)
        except EOFError:
            # One restart per call: a later crash gets its own restart.
            self.close()
            self.start()
            try:
                res = self._send("tools/call", params)
            except EOFError:
                raise McpError("dropped", "server closed")
        res = res or {}
        text = ""
        for c in res.get("content", []):
            if c.get("type", "text") == "text":
                text = c.get("text", "")
                break
        if res.get("isError"):
            raise McpError(_kind(text), text[:300])
        try:
            return json.loads(text)
        except ValueError:
            return text

    def close(self) -> None:
        p, self._proc = self._proc, None
        if p is None:
            return
        try:
            p.stdin.close()
        except Exception:
            pass
        try:
            p.stdout.close()
        except Exception:
            pass
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


def resolve_tools(tools: List[str], suffixes: Set[str]) -> Set[str]:
    """Full tool names whose last `__` segment equals one of `suffixes` (the gateway prefix varies)."""
    return {t for t in tools if t.rsplit("__", 1)[-1] in suffixes}


class ReadOnlyClient:
    """Calls only tools on the read allow-list. `allowed` are the full names resolved against the server; `suffixes`
    (the adapter's READ_TOOLS) say what may be asked for at all. A name outside `suffixes` is a programming error
    (`ForbiddenTool`); an allowed suffix the server does not offer, or offers twice, is a source failure
    (`McpError`), so one missing read tool marks that source instead of stopping the watcher."""

    def __init__(self, client, allowed: Set[str], suffixes: Set[str] = None):
        self._client = client
        self.allowed = set(allowed)
        self.suffixes = set(suffixes) if suffixes is not None else {a.rsplit("__", 1)[-1] for a in self.allowed}

    @property
    def tools(self) -> List[str]:
        return self._client.tools

    def call(self, name: str, arguments: dict = None) -> Any:
        full = name
        if full not in self.allowed:
            suffix = name.rsplit("__", 1)[-1]
            if suffix not in self.suffixes:
                raise ForbiddenTool("tool not on the read allow-list: %s" % name)
            hits = [a for a in self.allowed if a.rsplit("__", 1)[-1] == suffix]
            if len(hits) != 1:
                raise McpError("other", "the server offers %s read tool %s" % (
                    "no" if not hits else "more than one", suffix))
            full = hits[0]
        return self._client.call(full, arguments or {})

    def close(self) -> None:
        self._client.close()
