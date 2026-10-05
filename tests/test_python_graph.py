"""Regression tests for the Python indexer (graph_lib.parse_file + pass-2
resolution), pinned to tests/edge_repo/.

Each test here corresponds to a specific correctness claim in
docs/DESIGN.md - several of them to bugs that were found and fixed once by
hand. They exist so those fixes can't silently regress.
"""
from __future__ import annotations

from conftest import edge_rows, edges, node_ids


# --- per-file error isolation ------------------------------------------

def test_syntax_error_file_is_skipped_but_run_succeeds(py_db, py_conn):
    """A file that doesn't parse is skipped alone; the rest of the repo
    still indexes and the process exits 0."""
    bad = {n for n in node_ids(py_conn) if n.startswith("syntax_error.py")}
    assert bad == set()
    assert "nested_calls.py::outer" in node_ids(py_conn)


def test_redefinition_does_not_abort_the_run(py_conn):
    """DESIGN bug #1: a same-scope redefinition (legal Python) used to
    raise sqlite3.IntegrityError and kill the WHOLE run. One node survives
    for the duplicated name, and other files are unaffected."""
    foo_nodes = [n for n in node_ids(py_conn) if n.startswith("duplicate_names.py::foo")]
    assert len(foo_nodes) == 1
    assert "recursive.py::factorial" in node_ids(py_conn)


def test_empty_and_comment_only_files_produce_a_module_node(py_conn):
    ids = node_ids(py_conn)
    assert "empty.py" in ids
    assert "comments_only.py" in ids


# --- call attribution ---------------------------------------------------

def test_nested_function_calls_are_not_attributed_to_the_enclosing_scope(py_conn):
    """DESIGN bug #2: ast.walk() doesn't stop at nested scopes, so a call
    made only inside `inner` was also recorded as made by `outer`,
    inflating the call graph with edges that don't exist."""
    outer = edges(py_conn, "nested_calls.py::outer")
    assert "nested_calls.py::helper_a" in outer
    assert "nested_calls.py::outer.inner" in outer
    assert "nested_calls.py::helper_b" not in outer

    assert edges(py_conn, "nested_calls.py::outer.inner") == {"nested_calls.py::helper_b"}


def test_deep_nesting_attributes_each_level_to_itself_only(py_conn):
    method = "deep_nesting.py::A.B.C.method"
    assert edges(py_conn, method) == {f"{method}.local_fn"}
    assert edges(py_conn, f"{method}.local_fn") == {f"{method}.local_fn.deeper"}


def test_self_recursion_produces_a_self_edge(py_conn):
    assert "recursive.py::factorial" in edges(py_conn, "recursive.py::factorial")


def test_unicode_identifiers_resolve(py_conn):
    assert edges(py_conn, "unicode_names.py::caller") == {"unicode_names.py::naïve_café"}


def test_user_definition_shadowing_a_builtin_wins_within_its_module(py_conn):
    """`def print(...)` really does shadow the builtin for calls in that
    module, so caller() must point at the local def, not be dropped as a
    builtin."""
    assert "shadow_builtin.py::print" in edges(py_conn, "shadow_builtin.py::caller")


# --- self / instance-method type inference ------------------------------

def test_self_call_resolves_to_the_owning_class_not_a_same_named_sibling(py_conn):
    """Unrelated.run and Worker.run share a simple name in one file. The
    old name-based heuristic saw two candidates and gave up (external);
    type inference must pick Worker.run specifically."""
    assert edges(py_conn, "self_resolution.py::Worker.dispatch") == {
        "self_resolution.py::Worker.run"
    }


def test_inherited_method_resolves_through_the_mro_same_file(py_conn):
    assert "self_resolution.py::BaseThing.shared" in edges(
        py_conn, "self_resolution.py::Derived.use_inherited"
    )


def test_inherited_method_resolves_through_the_mro_cross_file(py_conn):
    """RemoteBase lives in another file - only reachable via the
    import-bound `inherits` edge."""
    assert "repo_base.py::RemoteBase.remote_method" in edges(
        py_conn, "self_resolution.py::RemoteUser.use_remote"
    )
    assert ("self_resolution.py::RemoteUser", "repo_base.py::RemoteBase") in {
        (s, d) for s, _t, d in edge_rows(py_conn, "WHERE e.type = 'inherits'")
    }


def test_constructor_assigned_attribute_type_is_inferred(py_conn):
    """self.helper = Helper() in __init__ makes self.helper.assist()
    resolvable, even though Composed has no `assist` of its own."""
    assert "repo_helper.py::Helper.assist" in edges(
        py_conn, "self_resolution.py::Composed.do_work"
    )


def test_local_variable_instantiation_type_is_inferred(py_conn):
    assert "repo_helper.py::Helper.assist" in edges(
        py_conn, "self_resolution.py::Composed.do_local"
    )


def test_unknown_attribute_type_stays_external_rather_than_guessing(py_conn):
    """self.other was never assigned or annotated. Guessing Helper.assist
    here (the only `assist` in the repo) is exactly the false edge the
    type inference was built to avoid."""
    out = edges(py_conn, "self_resolution.py::Composed.do_unknown_attr")
    assert "external:assist" in out
    assert "repo_helper.py::Helper.assist" not in out


# --- graph integrity ----------------------------------------------------

def test_no_dangling_edges(py_conn):
    """Every non-external edge endpoint must exist in `nodes`. A dangling
    target silently claims a resolved call to a symbol that isn't there."""
    ids = node_ids(py_conn)
    rows = edge_rows(py_conn)
    dangling = [
        (src, etype, dst) for src, etype, dst in rows
        if not dst.startswith(("external:", "ambiguous:")) and dst not in ids
    ]
    assert dangling == []

    orphan_sources = [
        (src, etype, dst) for src, etype, dst in rows if src not in ids
    ]
    assert orphan_sources == []
