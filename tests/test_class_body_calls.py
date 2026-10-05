"""Calls in a class body, and in decorators.

`class Model: col = field()` produced NO edge at all. `_direct_calls` skips
`ClassDef` outright and `_visit_def` only collected calls for functions and
methods, so the entire class body was invisible - on both the Python and
the TS side. That is the shape Django, SQLAlchemy and Pydantic code is
built from: the calls live in the class body, not in a method.

Class-body calls are attributed to the CLASS node, which is where they
actually run. Decorator calls are attributed to the symbol they DECORATE
rather than to the enclosing scope - a deliberate choice: asking "what does
`validator` affect" should surface the decorated function, not the module
that happens to contain it.
"""
from __future__ import annotations

from conftest import edge_rows, edges, node_ids, requires_tree_sitter


# --- Python -------------------------------------------------------------

def test_class_body_call_is_attributed_to_the_class(py_conn):
    out = edges(py_conn, "class_body.py::Model")
    assert "class_body.py::field" in out


def test_every_class_body_call_lands_not_just_the_first(py_conn):
    """Two `field()` calls in one body - a loop that stops early would pass
    the test above and still lose one."""
    rows = [
        r[0] for r in py_conn.execute(
            "SELECT e.lineno FROM edges e "
            "JOIN syms a ON a.sym = e.src JOIN syms d ON d.sym = e.dst "
            "WHERE a.id = ? AND d.id = ? AND e.type = 'calls'",
            ("class_body.py::Model", "class_body.py::field"),
        )
    ]
    assert len(rows) == 2, rows
    assert len(set(rows)) == 2, f"expected two distinct call sites, got {rows}"


def test_method_calls_are_still_scoped_to_the_method(py_conn):
    """The existing nested-scope rule must survive: a call inside a method
    belongs to the method, not to the class that contains it."""
    assert "class_body.py::module_level_only" in edges(py_conn, "class_body.py::Model.check")
    assert "class_body.py::module_level_only" not in edges(py_conn, "class_body.py::Model")


def test_a_decorator_call_is_attributed_to_what_it_decorates(py_conn):
    assert "class_body.py::validator" in edges(py_conn, "class_body.py::Model.check")


def test_a_bare_decorator_name_is_not_recorded_as_a_call(py_conn):
    """`@register` (no parentheses) is a reference, not a call - recording
    one would be inventing an edge."""
    assert "class_body.py::register" not in edges(py_conn, "class_body.py::Model")


def test_an_empty_class_body_produces_no_calls(py_conn):
    assert edges(py_conn, "class_body.py::Plain") == set()


def test_impacted_by_now_reaches_through_a_class_body(py_conn):
    import query_lib as ql
    impacted = {n["id"] for n in ql.impacted_by(py_conn, "class_body.py::field")["impacted"]}
    assert "class_body.py::Model" in impacted


# --- TypeScript ---------------------------------------------------------

@requires_tree_sitter
def test_ts_property_initialiser_calls_are_attributed_to_the_class(ts_conn):
    out = edges(ts_conn, "src/classBody.ts::Model")
    assert "src/classBody.ts::makeField" in out
    assert "src/classBody.ts::makeOther" in out


@requires_tree_sitter
def test_ts_method_calls_stay_in_the_method(ts_conn):
    assert "src/classBody.ts::usedOnlyInAMethod" in edges(ts_conn, "src/classBody.ts::Model.run")
    assert "src/classBody.ts::usedOnlyInAMethod" not in edges(ts_conn, "src/classBody.ts::Model")


@requires_tree_sitter
def test_ts_class_node_exists_and_has_no_dangling_edges(ts_conn):
    assert "src/classBody.ts::Model" in node_ids(ts_conn)
    ids = node_ids(ts_conn)
    bad = [(s, d) for s, _t, d in edge_rows(
        ts_conn, "WHERE a.id LIKE 'src/classBody.ts%'")
        if not d.startswith(("external:", "ambiguous:")) and d not in ids]
    assert bad == []
