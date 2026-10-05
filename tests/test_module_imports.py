"""`imports` edges resolve to the in-repo module they point at.

Every imports edge used to target `external:<specifier>`, even when the
specifier named a file in this very repo. The import BINDINGS were resolved
- that is how cross-file call resolution works - but the edge itself was
never pointed at the module node. So:

    neighbors("server/utils/httpErrors.ts", direction="in") -> 0

for a file 32 other files import. "What imports this module" had no answer,
and `impacted_by` on a module came back empty, which for a route handler
wired up by an import is a false negative severe enough to mislead an
impact analysis. Found by a reviewer testing exactly that question against
api-service: 2,543 imports edges, every one external.

An import of something NOT in the repo (`vue`, `node:fs`) still points at
`external:` - that is correct and must stay.
"""
from __future__ import annotations

import query_lib as ql
from conftest import connect, edges, node_ids, requires_tree_sitter, run_index


def imports_of(conn, module_id, direction="in"):
    res = ql.neighbors(conn, module_id, direction=direction, limit=200)
    key, idkey = ("incoming", "source") if direction == "in" else ("outgoing", "target")
    return {e[idkey] for e in res[key] if e["type"] == "imports"}


# --- Python --------------------------------------------------------------

def test_a_python_module_knows_what_imports_it(py_conn):
    assert "self_resolution.py" in imports_of(py_conn, "repo_helper.py")


def test_incoming_total_is_not_zero_for_an_imported_module(py_conn):
    """The reviewer's exact probe: neighbors(module, 'in') reported
    incoming_total 0 universally."""
    res = ql.neighbors(py_conn, "repo_helper.py", direction="in")
    assert res["incoming_total"] > 0


def test_the_importing_side_points_at_the_module(py_conn):
    assert "repo_helper.py" in imports_of(py_conn, "self_resolution.py", direction="out")


def test_impacted_by_reaches_an_importer_of_a_module(py_conn):
    """This is what makes "what breaks if I delete this file" answerable."""
    impacted = {n["id"] for n in ql.impacted_by(py_conn, "repo_helper.py")["impacted"]}
    assert "self_resolution.py" in impacted


def test_a_third_party_import_stays_external(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("import os\nfrom json import dumps\n")
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)
    conn = connect(db)
    out = edges(conn, "m.py", types=("imports",))
    assert out and all(t.startswith("external:") for t in out), out
    conn.close()


def test_the_original_import_statement_is_still_readable(py_conn):
    """Pointing the edge at the module must not lose which name was
    imported - that is the part a reader needs."""
    ev = [e["evidence"] for e in ql.neighbors(py_conn, "self_resolution.py", direction="out")["outgoing"]
          if e["type"] == "imports" and e["target"] == "repo_helper.py"]
    assert ev and "Helper" in ev[0], ev


# --- TypeScript / Vue ----------------------------------------------------

@requires_tree_sitter
def test_a_ts_module_knows_what_imports_it(ts_conn):
    assert "src/utils/helpers.ts" in imports_of(ts_conn, "src/utils/helpers.ts") or True
    importers = imports_of(ts_conn, "src/utils/helpers.ts")
    assert "src/main.ts" in importers, importers


@requires_tree_sitter
def test_a_vue_file_importing_a_ts_module_is_recorded(ts_conn):
    assert "src/comp/Widget.vue" in imports_of(ts_conn, "src/utils/helpers.ts")


@requires_tree_sitter
def test_an_aliased_import_resolves_to_the_module(ts_conn):
    """`@/widget` -> src/widget.ts via tsconfig paths."""
    assert "src/main.ts" in imports_of(ts_conn, "src/widget.ts")


@requires_tree_sitter
def test_importing_a_vue_component_resolves(ts_conn):
    assert "src/comp/registry.ts" in imports_of(ts_conn, "src/comp/Widget.vue")


@requires_tree_sitter
def test_a_commonjs_require_resolves_to_the_module(ts_conn):
    assert "src/legacyRequire.js" in imports_of(ts_conn, "src/utils/helpers.ts")


@requires_tree_sitter
def test_a_bare_package_import_stays_external(ts_conn):
    out = edges(ts_conn, "src/main.ts", types=("imports",))
    assert "external:lodash" in out


@requires_tree_sitter
def test_a_default_import_of_a_module_counts(tmp_path):
    """The shape that motivated this: a router importing a handler module
    by default export and wiring it up by reference. The CALL never
    appears - it is passed as a value - so the import edge is the only
    evidence the module is still wired in."""
    repo = tmp_path / "repo"
    (repo / "handlers" / "[id]").mkdir(parents=True)
    (repo / "handlers" / "[id]" / "index.ts").write_text(
        "export default function handler() { return 1 }\n")
    (repo / "router.ts").write_text(
        "import h from './handlers/[id]/index'\nexport const routes = [h]\n")
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)
    conn = connect(db)
    assert "router.ts" in imports_of(conn, "handlers/[id]/index.ts")
    impacted = {n["id"] for n in ql.impacted_by(conn, "handlers/[id]/index.ts")["impacted"]}
    assert "router.ts" in impacted
    conn.close()


def test_deleting_an_imported_module_degrades_its_importers(tmp_path):
    """Pointing imports at the module node means deleting that file leaves
    every importer's edge aimed at a node that no longer exists. The edge
    must survive - someone really does import it - while giving up the
    claim of a resolved target."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "lib.py").write_text("def thing():\n    return 1\n")
    (repo / "app.py").write_text("import lib\n")
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)

    conn = connect(db)
    assert "lib.py" in edges(conn, "app.py", types=("imports",))
    conn.close()

    (repo / "lib.py").unlink()
    run_index(str(repo), db)

    conn = connect(db)
    ids = node_ids(conn)
    out = edges(conn, "app.py", types=("imports",))
    assert out, "the import edge was lost entirely"
    assert all(t.startswith("external:") or t in ids for t in out), out
    conn.close()
