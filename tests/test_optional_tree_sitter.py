"""tree-sitter is an OPTIONAL dependency.

Before this was fixed, `cg_index.py` imported `ts_parser` at module level
and `ts_parser` imported `tree_sitter_languages` at module level, so a
machine without it (or with a broken binary wheel - the pinned
tree_sitter_languages has no wheels for newer Pythons) could not index ANY
repo, including a pure-Python one that needs nothing but the stdlib.

These tests run the indexer in a subprocess with the import forced to
fail, so they assert the real degraded behaviour regardless of whether
tree-sitter happens to be installed in the environment running the suite.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from codegraph import ts_parser
from conftest import PY_FIXTURE, TS_FIXTURE, SRC_DIR, connect, node_ids, run_index


@pytest.fixture
def no_tree_sitter_env(tmp_path):
    """A PYTHONPATH entry where EVERY grammar pack ts_parser knows about
    raises on import, shadowing any real installation. Driven off
    ts_parser._PARSER_BACKENDS so adding a backend can't silently leave a
    hole here that makes these tests pass against a real install."""
    blocker = tmp_path / "blocker"
    blocker.mkdir()
    assert ts_parser._PARSER_BACKENDS, "expected at least one backend to block"
    for mod_name in ts_parser._PARSER_BACKENDS:
        (blocker / f"{mod_name}.py").write_text(
            'raise ImportError("blocked by tests/test_optional_tree_sitter.py")\n'
        )
    existing = os.environ.get("PYTHONPATH", "")
    return {"PYTHONPATH": f"{blocker}{os.pathsep}{existing}" if existing else str(blocker)}


def test_ts_parser_reports_unavailable_instead_of_raising_at_import(no_tree_sitter_env, tmp_path):
    """Importing ts_parser must always succeed; only actually parsing a
    TS file may fail."""
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from codegraph import ts_parser\n"
        "assert ts_parser.available() is False\n"
        "assert 'ImportError' in ts_parser.unavailable_reason()\n"
        "try:\n"
        "    ts_parser.get_parser('typescript')\n"
        "except ts_parser.TreeSitterUnavailable as e:\n"
        "    assert 'requirements.txt' in str(e)\n"
        "else:\n"
        "    raise AssertionError('expected TreeSitterUnavailable')\n"
        "print('ok')\n"
    ) % SRC_DIR
    child = {**os.environ, **no_tree_sitter_env}
    child["PYTHONPATH"] = os.pathsep.join(
        [SRC_DIR] + [p for p in (child.get("PYTHONPATH", "") or "").split(os.pathsep) if p])
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          env=child)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_python_repo_indexes_fully_without_tree_sitter(no_tree_sitter_env, tmp_path):
    db = str(tmp_path / "graph.db")
    proc = run_index(PY_FIXTURE, db, env=no_tree_sitter_env)

    conn = connect(db)
    ids = node_ids(conn)
    # Full Python graph, identical to the normal path.
    assert "self_resolution.py::Worker.dispatch" in ids
    assert conn.execute(
        "SELECT COUNT(*) FROM edges WHERE type = 'calls'"
    ).fetchone()[0] > 0
    conn.close()

    # No TS files in this fixture, so nothing to warn about.
    assert "tree-sitter not available" not in proc.stderr


def test_ts_repo_skips_ts_files_with_one_clear_warning(no_tree_sitter_env, tmp_path):
    db = str(tmp_path / "graph.db")
    proc = run_index(TS_FIXTURE, db, env=no_tree_sitter_env)

    assert "tree-sitter not available" in proc.stderr
    assert "requirements.txt" in proc.stderr
    assert "skipped (no tree-sitter)" in proc.stdout

    conn = connect(db)
    assert node_ids(conn) == set()  # fixture is TS-only
    conn.close()


def test_mixed_repo_still_indexes_its_python_half(no_tree_sitter_env, tmp_path):
    repo = tmp_path / "mixed"
    repo.mkdir()
    (repo / "mod.py").write_text("def py_target():\n    return 1\n")
    (repo / "mod.ts").write_text("export function tsTarget() { return 1; }\n")
    db = str(tmp_path / "graph.db")

    run_index(str(repo), db, env=no_tree_sitter_env)

    conn = connect(db)
    ids = node_ids(conn)
    assert "mod.py::py_target" in ids
    assert not any(i.startswith("mod.ts") for i in ids)
    conn.close()


def test_skipped_ts_files_are_not_purged_as_deleted(no_tree_sitter_env, tmp_path):
    """A run WITHOUT tree-sitter must not destroy TS nodes an earlier run
    WITH tree-sitter produced - the skipped files still exist on disk, so
    treating them as deleted would silently gut a shared DB."""
    repo = tmp_path / "mixed"
    repo.mkdir()
    (repo / "mod.py").write_text("def py_target():\n    return 1\n")
    (repo / "mod.ts").write_text("export function tsTarget() { return 1; }\n")
    db = str(tmp_path / "graph.db")

    run_index(str(repo), db, env=no_tree_sitter_env)

    # Stand in for "a previous run that DID have tree-sitter": insert the
    # TS node and its file hash the way that run would have.
    conn = connect(db)
    conn.execute("INSERT OR IGNORE INTO syms (id) VALUES ('mod.ts::tsTarget')")
    sym = conn.execute("SELECT sym FROM syms WHERE id = 'mod.ts::tsTarget'").fetchone()[0]
    conn.execute(
        "INSERT INTO nodes (sym, kind, name, qualname, file, lineno, end_lineno) "
        "VALUES (?,'function','tsTarget','tsTarget','mod.ts',1,1)", (sym,)
    )
    conn.execute(
        "INSERT INTO file_hashes (file, hash, indexed_at) VALUES ('mod.ts','deadbeef','2026-01-01')"
    )
    conn.commit()
    conn.close()

    proc = run_index(str(repo), db, env=no_tree_sitter_env)
    assert "0 removed" in proc.stdout

    conn = connect(db)
    assert "mod.ts::tsTarget" in node_ids(conn)
    conn.close()


# --- backend selection ---------------------------------------------------

def test_backends_are_tried_in_the_measured_order():
    """The order is a measured decision, not a preference: on a real
    4,113-file TS/Vue repo the newer pack fixes 3 files and breaks 8,
    so the older pack wins wherever it installs at all. Pinned here so a
    future reorder is a deliberate edit with a reason, not a drive-by."""
    assert ts_parser._PARSER_BACKENDS == (
        "tree_sitter_languages", "tree_sitter_language_pack")


def test_backend_names_the_pack_in_use_or_none():
    name = ts_parser.backend()
    if ts_parser.available():
        assert name in ts_parser._PARSER_BACKENDS
    else:
        assert name is None


def test_unavailable_reason_names_every_backend_it_tried(no_tree_sitter_env, tmp_path):
    """A single actionable message has to say what was actually attempted -
    reporting only the last failure would hide which packs are candidates."""
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from codegraph import ts_parser\n"
        "assert not ts_parser.available()\n"
        "reason = ts_parser.unavailable_reason()\n"
        "for name in ts_parser._PARSER_BACKENDS:\n"
        "    assert name in reason, (name, reason)\n"
        "print('ok')\n"
    ) % SRC_DIR
    child = {**os.environ, **no_tree_sitter_env}
    child["PYTHONPATH"] = os.pathsep.join(
        [SRC_DIR] + [p for p in (child.get("PYTHONPATH", "") or "").split(os.pathsep) if p])
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          env=child)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout
