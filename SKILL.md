---
name: codegraph
description: Use when working in an already-indexed repo to find symbols, trace call/import relationships, check blast radius before a change, or look up prior findings — instead of Grep/Glob across the whole tree. Not a substitute for reading the actual code once you've located it.
---

# codegraph

A local, three-layer index for a codebase:

- **Layer 1 — code graph.** Parsed structure (functions, classes, modules)
  and relationships (`defines`, `imports`, `calls`) — Python via the
  stdlib `ast` module, TypeScript/JavaScript/TSX/`.vue` (`<script>` and
  `<template>`) via tree-sitter. Deterministic, no network, no vector store.
- **Layer 2 — session memory.** Durable notes ("why this is built this
  way," "what broke last time," decisions made) attached to a symbol,
  written by whoever used the graph and persisted across sessions.
- **Layer 3 — session history (opt-in).** Claude Code's own past session
  transcripts for this repo, indexed and made full-text searchable, so a
  session can pull up what was discussed or done previously without
  re-deriving it. Off by default — see "Session history" below before
  turning it on, since it indexes raw conversation content.

All three are exposed as MCP tools (`search`, `search_code`,
`search_memory`, `neighbors`, `impacted_by`, `path_between`, `add_note`,
`search_sessions`, `list_sessions`) so a Claude Code session queries them
directly instead of re-deriving the same context with Grep every time.

## When to use this vs. Grep/Glob

Use codegraph first when the question is about **structure or history**:
"what calls this," "what breaks if I change this," "have we seen this
before." Use Grep/Glob when the question is about **exact text** not yet
in the graph (a string literal, a TODO comment, a config key), or about
what a Vue `<template>` calls — the graph only knows about parsed
Python/TS/JS/`.vue`-script symbols, not arbitrary text or template
bindings (see Known limitations).

Read the actual source before making a change — the graph tells you
*where* to look and *what's connected*, not what the code says.

## Installing this skill

This file is generic — nothing in it is specific to any one repo, so
install it ONCE at the user level rather than copying it into each
repo's `.claude/skills/`:

```
mkdir -p ~/.claude/skills/codegraph
cp SKILL.md ~/.claude/skills/codegraph/SKILL.md
```

It then applies to every repo you've indexed with codegraph, with no
per-repo copy to keep in sync (and, as with everything else in this
tool, no file added to any repo's own tree).

## Zero footprint, by design

Indexing or querying a repo with this tool never creates, modifies, or
leaves behind a single file inside that repo's working tree — not even
a gitignored one, regardless of who runs it. The tool code lives once,
globally, and every repo's graph DB lives in a global cache keyed off
that repo's absolute path (`~/.codegraph/repos/<repo-key>/graph.db` by
default — see `graph_lib.py`'s `codegraph_home()`/`repo_key()`/
`default_db_path()` for exactly how). The MCP server is registered
through Claude Code's own per-user config, never through a repo's
`.mcp.json` (which is meant to be committed and shared). Nothing about
this setup requires touching the repo at all.

## Setup (once per machine, then once per repo)

Once per machine — install the tool globally, outside any repo. From a
checkout: `./install.sh` (copies `src/codegraph/` + `requirements.txt` to
`~/.codegraph/venv/bin/`, then installs the pinned deps). No checkout yet?
`bootstrap.sh` clones the repo and runs `install.sh` for you in one
command — see README's "Installing without a checkout".

(`tree-sitter`/`tree_sitter_languages` are pinned in `requirements.txt`
to versions known to work together — newer `tree-sitter` broke the
language-loading API this package relies on.)

Once per repo — index it, then register the MCP server FROM INSIDE that
repo's directory (`--scope local` ties the registration to your current
project, so `cd` there first):

```
codemap-index /path/to/repo
cd /path/to/repo
claude mcp add --scope local codegraph -- \
    codemap-mcp --repo-root /path/to/repo
```

`--scope local` stores this in Claude Code's own user-level config
(`~/.claude.json`), scoped to this one project - never in any file
inside the repo.

