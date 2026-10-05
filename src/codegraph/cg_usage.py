#!/usr/bin/env python3
"""
Report on codegraph's MCP usage log (see usage_log.py for what's logged
and why, and cg_mcp_server.py for where it's wired in).

Usage:
    codemap-usage <repo_root> [--db PATH] [--json]

Answers, from real logged calls, not impressions:
  - is this being reached for at all, and for which tools
  - are the answers finding anything (resolved/empty-result rates)
  - what's actually being queried (top symbols/queries)
  - what it costs (latency per tool)

  - whether it is used INSTEAD of the built-in search tools, if the
    PostToolUse hook from cg_log_builtin_tool.sh is installed (see
    "Usage vs built-in tools" in the report)

Still does NOT answer: whether a given answer was trusted or
double-checked by reading the file anyway. That needs mining the session
transcripts this tool already indexes (--sessions / search_sessions).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime

from . import graph_lib as gl
from . import usage_log


def load_entries(log_path: str) -> list[dict]:
    entries = []
    if not os.path.exists(log_path):
        return entries
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"  ! skipping malformed log line {line_no}", file=sys.stderr)
    return entries


# A codegraph call followed this quickly by a Grep in the same repo is read
# as the session not having got what it needed. Short on purpose: long
# enough to cover "ask, read the answer, give up", short enough that an
# unrelated later search is not blamed on it.
_FALLBACK_WINDOW_SECONDS = 60


def _parse_ts(value):
    """usage.jsonl writes offset-aware ISO from datetime.isoformat(); the
    shell hook writes ...Z. Python 3.10's fromisoformat rejects the Z form
    and CI runs 3.10, so normalise before parsing. Returns None rather than
    raising - a usage report must never fail on a malformed log line."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _usefulness_summary(entries: list[dict], builtin: list | None):
    """"Is it called" and "is it useful" are different questions, and only
    the first was being reported. A tool can be reached for constantly and
    still be worthless.

    Three signals, all from logs that already exist:
      answered     - the call returned something. Calls with no notion of
                     finding anything (add_note) are excluded rather than
                     counted as successes, which would dilute the rate.
      unresolved   - the symbol could not be identified at all. Different
                     from "found nothing": it means the caller and the
                     graph disagree about what exists.
      fallback     - a Grep followed within a minute, in the same repo.
                     The sharpest signal available: the session asked the
                     graph and went and grepped anyway. Correlation, not
                     proof - the Grep may be unrelated - so it is reported
                     as a rate to watch, not a verdict.
    """
    measurable = answered = unresolved = 0
    for e in entries:
        outcome = e.get("outcome")
        if not isinstance(outcome, dict):
            continue
        if outcome.get("error") == "unresolved" or outcome.get("resolved") is False:
            unresolved += 1
            measurable += 1
            continue
        count_keys = ("result_count", "note_count", "impacted_count", "session_count")
        counts = [outcome[k] for k in count_keys if isinstance(outcome.get(k), int)]
        if counts:
            measurable += 1
            if any(c > 0 for c in counts):
                answered += 1
        elif "resolved" in outcome or "found" in outcome:
            measurable += 1
            if outcome.get("resolved") or outcome.get("found"):
                answered += 1

    followed = fallback_rate = None
    if builtin is not None:
        builtin_times = sorted(t for t in (_parse_ts(b.get("ts")) for b in builtin) if t)
        followed = 0
        for e in entries:
            t = _parse_ts(e.get("ts"))
            if t is None:
                continue
            if any(0 <= (bt - t).total_seconds() <= _FALLBACK_WINDOW_SECONDS
                   for bt in builtin_times):
                followed += 1
        fallback_rate = round(followed / len(entries), 3) if entries else 0.0

    return {
        "measurable": measurable,
        "answered": answered,
        "answered_rate": round(answered / measurable, 3) if measurable else None,
        "unresolved": unresolved,
        "followed_by_builtin": followed,
        "fallback_rate": fallback_rate,
        "fallback_window_seconds": _FALLBACK_WINDOW_SECONDS,
    }


def _builtin_summary(entries: list[dict], builtin: list | None):
    """The comparison this report existed without for too long: of all the
    times someone went looking for something in this repo, what fraction
    went through codegraph rather than Grep/Glob?

    A low share is not automatically bad - Grep is the right tool for string
    literals and config - but a share near zero after real use means the
    graph is not being reached for, which is the one outcome no amount of
    internal correctness testing can rule out."""
    if builtin is None:
        return None
    n_builtin = len(builtin)
    n_cg = len(entries)
    total = n_builtin + n_cg
    return {
        "total": n_builtin,
        "by_tool": dict(Counter(b.get("tool", "?") for b in builtin).most_common()),
        "codegraph_calls": n_cg,
        "codegraph_share": round(n_cg / total, 3) if total else 0.0,
    }


