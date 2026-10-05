"""
Core library for codegraph: SQLite schema, Python AST parsing into a
node/edge graph, and session-memory notes.

Design (deliberately "lite" v1, per the plan):
  - Single language (Python) via the stdlib `ast` module — no tree-sitter
    dependency, no network install required.
  - Two-pass indexing: pass 1 parses every file into nodes + raw edges
    (defines, imports) and collects "pending calls" (call-site -> callee
    name, not yet resolved); pass 2 resolves pending calls against a
    global symbol index built from ALL indexed files, so a call to a
    function defined in another file still resolves.
  - Known limitation: call resolution is name-based (qualname, then
    unique simple name) plus a lightweight, deliberately narrow type
    inference for self/this and constructor-assigned attributes/locals
    (see PendingBase/PendingAttrType below and SKILL.md) - not full
    scope/type resolution. An ambiguous or dynamic call
    ("getattr(obj, name)()") is not resolved and is skipped. This is
    stated up front rather than silently guessed.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

IGNORE_DIRS = {
    ".git", "__pycache__", ".codegraph", "venv", ".venv", "env",
    "node_modules", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".tox", ".ruff_cache",
}

SCHEMA = """
-- Every distinct node id, stored ONCE. Ids are long path strings
-- ("src/view/pages/general/Dash.vue::useNav"); keeping them inline in every
-- edge row and again in both edge indexes was the single largest cost in
-- the database - measured on a 4,113-file repo: 193 MB total, of which the
-- edges table was 90 MB and its two indexes 60 MB. Interning to integers
-- took the same graph to 50 MB. A target id that is not a defined symbol
-- (an "external:..." or "ambiguous:..." call target) lives here too, with
-- no corresponding `nodes` row.
CREATE TABLE IF NOT EXISTS syms (
    sym INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS nodes (
    sym INTEGER PRIMARY KEY,   -- -> syms.sym
    kind TEXT NOT NULL,        -- module | class | function | method | template | closure
    name TEXT NOT NULL,
    qualname TEXT NOT NULL,
    file TEXT NOT NULL,
    lineno INTEGER,
    end_lineno INTEGER
);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_qualname ON nodes(qualname);
CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file);