`cg_index.py` picks the parser by extension automatically — works on a
Python repo, a TS/JS/Vue repo, or a mix (`.py` → `ast`, `.ts`/`.tsx`/
`.js`/`.jsx` → tree-sitter, `.vue` → tree-sitter on its extracted
`<script>` block plus a best-effort `<template>` pass). `--scope local`
stores the MCP registration in Claude Code's own per-user config for
that project path — not in any file inside the repo, so it's invisible
to source control and doesn't need a teammate's buy-in to add.

Re-index after pulling changes, or periodically during a work session
— it's incremental (hashes each file, only re-parses what changed) and
fast enough to run often, e.g. from a pre-session hook or at the start
of a Claude Code session:

```
codemap-index /path/to/repo
```

One MCP server instance is bound to one repo's DB at startup. For a
second repo, run `claude mcp add` again with that repo's own
`--repo-root` — no naming collision, since each `claude mcp add` call
is itself scoped to the project you run it from.

## Tool guide

| Tool | Use for |
|---|---|
| `search(query)` | First stop — symbol name + any notes on it, together |
| `search_code(query)` | Structure only, no notes |
| `search_memory(query)` | Notes only, no structure |
| `neighbors(symbol, direction)` | What this calls/imports, or what calls/imports it |
| `impacted_by(symbol, max_depth)` | Blast radius before changing something |
| `path_between(a, b)` | How two symbols are connected |
| `add_note(note, symbol, session_id, source)` | Record a durable finding — do this whenever you learn something about the code that isn't obvious from reading it once |
| `search_sessions(query, limit, role)` | Full-text search past session transcripts for this repo (Layer 3, opt-in — see below) |
| `list_sessions(limit)` | What sessions are indexed, with message counts and time ranges — check this before assuming `search_sessions` has anything to find |

`source` on `add_note` should be `"stated"` (the user told you),
`"observed"` (you found it by reading code/tests), or `"derived"` (you
inferred it) — keep that distinction so future readers know how much to
trust a note.

## Session history (Layer 3, opt-in)

Indexes this repo's own Claude Code session transcripts — every past
session's prompts, replies, and a lightweight log of file/command actions
— into the same DB, full-text searchable via `search_sessions`. This
answers "what did we already try/decide about X," "did a past session
already run into this," without re-reading a whole prior transcript.

Off by default, and worth turning on deliberately rather than reflexively:
it makes whatever was discussed in those sessions — file contents,
commands run, business or customer context that came up — searchable
through the MCP server. Turn it on with `--sessions` on the same indexer
you already run:

```
codemap-index /path/to/repo --sessions
```

Incremental like everything else here — a session transcript that's only
grown since the last run is read from where indexing left off, not
reprocessed from the start, so this stays cheap to run often. Transcripts
are found under Claude Code's own `~/.claude/projects/<encoded-repo-path>/`
directory (same convention Claude Code itself uses to keep sessions
scoped to a project) — nothing indexed means no session transcripts exist
there yet, not a bug.

`search_sessions` returns matches from `user` (what was asked), `assistant`
(what Claude said or did — including a compact log of file edits, reads,
and commands run), and `summary` (Claude Code's own compaction summaries)
entries; filter with `role` when you only want one kind. This layer's
transcript format is Claude Code's internal storage, not a documented API
— parsing is defensive (a line or file that doesn't match the expected
shape is skipped, never a hard failure), but a future Claude Code version
could change it enough that indexing quietly finds nothing; `list_sessions`
returning empty after a real `--sessions` run is the signal to check for
that, rather than assuming the feature is broken.

## Usage logging

Every call through this MCP server is logged to `usage.jsonl` next to
`graph.db` (zero-footprint, same as everything else — not inside the
repo): tool name, arguments, latency, and a small found-anything signal.
Run `codemap-usage /path/to/repo` for a
report — calls by tool, error count, found-anything rate, top queries,
latency. This is what turns "does this help" from an impression into
data; see docs/DESIGN.md's "Usage logging" section for the full design
and what it deliberately doesn't measure (whether a session used Grep
instead, for the same question).