def _query_text(entry: dict) -> str | None:
    args = entry.get("args", {})
    for key in ("query", "symbol", "a", "note"):
        if args.get(key):
            return str(args[key])
    return None


def load_builtin_entries(home_dir: str, repo_root: str):
    """Built-in search-tool calls logged for THIS repo by
    cg_log_builtin_tool.sh. Returns None when the hook isn't installed, so
    the report can tell "nobody greps here" apart from "we aren't looking".

    The log is global (one file, every repo) because it is appended to on
    every Grep and must cost nothing; filtering happens here instead."""
    path = os.path.join(home_dir, "builtin-usage.jsonl")
    if not os.path.exists(path):
        return None
    want = os.path.abspath(repo_root)
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if os.path.abspath(d.get("repo", "")) == want:
                rows.append(d)
    return rows


def build_report(entries: list[dict], builtin: list | None = None) -> dict:
    base = {"total_calls": 0, "builtin": _builtin_summary(entries, builtin),
            "usefulness": _usefulness_summary(entries, builtin)}
    if not entries:
        return base

    by_tool = Counter(e.get("tool", "?") for e in entries)
    timestamps = sorted(e["ts"] for e in entries if e.get("ts"))
    errors = [e for e in entries if e.get("exception")]

    latency_by_tool: dict[str, list[float]] = defaultdict(list)
    for e in entries:
        if isinstance(e.get("latency_ms"), (int, float)):
            latency_by_tool[e.get("tool", "?")].append(e["latency_ms"])

    # "Did this find anything" rate, per tool that has a meaningful notion
    # of empty/unresolved (see usage_log._outcome_summary for the shape).
    outcome_rates = {}
    for tool in ("search", "search_code", "search_memory", "search_sessions"):
        calls = [e for e in entries if e.get("tool") == tool and "outcome" in e]
        if calls:
            nonempty = sum(1 for e in calls if e["outcome"].get("result_count", e["outcome"].get("note_count", 0)) > 0)
            outcome_rates[tool] = {"calls": len(calls), "nonempty_rate": round(nonempty / len(calls), 2)}
    for tool in ("neighbors",):
        calls = [e for e in entries if e.get("tool") == tool and "outcome" in e]
        if calls:
            resolved = sum(1 for e in calls if e["outcome"].get("resolved"))
            outcome_rates[tool] = {"calls": len(calls), "resolved_rate": round(resolved / len(calls), 2)}
    for tool in ("impacted_by",):
        calls = [e for e in entries if e.get("tool") == tool and "outcome" in e and "error" not in e["outcome"]]
        errored = [e for e in entries if e.get("tool") == tool and e.get("outcome", {}).get("error")]
        if calls or errored:
            total = len(calls) + len(errored)
            outcome_rates[tool] = {"calls": total, "resolved_rate": round(len(calls) / total, 2) if total else 0}
    for tool in ("path_between",):
        calls = [e for e in entries if e.get("tool") == tool and "outcome" in e]
        if calls:
            found = sum(1 for e in calls if e["outcome"].get("found"))
            outcome_rates[tool] = {"calls": len(calls), "found_rate": round(found / len(calls), 2)}

    query_counts = Counter(_query_text(e) for e in entries if _query_text(e))

    latency_summary = {}
    for tool, vals in latency_by_tool.items():
        vals_sorted = sorted(vals)
        p95_idx = min(len(vals_sorted) - 1, int(len(vals_sorted) * 0.95))
        latency_summary[tool] = {
            "avg_ms": round(statistics.mean(vals_sorted), 1),
            "median_ms": round(statistics.median(vals_sorted), 1),
            "p95_ms": round(vals_sorted[p95_idx], 1),
        }

    return {
        "total_calls": len(entries),
        "builtin": _builtin_summary(entries, builtin),
        "usefulness": _usefulness_summary(entries, builtin),
        "date_range": [timestamps[0], timestamps[-1]] if timestamps else None,
        "calls_by_tool": dict(by_tool.most_common()),
        "error_count": len(errors),
        "outcome_rates": outcome_rates,
        "top_queries": query_counts.most_common(10),
        "latency_by_tool": latency_summary,
    }


