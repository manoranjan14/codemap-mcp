#!/usr/bin/env bash
# Re-index a repo with codegraph, but ONLY if that repo already has an index.
#
# Intended as a Claude Code SessionStart hook. The index is a snapshot and
# nothing refreshes it, so after a pull or a branch switch the graph quietly
# describes the old tree - being confidently stale is the most likely way
# this tool misleads. This closes that gap for the common case.
#
# The "only if already indexed" guard is the important part: a session may
# start in ANY directory, and silently indexing a repo the user never opted
# into would both surprise them and write state for a project they never
# asked about. An index exists only where `cg_index.py` was run explicitly,
# so its presence IS the opt-in signal.
#
# Always exits 0. A hook that fails a session start is worse than a stale
# index.
set -uo pipefail

repo="${1:-}"
[ -n "$repo" ] || exit 0
[ -d "$repo" ] || exit 0

HOME_DIR="${CODEGRAPH_HOME:-$HOME/.codegraph}"
PY="$HOME_DIR/venv/bin/python"
INDEXER="codegraph.cg_index"

[ -x "$PY" ] || exit 0
"$PY" -c "import codegraph" 2>/dev/null || exit 0

# Ask the tool itself where this repo's DB would live, rather than
# reimplementing repo_key() here and letting the two drift.
db="$("$PY" - "$repo" <<'PY' 2>/dev/null
import os, sys
from codegraph import graph_lib as gl
print(gl.default_db_path(os.path.abspath(sys.argv[1])))
PY
)" || exit 0

[ -n "$db" ] && [ -f "$db" ] || exit 0   # not opted in: do nothing at all

"$PY" -m "$INDEXER" "$repo" >/dev/null 2>&1 || true
exit 0