-- `lineno` replaces the old free-text `evidence` column, which held 30 MB
-- of sentences like "call at api/install-app.js:10" whose path is already
-- encoded in the source id. query_lib rebuilds the sentence on read (see
-- _edge_evidence). `note` carries only what genuinely cannot be derived:
-- the candidate list of an ambiguous call, and whether an import was
-- `import`, `from ... import` or `require()`.
CREATE TABLE IF NOT EXISTS edges (
    src INTEGER NOT NULL,      -- -> syms.sym
    dst INTEGER NOT NULL,      -- -> syms.sym
    type TEXT NOT NULL,        -- defines | imports | calls | calls_external | calls_ambiguous | inherits
    lineno INTEGER,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS file_hashes (
    file TEXT PRIMARY KEY,
    hash TEXT NOT NULL,
    indexed_at TEXT NOT NULL
);

-- Files the parser rejected, keyed on CONTENT hash. Without this a file
-- that can't be parsed is retried on every single run forever: it never
-- earns a row in file_hashes, so it always looks "changed". On a real
-- 4,113-file repo that was 5 files re-parsed every run, turning a ~0.2s
-- no-op into 1.4s. Keyed on the hash, not the path, so repairing the file
-- brings it back automatically on the next run - and `--force` clears the
-- table outright, which is the escape hatch for "the parser got better".
-- Deliberately NOT part of file_hashes: a failed file must not look
-- successfully indexed to anything else that reads that table.
CREATE TABLE IF NOT EXISTS failed_files (
    file TEXT PRIMARY KEY,
    hash TEXT NOT NULL,
    error TEXT,
    failed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT,               -- may be NULL for repo-wide notes
    note TEXT NOT NULL,
    session_id TEXT,
    date TEXT NOT NULL,
    source TEXT NOT NULL        -- stated | observed | derived
);
CREATE INDEX IF NOT EXISTS idx_notes_node ON session_notes(node_id);

-- Layer 3: indexed Claude Code session transcripts for this repo (opt-in,
-- see session_indexer.py). session_notes above is curated, human/agent
-- -written findings; this is the raw conversation history, indexed
-- automatically so past sessions are searchable without re-deriving what
-- was already discussed or done.
CREATE TABLE IF NOT EXISTS session_files (
    path TEXT PRIMARY KEY,      -- absolute path to the .jsonl transcript
    session_id TEXT,
    size INTEGER NOT NULL,      -- last-seen size, for cheap incremental re-scan
    mtime REAL NOT NULL,
    bytes_indexed INTEGER NOT NULL DEFAULT 0,  -- byte offset to resume reading from
    lines_indexed INTEGER NOT NULL DEFAULT 0,  -- line count at that offset, for reporting
    last_cwd TEXT,              -- most recent 'cwd' field seen (see docstring)
    indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transcript_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    ts TEXT,                    -- ISO timestamp from the transcript, if present
    role TEXT NOT NULL,         -- user | assistant | summary
    kind TEXT NOT NULL,         -- message | action | summary
    text TEXT NOT NULL,
    source_file TEXT NOT NULL,
    line_no INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_session ON transcript_chunks(session_id);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON transcript_chunks(source_file);
"""

# FTS5 over transcript_chunks, best-effort: most Python sqlite3 builds have
# FTS5 compiled in, but not all (a minimal/older libsqlite3), so this is
# created separately from SCHEMA and failure just disables fast search -
# search_sessions() falls back to a plain LIKE scan (see query_lib.py). An
# external-content table (content='transcript_chunks') keeps the indexed
# text in exactly one place; the two triggers are what standard sqlite3
# docs prescribe to keep it in sync on insert/delete (chunks are never
# updated in place - a re-indexed file's stale chunks are deleted first).
_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
    text, content='transcript_chunks', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS transcript_chunks_ai AFTER INSERT ON transcript_chunks BEGIN
    INSERT INTO transcript_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS transcript_chunks_ad AFTER DELETE ON transcript_chunks BEGIN
    INSERT INTO transcript_fts(transcript_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
"""


# Bumped when the on-disk shape of the DERIVED tables changes. Version 2
# interned node ids to integers and replaced edges.evidence with
# lineno+note. There is no in-place upgrade: the derived tables are rebuilt
# from source, which is cheap (a full re-index of the largest repo tested
# is 30s) and cannot drift from the parser the way a converter would.
SCHEMA_VERSION = 2


def _migrate_if_old_format(conn: sqlite3.Connection) -> bool:
    """Detect a pre-interning database and clear what the indexer rebuilds.

    Reading an old DB with the new code would not raise - it would quietly
    match nothing, because the queries compare integers against stored
    text. That is the worst possible failure for a tool whose job is to
    answer "what calls this", so the old shape is detected and discarded
    rather than read.

    session_notes and the Layer 3 transcript tables are NOT derived from
    source and are deliberately left alone: a note is something a person
    (or a past session) wrote down, and re-indexing must never cost them."""
    cols = {r[1]: (r[2] or "").upper() for r in conn.execute("PRAGMA table_info(edges)")}
    if not cols or cols.get("src") == "INTEGER":
        return False   # new database, or already migrated
    for stmt in ("DROP TABLE IF EXISTS edges",
                 "DROP TABLE IF EXISTS nodes",
                 "DROP TABLE IF EXISTS file_hashes",
                 "DROP TABLE IF EXISTS failed_files",
                 "DROP TABLE IF EXISTS syms"):
        conn.execute(stmt)
    conn.commit()
    # Dropping tables frees pages but does not shrink the file, so without
    # this the whole point of the migration - a smaller database - would be
    # invisible: the old bytes would simply sit on the freelist waiting to
    # be reused. Only ever runs on the one-time migration, never on a
    # normal open.
    conn.execute("VACUUM")
    return True


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    # WAL: readers (e.g. the MCP server mid-query) aren't blocked by a
    # concurrent index run, and vice versa. synchronous=NORMAL is safe
    # under WAL (durable across app crashes, not OS crashes) and measurably
    # faster for the bursty bulk-insert pattern cg_index.py does - an
    # acceptable trade for a local dev index that's cheap to rebuild anyway.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    _migrate_if_old_format(conn)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (str(SCHEMA_VERSION),))
    try:
        conn.executescript(_FTS_SCHEMA)
    except sqlite3.OperationalError:
        # FTS5 not compiled into this Python's sqlite3 build - degrade to a
        # LIKE-based scan rather than fail. query_lib.search_sessions()
        # tries the FTS table and falls back to LIKE if it's missing,
        # rather than tracking this as separate connection state.
        pass
    return conn


def file_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def codegraph_home() -> str:
    """Where ALL codegraph state lives by default: never inside a target
    repo. Overridable via $CODEGRAPH_HOME for anyone who wants a
    non-default location (a shared machine, a custom drive, etc.)."""
    return os.environ.get("CODEGRAPH_HOME") or os.path.join(os.path.expanduser("~"), ".codegraph")


def repo_key(repo_root: str) -> str:
    """Deterministic, human-readable identifier for a repo, derived ONLY
    from its absolute path - no registry file to keep in sync, no state
    that can drift. Two different repos that happen to share a folder
    name (a common `backend/` in two different projects, say) still get
    distinct keys because the hash covers the full path; the basename
    prefix is there purely so `ls ~/.codegraph/repos/` stays readable
    instead of a wall of hashes."""
    abs_root = os.path.normpath(os.path.abspath(repo_root)).rstrip(os.sep) or os.sep
    digest = hashlib.sha256(abs_root.encode("utf-8")).hexdigest()[:10]
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(abs_root)) or "repo"
    return f"{base}-{digest}"


def default_db_path(repo_root: str) -> str:
    """The DB path used when --db isn't given explicitly: always OUTSIDE
    the repo, under codegraph_home()/repos/<repo_key>/graph.db. This is
    the whole point of the design - indexing a repo, by whoever runs it,
    never creates or modifies a single file inside that repo's working
    tree, so there's nothing for its source control to ever see, gitignore
    or not."""
    return os.path.join(codegraph_home(), "repos", repo_key(repo_root), "graph.db")


# .py handled by this module's own `ast`-based parser; the rest (including
# .vue, whose <script>/<template> get extracted first) by ts_parser.py
# (tree-sitter).
SOURCE_EXTS = (".py", ".ts", ".tsx", ".js", ".jsx", ".vue", ".go")


def iter_py_files(root: str, extensions: tuple = SOURCE_EXTS):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(extensions):
                yield os.path.relpath(os.path.join(dirpath, fn), root)


@dataclass
class PendingCall:
    caller_id: str
    called_name: str            # final segment, e.g. "self.foo()" / "foo()" -> "foo"
    lineno: int
    file: str
    base_name: str | None = None  # for obj.method(): the simple name of obj, if it's a bare Name
    attr_base: str | None = None
    """Set ONLY for a two-level self/this attribute chain - `self.<attr>.foo()`
    (Python) or `this.<attr>.foo()` (TS/JS) - to `<attr>`, the attribute name.
    base_name is still `_COMPLEX_BASE` in this case (the base genuinely isn't
    a simple Name), so this is carried separately rather than overloading
    base_name. cg_index.py's pass 2 uses it together with the lightweight
    attr-type inference in `attr_types` (see ParseResult) to resolve calls
    like `self.repo.save()` to `Repo.save` when `self.repo`'s type was
    inferred from a constructor assignment or (TS) a field/parameter type
    annotation - see SKILL.md for the exact scope of what is and isn't
    inferred."""
    ctor_type: str | None = None
    """Set for `Scorer().method()` (Python) / `new Scorer().method()` (TS) -
    a method called directly on a freshly constructed instance - to the
    CLASS NAME as written. This needs no inference at all: the type is
    right there in the expression. It was nonetheless the one instance-call
    shape that resolved to `external:<method>`, because the receiver is a
    call/new expression rather than a Name, so the attribute-call path had
    no base to work with. Found by an adversarial review against a real
    repo, where it made a load-bearing production method look uncalled -
    `neighbors(method, "in")` returned only the method's own defines edge.
    The stored form (`s = Scorer(); s.method()`) always worked."""

    no_cross_file_guess: bool = False
    """Set by ts_parser.py for calls extracted from a Vue <template> block
    (see _template_pending_calls). A bare call/handler reference there
    (base_name=None) is semantically like a script-level bare call and
    should still get the SAME resolution priority - same-file match
    first, then an import binding for the bare name - but must NOT fall
    back to "is there exactly one same-named function anywhere in the
    repo" the way an ordinary bare script call still does. Real numbers
    from webapp: without this flag, template-sourced bare calls
    alone pushed ambiguous from 2,106 to 8,322, because Vue component
    method names (`close`, `cancel`, `onSubmit`, ...) repeat across
    hundreds of components in a way ordinary global function names
    mostly don't - the cross-file "unique candidate" heuristic that's a
    reasonable bet for a genuinely global Python/JS function is a bad
    bet for an implicitly-`this`-scoped template handler reference."""


@dataclass
class ParseResult:
    nodes: list = field(default_factory=list)   # list[dict]
    edges: list = field(default_factory=list)   # list[dict] (defines/imports only)
    pending_calls: list = field(default_factory=list)  # list[PendingCall]
    import_bindings: dict = field(default_factory=dict)  # local_name -> ImportBinding
    class_bases: list = field(default_factory=list)     # list[PendingBase]
    attr_types: list = field(default_factory=list)      # list[PendingAttrType]
    pending_imports: list = field(default_factory=list)  # list[PendingImport]


@dataclass
class PendingImport:
    """An import statement whose TARGET MODULE is not yet resolved.

    Import *bindings* were always resolved - that is how a call to an
    imported name finds its definition - but the `imports` EDGE itself
    always pointed at `external:<specifier>`, even when the specifier
    named a file in this very repo. "What imports this module" therefore
    had no answer (measured: 2,543 imports edges in one repo, every one
    external, for a util file 32 files import), and impacted_by on a
    module came back empty - which for a route handler wired up by a
    default import is a false negative, not a gap.

    Resolved in cg_index pass 2 against the on-disk file set, the same
    place and the same way calls are resolved.

    candidates: repo-relative paths to try, best first.
    external:   the `external:<...>` id to fall back to when none exist.
    evidence:   the original statement text, so a reader still sees which
                name was imported, not just which file."""
    candidates: list
    external: str
    evidence: str
    lineno: int


@dataclass
class PendingBase:
    """A class's base/superclass reference, as written (`class Derived(Base):`,
    TS `class Derived extends Base`) - not yet resolved to a node id, same
    "extract the shape now, resolve repo-wide in pass 2" split as
    PendingCall. base_name/ref_name decompose the reference exactly like a
    call target does (see graph_lib._call_target / ts_parser._call_target):
    for a bare name (`Base`) base_name is None and ref_name is "Base"; for a
    one-level dotted reference (`mod.Base` / TS `mod.Base`) base_name is the
    module alias and ref_name is "Base". Only these two shapes are tracked -
    a deeper dotted chain, a generic subscript (`Base[T]`), or any other
    non-simple base expression is silently skipped (no PendingBase entry),
    which just means that base is absent from the resolved MRO rather than
    guessed at - the same conservative default used everywhere else in this
    tool."""
    class_id: str
    base_name: str | None
    ref_name: str
    file: str
    lineno: int


@dataclass
class PendingAttrType:
    """A lightweight, deliberately narrow type inference: `self.<attr> =
    ClassName(...)` (Python __init__, or any method) / `this.<attr> = new
    ClassName()` (TS/JS), a bare type annotation (`self.<attr>: ClassName`,
    TS field/constructor-parameter-property `<attr>: ClassName`), or a local
    variable instantiation (`x = ClassName(...)` / `let x = new ClassName()`
    / `let x: ClassName`). This is NOT full type inference - no control-flow
    awareness, no reassignment tracking (last write in source order wins),
    no return-type inference, no tracking through function parameters or
    anything beyond these direct shapes. See SKILL.md for the exact stated
    scope boundary.

    scope_kind is "self_attr" (scope_id is the owning CLASS's node id - the
    attribute's type is visible to every method of that class, matching how
    `self.x`/`this.x` actually behaves) or "local_var" (scope_id is the
    owning FUNCTION/METHOD's node id - a local variable's inferred type is
    visible only within that one function, no cross-function tracking).
    base_name/ref_name decompose the referenced class exactly like
    PendingBase above."""
    scope_id: str
    scope_kind: str   # "self_attr" | "local_var"
    var_name: str
    base_name: str | None
    ref_name: str
    file: str
    lineno: int


@dataclass
class ImportBinding:
    """What a local name refers to, from an `import`/`from...import` in
    this file - resolved to candidate repo-relative module PATHS (no
    existence check yet; that happens at call-resolution time against the
    actual file listing, since binding computation is per-file and
    shouldn't need the whole repo's file set).

    One binding serves both possible uses of the bound name:
      - as a module alias, in `local_name.something()`  -> module_candidates
      - as an imported symbol, in a bare `local_name()`  -> symbol_candidates
        + symbol_name (the name as exported by the source module - may
        differ from local_name when the import used `as`)
    Which one applies depends on how the call site actually uses the name
    (call-resolution time decides that, not binding computation).
    """
    local_name: str
    module_candidates: list = field(default_factory=list)
    symbol_candidates: list = field(default_factory=list)
    symbol_name: str | None = None


_COMPLEX_BASE = "<complex>"  # sentinel: see below, not a legal identifier


def _call_target(func_node: ast.expr):
    """Extract (base_name, called_name) from a Call.func node.
    base_name is the simple Name a method is called on (e.g. "os" in
    os.getcwd(), "self" in self.foo()) when resolvable, `_COMPLEX_BASE` when
    there IS a base but it's not a simple Name (e.g. `get_conn().execute()`,
    `self.x.y()`), and None only for a true bare/global call (`foo()`).
    Bug found via real-world testing: `get_conn().execute()` was previously
    returned as base_name=None (indistinguishable from a bare call), which
    let it wrongly fall into the bare-call "exactly one repo-wide candidate"
    guess in cg_index.py and match an unrelated same-named function/method
    elsewhere in the repo. `_COMPLEX_BASE` isn't a legal identifier so it
    never coincidentally matches a real import binding, but being non-None
    correctly routes the call through the (safer) attribute-call resolution
    path, which treats "no binding, no same-file match" as external/unknown
    rather than guessing."""
    if isinstance(func_node, ast.Name):
        return None, func_node.id
    if isinstance(func_node, ast.Attribute):
        base = func_node.value
        base_name = base.id if isinstance(base, ast.Name) else _COMPLEX_BASE
        return base_name, func_node.attr
    return None, None


def _ctor_type(func_node: ast.expr):
    """The class name in `Scorer().method()`, or None. Python has no `new`,
    so an inline construction is a Call on a bare Name - which is also what
    an ordinary function call looks like. Resolution checks the name is
    actually a CLASS before using it (cg_index pass 2), so `helper().run()`
    where helper is a function does not get mistaken for a construction."""
    if isinstance(func_node, ast.Attribute) and isinstance(func_node.value, ast.Call):
        inner = func_node.value.func
        if isinstance(inner, ast.Name):
            return inner.id
    return None


def _name_ref(expr: ast.expr) -> tuple[str | None, str] | None:
    """Like _call_target, but for a bare NAME reference rather than a call -
    a base class (`class Derived(Base):` / `class Derived(mod.Base):`) or
    the class/type on the right of a lightweight type-inference assignment
    (`self.x = Bar(...)`, `self.x: Bar`). Returns (module_alias_or_None,
    simple_name) for a plain Name or a one-level dotted Attribute; None for
    anything else (a Subscript like `Generic[T]`, a call result, a deeper
    dotted chain, ...) - skipped rather than guessed, same conservative
    default as _call_target's `_COMPLEX_BASE` path."""
    if isinstance(expr, ast.Name):
        return None, expr.id
    if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
        return expr.value.id, expr.attr
    return None


def _self_attr_chain(func_node: ast.expr) -> str | None:
    """If func_node (a Call.func) is the two-level chain `self.<attr>.<method>`
    (e.g. `self.repo.save()`), return `<attr>` ("repo"); else None. The
    method name itself ("save") is already captured as `called_name` by
    _call_target - this only extracts the extra attribute-name context
    needed to resolve THROUGH self's attribute rather than treating
    `self.repo.save()` as an unresolvable `_COMPLEX_BASE` call, the way a
    genuinely dynamic base (`get_conn().execute()`) still correctly is."""
    if not isinstance(func_node, ast.Attribute) or not isinstance(func_node.value, ast.Attribute):
        return None
    inner = func_node.value
    root = inner.value
    if isinstance(root, ast.Name) and root.id == "self":
        return inner.attr
    return None


def _collect_type_assignments(func_node) -> list[tuple[str, str, str | None, str, int]]:
    """Scan func_node's own body (stopping at nested defs/classes - same
    rule as _direct_calls, so a nested function's local assignments aren't
    misattributed to the outer scope) for the lightweight type-inference
    sources this tool tracks - see PendingAttrType's docstring for the exact
    scope. Returns raw (scope_kind, var_name, base_name, ref_name, lineno)
    tuples; _visit_def decides scope_id (class vs function) and whether a
    "self_attr" entry is even meaningful here (only inside an actual
    method - see call site)."""
    out: list[tuple[str, str, str | None, str, int]] = []
    stack = list(ast.iter_child_nodes(func_node))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        target = None
        ref = None
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.value, ast.Call):
            target = n.targets[0]
            ref = _name_ref(n.value.func)
        elif isinstance(n, ast.AnnAssign):
            target = n.target
            ref = _name_ref(n.annotation)
        if target is not None and ref is not None:
            lineno = getattr(n, "lineno", func_node.lineno)
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) \
                    and target.value.id == "self":
                out.append(("self_attr", target.attr, ref[0], ref[1], lineno))
            elif isinstance(target, ast.Name):
                out.append(("local_var", target.id, ref[0], ref[1], lineno))
        stack.extend(ast.iter_child_nodes(n))
    return out


