"""Ranking for `search_sessions` (Layer 3).

`search_sessions` ordered by raw FTS5 `rank`, which treats every chunk the
same. But the chunk kinds are not the same thing at all:

  message - what was actually said or explained. Usually the answer to
            "why did we do X".
  action  - a one-line summary of a tool call (Bash command, file path).
            Useful, but mostly long absolute paths that happen to contain
            the search terms.

On this repo's own 366-chunk transcript index, `action` chunks are 63% of
everything stored (231 of 366), so a search for an explanation routinely
came back with Bash commands above the sentence that explained the thing.
Observed live: searching "worker threads" returned the `note-add` command
that *mentioned* the bug before the message that *explained* it.

Relevance still leads; kind is a multiplier on it, so a genuinely
on-point action can still outrank a weakly-matching message.
"""
from __future__ import annotations

import sqlite3

import pytest

import graph_lib as gl
import query_lib as ql


@pytest.fixture
def sessions_db(tmp_path):
    conn = gl.connect(str(tmp_path / "graph.db"))
    rows = [
        # (session, ts, role, kind, text, line_no)
        ("s1", "2026-01-01T00:00:01Z", "assistant", "message",
         "We switched to per-thread connections because the worker thread "
         "could not reuse the main thread's sqlite handle.", 10),
        # Same terms, repeated, in a command - higher RAW relevance.
        ("s1", "2026-01-01T00:00:02Z", "assistant", "action",
         "Bash: grep -rn 'worker thread' --include='*.py' /very/long/absolute/path/"
         "worker/thread/worker_thread_notes.py # worker thread worker thread", 11),
        ("s1", "2026-01-01T00:00:03Z", "user", "message",
         "why did the worker thread break?", 12),
        ("s1", "2026-01-01T00:00:04Z", "summary", "summary",
         "Session covered the worker thread crash and its fix.", 13),
        ("s1", "2026-01-01T00:00:05Z", "assistant", "action",
         "Bash: ls /tmp/unrelated", 14),
    ]
    conn.executemany(
        "INSERT INTO transcript_chunks (session_id, ts, role, kind, text, source_file, line_no) "
        "VALUES (?,?,?,?,?,?,?)",
        [(s, ts, role, kind, text, "/fake/s1.jsonl", ln) for s, ts, role, kind, text, ln in rows],
    )
    conn.commit()
    yield conn
    conn.close()


def kinds(res):
    return [r["kind"] for r in res["results"]]


def test_a_message_outranks_a_term_stuffed_action(sessions_db):
    """The observed failure: an action chunk repeating the search terms in
    a long path beat the message that actually explained the thing."""
    res = ql.search_sessions(sessions_db, "worker thread")
    assert res["results"][0]["kind"] == "message"


def test_actions_are_not_excluded_only_demoted(sessions_db):
    """Demotion, not suppression - the command you ran is still a real
    answer to "what did we do", just not the first guess at "why"."""
    assert "action" in kinds(ql.search_sessions(sessions_db, "worker thread"))


def test_summaries_rank_with_messages_not_with_actions(sessions_db):
    """Compaction summaries are condensed recaps - the densest "why" in the
    whole transcript, so they must not be demoted alongside tool calls."""
    ks = kinds(ql.search_sessions(sessions_db, "worker thread"))
    assert ks.index("summary") < ks.index("action")


def test_kind_filter_returns_only_that_kind(sessions_db):
    for want in ("message", "action", "summary"):
        res = ql.search_sessions(sessions_db, "worker thread", kind=want)
        assert res["results"], f"no {want} results"
        assert set(kinds(res)) == {want}


def test_role_filter_still_works_alongside_ranking(sessions_db):
    res = ql.search_sessions(sessions_db, "worker thread", role="user")
    assert {r["role"] for r in res["results"]} == {"user"}


def test_role_and_kind_filters_combine(sessions_db):
    res = ql.search_sessions(sessions_db, "worker thread", role="assistant", kind="message")
    assert [(r["role"], r["kind"]) for r in res["results"]] == [("assistant", "message")]


def test_an_unmatched_term_returns_nothing(sessions_db):
    assert ql.search_sessions(sessions_db, "nonexistent_term_xyz")["results"] == []


def test_limit_is_respected(sessions_db):
    assert len(ql.search_sessions(sessions_db, "worker thread", limit=2)["results"]) == 2


def test_ranking_is_deterministic(sessions_db):
    a = [r["line_no"] for r in ql.search_sessions(sessions_db, "worker thread")["results"]]
    b = [r["line_no"] for r in ql.search_sessions(sessions_db, "worker thread")["results"]]
    assert a == b


class _NoFtsConn:
    """Delegates to a real connection but makes any query touching the FTS
    table fail, the way a sqlite3 built without FTS5 does. sqlite3.Connection
    attributes are read-only, so this can't be monkeypatched in place."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args, **kwargs):
        if "transcript_fts" in sql:
            raise sqlite3.OperationalError("no such table: transcript_fts")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_like_fallback_also_prefers_messages(sessions_db):
    """Some Python builds ship sqlite3 without FTS5; that path degrades to a
    LIKE scan and must not degrade the ranking with it. With no relevance
    score available, kind becomes the primary key."""
    res = ql.search_sessions(_NoFtsConn(sessions_db), "worker thread")
    assert res["results"], "fallback returned nothing"
    assert res["results"][0]["kind"] in ("message", "summary")
    ks = [r["kind"] for r in res["results"]]
    assert ks.index("action") > 0


def test_like_fallback_respects_filters(sessions_db):
    res = ql.search_sessions(_NoFtsConn(sessions_db), "worker thread", kind="action")
    assert {r["kind"] for r in res["results"]} == {"action"}
