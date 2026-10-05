#!/usr/bin/env python3
"""
Local MCP server exposing the codegraph (Layer 1: parsed code structure)
and session-memory notes (Layer 2: what past sessions learned) as tools
a Claude Code session can call directly, instead of grepping the repo.

This tool lives ONCE, globally, outside every repo it indexes (see
graph_lib.codegraph_home()) - indexing or querying a repo never creates
or touches a single file inside that repo's working tree, so there's
nothing for its source control to ever see. Register one server per
repo with Claude Code's user- or local-scoped MCP config (NOT the
project-scoped .mcp.json, which is meant to be committed):

    claude mcp add --scope local codegraph -- \\
        codemap-mcp --repo-root /path/to/repo

`--scope local` stores this in Claude Code's own per-user config for
that project path, not in any file inside the repo. Run that command
once per repo (from anywhere - `--repo-root` is what ties it to a
repo, not cwd).

Run standalone for a quick check:
    codemap-mcp --repo-root /path/to/repo
    codemap-mcp --db /path/to/graph.db   # or point at a DB directly

Exactly one of --repo-root / --db is required. --repo-root resolves the
DB path the same deterministic way cg_index.py does by default
(graph_lib.default_db_path) - so as long as you indexed with the same
default, you never have to know or type the actual DB path.

Note: the DB path is fixed at server start (MCP stdio servers don't take
per-call config), so one server instance = one indexed repo. Register a
separate `claude mcp add` entry per repo if you work across more than
one.

Every tool call here is logged (see usage_log.py) to usage.jsonl next to
the graph DB - not inside the repo, same zero-footprint storage as
everything else. This exists to answer a question no test so far has:
is this actually reached for in real sessions, and finding anything?
Run `codemap-usage` against the same repo/DB to see a report.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading

from . import graph_lib as gl
from . import query_lib as ql
from . import usage_log

# The server class was renamed in mcp 2.0 (FastMCP -> MCPServer); the
# decorator and run() surfaces are otherwise the same, so both are
# supported rather than pinning the SDK. Caught by actually launching this
# server: requirements.txt said `mcp>=1.0.0`, a fresh install resolved to
# 2.x, and every tool call failed at import with CONNECTION_CLOSED - the
# kind of break that only shows up when the thing is run, not tested.
try:
    from mcp.server.mcpserver import MCPServer as _ServerClass   # mcp >= 2.0
except ImportError:  # pragma: no cover - whichever branch this env lacks
    from mcp.server.fastmcp import FastMCP as _ServerClass       # mcp 1.x

mcp = _ServerClass("codegraph")

# One SQLite connection PER THREAD, not one shared one. mcp 2.x dispatches
# tool calls on worker threads, and a sqlite3 connection may only be used
# from the thread that created it - a single module-level connection made
# every tool call fail with "SQLite objects created in a thread can only be
# used in that same thread". Found by running the server, not by any test
# that called query_lib directly.
#
# Per-thread connections rather than check_same_thread=False: the DB is
# opened in WAL mode (see graph_lib.connect), so concurrent readers are
# cheap and correct, and this avoids sharing one connection's transaction
# state across threads.
#
# These are never explicitly closed. That is bounded, not a leak: the SDK
# dispatches through anyio's thread pool, so the count tops out at the pool
# size (40 by default) for the life of the process, and each connection is
# a read handle on a WAL database. One server instance serves one repo for
# one editor session, so this is not worth a cleanup protocol - but it is
# worth knowing the bound is "pool size", not "one".
_db_path = None       # set in main()
_local = threading.local()


def _require_conn():
    if _db_path is None:
        raise RuntimeError("server not initialized with a DB path")
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _local.conn = gl.connect(_db_path)
    return conn


@mcp.tool()
@usage_log.logged
def search_code(query: str, limit: int = 15) -> dict:
    """Search the parsed code graph (Layer 1) for symbols matching a name
    or partial name. Returns node info (kind, file, line) - not session
    notes. Use `search` instead if you also want prior findings about it.

    Best matches first: exact symbol name, then prefix, then substring,
    then a path-only hit; functions and classes ahead of modules within
    each of those. `total` and `truncated` say whether you are seeing
    everything - a broad term can match thousands of nodes, so narrow the
    term rather than assuming the top `limit` is all there is.

    An EMPTY result carries an `index` block saying whether the index is
    out of date. A symbol written since the last index looks exactly like
    one that never existed, so check it before concluding something is
    missing."""
    conn = _require_conn()
    full = ql.search(conn, query, limit=limit)
    out = {"results": [r["node"] for r in full["results"]],
           "total": full["total"], "limit": full["limit"],
           "truncated": full["truncated"]}
    for key in ("truncation_note", "index"):
        if full.get(key):
            out[key] = full[key]
    return out


@mcp.tool()
@usage_log.logged
def search_memory(query: str, limit: int = 15) -> dict:
    """Search session notes (Layer 2: things learned/decided in past
    sessions) for text matches. Does not search code structure - use
    `search_code` or `search` for that."""
    conn = _require_conn()
    full = ql.search(conn, query, limit=limit)
    notes = []
    for r in full["results"]:
        for n in r["notes"]:
            notes.append({**n, "node": r["node"]})
    notes.extend({**n, "node": None} for n in full["repo_wide_notes"])
    out = {"notes": notes}
    if not notes and full.get("index"):
        out["index"] = full["index"]
    return out


@mcp.tool()
@usage_log.logged
def search(query: str, limit: int = 15) -> dict:
    """Combined search across the code graph AND session memory, joined by
    symbol so you get 'what it is' (graph) and 'what we learned about it'
    (notes) together. This is the tool to reach for first when orienting
    in an unfamiliar part of the codebase - faster than grep + re-reading
    files, and it surfaces past findings that grep can't see at all.

    Best matches first: exact symbol name, then prefix, then substring,
    then a path-only hit; functions and classes ahead of modules within
    each tier; nodes found only through their notes last. Check `truncated`
    and `total` before concluding a symbol doesn't exist - a broad term can
    match thousands of nodes.

    An EMPTY result carries an `index` block saying whether the index is
    out of date; a symbol written since the last index is indistinguishable
    from one that never existed without it."""
    conn = _require_conn()
    return ql.search(conn, query, limit=limit)


@mcp.tool()
@usage_log.logged
def neighbors(symbol: str, direction: str = "both", limit: int = 30) -> dict:
    """Get a symbol's direct graph neighbors: what it calls/imports/defines
    (direction='out'), what calls/imports/defines it (direction='in'), or
    both (default). `symbol` can be a function/class/module name or a full
    node id.

    Each direction is capped at `limit` rows. The result always carries
    `incoming_total` / `outgoing_total` (the true counts) and a `truncated`
    flag - if `truncated` is true you are NOT seeing every caller, so raise
    `limit` before concluding anything about a symbol's full fan-in."""
    conn = _require_conn()
    return ql.neighbors(conn, symbol, direction=direction, limit=limit)