def _module_candidates(base: str) -> list[str]:
    """base is a repo-relative path with no extension (e.g. 'pkg/sub');
    it could be the file itself or a package __init__."""
    if not base:
        return []
    base = base.replace(os.sep, "/")
    return [f"{base}.py", f"{base}/__init__.py"]


def _sibling_base(current_file: str, base: str) -> str:
    """Absolute (non-relative) imports resolve from the repo root by
    default, but a common real-world pattern - a flat scripts/ or src/
    directory of standalone files with no __init__.py - relies on
    Python's runtime sys.path (the importing script's own directory) to
    resolve `import sibling_module`, not a real package relationship.
    This tool's own scripts/ layout is exactly that pattern (caught by
    self-indexing), so this computes the sibling-directory candidate as
    a fallback alongside the repo-root one, not instead of it."""
    d = os.path.dirname(current_file)
    return f"{d}/{base}" if d else base


def _relative_import_base(current_file: str, level: int, module: str | None) -> str | None:
    """Resolve a relative `from` import's source to a repo-relative base
    path (no extension). level=1 means the current file's own package
    (its containing directory) - `from . import x`; each extra dot goes
    up one more directory - `from .. import x`, `from ...pkg import x`.
    Returns None if this would go above the repo root (can't resolve)."""
    base_dir = os.path.dirname(current_file)
    for _ in range(level - 1):
        if not base_dir:
            return None  # already at repo root, can't go higher
        base_dir = os.path.dirname(base_dir)
    if module:
        mod_path = module.replace(".", "/")
        return f"{base_dir}/{mod_path}" if base_dir else mod_path
    return base_dir  # `from . import x` / `from .. import x` (module is None)