def _print_builtin(report: dict):
    """Question 1: is it being used at all?"""
    b = report.get("builtin")
    print("\nIs it being used?")
    if b is None:
        print("  Not measured. Install the PostToolUse hook from cg_log_builtin_tool.sh")
        print("  to record Grep/Glob calls and get a share here.")
        return
    print(f"  codegraph     {b['codegraph_calls']}")
    for tool, n in b["by_tool"].items():
        print(f"  {tool:<13} {n}")
    share = b["codegraph_share"] * 100
    print(f"  -> codegraph served {share:.0f}% of lookups in this repo")
    if b["codegraph_calls"] == 0 and b["total"] > 0:
        print("     (zero. The graph is not being reached for at all - that is the "
              "result, not a measurement error.)")


def _print_usefulness(report: dict):
    """Question 2: when it IS used, does it help? A tool can be reached for
    constantly and still be worthless, so these are reported separately."""
    u = report.get("usefulness") or {}
    print("\nIs it useful?")
    if not u.get("measurable"):
        print("  Nothing measurable yet - no call has returned a countable outcome.")
        return
    rate = (u["answered_rate"] or 0) * 100
    print(f"  returned something        {u['answered']}/{u['measurable']} ({rate:.0f}%)")
    if u["unresolved"]:
        print(f"  symbol did not resolve    {u['unresolved']}"
              f"   (the caller and the graph disagree about what exists)")
    if u["followed_by_builtin"] is None:
        print("  grepped anyway            not measured (needs the PostToolUse hook)")
    else:
        fb = (u["fallback_rate"] or 0) * 100
        print(f"  grepped anyway            {u['followed_by_builtin']} "
              f"({fb:.0f}% within {u['fallback_window_seconds']}s)")
        if fb >= 50:
            print("     (over half of answers were followed by a Grep - the graph is "
                  "being reached for but is not settling the question)")


def print_report(report: dict, log_path: str):
    if report["total_calls"] == 0:
        print(f"No codegraph usage logged yet at {log_path}.")
        print("(Normal if the MCP server hasn't been used since this was added, or --db points "
              "at a DB the server isn't actually configured against.)")
        _print_builtin(report)
        _print_usefulness(report)
        return

    lo, hi = report["date_range"]
    print(f"Usage log: {log_path}")
    print(f"Covers {report['total_calls']} calls, {lo} to {hi}")
    print()
    print("Calls by tool:")
    for tool, n in report["calls_by_tool"].items():
        print(f"  {tool:<16} {n}")
    if report["error_count"]:
        print(f"\n{report['error_count']} call(s) raised an exception (see log's \"exception\" field).")

    if report["outcome_rates"]:
        print("\nFound-anything rate, per tool:")
        for tool, stats in report["outcome_rates"].items():
            rate_key = next(k for k in stats if k.endswith("_rate"))
            print(f"  {tool:<16} {stats[rate_key]*100:.0f}% ({stats['calls']} calls)")

    if report["top_queries"]:
        print("\nMost-queried symbols/queries:")
        for q, n in report["top_queries"]:
            print(f"  {n:>3}x  {q}")

    if report["latency_by_tool"]:
        print("\nLatency by tool (ms):")
        for tool, stats in report["latency_by_tool"].items():
            print(f"  {tool:<16} avg {stats['avg_ms']:<8} median {stats['median_ms']:<8} p95 {stats['p95_ms']}")

    _print_builtin(report)
    _print_usefulness(report)

    print(
        "\nNote: a share below 100% is expected and healthy - Grep is the right tool for "
        "string literals, config and comments. And \"grepped anyway\" is correlation, not "
        "proof: the Grep may be about something else entirely, so treat a high rate as a "
        "prompt to look rather than a verdict."
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo_root", nargs="?", default=None)
    ap.add_argument("--db", default=None, help="Explicit graph DB path, if not using the default for repo_root")
    ap.add_argument("--json", action="store_true", help="Print the report as JSON instead of text")
    args = ap.parse_args()

    if args.db:
        db_path = args.db
    elif args.repo_root:
        db_path = gl.default_db_path(os.path.abspath(args.repo_root))
    else:
        raise SystemExit("pass repo_root (recommended) or --db")

    log_path = os.path.join(os.path.dirname(db_path), "usage.jsonl")
    entries = load_entries(log_path)
    repo_root = os.path.abspath(args.repo_root) if args.repo_root else None
    builtin = load_builtin_entries(gl.codegraph_home(), repo_root) if repo_root else None
    report = build_report(entries, builtin)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report, log_path)


if __name__ == "__main__":
    main()