@mcp.tool()
@usage_log.logged
def impacted_by(symbol: str, max_depth: int = 3,
                 limit: int = ql.IMPACTED_BY_DEFAULT_LIMIT) -> dict:
    """Reverse-reachability: everything that transitively calls, imports,
    or depends on `symbol`, up to max_depth hops. Use this before changing
    a function/class to see what else could break.

    Pass a MODULE path to ask what would break if that FILE changed or was
    deleted, including every file that imports it. That is the right
    question for a route handler wired up by import and passed by
    reference rather than called - asking about the handler SYMBOL will
    not show the router, because no call to it is ever written.

    If the symbol does not resolve, the reply carries an `index` block
    saying whether the index is simply out of date; treat a miss as
    "does not exist" only when that says the index is current.

    Results come back NEAREST FIRST (closest dependents break first) and
    are capped at `limit`. `total` is always the true count and `truncated`
    says whether you are seeing all of it - a hub symbol can easily have
    over a thousand dependents, so check `truncated` before concluding
    anything is safe to change, and raise `limit` if you need the full
    list. Each result is {id, kind, lineno, depth}; a node id is already
    `file::qualname`, so file and symbol name are read off the id."""
    conn = _require_conn()
    return ql.impacted_by(conn, symbol, max_depth=max_depth, limit=limit)


