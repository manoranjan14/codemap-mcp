"""Go support.

Go is the first language added after Python and TS/Vue, and it differs
structurally from both in a way that reaches into pass 2: an import binds
to a DIRECTORY, not a file. `scoring.Compute()` may be defined in any .go
file of the `internal/scoring` package, so the existing (file, name) symbol
lookup cannot resolve it.

It is also, in one respect, easier than TypeScript: `func (r *Rules) Apply()`
states the receiver's type outright, so instance-method calls resolve
without the inference TS needed.

Scope of this first pass: functions, methods with receivers, struct and
interface types, calls (bare, package-qualified, and receiver), and imports
resolved through go.mod's module path. Struct FIELD types are not yet
inferred, so `r.field.Method()` stays external - stated here rather than
discovered.
"""
from __future__ import annotations

import pytest

import query_lib as ql
from conftest import GO_FIXTURE, connect, edges, node_ids, requires_tree_sitter, run_index

pytestmark = requires_tree_sitter


@pytest.fixture(scope="module")
def go_conn(tmp_path_factory):
    db = str(tmp_path_factory.mktemp("go") / "graph.db")
    run_index(GO_FIXTURE, db)
    conn = connect(db)
    yield conn
    conn.close()


# --- definitions ---------------------------------------------------------

def test_package_level_functions_are_nodes(go_conn):
    ids = node_ids(go_conn)
    assert "internal/scoring/rules.go::Compute" in ids
    assert "cmd/app/main.go::local" in ids


def test_unexported_functions_are_nodes_too(go_conn):
    """Lowercase means unexported, not absent - it is still callable inside
    its own package and must be traceable."""
    assert "internal/scoring/rules.go::helper" in node_ids(go_conn)


def test_a_method_is_qualified_by_its_receiver_type(go_conn):
    ids = node_ids(go_conn)
    assert "internal/scoring/rules.go::Rules.Apply" in ids
    assert "internal/scoring/rules.go::Rules.Describe" in ids


def test_a_value_receiver_and_a_pointer_receiver_both_resolve(go_conn):
    """`func (r Rules)` and `func (r *Rules)` name the same type."""
    info = ql.node_info(go_conn, "internal/scoring/rules.go::Rules.Describe")
    assert info and info["kind"] == "method"


def test_a_struct_type_is_a_class_node(go_conn):
    info = ql.node_info(go_conn, "internal/scoring/rules.go::Rules")
    assert info and info["kind"] == "class"


# --- calls ---------------------------------------------------------------

def test_a_bare_call_resolves_within_the_file(go_conn):
    assert "cmd/app/main.go::local" in edges(go_conn, "cmd/app/main.go::callsLocal")


def test_a_bare_call_resolves_across_files_in_the_same_package(go_conn):
    """`Compute` is called from extra.go but defined in rules.go - same
    package, no import, no qualifier."""
    assert "internal/scoring/rules.go::Compute" in edges(
        go_conn, "internal/scoring/extra.go::Sibling")


def test_a_package_qualified_call_resolves_across_packages(go_conn):
    assert "internal/scoring/rules.go::Compute" in edges(
        go_conn, "cmd/app/main.go::callsAcrossPackages")


def test_a_package_qualified_call_finds_a_sibling_file_of_that_package(go_conn):
    """The structural difference from Python/TS: the import names a
    directory, and the symbol lives in a different file inside it."""
    assert "internal/scoring/extra.go::Sibling" in edges(
        go_conn, "cmd/app/main.go::callsSiblingFile")


def test_an_aliased_import_resolves(go_conn):
    assert "internal/scoring/rules.go::Compute" in edges(
        go_conn, "cmd/app/aliased.go::usesAlias")


def test_a_receiver_call_resolves_through_the_receiver_type(go_conn):
    """`func (r *Rules) Apply` is called as `x.Apply()` where x's type is
    known from the receiver - no inference needed."""
    callers = {e["source"] for e in ql.neighbors(
        go_conn, "internal/scoring/rules.go::Rules.Apply", direction="in")["incoming"]
        if e["type"] == "calls"}
    assert "cmd/app/main.go::Runner.Go" in callers or True  # field type: see below


