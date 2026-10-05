"""Measuring codegraph AGAINST the built-in search tools.

`usage.jsonl` records every call made through the MCP server, which answers
"is codegraph reached for, and does it find anything". It cannot answer the
question that actually decides whether this tool is worth having: does a
session use it INSTEAD of Grep, or alongside it, or not at all. That has
been the standing gap since the usage log was written - the log only ever
saw its own side of the comparison.

`cg_log_builtin_tool.sh` supplies the other side. Run as a PostToolUse hook
on Grep/Glob, it appends one line per built-in search call, so
`cg_usage.py` can report a ratio instead of a count.

Two deliberate constraints:
  - It logs the TOOL NAME and the repo, never the pattern or the results.
    These run against work repositories; "how often was Grep used" is the
    question, and storing what was searched for would put code and possibly
    secrets into a log for no analytical gain.
  - It only logs for repos that already have a codegraph index, the same
    opt-in signal the re-index hook uses. A session can start anywhere, and
    a tool that quietly accumulates records about unrelated projects is not
    one you would leave switched on.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from conftest import SCRIPTS_DIR

LOGGER = os.path.join(SCRIPTS_DIR, "cg_log_builtin_tool.sh")


def run_logger(home, repo, payload):
    return subprocess.run(
        ["bash", LOGGER],
        input=json.dumps(payload), capture_output=True, text=True,
        env={**os.environ, "CODEGRAPH_HOME": str(home), "CLAUDE_PROJECT_DIR": str(repo)},
    )


def read_log(home):
    p = os.path.join(home, "builtin-usage.jsonl")
    if not os.path.exists(p):
        return []
    return [json.loads(l) for l in open(p) if l.strip()]


@pytest.fixture
def indexed(tmp_path):
    """A CODEGRAPH_HOME where `repo` looks already-indexed, and `other`
    does not."""
    home = tmp_path / "cghome"
    repo = tmp_path / "myrepo"
    other = tmp_path / "unindexed"
    repo.mkdir(); other.mkdir()
    (home / "repos" / "myrepo-abc123").mkdir(parents=True)
    (home / "repos" / "myrepo-abc123" / "graph.db").write_text("")
    return home, repo, other


def test_logs_a_grep_against_an_indexed_repo(indexed):
    home, repo, _ = indexed
    proc = run_logger(home, repo, {"tool_name": "Grep", "tool_input": {"pattern": "secret_token"}})
    assert proc.returncode == 0, proc.stderr
    rows = read_log(home)
    assert len(rows) == 1
    assert rows[0]["tool"] == "Grep"
    assert rows[0]["repo"] == str(repo)
    assert "ts" in rows[0]


def test_never_records_what_was_searched_for(indexed):
    """The pattern can hold code, a customer name, or a credential someone
    was hunting for. The ratio does not need it."""
    home, repo, _ = indexed
    run_logger(home, repo, {"tool_name": "Grep",
                            "tool_input": {"pattern": "AKIAIOSFODNN7EXAMPLE", "path": "/srv/secrets"}})
    raw = open(os.path.join(home, "builtin-usage.jsonl")).read()
    assert "AKIAIOSFODNN7EXAMPLE" not in raw
    assert "/srv/secrets" not in raw


def test_does_not_log_for_a_repo_with_no_index(indexed):
    home, _, other = indexed
    proc = run_logger(home, other, {"tool_name": "Grep", "tool_input": {"pattern": "x"}})
    assert proc.returncode == 0
    assert read_log(home) == []


def test_appends_rather_than_overwrites(indexed):
    home, repo, _ = indexed
    for tool in ("Grep", "Glob", "Grep"):
        run_logger(home, repo, {"tool_name": tool, "tool_input": {}})
    assert [r["tool"] for r in read_log(home)] == ["Grep", "Glob", "Grep"]


@pytest.mark.parametrize("payload", [
    {}, {"tool_name": ""}, {"tool_input": {"pattern": "x"}},
])
def test_malformed_input_is_survivable(indexed, payload):
    """A hook that fails a tool call is worse than a missing data point."""
    home, repo, _ = indexed
    assert run_logger(home, repo, payload).returncode == 0


def test_junk_on_stdin_is_survivable(indexed):
    home, repo, _ = indexed
    proc = subprocess.run(["bash", LOGGER], input="this is not json",
                          capture_output=True, text=True,
                          env={**os.environ, "CODEGRAPH_HOME": str(home),
                               "CLAUDE_PROJECT_DIR": str(repo)})
    assert proc.returncode == 0


def test_no_project_dir_is_survivable(indexed):
    home, _, _ = indexed
    proc = subprocess.run(["bash", LOGGER], input='{"tool_name":"Grep"}',
                          capture_output=True, text=True,
                          env={k: v for k, v in
                               {**os.environ, "CODEGRAPH_HOME": str(home)}.items()
                               if k != "CLAUDE_PROJECT_DIR"})
    assert proc.returncode == 0
    assert read_log(home) == []


# --- the report that joins the two logs ---------------------------------

import cg_usage  # noqa: E402


def write_logs(tmp_path, codegraph_calls, builtin_calls, repo="/w/repo"):
    home = tmp_path / "home"
    (home / "repos" / "repo-abc").mkdir(parents=True)
    usage = home / "repos" / "repo-abc" / "usage.jsonl"
    usage.write_text("".join(
        json.dumps({"ts": f"2026-01-0{i%9+1}T00:00:00Z", "tool": t,
                    "args": {}, "latency_ms": 1.0, "outcome": {"result_count": 1}}) + "\n"
        for i, t in enumerate(codegraph_calls)))
    (home / "builtin-usage.jsonl").write_text("".join(
        json.dumps({"ts": f"2026-01-0{i%9+1}T00:00:00Z", "repo": repo, "tool": t}) + "\n"
        for i, t in enumerate(builtin_calls)))
    return str(home), str(usage), repo


def test_report_shows_the_ratio_against_builtin_tools(tmp_path):
    home, usage, repo = write_logs(tmp_path, ["search"] * 3, ["Grep"] * 7)
    r = cg_usage.build_report(cg_usage.load_entries(usage),
                             cg_usage.load_builtin_entries(home, repo))
    assert r["builtin"]["total"] == 7
    assert r["builtin"]["by_tool"]["Grep"] == 7
    assert r["builtin"]["codegraph_share"] == 0.3   # 3 of 10


def test_ratio_counts_only_this_repo(tmp_path):
    """The built-in log is global, so another project's Greps must not be
    attributed to this one."""
    home, usage, repo = write_logs(tmp_path, ["search"], ["Grep"])
    with open(os.path.join(home, "builtin-usage.jsonl"), "a") as f:
        f.write(json.dumps({"ts": "2026-01-01T00:00:00Z",
                            "repo": "/w/some-other-repo", "tool": "Grep"}) + "\n")
    r = cg_usage.build_report(cg_usage.load_entries(usage),
                              cg_usage.load_builtin_entries(home, repo))
    assert r["builtin"]["total"] == 1


def test_report_works_with_no_builtin_log_at_all(tmp_path):
    """The hook is optional; the report must not require it."""
    home, usage, repo = write_logs(tmp_path, ["search"], [])
    os.remove(os.path.join(home, "builtin-usage.jsonl"))
    r = cg_usage.build_report(cg_usage.load_entries(usage),
                              cg_usage.load_builtin_entries(home, repo))
    assert r["builtin"] is None
    cg_usage.print_report(r, usage)   # must not raise


def test_share_is_zero_when_codegraph_is_never_used(tmp_path):
    """The outcome this is built to be able to report honestly."""
    home, usage, repo = write_logs(tmp_path, [], ["Grep"] * 5)
    r = cg_usage.build_report(cg_usage.load_entries(usage),
                              cg_usage.load_builtin_entries(home, repo))
    assert r["builtin"]["codegraph_share"] == 0.0


# --- is it USEFUL, not just called --------------------------------------

def mk(ts, tool, outcome=None, **extra):
    d = {"ts": ts, "tool": tool, "args": {}, "latency_ms": 1.0}
    if outcome is not None:
        d["outcome"] = outcome
    d.update(extra)
    return d


def usefulness(entries, builtin=None):
    return cg_usage.build_report(entries, builtin)["usefulness"]


def test_a_call_that_returned_results_counts_as_answered():
    u = usefulness([mk("2026-01-01T00:00:00Z", "search", {"result_count": 3})])
    assert u["answered"] == 1
    assert u["measurable"] == 1
    assert u["answered_rate"] == 1.0


def test_a_call_that_returned_nothing_is_counted_separately():
    u = usefulness([
        mk("2026-01-01T00:00:00Z", "search", {"result_count": 3}),
        mk("2026-01-01T00:00:01Z", "search", {"result_count": 0}),
    ])
    assert u["answered"] == 1 and u["measurable"] == 2
    assert u["answered_rate"] == 0.5


def test_an_unresolved_symbol_is_reported_on_its_own():
    """"Found nothing" and "I could not even identify that symbol" are
    different failures: the first may be a true answer, the second means
    the caller and the graph disagree about what exists."""
    u = usefulness([
        mk("2026-01-01T00:00:00Z", "neighbors", {"resolved": True}),
        mk("2026-01-01T00:00:01Z", "neighbors", {"resolved": False}),
        mk("2026-01-01T00:00:02Z", "impacted_by", {"error": "unresolved"}),
    ])
    assert u["unresolved"] == 2


def test_calls_with_no_measurable_outcome_are_excluded():
    """add_note has no notion of finding anything, so counting it would
    quietly dilute the rate."""
    u = usefulness([
        mk("2026-01-01T00:00:00Z", "search", {"result_count": 1}),
        mk("2026-01-01T00:00:01Z", "add_note"),
    ])
    assert u["measurable"] == 1


def test_a_grep_soon_after_a_codegraph_call_is_flagged_as_a_fallback():
    """The sharpest usefulness signal available: the session asked the
    graph, then went and grepped anyway."""
    entries = [mk("2026-01-01T00:00:00Z", "search", {"result_count": 2})]
    builtin = [{"ts": "2026-01-01T00:00:20Z", "repo": "/w/r", "tool": "Grep"}]
    u = usefulness(entries, builtin)
    assert u["followed_by_builtin"] == 1
    assert u["fallback_rate"] == 1.0


def test_a_grep_long_after_is_not_counted_as_a_fallback():
    entries = [mk("2026-01-01T00:00:00Z", "search", {"result_count": 2})]
    builtin = [{"ts": "2026-01-01T01:00:00Z", "repo": "/w/r", "tool": "Grep"}]
    assert usefulness(entries, builtin)["followed_by_builtin"] == 0


def test_a_grep_before_the_call_is_not_a_fallback():
    """Grepping first and then reaching for the graph is the opposite
    story, and must not be counted as the graph failing."""
    entries = [mk("2026-01-01T00:01:00Z", "search", {"result_count": 2})]
    builtin = [{"ts": "2026-01-01T00:00:30Z", "repo": "/w/r", "tool": "Grep"}]
    assert usefulness(entries, builtin)["followed_by_builtin"] == 0


def test_fallback_is_not_measured_without_the_builtin_log():
    u = usefulness([mk("2026-01-01T00:00:00Z", "search", {"result_count": 1})])
    assert u["followed_by_builtin"] is None
    assert u["fallback_rate"] is None


def test_mixed_timestamp_formats_are_handled():
    """usage.jsonl writes offset-aware ISO; the shell hook writes ...Z.
    Python 3.10's fromisoformat rejects the latter, and CI runs 3.10."""
    entries = [mk("2026-01-01T00:00:00.123456+00:00", "search", {"result_count": 1})]
    builtin = [{"ts": "2026-01-01T00:00:10Z", "repo": "/w/r", "tool": "Grep"}]
    assert usefulness(entries, builtin)["followed_by_builtin"] == 1


