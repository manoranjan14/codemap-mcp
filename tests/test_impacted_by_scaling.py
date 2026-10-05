"""`impacted_by` must not read the whole edges table on every call.

The original implementation built a full reverse-adjacency map up front:

    SELECT src, dst FROM edges WHERE type IN ('calls','calls_external','imports')

That is every call/import edge in the repo, materialised into a dict, for a
query that usually touches a few dozen nodes. Measured on webapp
(~360K such edges): 166ms per call, independent of how small the answer is.
`path_between` was given an indexed, layer-by-layer BFS for exactly this
reason; `impacted_by` never got the same treatment.

These tests pin both halves of the fix: the answer must not change, and the
query must stay scoped to the frontier.
"""
from __future__ import annotations

from collections import deque

import pytest

import query_lib as ql
from conftest import connect, run_index

REACHABLE_TYPES = ("calls", "calls_external", "imports")


def brute_force_impacted(conn, node_id: str, max_depth: int = 3) -> set:
    """Deliberately naive reference: load every edge, walk it in Python.
    Slow and obviously correct - that is the point."""
    rev: dict = {}
    placeholders = ",".join("?" * len(REACHABLE_TYPES))
    for src, dst in conn.execute(
        f"SELECT a.id, d.id FROM edges e "
        f"JOIN syms a ON a.sym = e.src JOIN syms d ON d.sym = e.dst "
        f"WHERE e.type IN ({placeholders})", REACHABLE_TYPES
    ):
        rev.setdefault(dst, set()).add(src)

    frontier = deque([(node_id, 0)])
    seen = {node_id}
    out = set()
    while frontier:
        node, depth = frontier.popleft()
        if depth >= max_depth:
            continue
        for src in rev.get(node, ()):
            if src not in seen:
                seen.add(src)
                out.add(src)
                frontier.append((src, depth + 1))
    return out


@pytest.fixture(scope="module")
def fanout_repo(tmp_path_factory):
    """A repo with a deep call chain into the target plus a large amount of
    unrelated graph, so a full-table scan costs much more than the answer."""
    tmp = tmp_path_factory.mktemp("fanout")
    repo = tmp / "repo"
    repo.mkdir()

    (repo / "target.py").write_text("def target():\n    return 1\n")

    # 4 levels deep so max_depth actually bites.
    (repo / "chain.py").write_text(
        "from target import target\n\n\n"
        "def level1():\n    return target()\n\n\n"
        "def level2():\n    return level1()\n\n\n"
        "def level3():\n    return level2()\n\n\n"
        "def level4():\n    return level3()\n"
    )

    # Unrelated bulk: 60 files that never touch target.
    for i in range(60):
        body = "\n\n".join(
            f"def noise_{i}_{j}():\n    return noise_{i}_{(j + 1) % 20}()" for j in range(20)
        )
        (repo / f"noise_{i}.py").write_text(body + "\n")

    db = str(tmp / "graph.db")
    run_index(str(repo), db)
    conn = connect(db)
    yield conn
    conn.close()


def test_the_fixture_really_is_mostly_unrelated_edges(fanout_repo):
    """Guards the premise: if the noise ever stopped dominating, the scan
    test below would pass for the wrong reason."""
    placeholders = ",".join("?" * len(REACHABLE_TYPES))
    total = fanout_repo.execute(
        f"SELECT COUNT(*) FROM edges WHERE type IN ({placeholders})", REACHABLE_TYPES
    ).fetchone()[0]
    assert total > 1000


@pytest.mark.parametrize("max_depth", [1, 2, 3, 5])
def test_matches_brute_force_at_every_depth(fanout_repo, max_depth):
    got = {n["id"] for n in ql.impacted_by(
        fanout_repo, "target.py::target", max_depth=max_depth)["impacted"]}
    assert got == brute_force_impacted(fanout_repo, "target.py::target", max_depth)


def test_depth_boundary_is_exact(fanout_repo):
    at2 = {n["id"] for n in ql.impacted_by(
        fanout_repo, "target.py::target", max_depth=2)["impacted"]}
    assert "chain.py::level1" in at2
    assert "chain.py::level2" in at2
    assert "chain.py::level3" not in at2


def test_does_not_scan_the_whole_edges_table(fanout_repo):
    """Every read of `edges` must be constrained to the current frontier.
    An unconstrained `FROM edges` is the regression this guards."""
    seen_sql = []
    fanout_repo.set_trace_callback(seen_sql.append)
    try:
        ql.impacted_by(fanout_repo, "target.py::target")
    finally:
        fanout_repo.set_trace_callback(None)

    edge_reads = [q for q in seen_sql if "FROM edges" in q]
    assert edge_reads, "expected at least one edges query"
    unconstrained = [q for q in edge_reads if "dst IN" not in q and "dst =" not in q]
    assert unconstrained == [], f"unconstrained scan of edges: {unconstrained}"


