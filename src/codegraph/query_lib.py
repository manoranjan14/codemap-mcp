"""
Query + session-memory logic shared by the CLI (cg_query.py) and the MCP
server (cg_mcp_server.py), so both surfaces behave identically.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime

from . import graph_lib as gl

_NODE_COLUMNS = ["id", "kind", "name", "qualname", "file", "lineno", "end_lineno"]


# Node ids are interned to integers in `edges` (see graph_lib.SCHEMA). The
# PUBLIC surface of this module is unchanged - every function still takes
# and returns id STRINGS - so the integers never escape past these helpers.
# That was a deliberate constraint: it kept the whole existing test suite
# valid as the regression check for the storage change.

def _sym_of(conn: sqlite3.Connection, node_id: str):
    row = conn.execute("SELECT sym FROM syms WHERE id = ?", (node_id,)).fetchone()
    return row[0] if row else None


def _resolve_sym(conn: sqlite3.Connection, ref: str):
    """Internal twin of resolve_symbol returning (sym, candidate_id_strings)."""
    row = conn.execute(
        "SELECT n.sym FROM nodes n JOIN syms s ON s.sym = n.sym WHERE s.id = ?", (ref,)
    ).fetchone()
    if row:
        return row[0], []
    for column in ("qualname", "name"):
        rows = conn.execute(
            f"SELECT n.sym, s.id FROM nodes n JOIN syms s ON s.sym = n.sym "
            f"WHERE n.{column} = ?", (ref,)
        ).fetchall()
        if len(rows) == 1:
            return rows[0][0], []
        if len(rows) > 1 and column == "name":
            return None, [r[1] for r in rows]
    return None, []


def _unresolved(conn: sqlite3.Connection, candidates):
    """The standard unresolved reply. Ambiguity is a complete answer - the
    caller got a list to choose from - so it is NOT muddied with a
    re-index suggestion; only a true miss carries one."""
    out = {"error": "unresolved", "ambiguous_candidates": candidates}
    if not candidates:
        out["index"] = _index_state(conn)
    return out


def _index_state(conn: sqlite3.Connection):
    """How old the index is, and how much of the working tree has changed
    since - attached to UNRESOLVED results only.

    A miss used to be ambiguous evidence: a function written an hour ago
    and a symbol typed at random returned byte-identical output. They are
    very different answers, and the common one is "re-index". This does
    not resolve the ambiguity for the caller, it gives them the fact that
    settles it.

    Deliberately only on the miss path: it stats the working tree, which
    is cheap but not free, and a successful lookup has no reason to pay."""
    row = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    indexed_at, repo_root = row.get("indexed_at"), row.get("repo_root")
    state = {"indexed_at": indexed_at, "files_changed_since_index": None,
             "stale": False, "hint": ""}
    if not indexed_at or not repo_root or not os.path.isdir(repo_root):
        state["hint"] = ("Cannot tell whether this index is current (the repo root "
                         "recorded at index time is not readable). Re-index before "
                         "trusting this miss.")
        return state
    try:
        cutoff = datetime.fromisoformat(indexed_at).timestamp()
    except ValueError:
        return state
    changed = 0
    for rel in gl.iter_py_files(repo_root):
        try:
            if os.path.getmtime(os.path.join(repo_root, rel)) > cutoff:
                changed += 1
        except OSError:
            continue
    state["files_changed_since_index"] = changed
    state["stale"] = changed > 0
    state["hint"] = (
        f"{changed} file(s) have changed since this index was built - re-index "
        f"before treating this as 'does not exist'."
        if changed else
        "The index is current with the working tree, so this symbol really is absent."
    )
    return state


def resolve_symbol_detail(conn: sqlite3.Connection, ref: str):
    """resolve_symbol plus, when it misses, evidence about whether the
    index is simply out of date."""
    node_id, candidates = resolve_symbol(conn, ref)
    out = {"node_id": node_id, "ambiguous_candidates": candidates}
    if node_id is None and not candidates:
        out["index"] = _index_state(conn)
    return out


def resolve_symbol(conn: sqlite3.Connection, ref: str):
    """Resolve a user-given string to a node id.

    Tries, in order: exact node id, exact qualname, exact name (if unique).
    Returns (node_id, candidates) - node_id is None if unresolved or
    ambiguous; candidates lists what matched by name when ambiguous.
    """
    sym, candidates = _resolve_sym(conn, ref)
    if sym is None:
        return None, candidates
    return _id_of(conn, sym), []


def _id_of(conn: sqlite3.Connection, sym: int):
    row = conn.execute("SELECT id FROM syms WHERE sym = ?", (sym,)).fetchone()
    return row[0] if row else None


def _node_info_by_sym(conn: sqlite3.Connection, sym):
    row = conn.execute(
        "SELECT s.id, n.kind, n.name, n.qualname, n.file, n.lineno, n.end_lineno "
        "FROM nodes n JOIN syms s ON s.sym = n.sym WHERE n.sym = ?", (sym,)
    ).fetchone()
    if not row:
        return None
    return dict(zip(_NODE_COLUMNS, row))


def node_info(conn: sqlite3.Connection, node_id: str):
    sym = _sym_of(conn, node_id)
    return _node_info_by_sym(conn, sym) if sym is not None else None


def _edge_evidence(etype, src_id, dst_id, dst_info, lineno, note):
    """Rebuild the human-readable evidence string an edge used to store.

    The old column held 30 MB of sentences whose content was already
    implied by the edge: "call at api/install-app.js:10" repeats the file
    encoded in `src_id`, and "function GET defined at ..." repeats the
    target node's own kind, name, file and line. Only what cannot be
    derived is stored (see cg_index.split_evidence), and the sentence is
    rebuilt here so callers see exactly what they saw before."""
    if note:                       # imports / ambiguous: kept verbatim
        return note
    src_file = src_id.split("::", 1)[0]
    at = f"{src_file}:{lineno}" if lineno else src_file
    if etype == "defines":
        if dst_info:
            return (f"{dst_info['kind']} {dst_info['name']} defined at "
                    f"{dst_info['file']}:{dst_info['lineno']}")
        return f"defines {dst_id}"
    if etype == "inherits":
        return f"extends {dst_id.split('::')[-1].split(':')[-1]}" + (f" (line {lineno})" if lineno else "")
    if etype in ("calls", "calls_external", "calls_ambiguous"):
        # A `calls` edge into a closure is the enclosing scope causing that
        # callback to run, and read differently from an ordinary call.
        if dst_info and dst_info.get("kind") == "closure":
            return f"defines callback at {at}"
        if dst_info and dst_info.get("kind") == "template":
            return f"template block of {src_file}"
        return f"call at {at}"
    return at


def neighbors(conn: sqlite3.Connection, ref: str, direction: str = "both", limit: int = 30):
    sym, candidates = _resolve_sym(conn, ref)
    if sym is None:
        return _unresolved(conn, candidates)
    node_id = _id_of(conn, sym)

    # Totals come from a separate COUNT, not len() of the capped rows: a
    # caller asking "find every caller of this" has to be able to tell a
    # complete answer from a truncated one. Returning 30 of 57 with no
    # signal is a wrong answer dressed as a complete one.
    out = {
        "node": _node_info_by_sym(conn, sym),
        "outgoing": [], "incoming": [],
        "outgoing_total": 0, "incoming_total": 0,
        "limit": limit, "truncated": False,
    }
    if direction in ("out", "both"):
        out["outgoing_total"] = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE src = ?", (sym,)
        ).fetchone()[0]
        for dst_sym, dst_id, etype, lineno, note in conn.execute(
            "SELECT e.dst, s.id, e.type, e.lineno, e.note FROM edges e "
            "JOIN syms s ON s.sym = e.dst WHERE e.src = ? LIMIT ?", (sym, limit)
        ):
            info = _node_info_by_sym(conn, dst_sym)
            out["outgoing"].append({
                "type": etype, "target": dst_id, "target_info": info,
                "evidence": _edge_evidence(etype, node_id, dst_id, info, lineno, note),
            })
    if direction in ("in", "both"):
        out["incoming_total"] = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE dst = ?", (sym,)
        ).fetchone()[0]
        for src_sym, src_id, etype, lineno, note in conn.execute(
            "SELECT e.src, s.id, e.type, e.lineno, e.note FROM edges e "
            "JOIN syms s ON s.sym = e.src WHERE e.dst = ? LIMIT ?", (sym, limit)
        ):
            out["incoming"].append({
                "type": etype, "source": src_id, "source_info": _node_info_by_sym(conn, src_sym),
                "evidence": _edge_evidence(etype, src_id, node_id, _node_info_by_sym(conn, sym), lineno, note),
            })

    out["truncated"] = (len(out["outgoing"]) < out["outgoing_total"]
                        or len(out["incoming"]) < out["incoming_total"])
    if out["truncated"]:
        out["truncation_note"] = (
            f"showing {len(out['outgoing'])}/{out['outgoing_total']} outgoing and "
            f"{len(out['incoming'])}/{out['incoming_total']} incoming edges - "
            f"raise `limit` (currently {limit}) for the complete list."
        )
    return out


_SQLITE_MAX_VARS = 900  # stay under SQLite's default ~999-parameter limit


def _batch_frontier_neighbors(conn: sqlite3.Connection, frontier: list[str]):
    """One or two indexed queries per BFS layer (chunked if the frontier is
    large) instead of loading the whole edges table. Returns list of
    (known_node, candidate_neighbor) pairs, undirected."""
    pairs = []
    for i in range(0, len(frontier), _SQLITE_MAX_VARS):
        chunk = frontier[i:i + _SQLITE_MAX_VARS]
        placeholders = ",".join("?" * len(chunk))
        for src, dst in conn.execute(f"SELECT src, dst FROM edges WHERE src IN ({placeholders})", chunk):
            pairs.append((src, dst))
        for src, dst in conn.execute(f"SELECT src, dst FROM edges WHERE dst IN ({placeholders})", chunk):
            pairs.append((dst, src))
    return pairs


MAX_FRONTIER_NODES = 4000  # see docstring: fail fast on hub-node queries


def _real_nodes_only(conn: sqlite3.Connection, syms: list) -> list:
    """Filter to symbols that are actually defined in this repo. A sym with
    no `nodes` row is an `external:`/`ambiguous:` placeholder."""
    if not syms:
        return []
    out = []
    for i in range(0, len(syms), _SQLITE_MAX_VARS):
        chunk = syms[i:i + _SQLITE_MAX_VARS]
        ph = ",".join("?" * len(chunk))
        real = {r[0] for r in conn.execute(
            f"SELECT sym FROM nodes WHERE sym IN ({ph})", chunk)}
        out.extend(x for x in chunk if x in real)
    return out


def shortest_path(conn: sqlite3.Connection, a: str, b: str, max_depth: int = 6,
                   max_frontier_nodes: int = MAX_FRONTIER_NODES):
    """BFS that expands one layer at a time via indexed queries scoped to
    the current frontier, rather than loading every edge in the repo up
    front. Measured on a 44K-edge real-world graph (Python stdlib):
    symbol-to-symbol queries (the intended use - see SKILL.md) went from
    ~sub-ms to sub-ms either way but stayed fast; module-level queries
    (e.g. 'os.py' to 'json/__init__.py') dropped from ~100ms to ~75ms -
    a real but modest win, because a module node's fan-out via `defines`
    edges to every symbol it contains means the BFS still touches a large
    slice of the graph regardless of algorithm. Worse, a query between
    two such hub modules with NO real path can take longer (~170ms
    measured) because it explores outward for the full max_depth before
    giving up. So this also caps how many nodes one BFS layer may touch
    (MAX_FRONTIER_NODES) and fails fast with a clear reason instead of
    silently doing a lot of work - prefer function/class-level symbols
    over whole-module queries, which is what this tool is designed for."""
    a_id, a_cand = _resolve_sym(conn, a)
    b_id, b_cand = _resolve_sym(conn, b)
    if a_id is None or b_id is None:
        out = {"error": "unresolved", "a_candidates": a_cand, "b_candidates": b_cand}
        if not a_cand and not b_cand:
            out["index"] = _index_state(conn)
        return out
    if a_id == b_id:
        return {"path": [_node_info_by_sym(conn, a_id) or {"id": _id_of(conn, a_id)}]}

    parent: dict[str, str | None] = {a_id: None}
    frontier = [a_id]
    found = False
    for _ in range(max_depth):
        if not frontier:
            break
        if len(frontier) > max_frontier_nodes:
            return {
                "path": None,
                "reason": f"query touched over {max_frontier_nodes} nodes in one layer - "
                          f"likely a highly-connected hub (e.g. a whole module), and may be "
                          f"giving up on a real but deep path through it. Prefer function/class-level "
                          f"symbols, or pass a higher max_frontier_nodes if you need completeness over speed.",
            }
        next_frontier = []
        for known, candidate in _batch_frontier_neighbors(conn, frontier):
            if candidate not in parent:
                parent[candidate] = known
                next_frontier.append(candidate)
                if candidate == b_id:
                    found = True
        if found:
            break
        # Do not travel THROUGH a synthetic node. Every `.includes()` call
        # in a repo collapses to one `external:includes`, so expanding
        # through it links two files whose only commonality is calling a
        # built-in - reported as a confident multi-hop path between a Vue
        # composable and an unrelated scoring method. An external (or
        # ambiguous) target is a SINK: something left the repo there. It can
        # be a destination, never a conduit. Having a `nodes` row is exactly
        # the test for "this is a real symbol in this repo".
        frontier = _real_nodes_only(conn, next_frontier)

    if not found:
        # "No path" is a negative result staleness can explain: the code
        # that connects these two may have been written after the index.
        return {"path": None, "reason": f"no path within {max_depth} hops",
                "index": _index_state(conn)}

    path = [b_id]
    while path[-1] != a_id:
        path.append(parent[path[-1]])
    path.reverse()
    return {"path": [_node_info_by_sym(conn, n) or {"id": _id_of(conn, n)} for n in path]}


REACHABLE_EDGE_TYPES = ("calls", "calls_external", "imports")

def _reverse_frontier(conn: sqlite3.Connection, frontier: list[str]) -> set[str]:
    """Sources of calls/imports edges pointing INTO any node in `frontier`,
    via the dst index (idx_edges_dst), chunked to stay under SQLite's
    ~999-parameter limit. One or two indexed queries per BFS layer instead
    of materialising every call/import edge in the repo."""
    budget = _SQLITE_MAX_VARS - len(REACHABLE_EDGE_TYPES)
    types_ph = ",".join("?" * len(REACHABLE_EDGE_TYPES))
    out: set[str] = set()
    for i in range(0, len(frontier), budget):
        chunk = frontier[i:i + budget]
        ph = ",".join("?" * len(chunk))
        for (src,) in conn.execute(
            f"SELECT DISTINCT src FROM edges WHERE dst IN ({ph}) AND type IN ({types_ph})",
            (*chunk, *REACHABLE_EDGE_TYPES),
        ):
            out.add(src)
    return out


def _node_info_many(conn: sqlite3.Connection, syms: list) -> dict:
    """Batched node_info, keyed by SYMBOL. The per-result version cost one
    SELECT per impacted node - 1,703 round trips on a real hub query."""
    info: dict = {}
    for i in range(0, len(syms), _SQLITE_MAX_VARS):
        chunk = syms[i:i + _SQLITE_MAX_VARS]
        ph = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT n.sym, s.id, n.kind, n.name, n.qualname, n.file, n.lineno, n.end_lineno "
            f"FROM nodes n JOIN syms s ON s.sym = n.sym WHERE n.sym IN ({ph})", chunk
        ):
            info[row[0]] = dict(zip(_NODE_COLUMNS, row[1:]))
    return info


def _ids_of(conn: sqlite3.Connection, syms: list) -> dict:
    """Batched symbol -> id-string. Needed for results that have no `nodes`
    row at all (external: and ambiguous: targets)."""
    out: dict = {}
    for i in range(0, len(syms), _SQLITE_MAX_VARS):
        chunk = syms[i:i + _SQLITE_MAX_VARS]
        ph = ",".join("?" * len(chunk))
        for sym, ident in conn.execute(
            f"SELECT sym, id FROM syms WHERE sym IN ({ph})", chunk
        ):
            out[sym] = ident
    return out


# Default cap on impacted_by results. Measured motivation: on a real
# 417K-edge graph an uncapped query against a hub symbol returned 1,706
# results as full node dicts - 452 KB, roughly 116,000 tokens, in a single
# MCP call. This tool exists to cost LESS context than grep-and-read; it
# must not be able to cost more than a session has. Raise it explicitly
# when completeness matters more than size.
IMPACTED_BY_DEFAULT_LIMIT = 100


def impacted_by(conn: sqlite3.Connection, ref: str, max_depth: int = 3,
                 limit: int = IMPACTED_BY_DEFAULT_LIMIT):
    """Everything that transitively calls/imports/defines-down-to the target
    (reverse reachability) - i.e. what could break if this symbol changes.

    Layer-by-layer BFS over indexed, frontier-scoped queries, the same shape
    shortest_path already used. The previous version loaded EVERY
    call/import edge in the repo into a Python dict on every call, which on
    a real 417K-edge graph cost ~166ms regardless of how small the answer
    was, then spent one more SELECT per result resolving node info.

    Results are capped at `limit` and reported NEAREST FIRST (depth 1 before
    depth 2), because the closest dependents are the ones that actually
    break first. `total` is always the true count and `truncated` says
    whether you are seeing all of it.

    Each result carries only id, kind, lineno and depth. A node id is
    already `file::qualname`, so also sending `file`, `qualname` and `name`
    re-transmits the same string three more times - 56% of the payload on
    the measured hub query, for nothing the caller cannot derive."""
    node_sym, candidates = _resolve_sym(conn, ref)
    if node_sym is None:
        return _unresolved(conn, candidates)

    seen = {node_sym}
    order: list[tuple[int, int]] = []   # (symbol, depth), nearest first
    frontier = [node_sym]
    for depth in range(1, max_depth + 1):
        if not frontier:
            break
        # sorted() so a given graph always yields the same ordering - the
        # old set-iteration order varied between runs for no reason.
        next_frontier = [src for src in sorted(_reverse_frontier(conn, frontier))
                         if src not in seen]
        seen.update(next_frontier)
        order.extend((src, depth) for src in next_frontier)
        frontier = next_frontier

    total = len(order)
    shown = order[:limit] if limit is not None and limit >= 0 else order
    info = _node_info_many(conn, [sy for sy, _ in shown] + [node_sym])
    ids = _ids_of(conn, [sy for sy, _ in shown] + [node_sym])

    impacted = []
    for sy, depth in shown:
        # an "external:<name>" target has no row in `nodes` - it still
        # belongs in the answer, just with nothing to look up.
        node = info.get(sy)
        impacted.append({
            "id": ids.get(sy),
            "kind": node["kind"] if node else None,
            "lineno": node["lineno"] if node else None,
            "depth": depth,
        })

    out = {
        "target": info.get(node_sym),
        "impacted": impacted,
        "total": total,
        "limit": limit,
        "max_depth": max_depth,
        "truncated": len(impacted) < total,
    }
    if out["truncated"]:
        out["truncation_note"] = (
            f"showing the {len(impacted)} nearest of {total} impacted symbols "
            f"(depth 1..{impacted[-1]['depth'] if impacted else 0} of {max_depth}) - "
            f"raise `limit` (currently {limit}) for the full blast radius."
        )
    return out


def add_note(conn: sqlite3.Connection, ref: str | None, note: str, session_id: str, source: str = "observed"):
    node_id = None
    if ref:
        node_id, candidates = resolve_symbol(conn, ref)
        if node_id is None:
            # Do NOT silently fall back to a repo-wide note here - that
            # buries a typo'd or ambiguous symbol with no signal, and a
            # note nobody can find again is worse than no note. Caller
            # must either fix the name or explicitly pass '' / None for
            # a repo-wide note.
            out = {
                "error": "unresolved",
                "message": f"'{ref}' did not resolve to exactly one symbol - note NOT stored. "
                           f"Use the full node id, or pass an empty symbol for a repo-wide note.",
                "ambiguous_candidates": candidates,
            }
            if not candidates:
                out["index"] = _index_state(conn)
            return out
    conn.execute(
        "INSERT INTO session_notes (node_id, note, session_id, date, source) VALUES (?,?,?,?,?)",
        (node_id, note, session_id, gl.now_iso(), source),
    )
    conn.commit()
    return {"stored_against": node_id or "(repo-wide)", "note": note}


# Ranking for `search`, in two keys. Both exist because of a measured
# failure: on a real 59K-node graph `search("assessment")` matched 1,953
# nodes and returned 15 module nodes for route files, burying every
# function. `search` is the tool a session is told to reach for FIRST, so a
# bad 15 is expensive.
#
#   tier  - how well the text matched: an exact symbol name beats a prefix
#           beats a substring beats a qualname-only hit beats a node found
#           solely through its notes.
#   kind  - what the symbol IS: you are almost always looking for the
#           function or class, not the file that happens to contain the
#           word somewhere in its path. Modules rank last WITHIN a tier,
#           so searching for a module by its exact name still wins on tier.
_SEARCH_KIND_WEIGHT = {
    "function": 0, "method": 0,
    "class": 1,
    "template": 2,
    "closure": 3,
    "module": 4,
}

def _kind_weight_sql(column: str = "kind") -> str:
    cases = " ".join(f"WHEN '{k}' THEN {w}" for k, w in _SEARCH_KIND_WEIGHT.items())
    return f"CASE {column} {cases} ELSE 5 END"


def search(conn: sqlite3.Connection, text: str, limit: int = 15):
    """Joined search: finds nodes whose NAME matches, or whose NOTES match,
    then for every such node returns ALL of its notes (not just the ones
    that happened to match the search text) - so 'search resolve_symbol'
    surfaces every note ever filed against it, and 'search skipped' finds
    the node via a note even though the symbol name doesn't contain the
    word.

    Ranking and truncation happen in SQL, in that order, so the `limit`
    slices the BEST matches rather than whatever the table scan reached
    first. `total` and `truncated` are always reported."""
    like = f"%{text}%"
    lowered = text.lower()

    # Tiers are computed case-insensitively to match LIKE, which is already
    # ASCII-case-insensitive in SQLite - otherwise searching "Assessment"
    # would drop the symbol `assessment` out of the exact tier for no
    # reason the caller could see.
    code_rows = conn.execute(
        f"""
        SELECT s.id,
               CASE
                   WHEN lower(n.name) = ?               THEN 0
                   WHEN lower(n.name) LIKE ? || '%'     THEN 1
                   WHEN lower(n.name) LIKE '%' || ? || '%' THEN 2
                   ELSE 3
               END AS tier
        FROM nodes n JOIN syms s ON s.sym = n.sym
        WHERE n.name LIKE ? OR n.qualname LIKE ?
        ORDER BY tier, {_kind_weight_sql('n.kind')}, length(n.qualname), s.id
        LIMIT ?
        """,
        (lowered, lowered, lowered, like, like, limit),
    ).fetchall()
    code_total = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE name LIKE ? OR qualname LIKE ?", (like, like)
    ).fetchone()[0]

    matched_ids = [row[0] for row in code_rows]
    matched_set = set(matched_ids)

    # Nodes found ONLY through their note text rank after every code match:
    # the text didn't appear in the symbol at all.
    #
    # "Only" has to mean "does not match by name/qualname AT ALL", not
    # "wasn't in the rows we just returned". Excluding merely the returned
    # rows had two consequences: `total` counted a node that matched both
    # ways twice (so truncation_note promised matches that do not exist),
    # and a low-ranked CODE match could re-enter through this tail, jumping
    # ahead of better-ranked code matches cut by the same limit.
    note_only = [
        node_id for (node_id,) in conn.execute(
            f"""
            SELECT DISTINCT sy.id FROM session_notes s
            JOIN syms sy ON sy.id = s.node_id
            JOIN nodes n ON n.sym = sy.sym
            WHERE s.note LIKE ? AND NOT (n.name LIKE ? OR n.qualname LIKE ?)
            ORDER BY {_kind_weight_sql('n.kind')}, length(n.qualname), sy.id
            """,
            (like, like, like),
        )
        if node_id not in matched_set
    ]
    note_only_total = len(note_only)

    ordered = matched_ids + note_only[:max(0, limit - len(matched_ids))]

    results = []
    for node_id in ordered:
        info = node_info(conn, node_id)
        if not info:
            continue
        notes = [
            {"note": note, "session_id": session_id, "date": date, "source": source}
            for (note, session_id, date, source) in conn.execute(
                "SELECT note, session_id, date, source FROM session_notes "
                "WHERE node_id = ? ORDER BY date DESC",
                (node_id,),
            )
        ]
        results.append({"node": info, "notes": notes})

    unmatched_notes = [
        {"note": note, "session_id": session_id, "date": date, "source": source}
        for (note, session_id, date, source) in conn.execute(
            "SELECT note, session_id, date, source FROM session_notes "
            "WHERE node_id IS NULL AND note LIKE ? ORDER BY date DESC LIMIT ?",
            (like, limit),
        )
    ]

    total = code_total + note_only_total
    out = {
        "results": results,
        "repo_wide_notes": unmatched_notes,
        "total": total,
        "limit": limit,
        "truncated": len(results) < total,
    }
    if out["truncated"]:
        out["truncation_note"] = (
            f"showing the {len(results)} best-ranked of {total} matches "
            f"(exact name, then prefix, then substring; functions and classes "
            f"before modules) - raise `limit` or search a more specific term."
        )
    if not results:
        # `search` is the documented FIRST stop for "what is X / where does
        # X live", and an empty result for a symbol written since the last
        # index looked exactly like one that never existed. The entry-point
        # tool was the one missing the evidence - reported by a reviewer
        # testing the other tools' staleness blocks against a clean index.
        out["index"] = _index_state(conn)
    return out


# Weighting for Layer 3 chunks, applied as a MULTIPLIER on the FTS5 bm25
# score rather than as a sort key ahead of it. Relevance still leads: a
# genuinely on-point action can outrank a weakly-matching message, it just
# has to earn it.
#
# Why it is needed: `action` chunks (one-line summaries of Bash/Edit/Read
# tool calls) are 63% of this repo's own transcript index - 231 of 366 -
# and are mostly long absolute paths. Searching for an explanation kept
# returning the command that MENTIONED a thing above the sentence that
# EXPLAINED it. Observed live on "worker threads".
#
# bm25 scores are negative and sort ascending (more negative = better), so
# a multiplier below 1 pulls a row toward zero, i.e. demotes it.
# The 0.15 is measured, not guessed. Across 12 real queries against this
# repo's own 366-chunk transcript index, counting how often an `action`
# chunk took the top slot and how many of the top 3 it occupied:
#
#     rank only (the old behaviour)   7/12 action-first,  21/30 of top 3
#     action weight 0.4               3/12,                9/30
#     action weight 0.15              1/12,                5/30
#     kind as a hard primary sort     1/12,                4/30
#
# 0.15 lands on the same practical outcome as making kind an absolute
# primary sort, without the absoluteness: a genuinely on-point action can
# still beat a weakly-matching message. The one remaining action-first
# query ("blast radius") is correct - an action is the ONLY chunk that
# matches it, so there is nothing to rank above it.
_SESSION_KIND_WEIGHT = {
    "message": 1.0,   # what was actually said - the usual answer to "why"
    "summary": 1.0,   # compaction recaps: the densest "why" in a transcript
    "action": 0.15,   # tool calls: real, but mostly paths and commands
}
_SESSION_KIND_ORDER = {"message": 0, "summary": 0, "action": 1}


def _session_rank_sql(rank_col: str = "rank", kind_col: str = "tc.kind") -> str:
    cases = " ".join(f"WHEN '{k}' THEN {w}" for k, w in _SESSION_KIND_WEIGHT.items())
    return f"{rank_col} * (CASE {kind_col} {cases} ELSE 1.0 END)"


def _session_kind_order_sql(kind_col: str = "kind") -> str:
    cases = " ".join(f"WHEN '{k}' THEN {w}" for k, w in _SESSION_KIND_ORDER.items())
    return f"CASE {kind_col} {cases} ELSE 1 END"


def search_sessions(conn: sqlite3.Connection, text: str, limit: int = 10,
                     role: str | None = None, kind: str | None = None):
    """Full-text search over indexed Claude Code session transcripts
    (Layer 3 - see session_indexer.py; only populated if the repo was
    indexed with cg_index.py --sessions). Tries the FTS5 index first (a
    phrase match on `text`, quoted so arbitrary input can't be
    misinterpreted as FTS query syntax); falls back to a plain LIKE scan
    if FTS5 isn't available in this Python's sqlite3 build (see
    graph_lib.connect).

    `role` filters to 'user', 'assistant' or 'summary'; `kind` filters to
    'message', 'action' or 'summary'. Results are ranked by relevance
    weighted by chunk kind - see _SESSION_KIND_WEIGHT."""
    clauses, params = "", []
    if role:
        clauses += " AND tc.role = ?"
        params.append(role)
    if kind:
        clauses += " AND tc.kind = ?"
        params.append(kind)

    try:
        fts_query = '"' + text.replace('"', '""') + '"'
        rows = conn.execute(
            f"SELECT tc.session_id, tc.ts, tc.role, tc.kind, tc.text, tc.source_file, tc.line_no "
            f"FROM transcript_fts f JOIN transcript_chunks tc ON tc.id = f.rowid "
            f"WHERE transcript_fts MATCH ?{clauses} "
            f"ORDER BY {_session_rank_sql('f.rank')}, tc.id "
            f"LIMIT ?",
            (fts_query, *params, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        # No FTS5 in this sqlite3 build. The LIKE scan has no relevance
        # score at all, so kind becomes the primary key rather than a
        # multiplier - still better than the previous pure id ordering.
        like = f"%{text}%"
        like_clauses = clauses.replace("tc.", "")
        rows = conn.execute(
            f"SELECT session_id, ts, role, kind, text, source_file, line_no "
            f"FROM transcript_chunks WHERE text LIKE ?{like_clauses} "
            f"ORDER BY {_session_kind_order_sql()}, id DESC LIMIT ?",
            (like, *params, limit),
        ).fetchall()

    return {
        "results": [
            {"session_id": sid, "timestamp": ts, "role": r, "kind": k, "text": text_,
             "source_file": src, "line_no": line_no}
            for (sid, ts, r, k, text_, src, line_no) in rows
        ]
    }


def list_sessions(conn: sqlite3.Connection, limit: int = 20):
    """Indexed sessions with a message count and timestamp range each, most
    recently indexed first - lets a caller see what history is available
    before searching it, and confirms whether --sessions indexing has been
    run at all (an empty list means it hasn't, not that there's nothing to
    find)."""
    rows = conn.execute(
        "SELECT session_id, COUNT(*) AS n, MIN(ts), MAX(ts) FROM transcript_chunks "
        "GROUP BY session_id ORDER BY MAX(ts) DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return {
        "sessions": [
            {"session_id": sid, "chunk_count": n, "earliest": earliest, "latest": latest}
            for (sid, n, earliest, latest) in rows
        ]
    }