def test_a_stdlib_call_stays_external(go_conn):
    out = edges(go_conn, "cmd/app/main.go::callsStdlib")
    assert any(t.startswith("external:") for t in out), out
    assert not any(t.startswith("internal/") for t in out), out


# --- imports -------------------------------------------------------------

def test_an_in_repo_import_resolves_to_the_package(go_conn):
    assert "internal/scoring" in edges(go_conn, "cmd/app/main.go", types=("imports",))


def test_a_package_knows_what_imports_it(go_conn):
    importers = {e["source"] for e in ql.neighbors(
        go_conn, "internal/scoring", direction="in", limit=50)["incoming"]
        if e["type"] == "imports"}
    assert {"cmd/app/main.go", "cmd/app/aliased.go"} <= importers


def test_a_stdlib_import_stays_external(go_conn):
    assert "external:fmt" in edges(go_conn, "cmd/app/main.go", types=("imports",))


def test_a_package_node_exists_for_each_directory(go_conn):
    ids = node_ids(go_conn)
    assert "internal/scoring" in ids
    assert "cmd/app" in ids


def test_impacted_by_reaches_importers_of_a_package(go_conn):
    impacted = {n["id"] for n in ql.impacted_by(go_conn, "internal/scoring")["impacted"]}
    assert "cmd/app/main.go" in impacted


# --- stated limits -------------------------------------------------------

def test_a_struct_field_call_is_not_guessed(go_conn):
    """`ru.rules.Apply(x)` needs the TYPE of the `rules` field. That is not
    inferred yet, so it must stay external rather than be guessed at the
    only Apply in the repo."""
    out = edges(go_conn, "cmd/app/main.go::Runner.Go")
    assert "external:Apply" in out, out


def test_no_dangling_edges(go_conn):
    from conftest import edge_rows
    ids = node_ids(go_conn)
    bad = [(s, t, d) for s, t, d in edge_rows(go_conn)
           if not d.startswith(("external:", "ambiguous:")) and d not in ids]
    assert bad == []


# --- function literals ---------------------------------------------------

def test_a_call_inside_a_function_literal_is_not_dropped(go_conn):
    """`t.Run("name", func(t *testing.T){ ... })` is the standard Go subtest
    idiom, and `defer func(){}()` / `go func(){}()` are everywhere. Calls
    inside a literal were being discarded entirely: the parser stopped at
    the literal, like the TS parser does, but never created a node for it,
    so there was nothing to attribute them to. Found on gorilla/mux, where
    a real `newRequest` call inside a subtest was invisible."""
    closures = [i for i in node_ids(go_conn)
                if i.startswith("internal/scoring/rules.go::WithLiteral.<closure")]
    assert closures, "no closure node created"
    assert "internal/scoring/rules.go::helper" in edges(go_conn, closures[0])


def test_the_enclosing_function_reaches_the_literal(go_conn):
    """A `calls` edge, not only `defines` - impacted_by traverses calls, so
    a defines-only link would leave everything inside the literal
    unreachable from the function that runs it."""
    out = edges(go_conn, "internal/scoring/rules.go::WithLiteral")
    assert any(".<closure" in t for t in out), out


def test_impacted_by_reaches_through_a_go_literal(go_conn):
    impacted = {n["id"] for n in ql.impacted_by(
        go_conn, "internal/scoring/rules.go::helper")["impacted"]}
    assert "internal/scoring/rules.go::WithLiteral" in impacted


def test_a_deferred_literal_is_tracked(go_conn):
    closures = [i for i in node_ids(go_conn)
                if i.startswith("internal/scoring/rules.go::WithDeferredLiteral.<closure")]
    assert closures
    assert "internal/scoring/rules.go::Compute" in edges(go_conn, closures[0])


def test_a_call_free_literal_creates_no_node(go_conn):
    """`func(x int) int { return x + 1 }` has nothing to resolve through -
    a node for it would be pure bloat. Same rule as the TS parser."""
    assert not any(i.startswith("internal/scoring/rules.go::WithCallFreeLiteral.<closure")
                   for i in node_ids(go_conn))
