#!/usr/bin/env bash
# One-command install of codegraph for anyone who does NOT already have this
# repo checked out - the "curl | bash" pattern (mirrors how other MCP
# servers ship a single install command instead of "clone, then run a
# script, then run another script").
#
# Usage, once this repo has a real GitHub URL:
#   CODEGRAPH_GIT_URL=git@github.com:<you>/codegraph.git \
#       bash <(curl -fsSL https://raw.githubusercontent.com/<you>/codegraph/main/bootstrap.sh)
#
#   # and, in the same command, also index + register a target repo:
#   CODEGRAPH_GIT_URL=git@github.com:<you>/codegraph.git \
#       bash <(curl -fsSL .../bootstrap.sh) --repo-root /path/to/your/repo
#
# Already have this repo cloned locally? Just run ./install.sh directly -
# this script exists ONLY for the "I don't have it yet" case, and it ends
# by calling install.sh itself, so the two never drift apart.
#
# Why CODEGRAPH_GIT_URL isn't hardcoded: this file ships inside the repo
# it clones, so it can't know its own remote's URL in advance. Set it once
# you've pushed this repo somewhere and know the real clone URL - private
# repo or public, either works as long as `git clone` can reach it with
# your own credentials (SSH key or a configured HTTPS credential helper;
# this script does no authentication of its own).
#
# Env overrides:
#   CODEGRAPH_GIT_URL   - required. This repo's own clone URL.
#   CODEGRAPH_SRC_DIR   - where to clone/update the source checkout
#                         (default: ~/.codegraph/src - separate from
#                         ~/.codegraph/venv, which install.sh manages)
set -euo pipefail

GIT_URL="${CODEGRAPH_GIT_URL:-}"
SRC_DIR="${CODEGRAPH_SRC_DIR:-$HOME/.codegraph/src}"

if [ -z "$GIT_URL" ]; then
    echo "error: set CODEGRAPH_GIT_URL to this repo's clone URL, e.g.:" >&2
    echo "  CODEGRAPH_GIT_URL=git@github.com:<you>/codegraph.git bash bootstrap.sh" >&2
    exit 1
fi

if [ -d "$SRC_DIR/.git" ]; then
    echo "Updating existing checkout at $SRC_DIR..."
    git -C "$SRC_DIR" pull --ff-only
else
    echo "Cloning $GIT_URL to $SRC_DIR..."
    mkdir -p "$(dirname "$SRC_DIR")"
    git clone "$GIT_URL" "$SRC_DIR"
fi

echo
bash "$SRC_DIR/install.sh"

# Optional: also index + register a target repo in this same command.
REPO_ROOT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --repo-root) REPO_ROOT="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
done

TOOL_DIR="${CODEGRAPH_HOME:-$HOME/.codegraph}/tool"

if [ -n "$REPO_ROOT" ]; then
    REPO_ROOT="$(cd "$REPO_ROOT" && pwd)"
    echo
    echo "Indexing $REPO_ROOT..."
    python3 "$TOOL_DIR/scripts/cg_index.py" "$REPO_ROOT"
    echo
    echo "Registering the MCP server for $REPO_ROOT..."
    ( cd "$REPO_ROOT" && claude mcp add --scope local codegraph -- \
        python3 "$TOOL_DIR/scripts/cg_mcp_server.py" --repo-root "$REPO_ROOT" )
    echo
    echo "Done - restart (or start) Claude Code in $REPO_ROOT to use codegraph."
else
    echo
    echo "Global install done. For each repo you want to use codegraph on:"
    echo "  python3 $TOOL_DIR/scripts/cg_index.py /path/to/repo"
    echo "  cd /path/to/repo && claude mcp add --scope local codegraph -- \\"
    echo "      python3 $TOOL_DIR/scripts/cg_mcp_server.py --repo-root /path/to/repo"
    echo "(or re-run this script with --repo-root /path/to/repo to do both at once)"
fi
