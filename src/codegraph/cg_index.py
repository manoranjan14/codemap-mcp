#!/usr/bin/env python3
"""
Index (or incrementally re-index) a Python repo into the codegraph SQLite DB.

Usage:
    codemap-index <repo_root> [--db PATH] [--force]

Two-pass design (see graph_lib.py docstring for rationale):
  Pass 1 - for every changed/new file: parse with `ast`, replace that file's
           nodes + defines/imports edges, collect pending calls, pending
           class bases, and pending attribute/local-variable type
           inferences (see graph_lib.PendingBase / PendingAttrType).
  Pass 2 - build a repo-wide symbol index from the DB (all files, not just
           changed ones); resolve pending class bases into `inherits`
           edges and build each class's (single-chain, non-C3) MRO;
           resolve pending attr/var types against that same symbol index;
           then resolve pending calls into `calls` / `calls_external`
           edges - self/this-attribute and local-variable calls now
           resolve through the owning class's MRO when a type is known
           (see SKILL.md's "self/instance-method calls" entry), falling
           back to the legacy name-based resolution otherwise.

Unchanged files are left untouched (their old edges/nodes stay), and node
ids are deterministic (file::qualname) so edges from OTHER files pointing
at a changed file's symbols keep working without being touched.
"""
from __future__ import annotations

import argparse
import builtins
import os
import re
import sys
import time

from . import graph_lib as gl
from . import session_indexer
from . import go_parser
from . import ts_parser

BUILTIN_NAMES = set(dir(builtins))

TS_EXTS = (".ts", ".tsx", ".js", ".jsx")
GO_EXTS = go_parser.SOURCE_EXTS


def insert_edges(conn, rows) -> None:
    """Insert (src_id, dst_id, type, evidence) tuples, interning the two
    endpoints and splitting the evidence string. Pass 2 resolves entirely
    in text ids - that logic is subtle and was left untouched - so the
    conversion to integers happens here, once, at the write boundary."""
    if not rows:
        return
    syms = gl.intern(conn, [r[0] for r in rows] + [r[1] for r in rows])
    conn.executemany(
        "INSERT INTO edges (src, dst, type, lineno, note) VALUES (?,?,?,?,?)",
        [(syms[src], syms[dst], etype, *split_evidence(etype, ev))
         for src, dst, etype, ev in rows],
    )


def purge_file(conn, rel_file: str) -> None:
    """Remove a file's own nodes and the edges it is the SOURCE of.

    Edges are keyed on integers now, so "every edge whose source lives in
    this file" is a join rather than the old `src LIKE 'file::%'` scan. The
    `syms` rows themselves are intentionally left behind: an id must keep
    the same integer across runs, because another file's edges may already
    point at it."""
    conn.execute(
        "DELETE FROM edges WHERE src IN ("
        "  SELECT sym FROM syms WHERE id = ? OR id LIKE ?)",
        (rel_file, rel_file + "::%"),
    )
    conn.execute("DELETE FROM nodes WHERE file = ?", (rel_file,))


def split_evidence(etype: str, evidence):
    """Split a parser's evidence string into (lineno, note).

    Almost all of it is reconstructible at read time from the edge itself -
    "call at api/install-app.js:10" repeats a path already encoded in the
    source id - so only the line number is kept (see query_lib._edge_evidence).
    The exceptions carry something no amount of joining can recover: which
    candidates an ambiguous call had, and whether an import was written as
    `import`, `from ... import` or `require()`.
    """
    if not evidence:
        return (None, None)
    if etype in ("imports", "calls_ambiguous"):
        return (_trailing_lineno(evidence), evidence)
    return (_trailing_lineno(evidence), None)


def _trailing_lineno(evidence: str):
    m = re.search(r":(\d+)\s*$", evidence) or re.search(r"\(line (\d+)\)", evidence)
    return int(m.group(1)) if m else None


def needs_tree_sitter(rel_file: str) -> bool:
    """True for files only the tree-sitter-backed parser can handle. The
    Python path needs nothing but the stdlib, so when the optional
    tree-sitter deps are missing these files are skipped (with one
    warning) rather than aborting the whole run - see main()."""
    return (rel_file.endswith(ts_parser.VUE_EXT) or rel_file.endswith(TS_EXTS)
            or rel_file.endswith(GO_EXTS))


def parse_any(root: str, rel_file: str, ts_aliases: list, go_module: str | None = None):
    """Dispatch to the right parser by extension. All of them return the
    same graph_lib.ParseResult shape, so everything downstream (pass 2
    resolution, query layer, MCP server) is language-agnostic."""
    if rel_file.endswith(ts_parser.VUE_EXT):
        return ts_parser.parse_vue_file(root, rel_file, ts_aliases)
    if rel_file.endswith(TS_EXTS):
        return ts_parser.parse_file(root, rel_file, ts_aliases)
    if rel_file.endswith(GO_EXTS):
        return go_parser.parse_file(root, rel_file, go_module)
    return gl.parse_file(root, rel_file)


