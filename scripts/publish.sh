#!/usr/bin/env bash
# Build and upload codemap-mcp to PyPI, taking credentials from .env.
#
# .env is gitignored and holds a live token, so this script never echoes
# its contents -- not the token, not the whole environment. Run it from
# the repo root:
#
#     ./scripts/publish.sh            # upload to PyPI
#     ./scripts/publish.sh --test     # upload to TestPyPI instead
#     ./scripts/publish.sh --check    # build and validate, upload nothing

set -euo pipefail

cd "$(dirname "$0")/.."

MODE=upload
case "${1:-}" in
    --test)  MODE=test ;;
    --check) MODE=check ;;
    "")      ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
esac

if [[ ! -f .env ]]; then
    echo "error: .env not found. Copy .env.example to .env and put your" >&2
    echo "       PyPI token in it. See https://pypi.org/manage/account/token/" >&2
    exit 1
fi

# Refuse to run if .env ever becomes trackable -- a token in a commit on a
# repo that mirrors to a public one is the failure this guards against.
if ! git check-ignore -q .env 2>/dev/null && \
   git ls-files --error-unmatch .env >/dev/null 2>&1; then
    echo "error: .env is tracked by git. Remove it from the index before" >&2
    echo "       publishing: git rm --cached .env" >&2
    exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

if [[ "${TWINE_PASSWORD:-}" == pypi-REPLACE_WITH_YOUR_TOKEN || -z "${TWINE_PASSWORD:-}" ]]; then
    echo "error: TWINE_PASSWORD in .env is still the placeholder." >&2
    exit 1
fi

if [[ "$MODE" == test ]]; then
    export TWINE_REPOSITORY_URL=https://test.pypi.org/legacy/
fi

# Pick an interpreter that actually has build + twine. macOS system
# python3 has neither, so fall back to the venv install.sh creates --
# that is where the 0.1.0 upload was built from. Override with PY=...
pick_python() {
    local c
    for c in "${PY:-}" .venv/bin/python venv/bin/python \
             "$HOME/.codegraph/venv/bin/python" python3 python; do
        [[ -n "$c" ]] || continue
        command -v "$c" >/dev/null 2>&1 || continue
        "$c" -c 'import build, twine' >/dev/null 2>&1 || continue
        echo "$c"; return 0
    done
    return 1
}

if ! PY=$(pick_python); then
    echo "error: no python on this machine has both 'build' and 'twine'." >&2
    echo "       Install them:  python3 -m pip install build twine" >&2
    echo "       Or point at one:  PY=/path/to/python ./scripts/publish.sh" >&2
    exit 1
fi
echo "==> using $PY"

VERSION=$(grep -m1 '^version' pyproject.toml | sed 's/.*"\(.*\)".*/\1/')
echo "==> codemap-mcp ${VERSION}"

echo "==> tests"
"$PY" -m pytest -q

echo "==> clean"
rm -rf dist build ./*.egg-info

echo "==> build"
"$PY" -m build

echo "==> validate"
"$PY" -m twine check --strict dist/*

if [[ "$MODE" == check ]]; then
    echo "==> --check given, stopping before upload"
    ls -la dist/
    exit 0
fi

TARGET=PyPI
[[ "$MODE" == test ]] && TARGET=TestPyPI
echo
echo "About to upload codemap-mcp ${VERSION} to ${TARGET}."
echo "A version number on PyPI cannot be reused or reverted."
read -r -p "Type the version to confirm: " CONFIRM
if [[ "$CONFIRM" != "$VERSION" ]]; then
    echo "aborted"; exit 1
fi

# --verbose so a rejection shows the actual reason. PyPI's 400s are
# generic in the default output; the real cause is only in the verbose
# body. Learned the hard way on the 0.1.0 upload.
"$PY" -m twine upload --verbose dist/*

echo
echo "==> done. Tag it:"
echo "    git tag v${VERSION} && git push origin v${VERSION}"