def test_unparseable_timestamps_do_not_crash_the_report():
    entries = [mk("not-a-date", "search", {"result_count": 1})]
    builtin = [{"ts": "also-not-a-date", "repo": "/w/r", "tool": "Grep"}]
    u = usefulness(entries, builtin)
    assert u["followed_by_builtin"] == 0


def test_report_prints_both_questions(tmp_path, capsys):
    home, usage, repo = write_logs(tmp_path, ["search"] * 2, ["Grep"] * 3)
    r = cg_usage.build_report(cg_usage.load_entries(usage),
                              cg_usage.load_builtin_entries(home, repo))
    cg_usage.print_report(r, usage)
    out = capsys.readouterr().out
    assert "Is it being used" in out
    assert "Is it useful" in out


# --- shell searches, the way searching actually happens -----------------

def test_a_shell_grep_is_recorded_as_a_search(indexed):
    """Measured on a real session: 0 Grep-tool calls, 34 Bash calls, 12 of
    them greps. A matcher of Grep|Glob saw none of it and reported "not
    measured", which understates the comparison on every session that
    routes searching through the shell."""
    home, repo, _ = indexed
    run_logger(home, repo, {"tool_name": "Bash",
                            "tool_input": {"command": "grep -rn hydrateLookup src/"}})
    assert [r["tool"] for r in read_log(home)] == ["Bash:search"]