def load_symbol_maps(conn):
    """Lookup structures built once per run from the FULL node table (not
    just changed files), used by pass-2 call resolution:
      by_name: simple_name -> [node_id]  (functions/methods; legacy
               name-based fallback for calls with no usable import binding)
      by_module_symbol: (file, top_level_name) -> node_id  (functions AND
               classes, top-level only i.e. qualname == name, not nested -
               this is what import-binding resolution looks up, since an
               import can only bind to a module's top-level symbol)
      by_class_name: simple_name -> [node_id]  (classes only; used the same
               way by_name is for the bare-call "exactly one repo-wide
               candidate" guess, but for a base-class/type reference with no
               binding and no same-file match instead of a call)
    """
    by_name: dict[str, list[str]] = {}
    by_module_symbol: dict[tuple[str, str], str] = {}
    by_class_name: dict[str, list[str]] = {}
    # (package_directory, top_level_name) -> node_id. Python and TS imports
    # bind to a FILE, so (file, name) is enough for them. A Go import binds
    # to a DIRECTORY and the symbol may live in any .go file inside it, so
    # `scoring.Compute()` is unresolvable without this.
    by_package_symbol: dict[tuple[str, str], str] = {}
    for node_id, name, qualname, kind, file in conn.execute(
        "SELECT s.id, n.name, n.qualname, n.kind, n.file FROM nodes n "
        "JOIN syms s ON s.sym = n.sym WHERE n.kind IN ('function','method','class')"
    ):
        if kind in ("function", "method"):
            by_name.setdefault(name, []).append(node_id)
        if kind == "class":
            by_class_name.setdefault(name, []).append(node_id)
        if qualname == name:  # top-level: not nested in a class/function
            by_module_symbol[(file, name)] = node_id
            pkg = os.path.dirname(file) or "."
            by_package_symbol.setdefault((pkg, name), node_id)
    return by_name, by_module_symbol, by_class_name, by_package_symbol


def _resolve_class_ref(base_name, ref_name: str, file: str, bindings: dict, on_disk: set,
                        by_module_symbol: dict, by_class_name: dict):
    """Resolve a (base_name, ref_name) class/type reference - from a base
    class (`class Derived(Base)` / `extends Base`), a constructor-assigned
    attribute, or a TS field/parameter type annotation - to a class node id,
    or None if it can't be resolved locally (a genuinely external base like
    `ast.NodeVisitor` or a third-party DI type, or a reference this tool's
    deliberately narrow scope doesn't cover). Mirrors the priority order of
    call resolution (import binding first, then same-file, then a
    same-named repo-wide guess ONLY for a truly bare name), for the same
    reason: a binding or a same-file match is real evidence, a bare
    cross-file name match is a bet, and a `base_name` that isn't a real
    binding at all (a dotted reference through an unknown/complex
    expression) gets neither - same-file only, never the repo-wide guess,
    to avoid the exact cross-file false-match risk documented for calls."""
    binding = None
    use_symbol = False
    if base_name and base_name in bindings:
        binding, use_symbol = bindings[base_name], False   # mod.Base
    elif base_name is None and ref_name in bindings:
        binding, use_symbol = bindings[ref_name], True      # Base after `from x import Base`

    if binding:
        node_id, confident_external = _resolve_via_binding(binding, ref_name, use_symbol, on_disk, by_module_symbol)
        if node_id:
            return node_id
        if confident_external:
            # Definitively a third-party/external type (its binding's
            # candidate module(s) don't exist locally) - don't fall
            # through to a same-file/repo-wide guess for the exact same
            # cross-file false-match reason call resolution treats this
            # as terminal (see _resolve_via_binding's docstring).
            return None
        # binding exists but pointed at a local module where the symbol
        # wasn't found (re-export or v1 gap) - fall through exactly like
        # call resolution does for the same inconclusive case.

    same_file = by_module_symbol.get((file, ref_name))
    if same_file:
        return same_file
    if base_name is None:
        # Only for a truly bare name with NO binding at all (an import
        # binding, resolved or not, is real evidence and is handled above;
        # this is the last-resort guess for a name with no evidence
        # whatsoever) - and even then, ONLY when it's unique repo-wide,
        # same bet-vs-guarantee reasoning as the bare-call fallback.
        candidates = by_class_name.get(ref_name, [])
        if len(candidates) == 1:
            return candidates[0]
    return None


