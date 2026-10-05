#!/usr/bin/env bash
# One-time global install of codegraph, run FROM a checkout of this repo.
#
# Creates an isolated virtualenv at ~/.codegraph/venv and pip-installs the
# package into it. Nothing is written into any repo you later index - all
# state lives under ~/.codegraph (override with $CODEGRAPH_HOME).
#
# A venv rather than a bare `pip install --user`: the MCP SDK needs Python
# 3.10+, a stock macOS `python3` is 3.9, and many distros now ship an
# externally-managed Python that refuses to install into at all. Resolving
# that per-machine was the single biggest source of install failure.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${CODEGRAPH_HOME:-$HOME/.codegraph}"
VENV="$HOME_DIR/venv"

usable_python() {
    command -v "$1" >/dev/null 2>&1 && \
        "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null
}

find_python() {
    # $PYTHON goes through the SAME checks as every other candidate.
    if [ -n "${PYTHON:-}" ]; then
        if usable_python "$PYTHON"; then command -v "$PYTHON"; fi
        return
    fi
    for cand in python3.13 python3.12 python3.11 python3.10 python3; do
        if usable_python "$cand"; then command -v "$cand"; return; fi
    done
}

[ -f "$SELF_DIR/pyproject.toml" ] || {
    echo "error: run this from a checkout of codegraph (pyproject.toml not found)" >&2; exit 1; }

PY_BIN="$(find_python)"
if [ -z "$PY_BIN" ]; then
    if [ -n "${PYTHON:-}" ]; then
        echo "error: \$PYTHON is set to '$PYTHON', which is missing or older than 3.10." >&2
    else
        echo "error: need Python 3.10+; none found." >&2
        echo "       Install one (e.g. 'brew install python@3.12') or set \$PYTHON." >&2
    fi
    exit 1
fi
echo "Using Python: $PY_BIN ($("$PY_BIN" -V 2>&1))"

mkdir -p "$HOME_DIR"
"$PY_BIN" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
echo "Installing codegraph and its optional extras..."
"$VENV/bin/python" -m pip install --quiet "$SELF_DIR[server,ts]"

echo
echo "Installed to: $VENV"
echo "Indexes live under: $HOME_DIR/repos/<repo-key>/graph.db"
echo "Nothing above touched any of your project repos."
echo
echo "For EACH repo you want to use codegraph on:"
echo
echo "  $VENV/bin/codemap-index /path/to/repo"
echo "  cd /path/to/repo   # --scope local ties the registration to this project"
echo "  claude mcp add --scope local codegraph -- \\"
echo "      $VENV/bin/codemap-mcp --repo-root /path/to/repo"
echo
echo "Then start Claude Code in that repo."
echo
echo "Optional hooks for ~/.claude/settings.json:"
echo
echo "  SessionStart (startup|resume), async - keeps the index fresh:"
echo "      $VENV/bin/codemap-reindex-if-opted-in \"\$CLAUDE_PROJECT_DIR\""
echo
echo "  PostToolUse (Grep|Glob|Bash), async - measures codegraph against grep:"
echo "      $VENV/bin/codemap-log-builtin-tool"
echo
echo "Both act only on repos that already have an index, and the second"
echo "records tool names only - never what was searched for."