def _direct_calls(func_node: ast.AST) -> list[ast.Call]:
    """Call nodes made directly in func_node's own body - NOT inside a
    nested def/class, which is visited (and its calls attributed) on its
    own. Plain ast.walk() doesn't stop at nested scopes, so a call made
    only inside a nested function would otherwise also get attributed to
    every enclosing function - a real bug found in testing (see README)."""
    calls: list[ast.Call] = []
    stack = list(ast.iter_child_nodes(func_node))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue  # its own calls are captured when we visit it separately
        if isinstance(n, ast.Call):
            calls.append(n)
        stack.extend(ast.iter_child_nodes(n))
    return calls


class FileVisitor(ast.NodeVisitor):
    def __init__(self, rel_file: str, module_id: str):
        self.file = rel_file
        self.module_id = module_id
        self.stack: list[str] = []
        self.id_stack: list[str] = [module_id]
        self.result = ParseResult()

    def _qualname(self, name: str) -> str:
        return ".".join(self.stack + [name]) if self.stack else name

    def _node_id(self, qualname: str) -> str:
        return f"{self.file}::{qualname}"

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            if alias.asname:
                # `import a.b.c as x` - x is bound to the full dotted path
                local = alias.asname
                base = alias.name.replace(".", "/")
            else:
                # `import a.b.c` (no alias) binds only the TOP component
                # ("a") per Python semantics - a deeper dotted chain used
                # at a call site (a.b.c.func()) isn't resolvable via this
                # binding since we only capture the call's immediate base
                # name, not the full chain. Documented gap, not a bug.
                local = alias.name.split(".")[0]
                base = local
            # sibling-directory candidate first (the common script-layout
            # pattern), repo-root candidate second - whichever exists on
            # disk wins at resolution time, order only matters if both do
            candidates = _module_candidates(_sibling_base(self.file, base)) + _module_candidates(base)
            self.result.import_bindings[local] = ImportBinding(
                local_name=local, module_candidates=candidates,
            )
            self.result.pending_imports.append(PendingImport(
                candidates=candidates, external=f"external:{alias.name}",
                evidence=f"import {alias.name} (line {node.lineno})", lineno=node.lineno,
            ))

    def visit_ImportFrom(self, node: ast.ImportFrom):
        mod = node.module or ""
        for alias in node.names:

            if alias.name == "*":
                continue  # star import - can't bind individual names, leave unresolved rather than guess
            local = alias.asname or alias.name
            is_relative = bool(node.level and node.level > 0)
            if is_relative:
                base = _relative_import_base(self.file, node.level, node.module)
            else:
                base = mod.replace(".", "/")
            if base is None:
                continue  # relative import climbs above repo root - can't resolve

            symbol_bases = [base]
            submodule_bases = [f"{base}/{alias.name}" if base else alias.name]
            if not is_relative:
                # same sibling-directory fallback as plain `import` (see
                # _sibling_base) - only meaningful for absolute imports;
                # a relative import's base is already directory-anchored
                sib = _sibling_base(self.file, base) if base else _sibling_base(self.file, "")
                if sib and sib != base:
                    symbol_bases.insert(0, sib)
                    submodule_bases.insert(0, f"{sib}/{alias.name}" if sib else alias.name)

            symbol_candidates = [c for b in symbol_bases for c in _module_candidates(b)]
            module_candidates = [c for b in submodule_bases for c in _module_candidates(b)]
            # A `from X import y` edge targets the module X itself when X is
            # in the repo; `import X.y` style submodules are tried first, in
            # case the imported name is a module rather than a symbol.
            self.result.pending_imports.append(PendingImport(
                candidates=module_candidates + symbol_candidates,
                external=f"external:{mod}.{alias.name}" if mod else f"external:{alias.name}",
                evidence=f"from {mod} import {alias.name} (line {node.lineno})",
                lineno=node.lineno,
            ))
            self.result.import_bindings[local] = ImportBinding(
                local_name=local,
                symbol_candidates=symbol_candidates, symbol_name=alias.name,
                module_candidates=module_candidates,
            )

    def _visit_def(self, node, kind: str):
        qn = self._qualname(node.name)
        node_id = self._node_id(qn)
        end_lineno = getattr(node, "end_lineno", node.lineno)
        self.result.nodes.append({
            "id": node_id, "kind": kind, "name": node.name, "qualname": qn,
            "file": self.file, "lineno": node.lineno, "end_lineno": end_lineno,
        })
        parent_id = self.id_stack[-1]
        self.result.edges.append({
            "src": parent_id, "dst": node_id, "type": "defines",
            "evidence": f"{kind} {node.name} defined at {self.file}:{node.lineno}",
        })

        # record calls made directly in this function/method body (not inside
        # a nested def, which records its own calls when visited below).
        #
        # Classes get the same treatment: a call in a CLASS BODY
        # (`col = field()`, a decorator, a computed base class) runs at
        # definition time and is every bit as real as one in a function.
        # Skipping it made the whole Django/SQLAlchemy/Pydantic shape -
        # where the calls ARE the class body - invisible to the graph.
        # _direct_calls already stops at nested defs, so a method's own
        # calls still belong to the method, not to its class.
        if kind in ("function", "method", "class"):
            for sub in _direct_calls(node):
                base_name, name = _call_target(sub.func)
                if name:
                    self.result.pending_calls.append(
                        PendingCall(caller_id=node_id, called_name=name, base_name=base_name,
                                    attr_base=_self_attr_chain(sub.func),
                                    ctor_type=_ctor_type(sub.func),
                                    lineno=getattr(sub, "lineno", node.lineno), file=self.file)
                    )

        if kind in ("function", "method"):
            # lightweight type inference (see PendingAttrType docstring):
            # self.<attr> = Class(...) / self.<attr>: Class is scoped to the
            # ENCLOSING CLASS (parent_id, since kind == "method" is exactly
            # when parent_id is a class node - a plain module-level
            # "function" has no self); a local `x = Class(...)` is scoped to
            # THIS function/method only, regardless of kind.
            for scope_kind, var_name, base_name, ref_name, lineno in _collect_type_assignments(node):
                if scope_kind == "self_attr":
                    if kind != "method":
                        continue  # a bare module-level function has no `self`
                    scope_id = parent_id
                else:
                    scope_id = node_id
                self.result.attr_types.append(PendingAttrType(
                    scope_id=scope_id, scope_kind=scope_kind, var_name=var_name,
                    base_name=base_name, ref_name=ref_name, file=self.file, lineno=lineno,
                ))

        if kind == "class":
            # base classes (`class Derived(Base):` / `class Derived(mod.Base):`)
            # - resolved against the repo-wide symbol index in cg_index.py's
            # pass 2, same split as everything else pending here. Only a
            # bare Name or one-level dotted Attribute base is tracked (see
            # _name_ref) - a metaclass kwarg, `Generic[T]`, or any other
            # non-simple base expression is silently skipped, not guessed.
            for base_expr in node.bases:
                ref = _name_ref(base_expr)
                if ref is not None:
                    self.result.class_bases.append(PendingBase(
                        class_id=node_id, base_name=ref[0], ref_name=ref[1],
                        file=self.file, lineno=getattr(base_expr, "lineno", node.lineno),
                    ))

        self.stack.append(node.name)
        self.id_stack.append(node_id)
        # only descend into nested defs/classes, not re-walk calls (already captured above)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                self.visit(child)
        self.id_stack.pop()
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef):
        self._visit_def(node, "class")

    def visit_FunctionDef(self, node: ast.FunctionDef):
        kind = "method" if self.stack else "function"
        self._visit_def(node, kind)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        kind = "method" if self.stack else "function"
        self._visit_def(node, kind)