@mcp.tool()
@usage_log.logged
def path_between(a: str, b: str) -> dict:
    """Shortest connection path between two symbols in the graph (via
    calls/imports/defines edges), useful for answering 'how is A related
    to B' without manually tracing imports.

    A null path, or an unresolved endpoint, carries an `index` block: the
    code connecting them may simply have been written since the index was
    built."""
    conn = _require_conn()
    return ql.shortest_path(conn, a, b)


@mcp.tool()
@usage_log.logged
def add_note(note: str, symbol: str = "", session_id: str = "mcp", source: str = "observed") -> dict:
    """Persist a durable finding to session memory (Layer 2), attached to
    a symbol when one is given ('' for a repo-wide note). Use this to
    record things future sessions shouldn't have to re-derive: why code is
    structured a certain way, what broke last time, decisions made about
    a module. source should be 'stated' (the user said it), 'observed'
    (you found it in code/tests), or 'derived' (you inferred it)."""
    conn = _require_conn()
    return ql.add_note(conn, symbol or None, note, session_id, source)


@mcp.tool()
@usage_log.logged
def search_sessions(query: str, limit: int = 10, role: str = "", kind: str = "") -> dict:
    """Full-text search over past Claude Code session transcripts for this
    repo (Layer 3) - what was discussed or done in earlier sessions, not
    just what's in the current one. Only returns results if the repo was
    indexed with `cg_index.py --sessions` (opt-in, since it indexes raw
    conversation history); an empty result set most likely means that
    hasn't been run yet, not that nothing matched - use `list_sessions` to
    check what's indexed. `role` optionally filters to 'user' (what was
    asked), 'assistant' (what Claude said or did), or 'summary'
    (compaction summaries); leave it '' for all.

    `kind` optionally filters to 'message' (what was said - the usual
    answer to "why did we do X"), 'action' (a one-line summary of a tool
    call: a Bash command, a file path) or 'summary'. Results are ranked by
    relevance WEIGHTED BY KIND, so an explanation comes ahead of a command
    that merely mentions the same words; pass kind='action' when you
    specifically want "what command did we run"."""
    conn = _require_conn()
    return ql.search_sessions(conn, query, limit=limit, role=role or None, kind=kind or None)


@mcp.tool()
@usage_log.logged
def list_sessions(limit: int = 20) -> dict:
    """List indexed session transcripts for this repo (Layer 3), most
    recently active first, with a message count and time range each. Use
    this to see what history is available before searching it with
    `search_sessions` - an empty list means `cg_index.py --sessions`
    hasn't been run for this repo yet."""
    conn = _require_conn()
    return ql.list_sessions(conn, limit=limit)


def main():
    global _db_path
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", default=None,
                     help="Repo this server indexes. DB path is derived the same way "
                          "cg_index.py's default is (graph_lib.default_db_path) - use this "
                          "unless you passed --db explicitly when indexing.")
    ap.add_argument("--db", default=None,
                     help="Explicit path to the graph DB, if it isn't at the default "
                          "location for --repo-root.")
    args = ap.parse_args()

    if args.db:
        db_path = args.db
    elif args.repo_root:
        db_path = gl.default_db_path(os.path.abspath(args.repo_root))
    else:
        raise SystemExit("pass --repo-root (recommended) or --db")

    if not os.path.exists(db_path):
        raise SystemExit(
            f"no graph DB at {db_path} - run cg_index.py "
            f"{'--db ' + args.db if args.db else args.repo_root or ''} first"
        )
    _db_path = db_path
    # Open once up front so a bad/corrupt DB fails loudly at startup rather
    # than on the first tool call, where the client only sees a tool error.
    gl.connect(db_path).close()
    usage_log.set_db_path(db_path)
    mcp.run()


if __name__ == "__main__":
    main()
