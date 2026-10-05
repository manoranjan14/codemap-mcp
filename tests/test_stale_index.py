"""An unresolved result says whether the index might simply be stale.

A reviewer asked for a function they had written an hour earlier
(`sanitizeInput`) and for a symbol they typed at random
(`totallyFakeSymbolXyz123`). Both returned the identical

    {"error": "unresolved", "ambiguous_candidates": []}

so a miss was ambiguous evidence by itself: "this does not exist" and
"your index predates it" are very different answers, and the second is
the common one. Unresolved results now carry the index's age and how many
files on disk have changed since it was built, which turns a bare miss
into something actionable.

The check only runs on the unresolved path - it stats the working tree,
which is cheap but not free, and a successful lookup has no reason to pay
for it.
"""
from __future__ import annotations

import os
import time

import query_lib as ql
from conftest import connect, run_index


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def existing():\n    return 1\n")
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)
    return repo, connect(db)


def test_a_fresh_index_reports_nothing_changed(tmp_path):
    repo, conn = make_repo(tmp_path)
    res = ql.resolve_symbol_detail(conn, "totallyFakeSymbolXyz123")
    assert res["node_id"] is None
    assert res["index"]["files_changed_since_index"] == 0
    assert res["index"]["stale"] is False
    conn.close()


def test_a_symbol_added_after_indexing_reports_the_repo_as_changed(tmp_path):
    """The reviewer's exact case: a function written after the last index."""
    repo, conn = make_repo(tmp_path)
    time.sleep(1.1)   # filesystem mtime granularity
    (repo / "new_file.py").write_text("def sanitizeInput():\n    return 1\n")

    res = ql.resolve_symbol_detail(conn, "sanitizeInput")
    assert res["node_id"] is None
    assert res["index"]["files_changed_since_index"] >= 1
    assert res["index"]["stale"] is True
    assert "re-index" in res["index"]["hint"].lower()
    conn.close()


def test_the_two_cases_are_now_distinguishable(tmp_path):
    """The whole point: a real-but-new symbol and a fake one must no longer
    produce identical output."""
    repo, conn = make_repo(tmp_path)
    time.sleep(1.1)
    (repo / "new_file.py").write_text("def sanitizeInput():\n    return 1\n")

    real_but_new = ql.resolve_symbol_detail(conn, "sanitizeInput")
    pure_fiction = ql.resolve_symbol_detail(conn, "totallyFakeSymbolXyz123")
    # Same resolution outcome, different evidence about why.
    assert real_but_new["node_id"] is pure_fiction["node_id"] is None
    assert real_but_new["index"]["stale"] is True
    assert real_but_new["index"] == pure_fiction["index"]  # repo-level, not symbol-level
    assert pure_fiction["index"]["files_changed_since_index"] >= 1


def test_neighbors_carries_the_staleness_evidence(tmp_path):
    repo, conn = make_repo(tmp_path)
    time.sleep(1.1)
    (repo / "new_file.py").write_text("def brandNew():\n    return 1\n")
    res = ql.neighbors(conn, "brandNew")
    assert res["error"] == "unresolved"
    assert res["index"]["stale"] is True
    conn.close()


def test_impacted_by_carries_it_too(tmp_path):
    repo, conn = make_repo(tmp_path)
    time.sleep(1.1)
    (repo / "new_file.py").write_text("def brandNew():\n    return 1\n")
    assert ql.impacted_by(conn, "brandNew")["index"]["stale"] is True
    conn.close()


def test_an_ambiguous_symbol_is_not_called_stale(tmp_path):
    """Ambiguity is a complete answer, not a miss - it must not be muddied
    with a re-index suggestion."""
    repo = tmp_path / "repo"
    (repo / "a").mkdir(parents=True)
    (repo / "b").mkdir(parents=True)
    (repo / "a" / "x.py").write_text("def dup():\n    return 1\n")
    (repo / "b" / "y.py").write_text("def dup():\n    return 2\n")
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)
    conn = connect(db)
    res = ql.neighbors(conn, "dup")
    assert res["error"] == "unresolved"
    assert len(res["ambiguous_candidates"]) == 2
    assert res.get("index") is None, "ambiguity is an answer, not a staleness problem"
    conn.close()


def test_a_successful_lookup_does_not_pay_for_the_check(tmp_path):
    repo, conn = make_repo(tmp_path)
    assert "index" not in ql.neighbors(conn, "existing")
    conn.close()