def test_node_lookups_are_not_one_query_per_result(fanout_repo):
    """The old version also called node_info() per impacted node - one
    SELECT each, which on a 1,700-result query is 1,700 round trips."""
    seen_sql = []
    fanout_repo.set_trace_callback(seen_sql.append)
    try:
        res = ql.impacted_by(fanout_repo, "target.py::target", max_depth=5)
    finally:
        fanout_repo.set_trace_callback(None)

    node_reads = [q for q in seen_sql if "FROM nodes" in q]
    assert len(res["impacted"]) >= 4
    assert len(node_reads) < len(res["impacted"]), (
        f"{len(node_reads)} node queries for {len(res['impacted'])} results - "
        "looks like one lookup per result")


def test_result_is_deterministic(fanout_repo):
    a = [n["id"] for n in ql.impacted_by(fanout_repo, "target.py::target", max_depth=5)["impacted"]]
    b = [n["id"] for n in ql.impacted_by(fanout_repo, "target.py::target", max_depth=5)["impacted"]]
    assert a == b


def test_unresolved_and_ambiguous_behaviour_is_unchanged(fanout_repo, py_conn):
    assert ql.impacted_by(fanout_repo, "nope_not_here")["error"] == "unresolved"
    amb = ql.impacted_by(py_conn, "run")
    assert amb["error"] == "unresolved"
    assert len(amb["ambiguous_candidates"]) == 2


def test_nodes_missing_from_the_node_table_still_appear(fanout_repo):
    """`calls_external` targets (external:foo) have no row in `nodes`. The
    old code fell back to {"id": src}; that must survive the batching."""
    res = ql.impacted_by(fanout_repo, "target.py::target", max_depth=5)
    assert all("id" in n for n in res["impacted"])


# --- bounded, compact output --------------------------------------------

def test_impacted_by_caps_results_and_reports_the_true_total(fanout_repo):
    """Unbounded output is the problem: on a real graph a hub symbol
    returned 1,706 results as full node dicts - 452 KB, roughly 116,000
    tokens, in ONE MCP call. A tool whose point is to cost less than
    grep-and-read cannot hand back more context than the session has."""
    capped = ql.impacted_by(fanout_repo, "target.py::target", max_depth=5, limit=2)
    assert len(capped["impacted"]) == 2
    assert capped["truncated"] is True
    assert capped["total"] > 2
    assert "raise `limit`" in capped["truncation_note"]

    full = ql.impacted_by(fanout_repo, "target.py::target", max_depth=5, limit=1000)
    assert full["truncated"] is False
    assert full["total"] == len(full["impacted"])
    assert capped["total"] == full["total"]


def test_truncation_keeps_the_nearest_dependents(fanout_repo):
    """When it does truncate, the kept results must be the closest ones -
    those are what actually break first."""
    capped = ql.impacted_by(fanout_repo, "target.py::target", max_depth=5, limit=2)
    assert [n["depth"] for n in capped["impacted"]] == sorted(
        n["depth"] for n in capped["impacted"])
    assert capped["impacted"][0]["depth"] == 1


def test_results_are_compact(fanout_repo):
    """A node id is already `file::qualname`, so shipping `file`,
    `qualname` and `name` alongside it re-sends the same string three more
    times - 56% of the payload on the measured hub query."""
    res = ql.impacted_by(fanout_repo, "target.py::target", max_depth=5, limit=1000)
    for n in res["impacted"]:
        assert set(n) <= {"id", "kind", "lineno", "depth"}
        assert "qualname" not in n and "file" not in n and "name" not in n


def test_depth_is_reported_and_correct(fanout_repo):
    res = ql.impacted_by(fanout_repo, "target.py::target", max_depth=5, limit=1000)
    by_id = {n["id"]: n["depth"] for n in res["impacted"]}
    assert by_id["chain.py::level1"] == 1
    assert by_id["chain.py::level2"] == 2
    assert by_id["chain.py::level3"] == 3


def test_target_keeps_full_detail(fanout_repo):
    """One node, so the bytes are free - and the caller needs the file and
    line to confirm it resolved what they meant."""
    t = ql.impacted_by(fanout_repo, "target.py::target")["target"]
    assert t["file"] == "target.py"
    assert t["qualname"] == "target"


def test_payload_size_is_bounded_regardless_of_graph_size(fanout_repo):
    import json
    res = ql.impacted_by(fanout_repo, "target.py::target", max_depth=5)
    assert len(json.dumps(res)) < 20_000
