#!/usr/bin/env bash
# Record that a BUILT-IN search tool (Grep/Glob/...) was used, so codegraph
# can be measured against the thing it claims to replace.
#
# Intended as a Claude Code PostToolUse hook on Grep|Glob. usage.jsonl
# already records every call made through the codegraph MCP server, which
# answers "is it reached for, and does it find anything" - but not the
# question that decides whether the tool earns its keep: is it used INSTEAD
# of Grep, alongside it, or not at all. This supplies the other side of
# that ratio; cg_usage.py joins the two.
#
# Two deliberate limits:
#
#   - It records the TOOL NAME and the repo, never the pattern, the path or
#     the results. These hooks run against work repositories; the question
#     is "how often was Grep used", and storing what was searched for would
#     put code - and occasionally a credential someone was hunting for -
#     into a log, for no analytical gain.
#
#   - It logs only for repos that already have a codegraph index, the same
#     opt-in signal cg_reindex_if_opted_in.sh uses. A session can start in
#     any directory, and a tool that quietly accumulates records about
#     unrelated projects is not one anybody should leave switched on.
#
# Always exits 0, and never writes to stdout: a hook that fails or chatters
# is worse than a missing data point.
set -uo pipefail

repo="${CLAUDE_PROJECT_DIR:-}"
[ -n "$repo" ] || exit 0

HOME_DIR="${CODEGRAPH_HOME:-$HOME/.codegraph}"

# Cheap opt-in test: an indexed repo owns a directory named after its
# basename. Deliberately not asking Python for the exact key - this runs on
# every Grep, so it must cost nothing, and a basename collision only risks
# logging a tool name we would have logged anyway.
base="$(basename "$repo")"
compgen -G "$HOME_DIR/repos/$base-*" >/dev/null 2>&1 || exit 0

command -v jq >/dev/null 2>&1 || exit 0
payload="$(cat)"
tool="$(printf '%s' "$payload" | jq -r 'if type == "object" then (.tool_name // "") else "" end' 2>/dev/null)" || exit 0
[ -n "$tool" ] && [ "$tool" != "null" ] || exit 0

# Searching does not only happen through the Grep tool. Measured on a real
# session against api-service: 0 Grep-tool calls, 34 Bash calls, 12 of
# them greps - so a matcher of Grep|Glob reported "not measured" while the
# session was in fact searching constantly. Bash is matched too, and
# classified here.
if [ "$tool" = "Bash" ]; then
    cmd="$(printf '%s' "$payload" | jq -r 'if type == "object" then (.tool_input.command // "") else "" end' 2>/dev/null)" || exit 0
    [ -n "$cmd" ] && [ "$cmd" != "null" ] || exit 0
    # A search tool in COMMAND position - start of the line, or after && / ;
    # - is someone looking for code. After a pipe it is filtering the output
    # of something else ("npm test | grep -i fail"), which codegraph was
    # never a candidate for and must not inflate the comparison.
    # Note the alternatives: start of line (grep matches line by line, so a
    # search inside a loop body counts), and after && || ; - but never
    # after a single |, which is the filtering case.
    if printf '%s' "$cmd" | grep -Eq '(^|&&[[:space:]]*|\|\|[[:space:]]*|;[[:space:]]*)[[:space:]]*(grep|egrep|fgrep|rg|ripgrep|ag|ack|find)[[:space:]]'; then
        tool="Bash:search"
    else
        exit 0
    fi
fi

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '{"ts":"%s","repo":"%s","tool":"%s"}\n' "$ts" "$repo" "$tool" \
    >> "$HOME_DIR/builtin-usage.jsonl" 2>/dev/null || true
exit 0