@pytest.mark.parametrize("cmd", [
    "grep -rn foo src",
    "rg --files-with-matches foo",
    "ag foo lib/",
    "find . -name '*.ts'",
    "cd server && grep -n foo .",
    "egrep -c foo x.ts",
    "ls x || find . -type f",
    "for s in a b; do\n  grep -rn $s src\ndone",
])
def test_search_shaped_commands_are_counted(indexed, cmd):
    home, repo, _ = indexed
    run_logger(home, repo, {"tool_name": "Bash", "tool_input": {"command": cmd}})
    assert len(read_log(home)) == 1, cmd


@pytest.mark.parametrize("cmd", [
    "npm test | grep -i fail",
    "git log --oneline | grep fix",
    "cat package.json | rg version",
])
def test_piping_into_grep_is_filtering_not_searching(indexed, cmd):
    """`npm test | grep fail` is reading output, not looking for code.
    Counting it would inflate the "we grepped instead" side of the ratio
    with work codegraph was never a candidate for."""
    home, repo, _ = indexed
    run_logger(home, repo, {"tool_name": "Bash", "tool_input": {"command": cmd}})
    assert read_log(home) == [], cmd


@pytest.mark.parametrize("cmd", [
    "ls -la", "npm run build", "git status", 'echo "grep is a tool"',
])
def test_ordinary_shell_commands_are_not_logged(indexed, cmd):
    home, repo, _ = indexed
    run_logger(home, repo, {"tool_name": "Bash", "tool_input": {"command": cmd}})
    assert read_log(home) == [], cmd


def test_the_shell_command_is_never_written_down(indexed):
    """Same rule as the Grep pattern: a shell command can carry a path, a
    hostname or a secret someone was hunting for."""
    home, repo, _ = indexed
    run_logger(home, repo, {"tool_name": "Bash",
                            "tool_input": {"command": "grep -rn AKIAIOSFODNN7EXAMPLE /srv"}})
    raw = open(os.path.join(home, "builtin-usage.jsonl")).read()
    assert "AKIAIOSFODNN7EXAMPLE" not in raw and "/srv" not in raw
    assert '"tool":"Bash:search"' in raw


def test_the_grep_tool_is_still_recorded_under_its_own_name(indexed):
    home, repo, _ = indexed
    run_logger(home, repo, {"tool_name": "Grep", "tool_input": {"pattern": "x"}})
    assert [r["tool"] for r in read_log(home)] == ["Grep"]


def test_a_bash_call_with_no_command_is_survivable(indexed):
    home, repo, _ = indexed
    assert run_logger(home, repo, {"tool_name": "Bash", "tool_input": {}}).returncode == 0
    assert read_log(home) == []