def test_a_missing_repo_root_degrades_quietly(tmp_path):
    """The repo may have moved or been deleted since indexing; a usage
    report must not blow up over it."""
    repo, conn = make_repo(tmp_path)
    conn.execute("UPDATE meta SET value = '/nowhere/at/all' WHERE key = 'repo_root'")
    res = ql.resolve_symbol_detail(conn, "nope")
    assert res["index"]["files_changed_since_index"] is None
    conn.close()


# --- every tool that can return a miss, not just two of them ------------

def stale_repo(tmp_path):
    """An index that predates a function added afterwards."""
    repo, conn = make_repo(tmp_path)
    time.sleep(1.1)
    (repo / "new_file.py").write_text(
        "def addedAfterIndexing():\n    return 1\n")
    return repo, conn


def test_search_says_whether_an_empty_result_might_be_stale(tmp_path):
    """`search` is the documented FIRST stop for "what is X / where does X
    live", and returned {"results": [], "total": 0} for a symbol written
    since the last index - indistinguishable from one that never existed.
    The entry-point tool was the one without the evidence."""
    repo, conn = stale_repo(tmp_path)
    res = ql.search(conn, "addedAfterIndexing")
    assert res["results"] == [] and res["total"] == 0
    assert res["index"]["stale"] is True
    assert "re-index" in res["index"]["hint"].lower()
    conn.close()


def test_search_on_a_clean_index_says_the_symbol_is_really_absent(tmp_path):
    repo, conn = make_repo(tmp_path)
    res = ql.search(conn, "neverWrittenAnywhere")
    assert res["total"] == 0
    assert res["index"]["stale"] is False
    assert "absent" in res["index"]["hint"].lower()
    conn.close()


def test_search_with_results_does_not_pay_for_the_check(tmp_path):
    repo, conn = make_repo(tmp_path)
    assert "index" not in ql.search(conn, "existing")
    conn.close()


def test_path_between_unresolved_endpoints_carry_it(tmp_path):
    repo, conn = stale_repo(tmp_path)
    res = ql.shortest_path(conn, "existing", "addedAfterIndexing")
    assert res["error"] == "unresolved"
    assert res["index"]["stale"] is True
    conn.close()


def test_path_between_no_path_found_carries_it(tmp_path):
    """"No path" is a negative result staleness can explain - the
    connecting code may have been written after the index."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def one():\n    return 1\n")
    (repo / "b.py").write_text("def two():\n    return 2\n")
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)
    conn = connect(db)
    res = ql.shortest_path(conn, "a.py::one", "b.py::two")
    assert res["path"] is None
    assert res["index"]["stale"] is False
    conn.close()


def test_add_note_refusal_carries_it(tmp_path):
    """Refusing to attach a note to a symbol you just wrote should say so,
    not just refuse."""
    repo, conn = stale_repo(tmp_path)
    res = ql.add_note(conn, "addedAfterIndexing", "a finding", "s1")
    assert res["error"] == "unresolved"
    assert res["index"]["stale"] is True
    conn.close()


def test_add_note_ambiguity_is_still_not_called_stale(tmp_path):
    repo = tmp_path / "repo"
    (repo / "a").mkdir(parents=True)
    (repo / "b").mkdir(parents=True)
    (repo / "a" / "x.py").write_text("def dup():\n    return 1\n")
    (repo / "b" / "y.py").write_text("def dup():\n    return 2\n")
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)
    conn = connect(db)
    res = ql.add_note(conn, "dup", "n", "s1")
    assert res["error"] == "unresolved" and len(res["ambiguous_candidates"]) == 2
    assert res.get("index") is None
    conn.close()


def test_every_lookup_tool_agrees_on_the_same_repo_state(tmp_path):
    """The rule file tells a reader to check the `index` block on any empty
    result. That instruction has to be true for every tool it applies to,
    not two of four."""
    repo, conn = stale_repo(tmp_path)
    blocks = [
        ql.search(conn, "addedAfterIndexing")["index"],
        ql.neighbors(conn, "addedAfterIndexing")["index"],
        ql.impacted_by(conn, "addedAfterIndexing")["index"],
        ql.shortest_path(conn, "existing", "addedAfterIndexing")["index"],
        ql.add_note(conn, "addedAfterIndexing", "n", "s1")["index"],
    ]
    assert all(b["stale"] is True for b in blocks)
    assert len({b["files_changed_since_index"] for b in blocks}) == 1
    conn.close()