## Known limitations (v1)

Found by rigorous testing — synthetic edge-case repos for all languages
(`tests/edge_repo/`, `tests/edge_repo_ts/` including `.vue` cases), a
real 635-file/16.9K-node Python corpus (the stdlib), and the company's's full
real `webapp` production corpus (3,995 files: TS/JS + `.vue`) —
rather than assumed. Several were outright bugs, now fixed and covered
by regression fixtures; the rest are accepted design trade-offs, stated
explicitly rather than left implicit:

- **`.vue` files: `<script>`/`<script setup>` AND `<template>` are both
  parsed** (`{{ expr }}` interpolations, and directive/bound-attribute
  values like `@click="save"`, `:prop="expr"`, `v-if="cond"`, `#slot="{ x
  }"` shorthand, and dynamic arguments `:[prop]="expr"`/`@[event]="expr"`),
  so a component method's real template-driven callers now show up in
  `impacted_by`/`neighbors` — verified end-to-end on webapp (see
  docs/DESIGN.md). It's still best-effort, not a real Vue template
  compiler: each
  expression fragment is parsed as standalone JS independently, and a
  fragment that fails to parse as JS at all is silently skipped rather
  than failing the file — in practice this is rare (destructuring
  patterns, `in`-loop heads, optional chaining, template literals,
  multi-statement handlers, and ordinary object-literal bindings like
  `:style="{ color: getColor() }"` all parse fine against the pinned
  tree-sitter grammar, verified directly). The gap that measurably
  mattered was narrower than "best-effort JS parsing" suggested: the `#`
  slot-shorthand and dynamic-argument (`:[...]`/`@[...]`) attribute forms
  weren't even being looked at — the attribute-matching regex stopped
  before reaching them, so their values were never attempted, not merely
  skipped on a parse error. Fixed; regression-tested (`tests/edge_repo_ts/
  src/comp/SlotShorthand.vue`) with a call inside a dynamic-argument
  binding that the old regex genuinely missed and the new one finds.
  Template call sites attach to a synthetic `<file>.vue::<template>` node,
  not a real function. `<style>` is never parsed (not relevant to a call
  graph).
- **Call resolution uses import bindings first** (`import x [as y]`,
  `from x import y [as z]` / ESM `import`/`export`, including relative
  imports and `tsconfig.json` path aliases), falling back to same-file
  name matching, and treats any attribute/method call with no same-file
  match and no import binding as `external`/unknown rather than ever
  guessing across files — including chained calls like
  `res.status(503).json(...)` (see docs/DESIGN.md for the real cross-file
  false-match bug this guards against, found via webapp). This
  trades recall for correctness deliberately: a modest resolved-call
  count you can trust beats a higher one you can't.