def _compute_mro(class_id: str, bases_map: dict, cache: dict, _visiting: set | None = None) -> list:
    """Deliberately simple linearization - NOT Python's real C3 MRO: self
    first, then each LOCALLY RESOLVED base's own MRO in declaration order,
    depth-first, deduplicated (first occurrence wins). Good enough for
    "does this method exist somewhere in the inheritance chain", which is
    all self/this-attribute resolution needs; a base that didn't resolve
    locally (external/unknown) is simply absent from this list - see
    _resolve_class_ref - so it contributes no methods, which is the correct
    conservative behavior (we genuinely don't know what it defines)."""
    if class_id in cache:
        return cache[class_id]
    visiting = _visiting if _visiting is not None else set()
    if class_id in visiting:
        return [class_id]  # inheritance cycle (shouldn't happen, stay safe)
    visiting.add(class_id)
    mro = [class_id]
    for base_id in bases_map.get(class_id, []):
        for c in _compute_mro(base_id, bases_map, cache, visiting):
            if c not in mro:
                mro.append(c)
    visiting.discard(class_id)
    cache[class_id] = mro
    return mro


def _lookup_in_mro(class_id: str, name: str, class_methods: dict, bases_map: dict, mro_cache: dict):
    for cid in _compute_mro(class_id, bases_map, mro_cache):
        m = class_methods.get(cid, {}).get(name)
        if m:
            return m
    return None


def load_class_hierarchy(conn):
    """Built once per run from the FULL edges/nodes tables (same
    span-the-whole-repo rationale as load_symbol_maps - a class's base may
    live in a file that didn't change this run):
      bases_map: class_id -> [resolved base class node_id, ...], from
                 persisted `inherits` edges - excludes any that resolved to
                 an `external:` sentinel, since an unresolved/external base
                 contributes no known methods to the MRO.
      class_methods: class_id -> {method_name: method_node_id} - only
                 methods/functions whose DIRECT `defines` parent is a
                 class-kind node (excludes nested functions and closures,
                 whose parent is a function/closure, not a class).
      method_owner: method_node_id -> owning class_id (inverse of
                 class_methods, one entry per method - this is how pass 2
                 finds "which class is `self`/`this` here" for a given
                 pending call's caller_id).
    """
    bases_map: dict[str, list[str]] = {}
    for src, dst in conn.execute(
        "SELECT a.id, b.id FROM edges e JOIN syms a ON a.sym = e.src "
        "JOIN syms b ON b.sym = e.dst WHERE e.type = 'inherits'"
    ):
        if not dst.startswith("external:"):
            bases_map.setdefault(src, []).append(dst)

    class_methods: dict[str, dict[str, str]] = {}
    method_owner: dict[str, str] = {}
    rows = conn.execute("""
        SELECT ps.id, p.kind, ns.id, n.name FROM edges e
        JOIN nodes n ON n.sym = e.dst
        JOIN nodes p ON p.sym = e.src
        JOIN syms ns ON ns.sym = n.sym
        JOIN syms ps ON ps.sym = p.sym
        WHERE e.type = 'defines' AND n.kind IN ('method', 'function')
    """).fetchall()
    for parent_id, parent_kind, method_id, name in rows:
        if parent_kind == "class":
            class_methods.setdefault(parent_id, {})[name] = method_id
            method_owner[method_id] = parent_id
    return bases_map, class_methods, method_owner


