"""Shared fixtures for the codegraph regression suite.

These tests run the real `cg_index.py` as a subprocess against the fixture
repos in this directory and assert on the resulting SQLite graph. Running
the CLI (rather than importing and calling internals) is deliberate: the
indexer's contract that this project actually depends on is "point it at a
repo, get a correct graph, exit 0", including its per-file error isolation
and its incremental hashing - all of which live in main().

Every DB is written to a pytest tmp dir, never to ~/.codegraph, so running
the suite can't disturb a real indexed repo.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
SRC_DIR = os.path.join(REPO_ROOT, "src")
# Kept under the old name: several tests locate the shell hook helpers and
# the package source through it.
SCRIPTS_DIR = os.path.join(SRC_DIR, "codegraph")

PY_FIXTURE = os.path.join(TESTS_DIR, "edge_repo")
TS_FIXTURE = os.path.join(TESTS_DIR, "edge_repo_ts")
GO_FIXTURE = os.path.join(TESTS_DIR, "edge_repo_go")

# Run against the package as it is laid out for distribution, so the tests
# exercise the real import structure rather than a flat directory that only
# exists in a checkout.
sys.path.insert(0, SRC_DIR)

from codegraph import graph_lib as gl  # noqa: E402
from codegraph import query_lib as ql  # noqa: E402
from codegraph import ts_parser  # noqa: E402

# Several test modules do `import query_lib` / `import ts_parser` directly.
# Alias them so the package layout did not force a rewrite of every test.
sys.modules.setdefault("graph_lib", gl)
sys.modules.setdefault("query_lib", ql)
sys.modules.setdefault("ts_parser", ts_parser)
from codegraph import cg_usage as _cg_usage  # noqa: E402
sys.modules.setdefault("cg_usage", _cg_usage)

# TS/JS/Vue parsing needs the OPTIONAL tree-sitter dependency. The suite
# must be meaningful either way: the Python and query-layer tests run
# everywhere, the TS ones skip cleanly when it isn't installed (and CI runs
# a job with it installed so they actually execute).
requires_tree_sitter = pytest.mark.skipif(
    not ts_parser.available(),
    reason=f"tree-sitter not installed ({ts_parser.unavailable_reason()})",
)


def run_index(repo_root: str, db: str, *extra: str, env: dict | None = None):
    """Run cg_index.py, asserting it exits 0. Returns the CompletedProcess
    so tests can also assert on its stdout summary."""
    # PYTHONPATH is COMPOSED, not overwritten: callers pass their own entry
    # (e.g. the stub that makes tree-sitter unimportable) and the package
    # still has to be findable, so an override must not displace SRC_DIR.
    child = {**os.environ, **(env or {})}
    parts = [SRC_DIR] + [p for p in (child.get("PYTHONPATH", "") or "").split(os.pathsep) if p]
    child["PYTHONPATH"] = os.pathsep.join(parts)
    proc = subprocess.run(
        [sys.executable, "-m", "codegraph.cg_index", repo_root, "--db", db, *extra],
        capture_output=True, text=True, env=child,
    )
    assert proc.returncode == 0, f"cg_index failed:\n{proc.stdout}\n{proc.stderr}"
    return proc


def connect(db: str) -> sqlite3.Connection:
    return gl.connect(db)


def edges(conn, src: str, types=("calls", "calls_external")) -> set:
    """Outgoing edge targets from `src`, as id STRINGS, restricted to
    `types`. Edges store interned integers (see graph_lib.SCHEMA); joining
    through `syms` here means the tests keep asserting on readable ids."""
    placeholders = ",".join("?" * len(types))
    return {
        row[0] for row in conn.execute(
            f"SELECT d.id FROM edges e "
            f"JOIN syms a ON a.sym = e.src JOIN syms d ON d.sym = e.dst "
            f"WHERE a.id = ? AND e.type IN ({placeholders})",
            (src, *types),
        )
    }


def edge_rows(conn, where_sql="", params=()):
    """(src_id, type, dst_id) triples with both endpoints resolved to
    strings - for tests that assert on the whole edge set."""
    return conn.execute(
        f"SELECT a.id, e.type, d.id FROM edges e "
        f"JOIN syms a ON a.sym = e.src JOIN syms d ON d.sym = e.dst {where_sql}",
        params,
    ).fetchall()


def node_ids(conn) -> set:
    return {row[0] for row in conn.execute(
        "SELECT s.id FROM nodes n JOIN syms s ON s.sym = n.sym")}


@pytest.fixture(scope="session")
def py_db(tmp_path_factory):
    """tests/edge_repo indexed as its own repo root, once per session."""
    db = str(tmp_path_factory.mktemp("py") / "graph.db")
    run_index(PY_FIXTURE, db)
    return db


@pytest.fixture(scope="session")
def py_conn(py_db):
    conn = connect(py_db)
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def ts_db(tmp_path_factory):
    db = str(tmp_path_factory.mktemp("ts") / "graph.db")
    run_index(TS_FIXTURE, db)
    return db


@pytest.fixture(scope="session")
def ts_conn(ts_db):
    conn = connect(ts_db)
    yield conn
    conn.close()


@pytest.fixture
def scratch_repo(tmp_path):
    """A small, WRITABLE copy of a repo for tests that mutate source files
    (incremental re-index, deletion). Returns (repo_root, db_path)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "lib.py").write_text(
        "def target():\n    return 1\n\n\ndef sibling():\n    return 2\n"
    )
    (repo / "app.py").write_text(
        "from lib import target\n\n\ndef caller():\n    return target()\n"
    )
    return str(repo), str(tmp_path / "graph.db")