- **Fixed (partially): `self`/`this` calls now resolve through the
  owning class's inheritance chain, and a lightweight, deliberately
  narrow type inference resolves `self.<attr>.method()` / a local
  `x.method()` too.** This is NOT full type inference - no control-flow
  awareness, no reassignment tracking (last assignment in source order
  wins), no return-type inference, nothing beyond these direct, explicit
  shapes:
    - `class Derived(Base):` / TS `class Derived extends Base` builds a
      real (single-parent-chain, non-C3) MRO, resolved via the same
      import-binding priority as a call (same-file first, then a binding,
      never a cross-file guess for a dotted/complex base) - so
      `self.foo()` now resolves to the actual owning class's method,
      including one only defined on a BASE CLASS IN ANOTHER FILE, not
      just "the one same-named function/method anywhere in this file"
      the old heuristic used.
    - `self.<attr> = Class(...)` (Python, any method, not just
      `__init__`) / a bare `self.<attr>: Class` annotation / TS
      `this.<attr> = new Class()` / a TS field type annotation
      (`service: UserService;`) / a TS constructor PARAMETER PROPERTY
      (`constructor(private repo: Repo)`, `constructor(readonly x: Y)`)
      infers `self.<attr>`'s/`this.<attr>`'s class, so
      `self.repo.save()` resolves to `Repo.save` - this specifically
      covers dependency-injection-style composition
      (Angular/NestJS-style constructor params) where the dependency is
      never constructed with `new`/`Class()` anywhere in the file at
      all, which is why type annotations are read directly rather than
      only tracing instantiation.
    - A local `x = Class(...)` / TS `let x = new Class()` / `let x:
      Class` similarly lets `x.method()` resolve, scoped to that one
      function only (no cross-function tracking).
    - When the owning class (or inferred attribute/variable type) IS
      known but the method genuinely isn't found anywhere in its known
      local inheritance chain, this resolves to `external`/unknown
      immediately rather than falling back to the old same-file-name
      guess - deliberately, since that guess is scoped to "same simple
      name anywhere in the file", not "same simple name on the actually
      relevant class", and could silently resolve to an unrelated
      same-file class's same-named method. Falling back to the old
      heuristic only happens when there's truly no class/type
      information at all (preserves prior recall, never regresses it).
    - Measured impact: on this repo's own dedicated regression fixtures
      (`tests/edge_repo/self_resolution.py`,
      `tests/edge_repo_ts/src/selfResolution.ts`), same-file method-name
      collisions that the old heuristic marked `external` (or could have
      silently mismatched), cross-file inherited methods, and both
      attribute- and local-variable-typed calls now all resolve
      correctly, confirmed against a disabled-feature baseline; on this
      tool's own self-index, calls resolved via this mechanism went from
      0 to 18 (223 total resolved, up from 215; external dropped from
      476 to 468) as a direct, real-world side effect of also fixing a
      related bug found while building this: a bare base-class/type
      reference bound via `from x import Base` (not a module alias) was
      never checked against its import binding at all, only a
      repo-wide "exactly one same-named class" guess - which breaks the
      moment two same-named classes exist anywhere in the repo (as this
      tool's own two test fixtures, indexed together, immediately did).
    - Still explicitly NOT handled: multi-level attribute chains
      (`self.a.b.method()`), a variable's type inferred from a function's
      RETURN type or a parameter's default, and anything requiring real
      control-flow or data-flow analysis. These stay flagged, not built,
      consistent with this tool's "stay simple and fast" scope.
- **Only calls made directly inside a function/method body are tracked.**
  A call made at module level or directly in a class body (outside any
  method) isn't recorded as a `calls` edge.
- **Fixed: TS/JS anonymous callbacks with real calls in them now get their
  own graph node, and `require()` resolves like `import`.** Previously an
  anonymous callback (`useEffect(() => { doWork() })`) wasn't its own
  node at all, and its call was simply never recorded anywhere in the
  graph — not attributed to the enclosing function, just dropped, which
  meant `impacted_by`/`neighbors` on `doWork` silently missed every
  caller reached only through a callback. A callback that makes at least
  one direct call now gets a synthetic `<closure:LINE>` node (both a
  `defines` and a `calls` edge from the enclosing function — `calls`
  specifically because `impacted_by`'s reverse traversal only follows
  `calls`/`calls_external`/`imports`, not `defines`), and nested
  callbacks chain the same way. A callback with no direct calls
  (`.map(x => x.id)`) is deliberately skipped — no node, no graph
  bloat — since there's nothing to resolve through it. Verified on a
  dedicated fixture (`tests/edge_repo_ts/src/closures.ts`):
  `impacted_by("realWork")` now correctly returns callers reached
  through a block-body arrow callback, a `function(){}` expression
  callback, and two levels of nested callbacks, none of which showed up
  at all before this fix. Fixing this also surfaced two latent bugs,
  now fixed too: the tree-sitter grammar (`tree_sitter_languages`
  1.10.2) names anonymous *and* named function expressions `"function"`,
  not `"function_expression"`, so `const x = function() {...}` was never
  tracked; and concise-body arrows (`x => helper(x)`, where the body is
  the call expression directly, no `{ }` block) had their call silently
  missed even for ordinary *named* arrow functions, not just callbacks.
  `require()` (`const x = require('./foo')`, destructured, and
  destructured-with-rename forms) now produces the same `imports`
  edge and import-binding resolution as ESM `import`, falling back to
  `external:<package>` for bare packages exactly like an unresolved
  `import` would (verified on `tests/edge_repo_ts/src/legacyRequire.js`).
- **Fixed: dangling edges after a referenced file is deleted.** If file A
  calls a symbol in file B and B is deleted, A's `calls` edge used to be
  left untouched pointing at a node id that no longer existed — it
  degraded gracefully on query (`target_info: null`) rather than
  crashing, but stayed a phantom "resolved" edge until A itself happened
  to change. Deleting a file now downgrades every `calls` edge pointing
  at one of its about-to-be-removed nodes to `calls_external`
  (`external:<name>`) at delete time, in the same run — the same shape
  every other unresolved call already uses, so it's immediately correct
  rather than eventually correct.
- **`path_between` is for symbols, not whole modules.** BFS is scoped to
  each layer via indexed queries (fast for typical function/class-level
  lookups — sub-millisecond on the stdlib graph), but a query between two
  hub nodes (e.g. two entire modules) can still touch thousands of nodes
  because of how many symbols a module `defines`. Past
  `MAX_FRONTIER_NODES` (default 4000) it fails fast with a clear reason
  instead of silently taking ~100ms+; pass a higher
  `max_frontier_nodes` if you need completeness over speed for that case.
- **Fixed: builtin calls could be hijacked by an unrelated same-named
  repo function.** Found via self-indexing (this repo's own test fixture
  defines a function literally named `print`, which was silently
  swallowing every genuine `print()` call anywhere else in the repo).
  Root cause: the legacy resolver checked "is there exactly one
  same-named function anywhere in the repo" *before* checking "is this
  name a Python builtin," so a real builtin call could get force-matched
  to an unrelated module's coincidentally-same-named function - Python
  builtin shadowing is per-module, not per-repo, so that match was never
  valid to begin with. This existed since the original version, not
  something introduced later - it just needed a large enough corpus with
  a real name collision to surface. Fixed by checking builtins before
  the cross-file unique-match branch (same-file matches still correctly
  take priority even over a builtin, since a module redefining `print`
  really does shadow it for calls inside that same module). Measured
  impact on stdlib: 4,186 calls that were previously force-matched or
  marked ambiguous are now correctly recognized as builtin calls and
  skipped, out of 42,881 total - not a small edge case.
- **Fixed: sibling-directory imports weren't resolved.** Absolute
  `import x` / `from x import y` assumed proper package structure
  (resolving from the repo root), so a flat `src/codegraph/`-style directory
  where files import each other via Python's runtime `sys.path` (not a
  real package) fell through to the less-precise legacy resolver. Now
  tried as a fallback candidate alongside the repo-root interpretation.
  Caught by this tool self-indexing its own `src/codegraph/` layout.
- **Fixed: attribute/method calls with no same-file match or import
  binding were still guessed via a repo-wide unique-name search, and a
  chained call (`res.status(503).json(...)`) could bypass that guard
  entirely.** Found on webapp: a real handler's `res.json(...)`
  matched an unrelated test helper's `mockRes.json`. Both are fixed —
  see docs/DESIGN.md for the full writeup and before/after numbers.
- **Same-scope redefinition collapses to the last definition** (matches
  Python's own runtime shadowing semantics) — e.g. `def foo(): ...` twice
  in the same scope keeps only the second as `foo`'s node.
- **No doc/PDF/SQL-schema linking yet.**
- **Session history (Layer 3) association is per-transcript-file, not
  per-message.** A transcript is matched to a repo by which project
  directory Claude Code filed it under (fixed at session start); if a
  session changed working directory partway through, every message in
  that file is still attributed to the original repo. `search_sessions`
  is a phrase-style full-text match (FTS5, falling back to a plain
  substring scan if FTS5 isn't available in the local Python's sqlite3
  build) — not semantic search, so an unusual paraphrase of what was
  discussed can still miss.
