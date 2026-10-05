"""Ambiguous calls are recorded, not silently dropped.

When a called name matches more than one definition and nothing binds it -
no import, no inferred type - the resolver refuses to guess. That refusal
is right: measured on a real Next.js repo, 7,196 of 9,008 ambiguous calls
were to `t`, bound 546 times as a LOCAL variable from
`const t = await getTranslations()`. The six repo functions named `t` are
unrelated scripts and tests; resolving to any of them would have invented
thousands of false edges.

What was wrong is that the call then vanished entirely - no edge, nothing
in the query layer, so "what does this function call" quietly omitted it.
It is now recorded against a synthetic `ambiguous:<name>` target: the call
is visible and the candidates are named, without asserting a target that
may well be wrong.

The synthetic target is deliberately NOT reachable: it is never the source
of an edge, so it cannot connect two unrelated symbols that happen to share
a name.
"""
from __future__ import annotations

import query_lib as ql
from conftest import connect, edges, run_index


def ambiguous_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "a").mkdir(parents=True)
    (repo / "b").mkdir(parents=True)
    (repo / "a" / "one.py").write_text("def shared():\n    return 1\n")
    (repo / "b" / "two.py").write_text("def shared():\n    return 2\n")
    # No import binding: the name alone cannot pick between the two.
    (repo / "caller.py").write_text("def go():\n    return shared()\n")
    db = str(tmp_path / "graph.db")
    proc = run_index(str(repo), db)
    return connect(db), proc


def test_an_ambiguous_call_is_recorded_rather_than_dropped(tmp_path):
    conn, _ = ambiguous_repo(tmp_path)
    out = edges(conn, "caller.py::go", types=("calls", "calls_external", "calls_ambiguous"))
    assert "ambiguous:shared" in out
    conn.close()


def test_it_does_not_guess_a_target(tmp_path):
    conn, _ = ambiguous_repo(tmp_path)
    out = edges(conn, "caller.py::go", types=("calls",))
    assert "a/one.py::shared" not in out
    assert "b/two.py::shared" not in out
    conn.close()


def test_the_evidence_names_the_candidates(tmp_path):
    """A caller who sees the ambiguity should be able to act on it without
    re-deriving which symbols collided."""
    conn, _ = ambiguous_repo(tmp_path)
    ev = conn.execute(
        "SELECT e.note FROM edges e JOIN syms s ON s.sym = e.src "
        "WHERE s.id = ? AND e.type = 'calls_ambiguous'",
        ("caller.py::go",),
    ).fetchone()[0]
    assert "2 candidates" in ev
    assert "one.py::shared" in ev and "two.py::shared" in ev
    conn.close()


def test_neighbors_surfaces_it(tmp_path):
    conn, _ = ambiguous_repo(tmp_path)
    res = ql.neighbors(conn, "caller.py::go", direction="out")
    kinds = {e["type"] for e in res["outgoing"]}
    assert "calls_ambiguous" in kinds
    conn.close()


def test_it_creates_no_false_reachability(tmp_path):
    """The whole reason not to guess: `a/one.py::shared` must NOT appear to
    be called by `go`, and the synthetic target must not bridge the two
    same-named functions into one another's blast radius."""
    conn, _ = ambiguous_repo(tmp_path)
    for target in ("a/one.py::shared", "b/two.py::shared"):
        impacted = {n["id"] for n in ql.impacted_by(conn, target)["impacted"]}
        assert "caller.py::go" not in impacted
    conn.close()


def test_the_synthetic_target_is_never_an_edge_source(tmp_path):
    conn, _ = ambiguous_repo(tmp_path)
    assert conn.execute(
        "SELECT COUNT(*) FROM edges e JOIN syms s ON s.sym = e.src "
        "WHERE s.id LIKE 'ambiguous:%'").fetchone()[0] == 0
    conn.close()


def test_an_unambiguous_call_is_unaffected(tmp_path):
    """Guard against the fix firing where resolution actually worked."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def only():\n    return 1\n\n\ndef go():\n    return only()\n")
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)
    conn = connect(db)
    assert edges(conn, "m.py::go", types=("calls",)) == {"m.py::only"}
    assert conn.execute(
        "SELECT COUNT(*) FROM edges WHERE type = 'calls_ambiguous'").fetchone()[0] == 0
    conn.close()


def test_the_indexer_still_reports_the_count(tmp_path):
    conn, proc = ambiguous_repo(tmp_path)
    assert "1 ambiguous" in proc.stdout
    conn.close()
