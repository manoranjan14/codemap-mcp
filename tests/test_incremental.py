"""Regression tests for incremental re-indexing.

The claim under test (docs/DESIGN.md, "Performance"): re-indexing is cheap
because it hashes file CONTENT (SHA-256, not mtime), only reparses what
changed, and cleans up after deleted files without leaving edges pointing
at nodes that no longer exist.
"""
from __future__ import annotations

import os
import re
import time

from conftest import connect, edge_rows, edges, node_ids, run_index


def _reparsed(proc) -> int:
    m = re.search(r"(\d+) \(re\)parsed", proc.stdout)
    assert m, proc.stdout
    return int(m.group(1))


def _removed(proc) -> int:
    m = re.search(r"(\d+) removed", proc.stdout)
    assert m, proc.stdout
    return int(m.group(1))


def test_cold_index_then_noop_reindex(scratch_repo):
    repo, db = scratch_repo
    first = run_index(repo, db)
    assert _reparsed(first) == 2

    second = run_index(repo, db)
    assert _reparsed(second) == 0


def test_touch_without_content_change_does_not_reparse(scratch_repo):
    """mtime-based invalidation would reparse here; content hashing must
    not. This is what makes `git checkout` of an unchanged file free."""
    repo, db = scratch_repo
    run_index(repo, db)

    path = os.path.join(repo, "lib.py")
    os.utime(path, (time.time() + 10, time.time() + 10))

    assert _reparsed(run_index(repo, db)) == 0


def test_only_the_changed_file_is_reparsed(scratch_repo):
    repo, db = scratch_repo
    run_index(repo, db)

    with open(os.path.join(repo, "lib.py"), "a") as f:
        f.write("\n\ndef added():\n    return 3\n")

    proc = run_index(repo, db)
    assert _reparsed(proc) == 1

    conn = connect(db)
    assert "lib.py::added" in node_ids(conn)
    # The unchanged caller's edge into this file still resolves - node ids
    # are deterministic (file::qualname), so untouched files keep working.
    assert "lib.py::target" in edges(conn, "app.py::caller")
    conn.close()


def test_force_reparses_everything_without_duplicating_edges(scratch_repo):
    repo, db = scratch_repo
    run_index(repo, db)

    conn = connect(db)
    before = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    conn.close()

    proc = run_index(repo, db, "--force")
    assert _reparsed(proc) == 2

    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == before
    conn.close()


def test_deleted_file_is_purged_and_its_callers_degrade_to_external(scratch_repo):
    """Deleting lib.py must remove its nodes AND downgrade app.py's
    resolved `calls` edge into it to calls_external - otherwise the edge
    keeps asserting a resolved call to a node that no longer exists."""
    repo, db = scratch_repo
    run_index(repo, db)

    conn = connect(db)
    assert "lib.py::target" in edges(conn, "app.py::caller")
    conn.close()

    os.remove(os.path.join(repo, "lib.py"))
    proc = run_index(repo, db)
    assert _removed(proc) == 1
    assert "dangling call edge" in proc.stdout

    conn = connect(db)
    ids = node_ids(conn)
    assert not any(i.startswith("lib.py") for i in ids)

    out = edges(conn, "app.py::caller")
    assert "external:target" in out
    assert "lib.py::target" not in out

    dangling = [
        (src, dst) for src, _t, dst in edge_rows(conn)
        if not dst.startswith(("external:", "ambiguous:")) and dst not in ids
    ]
    assert dangling == []
    conn.close()


def test_a_file_that_fails_to_parse_is_not_reparsed_until_it_changes(tmp_path):
    """A file the parser rejects never got a hash recorded, so every
    subsequent "no-op" run parsed it again. On a real repo that was 5 files
    re-parsed on every single run, turning a ~0.2s no-op into 1.4s."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ok.py").write_text("def fine():\n    return 1\n")
    (repo / "bad.py").write_text("def broken(:\n    pass\n")
    db = str(tmp_path / "graph.db")

    first = run_index(str(repo), db)
    assert _reparsed(first) == 2
    assert "skipping bad.py" in first.stderr

    second = run_index(str(repo), db)
    assert _reparsed(second) == 0, "the unparseable file was retried"
    assert "skipping bad.py" not in second.stderr


def test_a_fixed_file_is_picked_up_on_the_next_run(tmp_path):
    """Skipping must be keyed on CONTENT, not on the filename - repairing
    the file has to bring it back without --force."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "bad.py").write_text("def broken(:\n    pass\n")
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)
    assert _reparsed(run_index(str(repo), db)) == 0

    (repo / "bad.py").write_text("def repaired():\n    return 1\n")
    third = run_index(str(repo), db)
    assert _reparsed(third) == 1

    conn = connect(db)
    assert "bad.py::repaired" in node_ids(conn)
    conn.close()


def test_force_retries_a_previously_failing_file(tmp_path):
    """--force is the escape hatch for "the parser got better", since a
    recorded failure otherwise sticks until the file itself changes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "bad.py").write_text("def broken(:\n    pass\n")
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)
    assert _reparsed(run_index(str(repo), db)) == 0

    forced = run_index(str(repo), db, "--force")
    assert "skipping bad.py" in forced.stderr
    assert _reparsed(forced) == 1


def test_a_failing_file_contributes_no_nodes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "bad.py").write_text("def broken(:\n    pass\n")
    (repo / "ok.py").write_text("def fine():\n    return 1\n")
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)
    run_index(str(repo), db)

    conn = connect(db)
    ids = node_ids(conn)
    assert not any(i.startswith("bad.py") for i in ids)
    assert "ok.py::fine" in ids
    conn.close()


def test_deleting_an_unparseable_file_clears_its_failure_record(tmp_path):
    """A file that ONLY ever failed has no file_hashes row, so it never
    enters the `deleted` set - its failed_files row outlived the file
    itself, forever."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "bad.py").write_text("def broken(:\n    pass\n")
    (repo / "ok.py").write_text("def fine():\n    return 1\n")
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)

    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM failed_files").fetchone()[0] == 1
    conn.close()

    (repo / "bad.py").unlink()
    run_index(str(repo), db)

    conn = connect(db)
    assert conn.execute(
        "SELECT file FROM failed_files").fetchall() == [], "stale failure record survived"
    conn.close()


def test_a_file_that_starts_parsing_cleanly_clears_its_failure_record(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "bad.py").write_text("def broken(:\n    pass\n")
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)
    (repo / "bad.py").write_text("def repaired():\n    return 1\n")
    run_index(str(repo), db)

    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM failed_files").fetchone()[0] == 0
    conn.close()