def parse_file(root: str, rel_file: str) -> ParseResult:
    abs_path = os.path.join(root, rel_file)
    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
        source = f.read()
    module_id = rel_file
    tree = ast.parse(source, filename=rel_file)
    result = ParseResult()
    result.nodes.append({
        "id": module_id, "kind": "module", "name": os.path.basename(rel_file),
        "qualname": rel_file, "file": rel_file, "lineno": 1,
        "end_lineno": getattr(tree, "end_lineno", 1),
    })
    visitor = FileVisitor(rel_file, module_id)
    visitor.result = result
    for child in ast.iter_child_nodes(tree):
        visitor.visit(child)

    # Calls made at MODULE scope - outside any def/class. These run at
    # import time, so they are real calls, and they used to be dropped
    # entirely: pending calls were only ever collected inside _visit_def,
    # which never fires for the module body. _direct_calls already stops
    # at nested def/class boundaries, so handing it the Module node gives
    # exactly the top-level calls and nothing a function already owns.
    for sub in _direct_calls(tree):
        base_name, name = _call_target(sub.func)
        if name:
            result.pending_calls.append(
                PendingCall(caller_id=module_id, called_name=name, base_name=base_name,
                            attr_base=_self_attr_chain(sub.func),
                            ctor_type=_ctor_type(sub.func),
                            lineno=getattr(sub, "lineno", 1), file=rel_file)
            )
    return result


