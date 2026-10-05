"""End-to-end smoke test of the MCP server over real stdio JSON-RPC.

This is the surface a Claude Code session actually talks to, and until now
it had no test at all - every other test exercised query_lib directly and
sailed past the server. That gap hid a total outage: requirements.txt said
`mcp>=1.0.0`, a fresh install resolved to mcp 2.x where FastMCP was renamed
to MCPServer, and the server died at import with CONNECTION_CLOSED. Nothing
in the suite noticed, because nothing had ever launched it.

So these tests launch the real process and speak the real protocol.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from conftest import PY_FIXTURE, SRC_DIR, run_index

mcp_sdk = pytest.importorskip("mcp", reason="mcp SDK not installed (not needed to index or query)")

SERVER = ["-m", "codegraph.cg_mcp_server"]
PROTOCOL_VERSION = "2024-11-05"


class Server:
    """Minimal stdio JSON-RPC client for the server under test."""

    def __init__(self, db: str):
        self.proc = subprocess.Popen(
            [sys.executable, *SERVER, "--db", db],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
            env={**os.environ, "PYTHONPATH": SRC_DIR + os.pathsep + os.environ.get("PYTHONPATH", "")},
        )

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _read(self):
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError(f"server closed the connection; stderr:\n{self.proc.stderr.read()}")
        return json.loads(line)

    def initialize(self):
        self._send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "codegraph-tests", "version": "1"}}})
        resp = self._read()
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return resp

    def list_tools(self):
        self._send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        return self._read()["result"]["tools"]

    def call(self, name, **arguments):
        self._send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}})
        result = self._read()["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            raise AssertionError(f"tool {name} errored: {text}\n{self.proc.stderr.readline()}")
        return json.loads(text)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    db = str(tmp_path_factory.mktemp("mcp") / "graph.db")
    run_index(PY_FIXTURE, db)
    s = Server(db)
    s.initialize()
    yield s
    s.close()


def test_server_starts_and_completes_the_handshake(server):
    """The regression this file exists for: an SDK rename killed the server
    at import, and `claude mcp get` reported only CONNECTION_CLOSED."""
    assert server.proc.poll() is None


def test_every_documented_tool_is_registered(server):
    names = {t["name"] for t in server.list_tools()}
    assert names == {
        "search", "search_code", "search_memory", "neighbors", "impacted_by",
        "path_between", "add_note", "search_sessions", "list_sessions",
    }


def test_every_tool_has_a_description(server):
    for tool in server.list_tools():
        assert tool.get("description"), f"{tool['name']} has no description"


def test_search_returns_ranked_results_with_totals(server):
    res = server.call("search", query="factorial")
    assert any(r["node"]["id"] == "recursive.py::factorial" for r in res["results"])
    assert "total" in res and "truncated" in res


def test_neighbors_reports_totals(server):
    res = server.call("neighbors", symbol="nested_calls.py::outer", direction="out")
    assert {e["target"] for e in res["outgoing"] if e["type"] == "calls"} == {
        "nested_calls.py::helper_a", "nested_calls.py::outer.inner"}
    assert res["truncated"] is False
    assert "outgoing_total" in res


def test_impacted_by_is_bounded_and_reports_truncation(server):
    res = server.call("impacted_by", symbol="nested_calls.py::helper_b")
    assert {n["id"] for n in res["impacted"]} >= {"nested_calls.py::outer.inner"}
    assert res["truncated"] is False
    assert all(set(n) <= {"id", "kind", "lineno", "depth"} for n in res["impacted"])

    capped = server.call("impacted_by", symbol="nested_calls.py::helper_b", limit=1)
    assert len(capped["impacted"]) == 1
    assert capped["truncated"] is True


def test_add_note_then_find_it_through_search_memory(server):
    stored = server.call("add_note", note="mcp round-trip note",
                         symbol="recursive.py::factorial", session_id="test")
    assert stored["stored_against"] == "recursive.py::factorial"
    found = server.call("search_memory", query="round-trip")
    assert any(n["note"] == "mcp round-trip note" for n in found["notes"])


def test_add_note_refuses_an_ambiguous_symbol_over_the_wire(server):
    res = server.call("add_note", note="should not store", symbol="run", session_id="test")
    assert res["error"] == "unresolved"


def test_path_between_over_the_wire(server):
    res = server.call("path_between", a="nested_calls.py::outer", b="nested_calls.py::helper_b")
    assert res["path"][0]["id"] == "nested_calls.py::outer"
    assert res["path"][-1]["id"] == "nested_calls.py::helper_b"


def test_list_sessions_is_empty_without_opt_in_indexing(server):
    assert server.call("list_sessions")["sessions"] == []


def test_repeated_calls_work_across_dispatch_threads(server):
    """mcp 2.x runs each tool call on a worker thread. A single shared
    sqlite3 connection made every call fail with "SQLite objects created in
    a thread can only be used in that same thread" - so hammer it."""
    for _ in range(12):
        res = server.call("search_code", query="factorial")
        assert any(n["id"] == "recursive.py::factorial" for n in res["results"])
