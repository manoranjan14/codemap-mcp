"""Node ids are interned to integers in the edge table.

Node ids are long path strings (`src/view/pages/general/Dash.vue::useNav`).
Storing them in full in every edge row AND again in both edge indexes was
the single largest cost in the database: measured on webapp, 193 MB
for 4,113 files, of which the edges table was 90 MB and its two indexes
60 MB. A 40,000-file monorepo projects to ~1.9 GB.

Each distinct id is now stored once in `syms` and referenced by integer.
`evidence` went the same way: it was 30 MB of strings like
"call at api/install-app.js:10", where the path is already encoded in the
edge's source id. Only the line number is stored; the sentence is rebuilt
at query time. Two cases carry text that genuinely is not derivable - the
candidate list on an ambiguous call, and whether an import was `import`,
`from ... import` or `require()` - and those keep a `note`.

The public API is unchanged: every query still takes and returns STRING
node ids. That is deliberate - it makes this change purely internal, so
the whole existing suite is the regression check.
"""
from __future__ import annotations

import query_lib as ql
from conftest import connect, edges, node_ids, run_index


# --- storage shape -------------------------------------------------------

def test_edges_reference_symbols_by_integer(py_conn):
    cols = {r[1]: r[2].upper() for r in py_conn.execute("PRAGMA table_info(edges)")}
    assert cols["src"] == "INTEGER"
    assert cols["dst"] == "INTEGER"
    assert "evidence" not in cols


def test_every_id_is_stored_exactly_once(py_conn):
    total, distinct = py_conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT id) FROM syms").fetchone()
    assert total == distinct


def test_every_edge_endpoint_resolves_to_a_symbol(py_conn):
    """A dangling integer is far less visible than a dangling string, so
    this is checked explicitly rather than trusted."""
    orphans = py_conn.execute(
        "SELECT COUNT(*) FROM edges e "
        "WHERE NOT EXISTS (SELECT 1 FROM syms s WHERE s.sym = e.src) "
        "   OR NOT EXISTS (SELECT 1 FROM syms s WHERE s.sym = e.dst)"
    ).fetchone()[0]
    assert orphans == 0


def test_every_node_has_a_symbol_row(py_conn):
    missing = py_conn.execute(
        "SELECT COUNT(*) FROM nodes n "
        "WHERE NOT EXISTS (SELECT 1 FROM syms s WHERE s.sym = n.sym)"
    ).fetchone()[0]
    assert missing == 0


def test_external_targets_get_symbols_without_nodes(py_conn):
    """`external:foo` is a real edge target but never a defined symbol, so
    it must exist in syms and NOT in nodes."""
    row = py_conn.execute(
        "SELECT s.sym FROM syms s WHERE s.id LIKE 'external:%' LIMIT 1").fetchone()
    assert row, "expected at least one external target"
    assert py_conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE sym = ?", (row[0],)).fetchone()[0] == 0


# --- the public API is unchanged ----------------------------------------

def test_queries_still_speak_string_ids(py_conn):
    node_id, _ = ql.resolve_symbol(py_conn, "recursive.py::factorial")
    assert node_id == "recursive.py::factorial"
    assert ql.node_info(py_conn, node_id)["file"] == "recursive.py"
    assert isinstance(next(iter(node_ids(py_conn))), str)


def test_neighbors_returns_string_ids(py_conn):
    res = ql.neighbors(py_conn, "nested_calls.py::outer", direction="out")
    for e in res["outgoing"]:
        assert isinstance(e["target"], str)
        assert "::" in e["target"] or e["target"].startswith(("external:", "ambiguous:"))


def test_impacted_by_returns_string_ids(py_conn):
    for n in ql.impacted_by(py_conn, "nested_calls.py::helper_b")["impacted"]:
        assert isinstance(n["id"], str)


# --- evidence is rebuilt, not stored ------------------------------------

def test_a_call_edge_rebuilds_its_evidence(py_conn):
    ev = {e["evidence"] for e in ql.neighbors(
        py_conn, "nested_calls.py::outer", direction="out")["outgoing"]
        if e["target"] == "nested_calls.py::helper_a"}
    assert any(e.startswith("call at nested_calls.py:") for e in ev), ev


def test_a_defines_edge_rebuilds_its_evidence(py_conn):
    ev = [e["evidence"] for e in ql.neighbors(
        py_conn, "recursive.py::factorial", direction="in")["incoming"]
        if e["type"] == "defines"]
    assert ev and "factorial" in ev[0] and "defined at recursive.py:" in ev[0], ev


def test_an_import_edge_keeps_its_non_derivable_text(py_conn):
    """`import x` vs `from m import x` cannot be told apart from the edge
    alone, so imports keep a note."""
    ev = [e["evidence"] for e in ql.neighbors(
        py_conn, "self_resolution.py", direction="out")["outgoing"]
        if e["type"] == "imports"]
    assert any("import" in e for e in ev), ev


def test_evidence_carries_the_line_number(py_conn):
    ev = [e["evidence"] for e in ql.neighbors(
        py_conn, "nested_calls.py::outer.inner", direction="out")["outgoing"]]
    assert any(any(ch.isdigit() for ch in e) for e in ev), ev


# --- migration from the old on-disk format -------------------------------

def test_an_old_format_database_is_rebuilt_not_read(tmp_path):
    """Old DBs store ids as TEXT. Reading one with the new code would
    silently return nothing rather than fail, so connect() detects the old
    shape and clears the derived tables, forcing a clean re-index."""
    db = str(tmp_path / "old.db")
    import sqlite3
    old = sqlite3.connect(db)
    old.executescript("""
        CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT,
                            qualname TEXT, file TEXT, lineno INT, end_lineno INT);
        CREATE TABLE edges (src TEXT, dst TEXT, type TEXT, evidence TEXT);
        CREATE TABLE file_hashes (file TEXT PRIMARY KEY, hash TEXT, indexed_at TEXT);
        CREATE TABLE session_notes (id INTEGER PRIMARY KEY AUTOINCREMENT, node_id TEXT,
                                    note TEXT, session_id TEXT, date TEXT, source TEXT);
        INSERT INTO nodes VALUES ('m.py::f','function','f','f','m.py',1,2);
        INSERT INTO edges VALUES ('m.py','m.py::f','defines','function f defined at m.py:1');
        INSERT INTO file_hashes VALUES ('m.py','deadbeef','2026-01-01');
        INSERT INTO session_notes (node_id, note, session_id, date, source)
            VALUES ('m.py::f','a note worth keeping','s1','2026-01-01','observed');
    """)
    old.commit()
    old.close()

    conn = connect(db)
    cols = {r[1]: r[2].upper() for r in conn.execute("PRAGMA table_info(edges)")}
    assert cols["src"] == "INTEGER"
    # Derived data is cleared so the next index run rebuilds it from source.
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM file_hashes").fetchone()[0] == 0
    # User-authored notes are NOT derived and must survive.
    assert conn.execute(
        "SELECT note FROM session_notes").fetchone()[0] == "a note worth keeping"
    conn.close()


def test_a_rebuilt_database_reindexes_cleanly(tmp_path):
    from conftest import PY_FIXTURE
    db = str(tmp_path / "graph.db")
    run_index(PY_FIXTURE, db)
    conn = connect(db)
    assert "recursive.py::factorial" in node_ids(conn)
    assert "nested_calls.py::helper_a" in edges(conn, "nested_calls.py::outer")
    conn.close()


def test_a_fresh_database_records_its_schema_version(py_conn):
    v = py_conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert v and int(v[0]) >= 2
