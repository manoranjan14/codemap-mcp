"""Regression tests for query_lib - the layer both the CLI (cg_query.py)
and the MCP server (cg_mcp_server.py) share, so these cover both surfaces.
"""
from __future__ import annotations

import pytest

import query_lib as ql
from conftest import connect, run_index


def test_resolve_by_exact_id_qualname_and_unique_name(py_conn):
    node_id, cands = ql.resolve_symbol(py_conn, "self_resolution.py::Worker.run")
    assert node_id == "self_resolution.py::Worker.run"
    assert cands == []

    node_id, _ = ql.resolve_symbol(py_conn, "Worker.run")
    assert node_id == "self_resolution.py::Worker.run"

    node_id, _ = ql.resolve_symbol(py_conn, "factorial")
    assert node_id == "recursive.py::factorial"


def test_ambiguous_name_resolves_to_nothing_and_reports_candidates(py_conn):
    """`run` exists on both Unrelated and Worker. Returning either one
    would be a guess; the caller needs to see both."""
    node_id, cands = ql.resolve_symbol(py_conn, "run")
    assert node_id is None
    assert set(cands) == {
        "self_resolution.py::Unrelated.run",
        "self_resolution.py::Worker.run",
    }


def test_unknown_symbol_resolves_to_nothing(py_conn):
    assert ql.resolve_symbol(py_conn, "no_such_symbol_anywhere") == (None, [])


def test_neighbors_direction_filtering(py_conn):
    out = ql.neighbors(py_conn, "nested_calls.py::outer", direction="out")
    assert {e["target"] for e in out["outgoing"] if e["type"] == "calls"} == {
        "nested_calls.py::helper_a", "nested_calls.py::outer.inner",
    }
    assert out["incoming"] == []

    inc = ql.neighbors(py_conn, "nested_calls.py::helper_b", direction="in")
    assert {e["source"] for e in inc["incoming"] if e["type"] == "calls"} == {
        "nested_calls.py::outer.inner"
    }
    assert inc["outgoing"] == []


def test_neighbors_on_unresolved_symbol_returns_an_error_not_a_crash(py_conn):
    assert ql.neighbors(py_conn, "run")["error"] == "unresolved"


def test_impacted_by_is_transitive(py_conn):
    """helper_b is called by inner, which is called by outer: changing
    helper_b can break both, and impacted_by must reach past one hop."""
    impacted = {n["id"] for n in ql.impacted_by(py_conn, "nested_calls.py::helper_b")["impacted"]}
    assert "nested_calls.py::outer.inner" in impacted
    assert "nested_calls.py::outer" in impacted


def test_impacted_by_respects_max_depth(py_conn):
    shallow = {n["id"] for n in ql.impacted_by(
        py_conn, "nested_calls.py::helper_b", max_depth=1)["impacted"]}
    assert "nested_calls.py::outer.inner" in shallow
    assert "nested_calls.py::outer" not in shallow


def test_path_between_finds_a_route_and_reports_when_there_is_none(py_conn):
    hit = ql.shortest_path(py_conn, "nested_calls.py::outer", "nested_calls.py::helper_b")
    path = [n["id"] for n in hit["path"]]
    # The traversal is undirected over calls/imports/defines, so the
    # shortest route may go via the module node rather than via `inner` -
    # both are 2 hops. Pin the endpoints and the length, not the tie-break.
    assert path[0] == "nested_calls.py::outer"
    assert path[-1] == "nested_calls.py::helper_b"
    assert len(path) == 3

    miss = ql.shortest_path(py_conn, "recursive.py::factorial", "repo_helper.py::Helper.assist")
    assert miss["path"] is None
    assert "no path" in miss["reason"]


# --- session memory (Layer 2) -------------------------------------------

@pytest.fixture
def notes_db(tmp_path):
    from conftest import PY_FIXTURE
    db = str(tmp_path / "notes.db")
    run_index(PY_FIXTURE, db)
    conn = connect(db)
    yield conn
    conn.close()


def test_note_round_trips_through_search(notes_db):
    ql.add_note(notes_db, "factorial", "recursion depth matters here", "s1", "observed")
    hits = ql.search(notes_db, "factorial")
    notes = [n["note"] for r in hits["results"] for n in r["notes"]]
    assert "recursion depth matters here" in notes


def test_repo_wide_note_is_stored_and_found(notes_db):
    ql.add_note(notes_db, None, "repo-wide convention note", "s1")
    assert any(n["note"] == "repo-wide convention note"
               for n in ql.search(notes_db, "convention")["repo_wide_notes"])


def test_note_against_an_ambiguous_symbol_is_refused_not_silently_widened(notes_db):
    """DESIGN's UX bug: this used to be stored as a repo-wide note, so a
    note filed against `run` could never be found again."""
    res = ql.add_note(notes_db, "run", "should not be stored", "s1")
    assert res["error"] == "unresolved"
    assert len(res["ambiguous_candidates"]) == 2
    assert notes_db.execute(
        "SELECT COUNT(*) FROM session_notes WHERE note = 'should not be stored'"
    ).fetchone()[0] == 0


def test_note_against_a_misspelled_symbol_is_refused(notes_db):
    res = ql.add_note(notes_db, "factoriall", "typo'd symbol", "s1")
    assert res["error"] == "unresolved"
    assert notes_db.execute("SELECT COUNT(*) FROM session_notes").fetchone()[0] == 0


def test_search_finds_a_symbol_via_its_note_text_alone(notes_db):
    """The note text doesn't appear in the symbol name - search must still
    join them, which is the whole point of the combined `search` tool."""
    ql.add_note(notes_db, "factorial", "blows the stack past ~1000", "s1")
    ids = [r["node"]["id"] for r in ql.search(notes_db, "blows the stack")["results"]]
    assert "recursive.py::factorial" in ids


# --- neighbors truncation ------------------------------------------------

def test_neighbors_reports_totals_and_flags_truncation(tmp_path):
    """`neighbors` caps each direction at `limit` rows. Returning a capped
    list with no signal silently answers "find every caller" with a subset -
    measured on a real repo, a symbol with 57 incoming edges returned 30
    with nothing to say so."""
    repo = tmp_path / "fanin"
    repo.mkdir()
    (repo / "target.py").write_text("def target():\n    return 1\n")
    callers = "\n\n".join(
        f"def caller_{i}():\n    return target()" for i in range(40)
    )
    (repo / "callers.py").write_text(f"from target import target\n\n{callers}\n")
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)
    conn = connect(db)

    # 40 `calls` edges plus the module's own `defines` edge - neighbors
    # counts every edge type, same as it returns every edge type.
    expected = 41

    capped = ql.neighbors(conn, "target.py::target", direction="in", limit=10)
    assert len(capped["incoming"]) == 10
    assert capped["incoming_total"] == expected
    assert capped["truncated"] is True
    assert "raise `limit`" in capped["truncation_note"]

    full = ql.neighbors(conn, "target.py::target", direction="in", limit=100)
    assert len(full["incoming"]) == expected
    assert full["incoming_total"] == expected
    assert full["truncated"] is False
    assert len([e for e in full["incoming"] if e["type"] == "calls"]) == 40
    conn.close()


def test_neighbors_totals_are_present_even_when_nothing_is_truncated(py_conn):
    out = ql.neighbors(py_conn, "nested_calls.py::outer", direction="both")
    assert out["truncated"] is False
    assert out["outgoing_total"] == len(out["outgoing"])
    assert out["incoming_total"] == len(out["incoming"])
