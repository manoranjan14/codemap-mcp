"""Two bugs found by an adversarial review pass against a real 813-file repo.

1. `new X().method()` - a method called directly on a freshly constructed
   instance - resolved to `external:method` instead of to the class's
   method. The stored form (`const x = new X(); x.method()`) worked, so the
   gap was specifically the inline-chained shape. Effect:
   `neighbors(method, "in")` showed only the method's own `defines` edge,
   i.e. the tool reported a load-bearing production method as uncalled.
   The type is written RIGHT THERE in the expression - this needed no
   inference at all, which is what made it an easy case to miss.

2. `path_between` routed through `external:*` nodes. Every `.includes()`
   call in the repo collapses to one `external:includes` node, so two
   unrelated files that both call a common built-in got a confident
   multi-hop "path" between them. An external node is a SINK - something
   left the repo there - never a conduit between two things inside it.
"""
from __future__ import annotations

import pytest

import query_lib as ql
from conftest import connect, edges, requires_tree_sitter, run_index


# --- 1. a method called on a freshly constructed instance ---------------

@pytest.fixture
def py_inline(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "svc.py").write_text(
        "class Scorer:\n"
        "    def consistency(self, x):\n        return x\n\n"
        "    def confidence(self, x):\n        return x\n"
    )
    (repo / "caller.py").write_text(
        "from svc import Scorer\n\n\n"
        "def inline_chained(v):\n"
        "    return Scorer().consistency(v)\n\n\n"
        "def stored_instance(v):\n"
        "    s = Scorer()\n"
        "    return s.confidence(v)\n"
    )
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)
    conn = connect(db)
    yield conn
    conn.close()


def test_python_inline_constructed_call_resolves(py_inline):
    assert "svc.py::Scorer.consistency" in edges(py_inline, "caller.py::inline_chained")


def test_python_inline_call_is_not_left_external(py_inline):
    assert "external:consistency" not in edges(py_inline, "caller.py::inline_chained")


def test_python_the_method_knows_it_has_a_caller(py_inline):
    """The user-visible symptom: a real method reported as uncalled."""
    callers = {e["source"] for e in ql.neighbors(
        py_inline, "svc.py::Scorer.consistency", direction="in")["incoming"]
        if e["type"] == "calls"}
    assert "caller.py::inline_chained" in callers


def test_python_stored_instance_still_works(py_inline):
    """Guard the path that already worked."""
    assert "svc.py::Scorer.confidence" in edges(py_inline, "caller.py::stored_instance")


@pytest.fixture
def ts_inline(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "svc.ts").write_text(
        "export class Scorer {\n"
        "    consistency(x: number) { return x }\n"
        "    confidence(x: number) { return x }\n"
        "}\n"
    )
    (repo / "src" / "caller.ts").write_text(
        'import { Scorer } from "./svc"\n\n'
        "export function inlineChained(v: number) {\n"
        "    return new Scorer().consistency(v)\n}\n\n"
        "export function storedInstance(v: number) {\n"
        "    const s = new Scorer()\n"
        "    return s.confidence(v)\n}\n"
    )
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)
    conn = connect(db)
    yield conn
    conn.close()


@requires_tree_sitter
def test_ts_inline_constructed_call_resolves(ts_inline):
    assert "src/svc.ts::Scorer.consistency" in edges(ts_inline, "src/caller.ts::inlineChained")


@requires_tree_sitter
def test_ts_inline_call_is_not_left_external(ts_inline):
    assert "external:consistency" not in edges(ts_inline, "src/caller.ts::inlineChained")


@requires_tree_sitter
def test_ts_the_method_knows_it_has_a_caller(ts_inline):
    callers = {e["source"] for e in ql.neighbors(
        ts_inline, "src/svc.ts::Scorer.consistency", direction="in")["incoming"]
        if e["type"] == "calls"}
    assert "src/caller.ts::inlineChained" in callers


@requires_tree_sitter
def test_ts_stored_instance_still_works(ts_inline):
    assert "src/svc.ts::Scorer.confidence" in edges(ts_inline, "src/caller.ts::storedInstance")


@requires_tree_sitter
def test_an_unknown_class_still_stays_external(tmp_path):
    """Resolving the written type must not become guessing when the class
    is not in this repo."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "m.ts").write_text(
        'import { Thing } from "some-package"\n'
        "export function go() { return new Thing().doIt() }\n")
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)
    conn = connect(db)
    assert "external:doIt" in edges(conn, "src/m.ts::go")
    conn.close()


# --- 2. external nodes must not be traversable ---------------------------

@pytest.fixture
def shared_external(tmp_path):
    """Two unrelated files whose only commonality is calling a built-in."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "colors.py").write_text(
        "def use_brand_color(items):\n    return items.index('red')\n")
    (repo / "scoring.py").write_text(
        "def consistency(items):\n    return items.index('x')\n")
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)
    conn = connect(db)
    yield conn
    conn.close()


def test_both_files_really_do_share_an_external_node(shared_external):
    """Guards the premise - if they stopped sharing it, the test below
    would pass for the wrong reason."""
    assert "external:index" in edges(shared_external, "colors.py::use_brand_color")
    assert "external:index" in edges(shared_external, "scoring.py::consistency")


def test_no_path_is_fabricated_through_a_shared_builtin(shared_external):
    """The reported failure: a confident multi-hop path between a Vue
    composable and a scoring method, routed through `external:includes`,
    because both files happen to call a common array method."""
    res = ql.shortest_path(shared_external,
                           "colors.py::use_brand_color", "scoring.py::consistency")
    assert res["path"] is None, [n.get("id") for n in (res.get("path") or [])]


def test_a_real_path_still_resolves(tmp_path):
    """The fix must not make path_between useless - a genuine chain through
    in-repo symbols has to keep working."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text(
        "def a():\n    return b()\n\n\n"
        "def b():\n    return c()\n\n\n"
        "def c():\n    return 1\n")
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)
    conn = connect(db)
    path = [n["id"] for n in ql.shortest_path(conn, "m.py::a", "m.py::c")["path"]]
    # Traversal is undirected over calls/imports/defines, so a -> module ->
    # c is as short as a -> b -> c. Pin the endpoints and the length, not
    # which equally-short route won.
    assert path[0] == "m.py::a" and path[-1] == "m.py::c"
    assert len(path) == 3
    conn.close()


def test_external_edges_are_still_visible_everywhere_else(shared_external):
    """The fix restricts PATH TRAVERSAL only. An external call is still a
    real fact about a function and must keep showing up in neighbors -
    otherwise suppressing a bogus path would have cost a true one."""
    out = ql.neighbors(shared_external, "colors.py::use_brand_color", direction="out")
    assert any(e["target"] == "external:index" and e["type"] == "calls_external"
               for e in out["outgoing"])


def test_an_external_node_is_not_addressable_as_an_endpoint(shared_external):
    """Documenting what the fix does NOT do: `external:index` has no node
    row, so it cannot be named as a path endpoint at all. Asking "what
    calls this built-in" is a fair question that this tool does not answer
    today - stated rather than left for someone to discover."""
    res = ql.shortest_path(shared_external, "colors.py::use_brand_color", "external:index")
    assert res["error"] == "unresolved"
