"""
Usage logging for codegraph's MCP server.

Why this exists: every test so far (edge-case fixtures, the stdlib,
webapp, self-indexing) answers "does codegraph produce a
structurally correct graph" - none of them answer "does having it change
what a Claude Code session actually does." This module is the
instrumentation to start closing that gap: it logs every MCP tool call
so that, after real day-to-day use, there's DATA to look at instead of
an impression.

Design, deliberately narrow:
  - One append-only JSONL file per indexed DB, living NEXT TO graph.db
    (same directory) - so it's outside the indexed repo exactly like
    everything else this tool stores (see graph_lib.codegraph_home() /
    default_db_path()), and travels with the DB rather than needing its
    own repo-root plumbing.
  - Logs every call: timestamp, tool name, the arguments (query/symbol
    text - short strings, not result bodies), latency, and a small
    per-tool OUTCOME signal (result count, whether the symbol even
    resolved, etc.) - enough to see both "is it being reached for" and
    "is it finding anything," without duplicating entire result
    payloads (notes, graph neighborhoods, ...) into a growing log file.
  - Best-effort and fail-open: a logging problem (disk full, bad
    permissions, whatever) is caught and swallowed - it must never break
    the actual tool call. Same philosophy as this project's existing
    "memory/notes are best-effort" stance.

Known, STATED limitation, not silently assumed away: this can only see
calls made THROUGH this MCP server. It has no visibility into whether
the same Claude Code session also reached for Grep/Read for the same
question, or skipped codegraph entirely and went straight to Grep - the
actual "vs Grep" comparison still needs either mining the session
transcripts this tool already indexes (session_indexer.py /
search_sessions, which DO capture every tool call including Read/Grep)
or a Claude Code hook. cg_usage.py's report says this explicitly rather
than implying this log is the whole picture.
"""
from __future__ import annotations

import functools
import inspect
import json
import os
import time
from datetime import datetime, timezone

_db_path_holder: dict[str, str | None] = {"path": None}


def set_db_path(db_path: str) -> None:
    """Called once, in cg_mcp_server.py's main(), after the DB path is
    known. Must happen before mcp.run() - tool calls before this is set
    are simply not logged (fails open, not an error)."""
    _db_path_holder["path"] = db_path


def usage_log_path() -> str | None:
    db_path = _db_path_holder["path"]
    if not db_path:
        return None
    return os.path.join(os.path.dirname(db_path), "usage.jsonl")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _outcome_summary(tool_name: str, result) -> dict:
    """A small, tool-specific "did this find anything" signal -
    deliberately NOT the full result payload (see module docstring: this
    log must not become a second copy of the graph/notes/session data)."""
    if not isinstance(result, dict):
        return {}
    if "error" in result:
        return {"error": result["error"]}
    if tool_name in ("search", "search_code"):
        return {"result_count": len(result.get("results", []))}
    if tool_name == "search_memory":
        return {"note_count": len(result.get("notes", []))}
    if tool_name == "neighbors":
        return {
            "resolved": result.get("node") is not None,
            "outgoing": len(result.get("outgoing", [])),
            "incoming": len(result.get("incoming", [])),
        }
    if tool_name == "impacted_by":
        return {"impacted_count": len(result.get("impacted", []))}
    if tool_name == "path_between":
        return {"found": bool(result.get("path"))}
    if tool_name == "search_sessions":
        return {"result_count": len(result.get("results", []))}
    if tool_name == "list_sessions":
        return {"session_count": len(result.get("sessions", []))}
    if tool_name == "add_note":
        return {"ok": True}
    return {}


def logged(fn):
    """Decorator: wraps an MCP tool function so every call - successful
    or not - appends one JSONL line, then returns/raises exactly as the
    wrapped function would have. Applied at import time (before the DB
    path is known); that's fine, since actual calls only happen after
    main() calls set_db_path() and starts serving."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.monotonic()
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            _write_entry(fn.__name__, sig, args, kwargs, start, error=f"{type(e).__name__}: {e}")
            raise
        _write_entry(fn.__name__, sig, args, kwargs, start, result=result)
        return result

    return wrapper


def _write_entry(name, sig, args, kwargs, start, result=None, error=None):
    path = usage_log_path()
    if not path:
        return
    try:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        entry = {
            "ts": _now_iso(),
            "tool": name,
            "args": dict(bound.arguments),
            "latency_ms": round((time.monotonic() - start) * 1000, 1),
        }
        if error:
            entry["exception"] = error
        else:
            entry["outcome"] = _outcome_summary(name, result)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass  # best-effort - a logging failure must never break the tool call