def _resolve_via_binding(binding, called_name: str, use_symbol: bool, on_disk: set,
                         by_module_symbol: dict, by_package_symbol: dict | None = None):
    """Try resolving through an import binding. Returns (node_id_or_None,
    is_confidently_external: bool). `use_symbol` picks which half of the
    binding applies: True for a bare call on an imported symbol (foo()
    after `from x import foo`), False for a call on a module alias
    (mod.foo() after `import x as mod`)."""
    candidates = binding.symbol_candidates if use_symbol else binding.module_candidates
    lookup_name = binding.symbol_name if use_symbol else called_name
    if not candidates or not lookup_name:
        return None, False
    # A Go import names a package DIRECTORY, which is never a member of
    # on_disk (that holds files). Treat a directory that defines symbols as
    # just as real as a file, or every cross-package Go call would be
    # written off as confidently external.
    packages = {pkg for pkg, _ in (by_package_symbol or {})}
    on_disk_candidates = [c for c in candidates if c in on_disk or c in packages]
    if not on_disk_candidates:
        # None of this binding's possible source files exist in the repo -
        # it's confidently an external/third-party import, not just an
        # unresolved name. Knowing this lets pass 2 skip the ambiguous
        # name-based fallback entirely, which matters: a common name like
        # `get` imported from an external library would otherwise risk
        # matching an unrelated same-named local function by coincidence.
        return None, True
    for candidate in on_disk_candidates:
        node_id = by_module_symbol.get((candidate, lookup_name))
        if node_id:
            return node_id, False
        if by_package_symbol:
            node_id = by_package_symbol.get((candidate, lookup_name))
            if node_id:
                return node_id, False
    return None, False  # source module IS local, but symbol not found there (re-export or v1 gap) - fall back


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo_root")
    ap.add_argument("--db", default=None,
                     help="Path to graph DB (default: ~/.codegraph/repos/<repo-key>/graph.db - "
                          "outside the repo entirely, see graph_lib.default_db_path)")
    ap.add_argument("--force", action="store_true", help="Re-index every file regardless of hash")
    ap.add_argument("--sessions", action="store_true",
                     help="Also index this repo's Claude Code session transcripts (Layer 3) so past "
                          "sessions are searchable via search_sessions. Opt-in: this makes whatever's "
                          "been discussed in those sessions - file contents, commands, business "
                          "context - searchable through the MCP server. Off by default.")
    args = ap.parse_args()

    repo_root = os.path.abspath(args.repo_root)
    db_path = args.db or gl.default_db_path(repo_root)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = gl.connect(db_path)

    ts_aliases = ts_parser.load_tsconfig_aliases(repo_root)
    go_module = go_parser.module_path(repo_root)
    on_disk = set(gl.iter_py_files(repo_root))
    known = {row[0] for row in conn.execute("SELECT file FROM file_hashes")}

    # A file that ONLY ever failed to parse has no file_hashes row, so it is
    # not in `known` and never reaches the `deleted` loop below - without
    # this sweep its failure record outlives the file itself, permanently.
    for (stale,) in conn.execute("SELECT file FROM failed_files").fetchall():
        if stale not in on_disk:
            conn.execute("DELETE FROM failed_files WHERE file = ?", (stale,))

    # deleted files: purge
    deleted = known - on_disk
    dangling_downgraded = 0
    for f in deleted:
        # Before removing this file's nodes, find any 'calls' edge from
        # ANOTHER file that points at one of them - without this, that
        # edge would keep referencing a node id that no longer exists in
        # `nodes` (a dangling reference: target_info degrades to null on
        # query rather than crashing, but the edge itself silently lied
        # about a real, resolvable call target that no longer exists).
        # Downgrade it to calls_external instead, using the node's own
        # name - the same "external:<name>" shape every other unresolved
        # call already uses, so a caller of this now-gone symbol shows up
        # exactly like any other call whose target isn't known, rather
        # than a phantom resolved edge. A full re-index of the calling
        # file (its own next change) will naturally re-resolve this the
        # normal way; --force does it for everything immediately.
        for sym, name in conn.execute("SELECT sym, name FROM nodes WHERE file = ?", (f,)).fetchall():
            ext = gl.intern(conn, [f"external:{name}"])[f"external:{name}"]
            cur = conn.execute(
                "UPDATE edges SET dst = ?, type = 'calls_external' WHERE dst = ? AND type = 'calls'",
                (ext, sym),
            )
            dangling_downgraded += cur.rowcount
            # Imports now target the MODULE node when it is in this repo, so
            # deleting a file leaves every importer's edge pointing at a node
            # that no longer exists. Same treatment as a call: keep the edge
            # (someone really does import this) but stop claiming a resolved
            # target. Caught by the dangling-edge check in the test suite, not
            # by inspection.
            cur = conn.execute(
                "UPDATE edges SET dst = ? WHERE dst = ? AND type = 'imports'", (ext, sym))
            dangling_downgraded += cur.rowcount
        purge_file(conn, f)
        conn.execute("DELETE FROM file_hashes WHERE file = ?", (f,))
        conn.execute("DELETE FROM failed_files WHERE file = ?", (f,))

    # tree-sitter is optional. Without it, TS/JS/Vue files are skipped and
    # the Python half of the repo still indexes - they stay in `on_disk` so
    # they are NOT treated as deleted (a DB built on a machine that HAS
    # tree-sitter keeps its TS nodes/edges intact instead of being purged
    # by a run from a machine that doesn't).
    ts_skipped = []
    if not ts_parser.available():
        ts_skipped = sorted(f for f in on_disk if needs_tree_sitter(f))
        if ts_skipped:
            print(f"  ! tree-sitter not available ({ts_parser.unavailable_reason()}) - "
                  f"skipping {len(ts_skipped)} TypeScript/JavaScript/Vue file(s); "
                  f"indexing Python only. Install with: "
                  f"python3 -m pip install -r requirements.txt", file=sys.stderr)
    ts_skipped_set = set(ts_skipped)

    # Files that previously failed to parse, keyed on content hash. Re-trying
    # them every run is pure waste: the same bytes fail the same way. A
    # changed hash (the file was repaired) falls through to a normal parse,
    # and --force clears the record so a better parser gets another go.
    if args.force:
        conn.execute("DELETE FROM failed_files")
    known_failures = dict(conn.execute("SELECT file, hash FROM failed_files"))

    changed = []
    skipped_known_bad = 0
    for f in sorted(on_disk):
        if f in ts_skipped_set:
            continue
        h = gl.file_hash(os.path.join(repo_root, f))
        if known_failures.get(f) == h:
            skipped_known_bad += 1
            continue
        row = conn.execute("SELECT hash FROM file_hashes WHERE file = ?", (f,)).fetchone()
        if args.force or row is None or row[0] != h:
            changed.append((f, h))

    pending_imports_by_file: dict[str, list] = {}
    pending_calls_by_file: dict[str, list[gl.PendingCall]] = {}
    import_bindings_by_file: dict[str, dict] = {}
    class_bases_by_file: dict[str, list] = {}
    attr_types_by_file: dict[str, list] = {}

    skipped_files = []
    failures = []   # (file, hash, error) for files the parser rejected
    for f, h in changed:
        # SAVEPOINT isolates this file's writes: on any failure we roll back
        # only this file's partial inserts, not the whole run's progress so
        # far (a plain conn.rollback() would undo every file since the last
        # commit(), which only happens once at the end of the batch).
        conn.execute("SAVEPOINT file_save")
        try:
            purge_file(conn, f)
            result = parse_any(repo_root, f, ts_aliases, go_module)
            syms = gl.intern(
                conn,
                [n["id"] for n in result.nodes]
                + [e["src"] for e in result.edges] + [e["dst"] for e in result.edges],
            )
            conn.executemany(
                "INSERT OR REPLACE INTO nodes (sym, kind, name, qualname, file, lineno, end_lineno) VALUES (?,?,?,?,?,?,?)",
                [(syms[n["id"]], n["kind"], n["name"], n["qualname"], n["file"],
                  n["lineno"], n["end_lineno"]) for n in result.nodes],
            )
            conn.executemany(
                "INSERT INTO edges (src, dst, type, lineno, note) VALUES (?,?,?,?,?)",
                [(syms[e["src"]], syms[e["dst"]], e["type"],
                  *split_evidence(e["type"], e["evidence"])) for e in result.edges],
            )
            pending_calls_by_file[f] = result.pending_calls
            pending_imports_by_file[f] = result.pending_imports
            import_bindings_by_file[f] = result.import_bindings
            class_bases_by_file[f] = result.class_bases
            attr_types_by_file[f] = result.attr_types
            conn.execute(
                "INSERT INTO file_hashes (file, hash, indexed_at) VALUES (?,?,?) "
                "ON CONFLICT(file) DO UPDATE SET hash=excluded.hash, indexed_at=excluded.indexed_at",
                (f, h, gl.now_iso()),
            )
        except SyntaxError as e:
            conn.execute("ROLLBACK TO file_save")
            print(f"  ! skipping {f}: syntax error ({e})", file=sys.stderr)
            skipped_files.append(f)
            failures.append((f, h, f"syntax error: {e}"))
        except Exception as e:
            # Any other per-file failure (e.g. a same-scope redefinition
            # colliding on id despite INSERT OR REPLACE, unreadable file,
            # unexpected AST shape) must not abort indexing of the rest of
            # the repo - one bad file is common in a large real codebase.
            conn.execute("ROLLBACK TO file_save")
            print(f"  ! skipping {f}: {type(e).__name__}: {e}", file=sys.stderr)
            skipped_files.append(f)
            failures.append((f, h, f"{type(e).__name__}: {e}"))
        finally:
            conn.execute("RELEASE file_save")

    # A file that parsed cleanly this time must not stay on the bad list.
    reparsed_ok = [f for f, _ in changed if f not in set(skipped_files)]
    for i in range(0, len(reparsed_ok), 400):
        batch = reparsed_ok[i:i + 400]
        conn.execute(
            f"DELETE FROM failed_files WHERE file IN ({','.join('?' * len(batch))})", batch
        )
    if failures:
        conn.executemany(
            "INSERT INTO failed_files (file, hash, error, failed_at) VALUES (?,?,?,?) "
            "ON CONFLICT(file) DO UPDATE SET hash=excluded.hash, error=excluded.error, "
            "failed_at=excluded.failed_at",
            [(f, h, err, gl.now_iso()) for f, h, err in failures],
        )
    conn.commit()

    # Pass 2: resolve pending calls against the FULL repo-wide symbol index.
    # Collected into one list and inserted with a single executemany() -
    # profiling on the stdlib corpus (635 files, ~25K resolved+external
    # calls) showed thousands of individual conn.execute() calls here were
    # the single largest cost in the whole indexing run (~2.9s of ~7s
    # total), dwarfing the actual AST parsing work.
    by_name, by_module_symbol, by_class_name, by_package_symbol = load_symbol_maps(conn)

    # Resolve this run's class bases (`class Derived(Base)` / `extends
    # Base`) into `inherits` edges FIRST, before touching pending calls -
    # self/this resolution below needs the full class hierarchy already in
    # place. Always records an edge either way (to a resolved node, or to
    # an `external:<name>` sentinel exactly like an unresolved call), same
    # "immediately correct, not eventually correct" convention as every
    # other edge type here - even though this run only RE-derives bases for
    # changed files, an unchanged file's classes keep whatever `inherits`
    # edges a previous run already gave them (same reasoning as calls).
    inherits_edges = []
    for f, bases in class_bases_by_file.items():
        bindings = import_bindings_by_file.get(f, {})
        for pb in bases:
            node_id = _resolve_class_ref(pb.base_name, pb.ref_name, f, bindings, on_disk, by_module_symbol, by_class_name)
            dst = node_id if node_id else f"external:{pb.ref_name}"
            inherits_edges.append((pb.class_id, dst, "inherits", f"extends {pb.ref_name} (line {pb.lineno})"))
    insert_edges(conn, inherits_edges)

    # Imports: point the edge at the MODULE when the specifier names a file
    # in this repo, so "what imports this file" is answerable and
    # impacted_by reaches a module's importers. Previously every imports
    # edge was `external:<specifier>` even for in-repo targets - measured on
    # a real repo: 2,543 imports edges, none resolved, and a util file that
    # 32 files import reported zero importers.
    import_edges = []
    imports_resolved = 0
    known_packages = {pkg for pkg, _ in by_package_symbol}
    for f, pending in pending_imports_by_file.items():
        for pi in pending:
            # A candidate is resolvable if it is a file (Python, TS) OR a
            # package directory (Go). Go's unit of importability is the
            # directory, so the edge targets the package node.
            target = next((c for c in pi.candidates
                           if c in on_disk or c in known_packages), None)
            if target:
                imports_resolved += 1
            import_edges.append((f, target or pi.external, "imports", pi.evidence))
    insert_edges(conn, import_edges)

    bases_map, class_methods, method_owner = load_class_hierarchy(conn)
    mro_cache: dict[str, list] = {}

    # Resolve this run's attr-type inferences (self.attr = Class(...) / TS
    # field & constructor-parameter-property type annotations / local var
    # instantiation) into a plain lookup, (scope_id, var_name) -> class
    # node id. Unlike inherits edges, this does NOT need cross-run
    # persistence: self.attr/this.attr and a local var's assignment always
    # live in the SAME FILE as any call site that uses them (a class's
    # methods, and a function's own locals, can't span files in
    # Python/TS/JS), so as long as the FILE containing both the assignment
    # and the call was reparsed this run - which it was, since pending
    # calls only exist for changed files - this run's fresh
    # attr_types_by_file[f] is already complete for every pending call in
    # that same file. Last write in source order wins on a collision
    # (multiple assignments to the same self.attr, e.g. a field default AND
    # a constructor re-assignment) - deliberately simple, no control-flow
    # awareness, per PendingAttrType's documented scope.
    attr_type_map: dict[tuple[str, str], str] = {}
    for f, entries in attr_types_by_file.items():
        bindings = import_bindings_by_file.get(f, {})
        for at in entries:
            node_id = _resolve_class_ref(at.base_name, at.ref_name, f, bindings, on_disk, by_module_symbol, by_class_name)
            if node_id:
                attr_type_map[(at.scope_id, at.var_name)] = node_id

    resolved, resolved_via_import, resolved_via_type, ambiguous, external, external_via_import = 0, 0, 0, 0, 0, 0
    call_edges = []
    for f, calls in pending_calls_by_file.items():
        bindings = import_bindings_by_file.get(f, {})
        for pc in calls:
            dst = etype = None

            # Import-aware resolution first: a binding tells us definitively
            # what a name refers to (or definitively that it's external),
            # which is both more accurate AND cheaper than the name-search
            # fallback below when it applies.
            binding = None
            use_symbol = False
            if pc.base_name and pc.base_name in bindings:
                binding, use_symbol = bindings[pc.base_name], False   # mod.func()
            elif pc.base_name is None and pc.called_name in bindings:
                binding, use_symbol = bindings[pc.called_name], True  # func() after `from x import func`

            if binding:
                node_id, confident_external = _resolve_via_binding(
                    binding, pc.called_name, use_symbol, on_disk, by_module_symbol,
                    by_package_symbol,
                )
                if node_id:
                    dst, etype = node_id, "calls"
                    resolved += 1
                    resolved_via_import += 1
                elif confident_external:
                    dst, etype = f"external:{pc.called_name}", "calls_external"
                    external += 1
                    external_via_import += 1

            if dst is None:
                # self/this-aware resolution: authoritative whenever we
                # actually know the owning class or an inferred attribute/
                # local-var type, so it takes priority over (and, when it
                # applies, fully REPLACES rather than falls through to) the
                # legacy same-file-simple-name guess below - deliberately,
                # because that guess is scoped to "same simple name
                # anywhere in the file", not "same simple name on the
                # actually-relevant class", and a file with two classes
                # that each define a same-named method is exactly the case
                # where the old heuristic could silently resolve to the
                # WRONG one. When we have NO class/type information at all
                # (e.g. `self` used outside any real method), this leaves
                # dst unset and falls through to the legacy path exactly as
                # before - purely additive, never a regression in recall.
                owner_class = method_owner.get(pc.caller_id)
                target_class = None
                if pc.ctor_type is not None:
                    # `Scorer().method()` / `new Scorer().method()`: the
                    # receiver's type is written in the expression, so no
                    # inference is needed - only a lookup of which class
                    # that name refers to HERE (import binding first, then
                    # same-file, then a unique repo-wide class). Resolving
                    # the class rather than trusting the bare name is what
                    # stops `helper().run()`, where helper is a function,
                    # from being mistaken for a construction.
                    target_class = _resolve_class_ref(
                        None, pc.ctor_type, pc.file, bindings, on_disk,
                        by_module_symbol, by_class_name)
                if target_class is None and pc.attr_base is not None:
                    if owner_class is not None:
                        target_class = attr_type_map.get((owner_class, pc.attr_base))
                elif target_class is None and pc.base_name in ("self", "this"):
                    target_class = owner_class
                elif target_class is None and pc.base_name is not None:
                    target_class = attr_type_map.get((pc.caller_id, pc.base_name))

                if target_class is not None:
                    m = _lookup_in_mro(target_class, pc.called_name, class_methods, bases_map, mro_cache)
                    if m:
                        dst, etype = m, "calls"
                        resolved += 1
                        resolved_via_type += 1
                    else:
                        dst, etype = f"external:{pc.called_name}", "calls_external"
                        external += 1

            if dst is None:
                # Legacy name-based fallback: used when there's no import
                # binding for this call, or the binding pointed at a local
                # module but the symbol wasn't found there.
                candidates = by_name.get(pc.called_name, [])
                local = [c for c in candidates if c.startswith(pc.file + "::")]
                if len(local) == 1:
                    # Same-file match wins even over a builtin name: Python
                    # scoping means a module that defines its own `print`
                    # really does shadow the builtin for calls inside that
                    # SAME module. Correct as-is.
                    dst, etype = local[0], "calls"
                    resolved += 1
                elif pc.file.endswith(".py") and pc.base_name is None and pc.called_name in BUILTIN_NAMES:
                    # Must be checked BEFORE the cross-file "unique
                    # candidate" branch below. Bug found in testing: Python
                    # builtin shadowing is per-module, not per-repo - some
                    # unrelated file elsewhere defining its own `print` or
                    # `open` must not hijack every OTHER file's genuine
                    # call to the real builtin just because it happens to
                    # be the only same-named function in the whole repo.
                    # The `.py` gate is deliberate and was itself a fix:
                    # BUILTIN_NAMES is Python's `dir(builtins)`, and
                    # without gating by source language a TS/JS/Vue
                    # function named e.g. `sum`, `map`, `list`, `filter`,
                    # or `type` (all real Python builtins, all plausible
                    # JS/TS names) would be silently dropped here as if it
                    # were a Python builtin call, with no edge created at
                    # all - found by inspection while adding the Vue
                    # template flag below, not by a real-world repro.
                    continue  # skip noise from print(), len(), etc.
                elif pc.base_name is not None:
                    # Attribute/method call (obj.method()) with no same-file
                    # match and no import binding: we have NO type
                    # information about `obj`, so a same-named function
                    # existing elsewhere in the repo is not reliable
                    # evidence of what this call actually reaches - a real
                    # bug found testing on a large TS codebase: an Express
                    # handler's `res.json(...)` matched an unrelated test
                    # helper's coincidentally same-named `mockRes.json`,
                    # because .json()/.then()/.map()/.on()-style library and
                    # DOM methods have no enumerable "builtin list" the way
                    # Python does, so nothing was catching that collision.
                    # Treat as external/unknown rather than guess, whether
                    # there's one repo-wide candidate or several.
                    dst, etype = f"external:{pc.called_name}", "calls_external"
                    external += 1
                elif pc.no_cross_file_guess:
                    # Set by ts_parser.py for a call extracted from a Vue
                    # <template> block. Real bug found testing on
                    # webapp: a template-sourced bare call/handler
                    # reference (base_name=None, e.g. `@click="close"`) is
                    # semantically scoped to the component instance, not
                    # the whole repo - but nothing here can tell it apart
                    # from a genuinely global bare call, so without this
                    # flag it fell into the SAME "exactly one repo-wide
                    # candidate" guess as a real bare call. Vue component
                    # method names (`close`, `cancel`, `onSubmit`, ...)
                    # repeat across hundreds of components in a way
                    # ordinary global function names mostly don't, so that
                    # guess is a much worse bet here specifically: measured
                    # impact, ambiguous calls jumped from 2,106 to 8,322 on
                    # webapp before this check existed. Same treatment
                    # as an attribute call: same-file match and import
                    # binding are still tried first (both above), only the
                    # cross-file guess is skipped.
                    dst, etype = f"external:{pc.called_name}", "calls_external"
                    external += 1
                elif len(candidates) == 1:
                    dst, etype = candidates[0], "calls"
                    resolved += 1
                elif len(candidates) > 1:
                    # Ambiguous: the name matches several definitions and
                    # nothing binds it. Do NOT pick one - measured on a real
                    # Next.js repo, 7,196 of 9,008 ambiguous calls were to
                    # `t`, bound 546 times as a LOCAL variable from
                    # `const t = await getTranslations()`; the repo's six
                    # functions named `t` are unrelated scripts and tests,
                    # so guessing would have invented thousands of false
                    # edges.
                    #
                    # But record it. Dropping it outright meant "what does
                    # this call" silently omitted the call entirely. The
                    # target is synthetic and is never an edge SOURCE, so it
                    # cannot bridge two same-named symbols into each other's
                    # blast radius - query_lib's reachability types exclude
                    # it for the same reason.
                    ambiguous += 1
                    shown = ", ".join(candidates[:3])
                    if len(candidates) > 3:
                        shown += f", +{len(candidates) - 3} more"
                    call_edges.append((
                        pc.caller_id, f"ambiguous:{pc.called_name}", "calls_ambiguous",
                        f"ambiguous call at {pc.file}:{pc.lineno} - "
                        f"{len(candidates)} candidates: {shown}",
                    ))
                    continue
                else:
                    dst, etype = f"external:{pc.called_name}", "calls_external"
                    external += 1

            call_edges.append((pc.caller_id, dst, etype, f"call at {pc.file}:{pc.lineno}"))
    insert_edges(conn, call_edges)
    conn.commit()

    # The query layer needs these to tell "this symbol does not exist" from
    # "your index predates it" - see query_lib._index_state.
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [("repo_root", repo_root), ("indexed_at", gl.now_iso())],
    )
    conn.commit()

    n_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    n_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    print(f"Indexed {repo_root}")
    print(f"  files: {len(on_disk)} total, {len(changed)} (re)parsed, {len(deleted)} removed"
          f"{f', {dangling_downgraded} dangling call edge(s) downgraded to external' if dangling_downgraded else ''}"
          f", {len(skipped_files)} skipped (errors)"
          f"{f', {len(ts_skipped)} skipped (no tree-sitter)' if ts_skipped else ''}"
          f"{f', {skipped_known_bad} skipped (unchanged since a previous parse failure)' if skipped_known_bad else ''}")
    print(f"  calls: {resolved} resolved ({resolved_via_import} via import binding, "
          f"{resolved_via_type} via self/type inference), "
          f"{ambiguous} ambiguous (skipped), {external} external ({external_via_import} confident via import binding)")
    print(f"  imports: {imports_resolved} resolved to an in-repo module, "
          f"{len(import_edges) - imports_resolved} external")
    print(f"  graph: {n_nodes} nodes, {n_edges} edges")

    if args.sessions:
        s = session_indexer.index_sessions(conn, repo_root, force=args.force)
        print(f"  sessions: {s['session_files_found']} transcript file(s) found, "
              f"{s['session_files_reindexed']} (re)scanned, {s['session_files_unchanged']} unchanged, "
              f"{s['chunks_added']} new chunk(s) indexed")
        if s["session_files_found"] == 0:
            print(f"    (no session transcripts found under "
                  f"{session_indexer.claude_projects_dir()}/{session_indexer.project_dir_name(repo_root)} - "
                  f"normal if you haven't run Claude Code in this repo yet)")

    print(f"  db:    {db_path}")


if __name__ == "__main__":
    main()