def intern(conn: sqlite3.Connection, ids) -> dict:
    """Map node-id strings to their integer symbols, creating any that are
    new. Returns {id: sym}.

    Done in bulk rather than per row: a single file can contribute
    thousands of edge endpoints, and a round trip each would dominate
    indexing. INSERT OR IGNORE keeps ids stable across runs, which matters
    because an edge written by one file may point at a symbol another file
    defines (or at an "external:..." target no file defines at all)."""
    ids = list(dict.fromkeys(ids))   # de-dup, order-stable
    if not ids:
        return {}
    for i in range(0, len(ids), _SQLITE_MAX_VARS):
        chunk = ids[i:i + _SQLITE_MAX_VARS]
        conn.executemany("INSERT OR IGNORE INTO syms (id) VALUES (?)",
                         [(x,) for x in chunk])
    out = {}
    for i in range(0, len(ids), _SQLITE_MAX_VARS):
        chunk = ids[i:i + _SQLITE_MAX_VARS]
        ph = ",".join("?" * len(chunk))
        for sym, ident in conn.execute(
            f"SELECT sym, id FROM syms WHERE id IN ({ph})", chunk
        ):
            out[ident] = sym
    return out


_SQLITE_MAX_VARS = 900   # stay under SQLite's default ~999-parameter limit


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
