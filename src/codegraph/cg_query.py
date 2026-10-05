#!/usr/bin/env python3
"""
Query the codegraph DB from the command line.

Usage (pass exactly one of --repo-root / --db):
    codemap-query --repo-root DIR neighbors <symbol> [--direction in|out|both]
    codemap-query --db PATH path <a> <b>
    codemap-query --repo-root DIR impacted-by <symbol>
    codemap-query --repo-root DIR search <text>
    codemap-query --repo-root DIR note-add <symbol_or_empty> <note> [--session ID] [--source stated|observed|derived]
    codemap-query --repo-root DIR search-sessions <text> [--limit N] [--role user|assistant|summary]
    codemap-query --repo-root DIR list-sessions [--limit N]

--repo-root resolves the DB the same deterministic way cg_index.py's
own default does (graph_lib.default_db_path) - use it unless you
indexed with an explicit --db pointing somewhere else, in which case
pass that same --db here.
"""
from __future__ import annotations

import argparse
import json
import os

from . import graph_lib as gl
from . import query_lib as ql


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", default=None,
                     help="Repo to query. DB path is derived the same way cg_index.py's "
                          "default is (graph_lib.default_db_path).")
    ap.add_argument("--db", default=None,
                     help="Explicit path to the graph DB, if it isn't at the default "
                          "location for --repo-root.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("neighbors")
    p.add_argument("symbol")
    p.add_argument("--direction", choices=["in", "out", "both"], default="both")
    p.add_argument("--limit", type=int, default=30,
                   help="Max edges returned per direction (default 30). The result's "
                        "`truncated` flag and `incoming_total`/`outgoing_total` say "
                        "whether you are seeing all of them.")

    p = sub.add_parser("path")
    p.add_argument("a")
    p.add_argument("b")

    p = sub.add_parser("impacted-by")
    p.add_argument("symbol")
    p.add_argument("--max-depth", type=int, default=3)
    p.add_argument("--limit", type=int, default=ql.IMPACTED_BY_DEFAULT_LIMIT,
                   help=f"Max results, nearest first (default {ql.IMPACTED_BY_DEFAULT_LIMIT}). "
                        f"The result's `total` and `truncated` say whether you are "
                        f"seeing the full blast radius.")

    p = sub.add_parser("search")
    p.add_argument("text")
    p.add_argument("--limit", type=int, default=15,
                   help="Max results, best-ranked first (default 15). The result's "
                        "`total` and `truncated` say whether you are seeing them all.")

    p = sub.add_parser("note-add")
    p.add_argument("symbol", help="Symbol/node id to attach to, or '-' for a repo-wide note")
    p.add_argument("note")
    p.add_argument("--session", default="cli")
    p.add_argument("--source", default="observed", choices=["stated", "observed", "derived"])

    p = sub.add_parser("search-sessions", help="Search indexed Claude Code session transcripts (Layer 3)")
    p.add_argument("text")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--role", default=None, choices=["user", "assistant", "summary"])
    p.add_argument("--kind", default=None, choices=["message", "action", "summary"],
                   help="message = what was said, action = a tool call, summary = a compaction recap")

    p = sub.add_parser("list-sessions", help="List indexed session transcripts (Layer 3)")
    p.add_argument("--limit", type=int, default=20)

    args = ap.parse_args()
    if args.db:
        db_path = args.db
    elif args.repo_root:
        db_path = gl.default_db_path(os.path.abspath(args.repo_root))
    else:
        raise SystemExit("pass --repo-root (recommended) or --db")
    if not os.path.exists(db_path):
        raise SystemExit(f"no graph DB at {db_path} - run cg_index.py first")
    conn = gl.connect(db_path)

    if args.cmd == "neighbors":
        result = ql.neighbors(conn, args.symbol, direction=args.direction, limit=args.limit)
    elif args.cmd == "path":
        result = ql.shortest_path(conn, args.a, args.b)
    elif args.cmd == "impacted-by":
        result = ql.impacted_by(conn, args.symbol, max_depth=args.max_depth, limit=args.limit)
    elif args.cmd == "search":
        result = ql.search(conn, args.text, limit=args.limit)
    elif args.cmd == "note-add":
        ref = None if args.symbol == "-" else args.symbol
        result = ql.add_note(conn, ref, args.note, args.session, args.source)
    elif args.cmd == "search-sessions":
        result = ql.search_sessions(conn, args.text, limit=args.limit, role=args.role, kind=args.kind)
    elif args.cmd == "list-sessions":
        result = ql.list_sessions(conn, limit=args.limit)
    else:
        raise SystemExit(f"unknown command {args.cmd}")

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
