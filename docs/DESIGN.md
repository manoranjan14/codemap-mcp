# Design notes & testing log

> **A note on names.** This document is the engineering record of a tool
> built and validated against real production repositories. Every repo,
> package, service, file and symbol name in it has been replaced with a
> neutral equivalent - `webapp`, `api-service`, `@acme/node-logger`,
> `useNavModel` and so on. The names are fictional; **every number is
> real** and was measured on the actual codebase. Nothing in the reasoning
> depends on the original names.


This is the detailed engineering record for codegraph: what was built, in
what order, and the concrete before/after evidence each change was
verified with. For the pitch, feature list, and how it works at a glance,
see the main [README](../README.md).

---

# codegraph (local prototype)

A local three-layer index for a codebase — a parsed code graph, persistent
session-memory notes, and (opt-in) full-text search over past Claude Code
session transcripts for the repo — exposed as MCP tools for Claude Code.
See `SKILL.md` for the full usage guide; this file is just install +
quick test.

## Requirements

- Python 3.10+
- `pip install mcp` (only needed to run the MCP server; the indexer and
  CLI query tool use only the standard library — `ast`, `sqlite3`)
- `tree-sitter` + `tree_sitter_languages` (**optional**) — needed only to
  parse TypeScript/JavaScript/Vue. They are imported lazily: without them
  `cg_index.py` still indexes Python fully and skips `.ts/.tsx/.js/.jsx/.vue`
  files with one warning, rather than failing the run. Python-only repos
  need nothing beyond the stdlib.

## Where state lives (zero footprint in indexed repos)

This tool deploys and stores everything OUTSIDE any repo it indexes —
indexing or querying a repo never creates, modifies, or leaves behind a
single file inside that repo's working tree, gitignored or not, no
matter who runs it. Concretely:

- **The tool code itself** lives once, globally — e.g. `~/.codegraph/venv/bin/`
  — not copied into each repo.
- **Every repo's DB** lives under `~/.codegraph/repos/<repo-key>/graph.db`,
  where `<repo-key>` is derived deterministically from the repo's
  absolute path (`graph_lib.repo_key()` — a short hash plus the repo's
  basename, purely for readability; no registry file to keep in sync or
  let drift). Override the root with `$CODEGRAPH_HOME` if you want state
  somewhere other than `~/.codegraph` (a shared machine, another drive).
- **The MCP registration** goes into Claude Code's own per-user config
  (`claude mcp add --scope local`, see below) — never into a repo's
  `.mcp.json`, which is meant to be committed and shared with teammates.

`cg_index.py`'s and `cg_mcp_server.py`'s `--db`/`--repo-root` flags can
still point anywhere explicitly if you want a non-default layout; the
global location is just what happens with no flags at all.

## Quick test (self-indexing)

```
cd codegraph
codemap-index .
codemap-query --repo-root . search index
codemap-query --repo-root . neighbors main
codemap-query --repo-root . impacted-by resolve_symbol
codemap-query --repo-root . note-add - "First note" --session test
```

## Running the tests

```
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
```

The suite indexes the fixture repos in `tests/` into pytest tmp dirs (never
`~/.codegraph`, so it can't disturb a real indexed repo) and asserts on the
resulting graph. Every test corresponds to a specific correctness claim in
this document — most of them to a bug that was found and fixed once by
hand, so the fix can't silently regress.

| File | Covers |
|---|---|
| `tests/test_python_graph.py` | Python parsing + pass-2 resolution: per-file error isolation, nested-scope call attribution, self/MRO/attribute type inference, no dangling edges |
| `tests/test_ts_graph.py` | TS/JS/Vue: closures, `require()`, tsconfig path aliases, `.vue` template-as-caller, TS-side type inference |
| `tests/test_incremental.py` | Content-hash invalidation (a `touch` must not reparse), single-file re-index, `--force`, deleted-file purge + dangling-edge downgrade |
| `tests/test_query_lib.py` | The layer the CLI and MCP server share: symbol resolution and ambiguity, `neighbors`, `impacted_by` depth, `path_between`, notes (including the refusal to store a note against an unresolvable symbol) |
| `tests/test_optional_tree_sitter.py` | The degraded path: indexing works with tree-sitter's import forced to fail |
| `tests/test_module_level_calls.py` | Top-level (module-scope) call attribution in Python, TS and `.vue` `<script setup>`, including dual-script SFCs |
| `tests/test_impacted_by_scaling.py` | `impacted_by` parity against a brute-force reference at every depth, plus guards that it never scans the whole `edges` table or does one node lookup per result |
| `tests/test_incremental.py` (failure cases) | A file the parser rejects is recorded by content hash and not retried until it changes; `--force` retries it |
| `tests/test_search_ranking.py` | `search` match tiers, kind weighting, truncation reporting, and that note-only matches still surface |
| `tests/test_mcp_server.py` | The MCP server end to end over real stdio JSON-RPC: handshake, tool registration, and every tool called over the wire |
| `tests/test_session_search_ranking.py` | Layer 3 kind weighting, the `kind` filter, and that the no-FTS5 LIKE fallback ranks too |
| `tests/test_install_script.py` | `install.sh`'s interpreter detection, including that a stale or too-old `$PYTHON` is rejected rather than trusted |
| `tests/test_class_body_calls.py` | Calls in a class body and in decorators, Python and TS, plus that method calls stay scoped to the method |
| `tests/test_tsconfig_parsing.py` | JSONC stripping that respects string literals, and alias targets that need normalising |
| `tests/test_ambiguous_calls.py` | Ambiguous calls recorded against a synthetic target, with no false reachability |
| `tests/test_interning.py` | The interned storage shape, that the public API still speaks string ids, evidence reconstruction, and the one-time migration off the old format |
| `tests/test_baseline_usage.py` | The built-in-tool logger (opt-in guard, that it never records search patterns, malformed input), the called-vs-Grep ratio, and the three usefulness signals including the fallback window |
| `tests/test_module_imports.py` | Imports resolving to in-repo modules in Python, TS, Vue, aliases and `require()`; third-party staying external; the deleted-module downgrade |
| `tests/test_stale_index.py` | Unresolved results carrying index age and changed-file count, and that ambiguity is not mislabelled as staleness |
| `tests/test_adversarial_findings.py` | `new X().method()` resolving in Python and TS, and that `path_between` cannot route through a shared `external:` node |

`tests/test_ts_graph.py` skips when tree-sitter isn't installed, so a CI
run needs at least one job with it installed for those to mean anything.
`tests/test_optional_tree_sitter.py` shadows the module with a failing stub
on `PYTHONPATH`, so it asserts the real degraded behaviour either way.

## One-time global setup

From a checkout of this repo:

```
./install.sh
```

(what it does under the hood, if you'd rather run it by hand or it's not
executable in your environment: copies `src/codegraph/` + `requirements.txt` to
`~/.codegraph/venv/bin/`, then `pip install -r ~/.codegraph/venv/bin/requirements.txt
--break-system-packages`.)

Don't have a checkout yet? `bootstrap.sh` clones this repo and runs
`install.sh` for you in one command - see "Installing without a checkout"
below.

## Index a real repo

```
codemap-index /path/to/your/repo
# writes ~/.codegraph/repos/<repo-key>/graph.db - nothing inside the repo
```

Re-run this after pulling changes or periodically — it's incremental
(hashes each file, only re-parses what changed).

Add `--sessions` to also index this repo's own Claude Code session
transcripts (Layer 3), so past sessions become searchable via
`search_sessions`. Off by default — it's opt-in because it indexes raw
conversation content, not just code — see SKILL.md's "Session history"
section before turning it on:

```
codemap-index /path/to/your/repo --sessions
```

Then, FROM INSIDE that repo's directory, register the MCP server with
Claude Code, scoped to your own user config rather than the repo (see
SKILL.md for more detail):

```
cd /path/to/your/repo
claude mcp add --scope local codegraph -- \
    codemap-mcp --repo-root /path/to/your/repo
```

## Installing without a checkout

`bootstrap.sh` collapses "clone this repo, run `install.sh`, then index and
register a target repo" into one command, for anyone who doesn't already
have codegraph checked out locally (same idea as the single-command
installers other MCP servers ship):

```
CODEGRAPH_GIT_URL=git@github.com:<you>/codegraph.git \
    bash <(curl -fsSL https://raw.githubusercontent.com/<you>/codegraph/main/bootstrap.sh) \
    --repo-root /path/to/your/repo
```

`CODEGRAPH_GIT_URL` is required and never hardcoded (this file ships
inside the repo it clones, so it can't know its own remote in advance);
`--repo-root` is optional — omit it to just do the one-time global
install, then index/register repos yourself as above. Works against a
private repo exactly like `git clone` does — your own SSH key or HTTPS
credential helper, not this script, does the authenticating. If the repo
is private, the `curl` fetch of the raw script itself also needs your own
auth (a token in the URL, or just clone the repo once and run
`./bootstrap.sh` from the checkout instead of piping from `curl`).

Verified end-to-end against a local bare-repo stand-in for GitHub: clone,
global install (a venv at `~/.codegraph/venv`), indexing a target repo, and
`claude mcp add` registration all completed correctly in one run.

## Files

- `install.sh` — one-time global install (copies `src/codegraph/` to
  `~/.codegraph/venv/bin/`, installs pinned deps); safe to re-run
- `src/codegraph/graph_lib.py` — SQLite schema + AST parser (nodes/edges) +
  the global-storage helpers (`codegraph_home`, `repo_key`, `default_db_path`)
- `src/codegraph/cg_index.py` — CLI: build/incrementally update the graph (and,
  with `--sessions`, the session-transcript index)
- `src/codegraph/session_indexer.py` — Layer 3: finds and incrementally parses
  this repo's Claude Code session transcripts into searchable chunks
- `src/codegraph/query_lib.py` — query + note + session-search logic, shared by
  CLI and MCP server
- `src/codegraph/cg_query.py` — CLI: neighbors / path / impacted-by / search /
  note-add / search-sessions / list-sessions
- `src/codegraph/cg_mcp_server.py` — MCP server exposing the same queries as tools
- `src/codegraph/ts_parser.py` — TS/JS/Vue parsing via tree-sitter, behind a lazy
  optional import (`available()` / `unavailable_reason()`)
- `src/codegraph/cg_reindex_if_opted_in.sh` — SessionStart hook helper: re-indexes
  a repo only if it already has an index, so a session starting anywhere
  never silently indexes a repo the user didn't opt into
- `tests/` — fixture repos (`edge_repo`, `edge_repo_ts`) plus the pytest
  suite that asserts on the graph they produce

## Testing performed

All of the below was verified by hand first; the claims that could be
pinned to a repeatable assertion now are, in the pytest suite above
(`tests/test_python_graph.py`, `tests/test_ts_graph.py`,
`tests/test_incremental.py`), so they stay verified.

- **Edge cases** (`tests/edge_repo/`, kept as a regression fixture): a
  syntax error, an empty file, same-scope redefinition, nested functions,
  unicode identifiers, deep class/function nesting, self-recursion, and a
  user-defined function shadowing a builtin name. Also tested outside
  that fixture: deleting a file mid-session (own symbols purged cleanly;
  cross-file references degrade to `null` info, don't crash) and adding
  a note to an ambiguous/misspelled symbol.
- **Real-world scale**: the Python 3.11 stdlib, 635 files / 16,872 nodes
  / 44,584 edges. Full cold index, incremental re-index (no-op and
  single-file-changed), and query latency for `search`, `neighbors`,
  `impacted_by`, and `path_between`.
- **Profiling**: `cProfile` on the full index run to find actual hot
  spots rather than guessing.
- **Session history (Layer 3)**: indexed against this tool's own real,
  live Claude Code session transcript (120+ messages, including thinking
  blocks, tool_use calls, and a mix of plain-string and block-array user
  content) — verified correct chunk extraction, FTS5 phrase search
  returning relevant ranked results, the unchanged-file fast path (0 new
  chunks, no re-scan), incremental resume on a grown file (only the newly
  appended lines parsed, not the whole transcript), `--force` full
  re-index (no duplicate chunks), and the shrunk/replaced-file reset path
  (stale chunks purged before re-indexing).

Two real bugs were found and fixed (not just reported):

1. A same-scope redefinition (legal Python) crashed the **entire**
   indexing run with a `sqlite3.IntegrityError`, not just that file.
   Fixed with per-file SQLite savepoints (so one bad file rolls back and
   is skipped without losing other files' work) plus `INSERT OR REPLACE`
   for the expected redefinition case.
2. A call made only inside a nested function was also wrongly attributed
   to every enclosing function (`ast.walk()` doesn't stop at nested
   scope boundaries), inflating the call graph with edges that don't
   correspond to real calls. Fixed by walking only each function's own
   body, explicitly not descending into nested `def`/`class`.

One UX bug was also found and fixed: `add_note` on an ambiguous or
misspelled symbol name silently stored the note as repo-wide instead of
against the intended symbol, with no warning — a note nobody can find
again is worse than no note. It now refuses and reports the ambiguity
instead.

## Performance

| Change | Before | After | Measured on |
|---|---|---|---|
| Batch pass-2 call-edge inserts (`executemany` vs. one `execute()` per call) | 7.0s cold index | 5.6s cold index (-20%) | stdlib, 635 files |
| Indexed, layer-by-layer BFS for `path_between` (vs. loading all edges every call) | ~100ms per symbol-level query | sub-ms for realistic (non-hub) queries | stdlib, 44.6K edges |
| Fast-fail guard on hub-to-hub `path_between` queries | ~170ms exploring before giving up | ~67ms, clear reason returned | stdlib, module-to-module query |
| WAL + `synchronous=NORMAL` | — | enables concurrent read during indexing; minor write speedup | — |

Re-profiling after the fixes confirms `sqlite3.Connection.execute()`
calls dropped 7.5x (28,482 → 3,817) and its share of runtime dropped
accordingly; the remaining cost is now dominated by `ast.compile()` and
the stdlib `ast` module's own traversal overhead — a hard floor for a
pure-Python-`ast`-based parser, not something further query/DB tuning
can improve. The honest next lever for speed, if indexing very large
repos becomes a real pain point, is a faster parser (tree-sitter), not
more SQLite tuning.

Incremental re-index (the common case once a repo is indexed once) is
~0.15–0.2s regardless of repo size, using content hashing (SHA-256, not
mtime) so a `touch` or `git checkout` that doesn't change content
correctly does not trigger a reparse.

## Accuracy: import-aware call resolution

Call resolution now tracks each file's `import`/`from...import` bindings
(including relative imports) and tries them before falling back to
bare-name matching, and can also now confidently classify a call as
external (rather than guessing or giving up) when its binding points at
a module that isn't part of the repo. Measured on the stdlib corpus:

| | Before | After |
|---|---|---|
| Resolved | 18,019 | 19,418 (+2,996 via a binding) |
| Ambiguous (skipped) | 18,214 (42.5%) | 17,145 (40.0%) |
| External | 6,648 | 6,318 (1,276 now confidently classified, not guessed) |

Smaller than the headline "42% ambiguous" number suggested it could be —
checking why (categorizing what's still ambiguous, not just reporting
the new total) found that only ~17% of remaining ambiguous calls are
`self.method()`, which needs knowing self's *type*, not its import; the
rest are calls on arbitrary variables/expressions, deep attribute chains
(`a.b.c()` — only the immediate call target is captured, not the full
chain), or `from x import *` (left unresolved on purpose, not guessed).
None of that is fixable with more import tracking. The real next lever
is type inference for `self`/instance calls, which is a materially
bigger feature and probably not worth it for a tool meant to stay simple
and fast — noted here rather than built.

(These particular numbers are superseded by the attribute-call fixes
below — see "Real-repo validation: webapp" for the current stdlib
totals. Kept here because the *shape* of the finding — checking why
before declaring victory on a headline number — still applies.)

## Two more fixes from continued testing

1. **Sibling-directory imports.** Import resolution assumed proper
   package structure (repo-root-relative or genuinely relative), so a
   flat `src/codegraph/`-style directory of files importing each other via
   Python's runtime `sys.path` — not a real package — fell through to
   the less-precise legacy resolver. This tool's own `src/codegraph/` layout
   is exactly that pattern, caught by self-indexing. Now tried as a
   fallback candidate. Verified: cross-file calls between this project's
   own `cg_query.py` → `query_lib.py` / `graph_lib.py` now resolve via
   import binding (0 → 20 such calls on this small codebase) instead of
   falling back to name search.

2. **Builtin calls could be hijacked by an unrelated same-named repo
   function** — found by the same self-index check: this repo's own
   `tests/edge_repo/shadow_builtin.py` test fixture defines a function
   named `print`, which was silently swallowing every genuine `print()`
   call anywhere else in the repo. Root cause: the legacy resolver
   checked "is there exactly one same-named function anywhere in the
   repo" *before* checking "is this name a Python builtin" — but builtin
   shadowing is per-module in real Python, not per-repo, so that match
   was never valid. This bug existed since the original version, not
   introduced later; it just needed a real name collision at scale to
   surface. Fixed by checking builtins first (same-file matches still
   correctly win even over a builtin — a module that really does
   redefine `print` shadows it for calls inside that same module, which
   is correct Python scoping). **Measured impact on stdlib: 4,186 of
   42,881 calls were being force-matched or marked ambiguous when they
   were actually genuine builtin calls** — now correctly recognized and
   skipped. Not a small edge case for a codebase this size.

Both are covered by the same self-index sanity check going forward, and
the edge-case regression suite still passes with no changes needed.

## TypeScript / JavaScript support (tree-sitter)

Added and validated against a real the company's repo (`webapp`), which
turned out to be TS/JS, not Python. `src/codegraph/ts_parser.py` parses
`.ts`/`.tsx`/`.js`/`.jsx` via tree-sitter and produces the same
`ParseResult`/`PendingCall`/`ImportBinding` shapes as the Python parser,
so pass-2 resolution, the query layer, and the MCP server are unchanged —
one graph, two parsers dispatched by file extension.

Scope, stated explicitly:
- Named definitions only (function declarations, class declarations,
  methods, named `const x = () => {}` / class-field arrows). Anonymous
  callbacks have no name to build a symbol around, same treatment as
  Python lambdas: their calls attribute to the nearest enclosing named
  function.
- ESM imports only (`import`/`export`); `require()` is not handled — a
  real check on webapp found zero `require()` usage, so this
  wasn't worth building for v1.
- `tsconfig.json` `compilerOptions.paths` aliases (e.g. `@/*` → `src/*`)
  are resolved — 281 files in webapp use `@/` imports, so skipping
  this would have made import-aware resolution nearly useless there.

## `.vue` Single File Component support

Added after the initial TS/JS pass, since webapp turned out to be
majority Vue (~726 `.vue` files). `ts_parser.parse_vue_file()` extracts
the `<script>`/`<script setup>` block(s) (via regex — `<template>` and
`<style>` don't need a real HTML parser here, just their boundaries),
picks the TS/JS grammar per-block from that block's own `lang`
attribute, and left-pads the extracted source with the same number of
newlines that preceded it in the real file so tree-sitter's row numbers
land on the correct line of the *original* `.vue` file with no separate
offset bookkeeping. A `.vue` file can have both a `<script setup>` and a
plain `<script>` block (common for options like `defineComponent({name:
...})` alongside setup code); both are walked into the same module node.
One malformed block doesn't blank out a sibling block in the same file.

Re-indexing webapp with `.vue` support (3,995 files, up from 3,269):

| | TS/JS only | + `.vue` (script only) |
|---|---|---|
| Files (skipped) | 3,269 (4) | 3,995 (5) |
| Cold index | ~10s | ~14s |
| Nodes / edges | 12,563 / 67,634 | 16,058 / 83,224 |
| Calls resolved | 7,268 | 9,000 |
| Ambiguous (skipped) | 1,097 | 2,106 |
| External/unknown | 33,511 | 38,628 |

The one new skipped file (`SettingsTable.vue`) and the 4 pre-existing
`.ts` skips were all root-caused (not left as "some files fail,
unclear why"), and all trace to the SAME thing: the pinned
tree-sitter-typescript grammar (kept at an older version deliberately —
see Performance/requirements notes on the `tree_sitter`/
`tree_sitter_languages` API breakage) doesn't parse a few newer/edge-case
TypeScript constructs:
- Inline `import("module").Type` type-query expressions used directly in
  a type annotation (3 files) — e.g.
  `testLibraryObjectIds: import("mongoose").Types.ObjectId[]`.
- `new` used as a plain property key in an object type literal (1 file)
  — `{ old: any; new: any }[]` — the grammar expects `new` to start a
  construct signature, not be an ordinary key.
- A regex literal the grammar's regex/divide disambiguation misreads (1
  file) — `/<!--CODE_FENCE_(\d+)-->/g`.
- Labeled tuple types inside a generic type argument (1 file, the new
  `.vue` one) — Vue 3.3+'s typed-emits syntax,
  `defineEmits<{ "x": [type: A, field: string] }>()`.

All four are real gaps tied to the grammar version, not to this tool's
own parsing logic — upgrading `tree-sitter`/`tree_sitter_languages`
would likely fix them but reintroduces the API breakage already worked
around (see Performance section), so left as a stated, diagnosed
limitation rather than "fixed" by an upgrade that trades one problem for
another. 5 of 3,995 files (0.13%) is the honest error rate either way.

## `.vue` `<template>` parsing

Added after the `.vue` script-only pass, specifically because that pass's
own stated limitation ("a component method is usually called FROM the
template, not from other script code, so `impacted_by` will under-report
real usage") turned out to be the single biggest gap for this repo — this
closes it rather than just documenting it.

`_template_pending_calls()` in `ts_parser.py` extracts every `{{ expr }}`
interpolation and every directive/bound-attribute value (`@click="save"`,
`:prop="expr"`, `v-if="cond"`, `v-for="x in items"`, ...) from the
`<template>` block, parses each as an isolated JS fragment, and records a
call from a synthetic per-file `<file>.vue::<template>` node to whatever
it references — including the Vue-specific case of a bare handler with no
explicit call (`@click="save"` calls `save` implicitly; `@click="save()"`
is the explicit form, both handled). It's deliberately best-effort, not a
real Vue template compiler: a fragment that isn't quite standalone JS
(`v-slot="{ item }"`) is silently skipped rather than failing the whole
file — real templates have dozens of these fragments, and one
non-standard one must not blank out everything else found in the same
file. `<style>` is still never parsed.

**Verified end-to-end on the real repo, not just the synthetic fixture**:
`cg_query.py impacted-by` on
`PromptQuestionsType.vue::openDeleteModal` now correctly returns
`PromptQuestionsType.vue::<template>` as a caller — exactly the case that
was silently invisible before. Real resolved call examples from
webapp: `App.vue::<template>` → `App.vue::handleTrialModalClose`,
`AiActionModal.vue::<template>` → `AiActionModal.vue::keepAddAssessmentQuestion`.
725 of the repo's `.vue` files got a `<template>` node; 2,499 template
call sites resolved to a real same-file method, 7,935 resolved to
external/unknown (composables, global directives, filters, or genuinely
unresolvable expressions).

**Two real bugs found by measuring the effect on webapp rather than
assuming the extraction was safe once it ran without crashing:**

1. **Template-sourced bare calls initially used the same weak
   cross-file "unique name" fallback as an ordinary script bare call,
   and that heuristic is a much worse bet for Vue specifically.** A
   bare script call to a genuinely global function is a reasonable
   candidate for "if there's exactly one repo-wide function with this
   name, it's probably that one." A bare template reference like
   `@click="close"` is implicitly scoped to the component instance, but
   nothing in the extraction could tell the resolver that — and Vue
   component method names (`close`, `cancel`, `onSubmit`, `save`, ...)
   repeat across hundreds of components in a way ordinary global
   function names mostly don't. Measured: turning on template calls
   with no other change pushed ambiguous from 2,106 to **8,322**.
   Fixed by adding a `no_cross_file_guess` flag to `PendingCall`, set
   by every template-sourced call in `ts_parser.py`: same-file match
   and import-binding resolution (e.g. a `<script setup>`-imported
   composable called directly from the template) are still tried
   first, exactly as before, but the risky cross-file guess is skipped
   — the same treatment attribute calls already got. With the fix,
   ambiguous only rose from 2,106 to 2,295 (a small, honest increase
   from real new ambiguous template calls, not a heuristic failure),
   while resolved rose from 9,000 to 11,499 — a real, trustworthy gain.
2. **A latent, previously-unnoticed cross-language leak: the Python
   builtin-shadowing check applied to every language, not just
   Python.** `BUILTIN_NAMES` is `dir(builtins)` — a TS/JS/Vue function
   named `sum`, `map`, `list`, `filter`, or `type` (all real Python
   builtins, all plausible JS/TS names) would be silently dropped with
   no edge at all if it had no same-file caller and no import binding,
   as if it were a bare `print()`/`len()` call. Found by inspection
   while adding the flag above (not a real-world repro this time), and
   gated the check on `pc.file.endswith(".py")` — Python's own
   resolution is completely unaffected (confirmed: stdlib numbers are
   byte-identical before and after this change).

## Real-repo validation: webapp

(Numbers below are from the TS/JS-only pass, before `.vue` support was
added — kept for the resolution-mix finding, which still holds. See
"`.vue` Single File Component support" and "`.vue` `<template>` parsing"
above for current totals including `.vue`, and for the now-complete,
root-caused list of all 5 skipped files across the whole repo.)

Pointed at a real, large, messy production repo
(`webapp`, 3,269 `.ts`/`.tsx`/`.js` files — Vue components excluded,
see above) rather than only a clean stdlib corpus.

| | Value |
|---|---|
| Cold index | 9.8–10.5s for 3,269 files |
| Files skipped (parse errors) | 4 (0.12%) |
| Nodes / edges | 12,563 / 67,634 |
| Calls resolved | 7,268 (145 via import binding, rest via same-file match) |
| Calls ambiguous (skipped) | 1,097 |
| Calls external/unknown | 33,511 (5,462 confidently external via import binding) |

Only 145 calls resolved via import binding (vs. ~3,000 on the Python
stdlib) because most calls in a typical React/Node handler are
attribute/method calls on local variables and parameters (`req`, `res`,
`db`, `conn.foo()`) — import tracking only helps when the call target
itself was imported, which is common in Python's `from x import y` style
but less common in idiomatic TS/JS. This is an honest reflection of what
AST + import-tracking (no type checker) can do, not a bug: fully
resolving those calls would need real TypeScript type information
(effectively re-implementing `tsserver`), which is out of scope for a
tool meant to stay simple and fast.

Of the 4 skipped files, all are now root-caused (see the `.vue` section
above) — 2 turned out to share the same cause (`import("mod").Type`
inline type queries the grammar doesn't accept in this position).

### A real bug this test surfaced, that the stdlib test couldn't

An Express-style handler's `res.json(...)` (in
`api/v1/health/index.ts::healthHandler`) was matching an unrelated test
helper's coincidentally same-named method,
`test/middleware/_helpers.ts::mockRes.json`. Two compounding causes,
both fixed:

1. **Attribute calls with no same-file match and no import binding were
   still guessed via a repo-wide "exactly one same-named
   function/method" search.** This is the same class of bug as the
   builtin-hijacking fix below, but worse for JS/TS: Python has an
   enumerable builtin list to guard against; common library/DOM method
   names (`.json()`, `.then()`, `.map()`, `.on()`) have no such list.
   Fixed in `cg_index.py`: any attribute/method call with no same-file
   match and no import binding is now classified `external`/unknown,
   never guessed cross-file — whether there's one repo-wide candidate or
   several.
2. **Chained calls lost their "this is an attribute call" signal
   entirely.** `res.status(503).json(...)` — the receiver of `.json()`
   is itself a call expression (`res.status(503)`), not a simple name.
   Both `_call_target()` functions (`graph_lib.py` for Python,
   `ts_parser.py` for TS/JS) returned `base_name=None` for this case,
   which is indistinguishable from a genuine bare/global call like
   `foo()` — so it fell into the *bare-call* fallback path (fix #1 above
   doesn't apply to bare calls) and could still be force-matched via the
   "exactly one repo-wide candidate" guess. Fixed by returning a
   sentinel (`base_name = "<complex>"`, not a legal identifier so it can
   never coincidentally match a real import binding) instead of `None`
   whenever there IS a receiver but its identity isn't a simple name —
   this correctly routes the call through the safer attribute-call path.
   The same fix applies to Python (`get_conn().execute()`,
   `self.x.y()`), found by inspection once the TS case was understood,
   not by a second independent real-world repro.

Verified via `SELECT src,dst FROM edges WHERE dst LIKE
'test/middleware/_helpers.ts::%'` after the fix: all 122 remaining edges
into that test-helper file originate from other files under `test/`
calling the genuinely-bare `mockReq()` — no more false cross-directory
matches.

Measured effect of both fixes together, on webapp: ambiguous calls
dropped from 2,784 to 1,097 (most "ambiguous" calls were actually
attribute calls that should never have been candidates for a guess in
the first place) and external/unknown rose from 27,328 to 33,511 —
i.e. a large share of what looked like "resolved" or "ambiguous" before
was actually "we don't know, and shouldn't pretend to." On the stdlib
corpus the same two fixes dropped ambiguous from 17,145 to 620 (of
39,362) and raised external from 6,318 to 23,417, with resolved settling
at 15,325 (2,993 via import binding) — a large, deliberate trade of
false precision for honesty: the tool now says "unknown" far more often
instead of quietly guessing.

## Four more fixes: dangling edges, Vue templates, TS/JS closures, self/type inference

Worked through the "Known limitations" list in SKILL.md, in increasing
order of complexity/risk, each verified with concrete before/after
evidence (not assumed) the same way as every fix above:

1. **Dangling edges after a referenced file is deleted.** A `calls` edge
   pointing at a since-deleted file's node used to sit unresolved until
   the CALLING file's own next change. Now downgraded to `calls_external`
   immediately, at delete time, in the same run.
2. **Vue template attribute extraction gap.** `#default="{ row }"` /
   `#empty` slot shorthand and `:[dynamicProp]="expr"` /
   `@[dynamicEvent]="expr"` dynamic-argument bindings were entirely
   unmatched by the attribute regex (a regex-match failure, not the parse
   failure the old docs claimed — most JS constructs in a template
   expression actually parse fine, verified directly). Fixed and
   regression-tested (`tests/edge_repo_ts/src/comp/SlotShorthand.vue`).
3. **TS/JS anonymous callbacks and `require()`.** A callback
   (`useEffect(() => { doWork() })`) with a real call inside it now gets
   its own synthetic `<closure:LINE>` graph node (skipped entirely, no
   bloat, if it has no direct call), reachable from `impacted_by` via both
   a `defines` and a `calls` edge from the enclosing function. `require()`
   (plain, destructured, destructured-with-rename) now resolves exactly
   like ESM `import`. Building this also surfaced two latent bugs: the
   pinned tree-sitter grammar names anonymous AND named function
   expressions `"function"`, not `"function_expression"`, so
   `const x = function() {...}` was never tracked; and a concise-body
   arrow (`x => helper(x)`, whose body IS the call expression directly)
   had its call silently dropped for every named arrow function too, not
   just callbacks. Verified on `tests/edge_repo_ts/src/closures.ts`.
4. **`self`/instance-method calls, via lightweight type inference** (the
   item flagged in the previous "Status / next steps" as the single
   largest remaining accuracy lever). Scope, deliberately narrow — no
   control-flow or reassignment tracking, last write wins: `self`/`this`
   direct calls now resolve through the owning class's actual (single-
   chain, non-C3) MRO instead of a same-file "any method with this simple
   name" guess; `self.<attr> = Class(...)` (Python), a TS field type
   annotation, and a TS constructor parameter property
   (`constructor(private repo: Repo)`) all infer `self.<attr>`'s/
   `this.<attr>`'s class so `self.repo.save()` resolves to `Repo.save`;
   and a local `x = Class(...)` gets the same treatment, scoped to that
   one function. When the owning class/type IS known but the method truly
   isn't in its known chain, this resolves to `external` immediately
   rather than falling back to the old guess - a real correctness fix,
   not just an addition: the old guess had no way to tell "the right
   class's method" from "a coincidentally same-named method on an
   unrelated class in the same file". Verified on dedicated fixtures
   (`tests/edge_repo/self_resolution.py`,
   `tests/edge_repo_ts/src/selfResolution.ts`) covering exactly that
   collision, cross-file inheritance, and both attribute- and
   local-variable-typed calls, each confirmed against a
   feature-disabled baseline. On this tool's own self-index: 18 calls now
   resolve via this mechanism (223 resolved overall, up from 215;
   external down from 476 to 468) — partly from a related bug this
   surfaced and fixed: a bare base-class/type reference bound via
   `from x import Base` was never checked against its import binding at
   all, only a repo-wide "exactly one same-named class" guess, which
   breaks the moment two same-named classes exist anywhere in the repo
   (as this tool's own two `RemoteBase` test fixtures, indexed together,
   immediately did).

## Usage logging: measuring real-world value

Every fix above answers "does the graph get built correctly" - none of
them answer "does having it change what a session actually does day to
day," which was flagged below as the one thing that hasn't been measured
yet. `cg_mcp_server.py` now logs every tool call (via `usage_log.py`) to
`usage.jsonl`, next to `graph.db` - same zero-footprint storage as
everything else, outside the indexed repo.

Per call, it records: timestamp, tool name, arguments (short query/symbol
strings, not full result payloads), latency, and a small per-tool outcome
signal (result count, whether a symbol resolved, whether a path was
found) - enough to see both "is this being reached for" and "is it
finding anything," without duplicating entire graph/notes payloads into a
growing log file. Fails open: a logging problem is caught and swallowed,
never breaking the actual tool call, same philosophy as this project's
existing memory/notes best-effort stance.

`cg_usage.py <repo_root>` reports on it: calls by tool, error count,
found-anything rate per tool, top-10 most-queried symbols, and
per-tool latency (avg/median/p95); `--json` for machine-readable output.

Verified end-to-end before considering this done, not just "written":
confirmed FastMCP's tool-schema introspection (name, params, docstring)
survives the `@usage_log.logged` wrapper unchanged (the single highest-risk
assumption - if this broke, every tool call would silently fail); exercised
all 9 tools directly against a real indexed DB, covering success,
empty/unresolved results, and a raised exception, and confirmed each
produced the expected JSONL shape (including that the fail-open path
never swallows the actual exception raised to the caller); confirmed
`cg_usage.py` renders a correct report in both text and `--json` mode
from that log, plus its empty-state message when no log exists yet.

Stated limitation, not silently assumed away: this only sees calls made
THROUGH the MCP server. It cannot see whether a session used Grep/Read
instead, for the same question - that comparison needs mining the
session transcripts this tool already indexes (`cg_index.py --sessions`
/ `search_sessions`), which do capture every tool call including
Read/Grep, not this log alone. `cg_usage.py`'s report says this
explicitly.

## Real-repo validation #2: webapp, and the top-level call bug

Re-tested against webapp's current corpus (4,113 indexed files, up
from 3,995). It held up on scale and on precision, and surfaced one real
accuracy bug that every prior test had missed.

| | Before | After |
|---|---|---|
| Cold index | 30.1s, 59,432 nodes / 379,378 edges | 30.2s, 59,432 nodes / **417,052** edges |
| Incremental no-op | 1.4s | 1.4s |
| Call sites seen | 264,335 | **302,038** (+37,703) |
| Resolved `calls` | 40,295 | **41,781** |
| `search` / `neighbors` / `impacted_by` | 6.6ms / 0.10ms / 173ms | 7.0ms / 0.09ms / 166ms |

**The bug: calls at module scope were never recorded.** Pending calls were
only ever collected while visiting a function/method/closure node, so a
call written outside any function - which runs at import time, and is
where almost all composable wiring lives in a composition-API codebase -
produced no edge at all. Every one of the 12,447 module-level `calls`
edges in the old graph pointed at a *closure*; not one pointed at a real
function. Confirmed with a three-line repro and present in Python too
(`x = foo()` at module scope).

Found by diffing `useNavModel`'s callers against grep: the graph
returned 4 of 5. The miss was `const { isTrial } = useNavModel()` at
the top level of a `<script setup>` block. Scale, measured by walking the
tree-sitter AST of all 4,070 parseable files: **38,302 top-level call
expressions, 13.0% of all 294,319 call expressions in the repo**, invisible.

Fixed in `graph_lib.parse_file` (hand the `Module` node to the existing
`_direct_calls`, which already stops at nested def/class boundaries) and
`ts_parser._record_module_level_calls` (same trick on the root node),
wired into `parse_file` and, per script block, `parse_vue_file`. Calls are
attributed to the file's module node, which already existed.

Verified against grep on four symbols after the fix - `useNavModel`
5/5, `useBadgeState` 3/3, `useTeamSkills` 24/24,
`useGridPersistence` 13/13, **zero misses and zero false positives in
all four**. Module-level call edges now resolve to 1,307 functions and 179
methods, where before they resolved to none. Indexing time did not move.

Two smaller fixes shipped alongside:

- `neighbors` capped each direction at 30 rows and said nothing about it,
  so "find every caller" of a symbol with 57 incoming edges returned 30
  and looked complete. It now always returns `incoming_total` /
  `outgoing_total` and a `truncated` flag, and `limit` is exposed on both
  the CLI and the MCP tool.
- Nothing else changed in resolution semantics: `Helper().assist()` at
  module scope records the constructor and leaves `.assist()` external,
  because return-type inference is still deliberately out of scope.

### Known limitations this test confirmed, and did NOT fix

- **5 files are dropped as "syntax error" but are valid TypeScript.** The
  pinned tree-sitter grammar can't parse a regex literal beginning `<!--`,
  `import("...").Type`, or `new:` used as a type-literal key. The message
  blames the repo's code for a parser limitation, and those files are
  re-parsed on every incremental run because they never get a hash stored.
  Investigated below ("Grammar packs") - the obvious fix makes it worse.
- **The self/type inference is inapplicable here, not insufficient.** It
  resolved 3 calls across 4,113 files, because the repo contains 15
  classes. On a composition-API codebase there is almost nothing for a
  class-and-`this`-based inference pass to bite on. That is worth knowing
  before investing in deeper inference.
- ~~**`impacted_by` loads every call/import edge on each query**~~ - fixed,
  see below.
- A literal `<script>` inside an HTML comment in a `.vue` file confuses
  block extraction (found while writing a fixture, not in real code).

## `impacted_by`: indexed BFS instead of a full-table load

`impacted_by` built a complete reverse-adjacency map on every call -
`SELECT src, dst FROM edges WHERE type IN ('calls','calls_external','imports')`,
every such edge in the repo, materialised into a Python dict - then spent
one further `SELECT` per result resolving node info. Cost was flat at
~166ms on webapp's 417K-edge graph no matter how small the answer
was. `path_between` was given a frontier-scoped, layer-by-layer BFS for
exactly this reason; `impacted_by` never got the same treatment.

Now it walks layer by layer through the `dst` index, chunked under
SQLite's parameter limit, and resolves node info for the whole result set
in one batched query. Measured on webapp:

| Query | Before | After | Speedup |
|---|---|---|---|
| `useNavModel` (153 results) | 166.5ms | **0.4ms** | 422x |
| `useApiClient.apiRequest` (1,706 results, hub) | 166.2ms | **6.7ms** | 25x |
| A symbol with no dependents | 169.3ms | **<0.1ms** | ~10,000x |

Results are identical, not merely similar: a parity sweep of **1,200
queries** (400 randomly sampled symbols x depths 1, 3 and 5) against the
old algorithm on the real graph found **0 mismatches**, averaging 0.05ms
per query on the new path. Ordering is now deterministic too - the old
version iterated a `set`, so equal-depth results came back in arbitrary
order between runs.

## Grammar packs: the migration that measurement rejected

`tree_sitter_languages` is abandoned at 1.10.2, pinned to `tree-sitter`
0.21.3, with no wheels past cp312. That caps what Python versions can have
TS support at all, and it was the suspected cause of the 5 valid-TypeScript
files being dropped as "syntax errors". The obvious move was to migrate to
`tree-sitter-language-pack`, the maintained successor (same `get_parser`
API, same language names, 372 grammars, Python 3.10+).

Measured on webapp before committing to it:

| | `tree_sitter_languages` 1.10.2 | `tree-sitter-language-pack` |
|---|---|---|
| Files skipped as parse errors | **5** | **10** |
| Of the old 5, now parsing | — | 3 recovered |
| Previously-fine files broken | — | 8 |
| Cold index | 30s | 44s |

It fixes a regex literal beginning `<!--`, `import("mod").Type`, and `new:`
as a type-literal key - and breaks `importOriginal<typeof import("mod")>()`
plus 7 other test files that the old grammar handled. Reproduced
identically on language-pack 0.9.0, 1.0.0 and 1.21.0, so this is a
grammar-lineage difference rather than a recent regression.

**So the migration was not made.** Straight-swapping would have traded 5
broken files for 10 and slowed indexing by 48%. What shipped instead:

- `ts_parser` now tries a *list* of backends (`_PARSER_BACKENDS`), older
  pack first, and reports every one it tried when none load. One is only
  ever installed at a time - they pin incompatible `tree-sitter` releases.
- `requirements.txt` splits by environment marker: the older pack on
  <= 3.12, `tree-sitter-language-pack` on 3.13+ where nothing else
  installs. Python 3.13 users get TS support for the first time, slightly
  worse on ~0.2% of files, which beats having none.
- CI's full-suite matrix now includes 3.13, so the second backend is
  actually exercised, and the "tree-sitter OK" step prints which pack
  resolved.

The backend order is pinned by a test, so flipping it later is a
deliberate edit that has to come with a fresh measurement.

Two files still fail under *both* packs, and both are valid TypeScript:
`import("mongoose").Types.ObjectId[]` (multi-level import type) and a
labeled tuple member inside `defineEmits<{...}>`. Those are upstream
grammar gaps with no workaround available here.

## Bounding what the tools return

A codebase review turned up three issues that are about cost rather than
correctness, all measured on webapp.

**`impacted_by` had no output limit.** Every other MCP tool caps its
results - `search` 15, `neighbors` 30, `search_sessions` 10 - but
`impacted_by` returned everything, as full node dicts. That inverts the
entire premise of the tool: it exists to cost less context than
grep-and-read, and one call could cost more than a session has. It is also
the tool most likely to be aimed at a hub, since "what breaks if I change
this" gets asked precisely about widely-used code.

It is now capped (default 100), ordered NEAREST FIRST so the dependents
that break first are the ones you keep, and always reports `total` and
`truncated` so a partial answer can never look complete.

**Results were 56% redundant.** A node id is already `file::qualname`, yet
every result also shipped `file`, `qualname` and `name` - the same strings
again. `impacted_by` results are now `{id, kind, lineno, depth}`.
`neighbors` and `search` were left alone: they cap at 30 and 15, so the
same change there saves single-digit KB and is not worth the churn.

| Query | Before | After |
|---|---|---|
| `useNavModel` (153 impacted) | 42.2 KB / ~11K tokens | **14.4 KB / ~3.7K tokens** |
| `useApiClient.apiRequest` (1,706 impacted) | 452.5 KB / ~116K tokens | **12.9 KB / ~3.3K tokens** |

Compaction alone (uncapped) takes the hub query from 452 KB to 222 KB; the
cap does the rest, without hiding anything, because `total: 1706` and
`truncated: true` come back with it.

**Files that failed to parse were re-parsed on every run, forever.** They
never earned a row in `file_hashes`, so they always looked changed. A new
`failed_files` table records them by CONTENT hash, so a repaired file is
picked up automatically on the next run and `--force` clears the table
outright (the escape hatch for "the parser got better"). It is deliberately
separate from `file_hashes` so a failed file never looks successfully
indexed to anything else reading that table.

| | Before | After |
|---|---|---|
| Incremental no-op on webapp | 5 re-parsed, 1.4s | **0 re-parsed, 0.70s** |

The resulting graph is byte-for-byte unchanged across all three fixes:
59,432 nodes / 417,052 edges / 83,926 `calls` before and after.

### Found in the same review, deliberately not fixed yet

- ~~**Ambiguous call sites are dropped with no trace.**~~ - investigated and
  fixed, see "Chasing the ambiguity" below. The answer was not what the
  framing suggested.

## Ranking `search`

`search` matched with `LIKE` and ordered by `(name = ?) DESC` and then
whatever SQLite happened to yield, capped at 15 with no indication there
was more. On webapp `search("assessment")` matched **1,953 nodes and
returned 15 module nodes** - route files like
`handlers/v1/assessment/[assessmentId]/.../get.ts`, whose basenames don't
contain the term at all; they matched only because it appears in the
directory path. Not one function or method surfaced. This is the tool the
MCP docstring tells a session to reach for *first*, so a bad 15 is
expensive.

Ranking is now two keys, applied in SQL so the `limit` slices the best
matches instead of whatever the scan reached first:

1. **match tier** - exact symbol name, then name prefix, then name
   substring, then a qualname/path-only hit, then nodes found solely
   through their notes.
2. **kind weight** - function/method, class, template, closure, module.
   You are usually looking for the symbol, not the file whose path happens
   to contain the word. Modules rank last *within* a tier, so searching a
   module by its exact name still wins on tier.

`length(qualname)` then `id` break remaining ties, so results are stable
between runs. Tiers are computed case-insensitively because `LIKE` already
is - otherwise "Assessment" would silently drop out of the exact tier.

| Query | Before (top 15) | After (top 15) |
|---|---|---|
| `assessment` (1,953 matches) | 15 modules, 0 functions | 7 functions, 2 methods, 6 modules |
| `save` (122 matches) | modules mixed in | 10 functions, 5 methods, 0 modules |
| `useApiClient` (4 matches) | test file's module ranked first | the function itself ranked first |

`total`, `limit` and `truncated` now come back on every search, so "15
results" can no longer be mistaken for "15 matches exist".

Cost: the `ORDER BY` means SQLite scans and sorts all matches rather than
stopping at the first 15, plus one `COUNT` for `total`. Measured on the
59K-node graph: **~14ms steady state** even for a term matching 58,382
nodes, and ~180ms on the very first query against a DB whose pages aren't
in the OS cache yet. Worth it for results that are actually useful.

## Dogfooding: three outages found in twenty minutes

Everything above was measured against the graph's internal correctness.
This was the first time the tool was installed the way a user installs it
and pointed at a repo from a Claude Code session. Three things were broken,
none of which any test had ever touched, because every test called
`query_lib` directly and sailed past the delivery surface.

1. **`install.sh` could not install.** It hardcoded `python3`, which on
   stock macOS is 3.9, and the `mcp` SDK needs 3.10+ - so it died with
   "No matching distribution found for mcp" and left a half-install.
   It now finds a 3.10+ interpreter (or takes `$PYTHON`), fails with an
   actionable message if there is none, and prints the per-repo commands
   using that interpreter rather than a bare `python3`.

2. **The MCP server would not start.** `requirements.txt` said
   `mcp>=1.0.0`; a fresh install resolves to mcp 2.3.0, where `FastMCP`
   was renamed `MCPServer`. `claude mcp get codegraph` reported only
   `CONNECTION_CLOSED`. The server now imports either class - the
   decorator and `run()` surfaces are identical.

3. **Every tool call failed once it did start.** mcp 2.x dispatches tool
   calls on worker threads, and the server held a single module-level
   SQLite connection created in the main thread:
   `sqlite3.ProgrammingError: SQLite objects created in a thread can only
   be used in that same thread`. Connections are now per-thread, which is
   cheap and correct given the DB is already in WAL mode.

`tests/test_mcp_server.py` now launches the real server process and speaks
real stdio JSON-RPC - handshake, `tools/list`, and every tool called over
the wire, including a loop that hammers one tool specifically to exercise
the worker-thread dispatch that bug #3 lived in.

The usage log earned its keep immediately: it recorded both failed calls,
with exceptions, before anything else noticed.

### What the tool was then actually used for

Asked "what breaks if I change `resolve_symbol`" against this repo's own
index. `impacted_by` returned 10 symbols, depth-ordered, correctly tracing
`query_lib` -> `cg_mcp_server` -> `cg_query`. The grep control run
alongside it returned **three** call sites and the graph returned **four**
callers; an AST check confirmed the graph was right - the grep had a
sloppy `^file:12` filter that silently also excluded lines 123 and 124.
One data point, not a study, but it is the failure mode the tool exists to
remove.

`neighbors _direct_calls` correctly refused to answer, naming both
`graph_lib._direct_calls` and `ts_parser._direct_calls` rather than
guessing - the ambiguity behaviour working as designed on a real lookup.

## Ranking `search_sessions` (Layer 3)

Using the tool for real immediately exposed the Layer 3 equivalent of the
`search` problem. `search_sessions` ordered by raw FTS5 `rank`, which
treats every chunk alike - but the kinds answer different questions:

- `message` - what was actually said. The usual answer to "why did we do X".
- `action` - a one-line summary of a tool call: a Bash command, a file
  path. Real, but mostly paths.

On this repo's own transcript index `action` chunks are **63% of
everything stored** (231 of 366), so searches for an explanation kept
returning the command that *mentioned* a thing above the sentence that
*explained* it. Observed live on "worker threads".

Relevance still leads; kind is a multiplier on the bm25 score rather than a
sort key ahead of it, so a genuinely on-point action can still win. The
multiplier was chosen by measurement across 12 real queries, counting how
often an action took the top slot and how much of the top 3 it occupied:

| Design | Action first | Actions in top 3 |
|---|---|---|
| rank only (old behaviour) | 7/12 | 21/30 |
| action weight 0.4 | 3/12 | 9/30 |
| **action weight 0.15 (shipped)** | **1/12** | **5/30** |
| kind as a hard primary sort | 1/12 | 4/30 |

0.15 reaches the same practical outcome as making kind an absolute primary
sort, without the absoluteness. The one remaining action-first query
("blast radius") is correct: an action is the only chunk that matches it.

A `kind` filter was added alongside, so "what command did we run" is still
one call away - `kind='action'` - rather than something the ranking has to
be loosened for. The no-FTS5 LIKE fallback has no relevance score at all,
so there kind becomes the primary key, which still beats the previous pure
`id DESC` ordering.

### Smaller thing noticed, not fixed

`cg_usage.py` takes its repo root positionally while every other script
takes `--repo-root`. Minor, but it is the kind of inconsistency that makes
a tool feel unfinished.

## Code review of the session's work

A review of all eight commits (29 files, +3,086/-95) found six issues. Each
was reproduced before being accepted - the review is a lead, not a verdict.
Four were fixed, one documented, one filed.

**Fixed: `search` reported an inflated `total`** (`query_lib.py`). `total`
summed code matches and note matches independently, but the note-only set
excluded only the rows actually RETURNED rather than every node matching by
name. So a node matching both ways that fell below `limit` was counted
twice, and `truncation_note` then promised matches that do not exist -
precisely the dishonesty the truncation signal was added to remove.
Reproduced: 5 nodes named `foo0..foo4`, one note on `foo4`,
`search("foo", limit=2)` reported `total: 6` against a real 5.

The same bug had a second face: a low-ranked CODE match could re-enter
through the note-only tail and jump ahead of better-ranked code matches cut
by the same limit. Both are gone - "note-only" now means "does not match by
name or qualname at all".

**Fixed: failure records outlived their files** (`cg_index.py`). A file
that ONLY ever failed to parse has no `file_hashes` row, so it never enters
the `deleted` set and its `failed_files` row survived deletion
permanently. Purged alongside the other per-file state, plus a sweep for
rows whose file is no longer on disk.

**Fixed: `install.sh` trusted `$PYTHON` blindly.** The override skipped the
`command -v` and 3.10+ checks applied to every other candidate, so a stale
value - a deleted venv is the common case - defeated the entire guard and
the script died later at `pip` with a bare "command not found". It now runs
the same checks and fails with a message naming the offending value.

**Fixed: `_NOTE_ONLY_TIER` was dead** - defined with an explanatory comment
and referenced nowhere, so the next reader would assume tiering applied to
note hits when ordering is positional.

**Documented: per-thread connections are never closed**
(`cg_mcp_server.py`). Bounded rather than leaking - the SDK dispatches
through anyio's thread pool, so the count tops out at the pool size (40 by
default) for the process lifetime. The comment now states that bound
instead of leaving a reader to work it out.

**Filed, then fixed: calls in a CLASS BODY produced no edges** - see the
next section.

One pattern worth naming: the dual-script Vue bug and this `total` bug have
the same signature - the feature's happy path was verified, the interaction
between two limits was not.

## Class-body calls

`class Model: col = field()` produced no edge at all. `_direct_calls` skips
`ClassDef` outright and `_visit_def` only collected calls for functions and
methods, so an entire class body was invisible - on the TS side too, where
property initialisers were lost the same way. That is the shape Django,
SQLAlchemy and Pydantic code is built from: the calls ARE the class body.

Class-body calls are now attributed to the CLASS node, which is where they
run. The nested-scope rule is unchanged - a call inside a method still
belongs to the method, because `_direct_calls` already stops at nested
definitions. Type inference stays function/method-scoped; only call
collection was widened.

Decorators turned out to need nothing: `ast.iter_child_nodes` on a
`FunctionDef` already includes `decorator_list`, so `@validator("col")` was
always attributed to the decorated function. That is also the more useful
attribution - asking what `validator` affects should surface the decorated
function, not the module containing it - so it is now pinned by a test
rather than left as an accident. A bare `@register` with no parentheses is
a reference, not a call, and is still correctly not recorded.

**The measurement is a lesson in picking the right corpus.** On
webapp this fix is worth **one edge**: 417,052 -> 417,053. That repo
has 15 classes in 4,113 files, so it simply cannot exercise the change -
the same reason the self/type inference is inert there.

Measured instead against the Python standard library (1,439 files, 31,825
nodes), where classes are everywhere:

| | |
|---|---|
| Call edges sourced from a class node | **692** |
| of which resolved to an in-repo symbol | **456** |

All 692 were previously missing entirely. The examples are exactly the
expected shape - `enum.auto()` called in an `IntEnum` body, repeated per
member. Indexing time on webapp was unchanged at 30s.

## Chasing the ambiguity: two real bugs and a non-bug

Indexing all twelve the company's repos put marketing-site at a **15% ambiguity
rate** - 9,078 of ~58,000 call sites dropped - against 1-3% everywhere
else. It also resolved only **99** calls via import binding, where
webapp managed 6,392. That second number was the tell.

### Bug 1: the JSONC stripper could not tell a comment from a glob

`load_tsconfig_aliases` stripped comments with three regexes that know
nothing about string literals. TypeScript path aliases ARE globs:

```json
"paths":   { "@/*": ["./*"] }
"include": ["**/*.ts"]
```

`@/*` opens what the regex reads as a block comment and `**/*.ts` closes
it, so the entire `paths` block between them was deleted. `json.loads` then
failed, the exception was swallowed, and the file looked like it had no
aliases at all. Every `@/...` import in a 1,555-file Next.js app went
unresolved.

Other repos escaped by luck, not by design: webapp's tsconfig has
`@/*` but no later `*/` to close the phantom comment. Replaced with a
scanner that tracks string literals, handling comments and trailing commas
in one pass.

### Bug 2: alias targets were never normalised

Next.js writes `"@/*": ["./*"]` - "the repo root". The alias branch of
`_resolve_module_candidates` concatenated without normalising, producing
candidates like `./lib/payload.ts` while every indexed file is keyed
repo-relative as `lib/payload.ts`. The candidate could never match, so the
import stayed unresolved even once the alias parsed. The relative-import
branch had always called `normpath`; the alias branch had not.

| | import-bound resolutions | ambiguous |
|---|---|---|
| marketing-site before | 99 | 9,078 |
| marketing-site after | **2,839** | 9,008 |
| webapp before | 6,392 | 6,687 |
| webapp after | **9,026** | 6,480 |

### The non-bug: ambiguity was mostly correct behaviour

Fixing both barely moved the ambiguity count, so the resolver was
instrumented to report WHICH names were ambiguous. The answer:

| calls | name | candidates |
|---|---|---|
| 7,196 | `t` | 6 |
| 1,132 | `test` | 2 |
| 568 | `render` | 7 |

Three names are **98.8%** of it. `t` is bound 546 times as a LOCAL variable
from `const t = await getTranslations()`; the repo's six functions named
`t` are unrelated scripts and tests. `test` and `render` come from vitest
and @testing-library. Resolving any of them to a repo symbol would have
invented thousands of false edges.

So "skip rather than guess" was right. What was wrong is that the call then
vanished - no edge, nothing in the query layer, so "what does this function
call" silently omitted it. Ambiguous calls are now recorded against a
synthetic `ambiguous:<name>` target carrying the candidate list in its
evidence. The target is never an edge SOURCE, so it cannot bridge two
same-named symbols into each other's blast radius, and reachability
deliberately excludes it. 9,008 calls that were invisible are now visible
without a single false target.

The lesson is the one from the class-body work, again: the headline number
("15% ambiguous") was a symptom of two unrelated parsing bugs, and the
part that looked like the bug turned out to be the correct behaviour.

## Keeping the index fresh automatically

The index is a snapshot and nothing refreshed it, so after a pull or a
branch switch the graph quietly described the old tree. Being confidently
stale is the most likely way this tool misleads, and it depends on the
reader noticing - which is exactly the kind of thing readers do not do.

`src/codegraph/cg_reindex_if_opted_in.sh` runs as a Claude Code `SessionStart`
hook (`startup|resume`), asynchronously so it never blocks a session start.
Measured: 0.79s on an opted-in repo, 0.03s when it decides to do nothing.

The guard is the part that matters. A session can start in ANY directory,
and silently indexing a repo the user never opted into would both surprise
them and write state for a project they never asked about. An index exists
only where `cg_index.py` was run deliberately, so **its presence is the
opt-in signal** - no index, no action, no files created. The script asks
`graph_lib.default_db_path()` where the DB would live rather than
reimplementing `repo_key()`, so the two cannot drift.

It always exits 0: a hook that fails a session start is worse than a stale
index.

Verified before wiring it up - an opted-in repo gained a symbol added
between runs (0 -> 1 nodes), a non-indexed directory created no DB (13
before, 13 after), and a missing path, a junk path and no argument at all
each exited 0.

Known gap: `startup|resume` does not fire mid-session, so a `git pull`
during a long session still leaves the graph stale until the next start.
`~/.claude/rules/codegraph.md` tells the reader to trust the file over the
graph and re-index when they disagree.

## Interning node ids

Node ids are long path strings - `src/view/pages/general/Dash.vue::useNav`
- and every edge stored two of them inline, then both edge indexes stored
them again. On webapp that was the whole database:

| | before |
|---|---|
| edges table | 90 MB |
| idx on src + dst | 60 MB |
| nodes | 11 MB |
| **total** | **193 MB** for 4,113 files |

A 40,000-file monorepo projects from there to about 1.9 GB, which is the
first dimension of this tool that degrades badly rather than gracefully.

Each distinct id is now stored once in `syms` and referenced by integer.
`edges.evidence` went the same way: it held 30 MB of sentences like
`"call at api/install-app.js:10"`, whose path is already encoded in the
edge's source id and whose verb is implied by the edge type and the target
node's kind. Only the line number is stored, and `query_lib._edge_evidence`
rebuilds the sentence on read. Two cases keep a `note` because nothing can
recover them by joining: an ambiguous call's candidate list, and whether an
import was written `import`, `from ... import` or `require()`.

| | before | after |
|---|---|---|
| webapp | 193 MB | **51 MB** |
| all 13 indexed repos | 337 MB | **111 MB** |
| cold index, webapp | 28.2s | **18.3s** |
| `impacted_by` on a hub | 6.7ms | **1.8ms** |
| `search("assessment")` | 14.0ms | 12.9ms |

Indexing and querying both got faster as a side effect - integer keys make
smaller, denser b-trees, and the BFS frontier chunking now passes integers
instead of long strings.

**The public API did not change.** Every function in `query_lib` still
takes and returns id STRINGS; the integers never escape past a handful of
private helpers. That was a deliberate constraint rather than a nicety: it
kept all 185 existing tests valid as the regression check for a change that
touched the storage of every node and edge. The conversion happens at two
boundaries only - `cg_index.insert_edges` on write, `_sym_of`/`_id_of` on
read - so pass-2 resolution, which is the subtle part, was left untouched.

Correctness was checked the same way the module-level work was: graph
versus grep on four composables - `useNavModel` 5/5, `useBadgeState`
3/3, `useTeamSkills` 24/24, `useGridPersistence` 13/13, zero
missed and zero phantom, identical to the pre-change results.

### Migration

There is no in-place upgrade. `graph_lib.SCHEMA_VERSION` is 2, and opening
a version-1 database drops the derived tables and `VACUUM`s, so the next
index run rebuilds from source. Reading an old database with the new code
would not have raised - it would have quietly matched nothing, comparing
integers against stored text - which is the worst failure available to a
tool whose job is answering "what calls this", so the old shape is detected
and discarded rather than read.

`session_notes` and the Layer 3 transcript tables are deliberately left
alone: they are not derived from source, and re-indexing must never cost
someone a note they wrote.

The `VACUUM` matters more than it looks. Dropping tables frees pages but
does not shrink the file, so without it the migration's entire benefit
would have been invisible - the first migrated database still measured
193 MB until the freed 31,848 of 45,476 pages were reclaimed.

## Closing the measurement gap

`usage.jsonl` has recorded every call through the MCP server since it was
written, which answers "is codegraph reached for, and does it find
anything". It has never been able to answer the question that actually
decides whether the tool earns its keep: is it used INSTEAD of Grep,
alongside it, or not at all. The log only ever saw its own side of the
comparison - `usage_log.py`'s docstring has said so since day one, and the
report printed a paragraph admitting it.

`src/codegraph/cg_log_builtin_tool.sh` supplies the other side. As a PostToolUse
hook on `Grep|Glob` it appends one line per built-in search call, and
`cg_usage.py` joins the two into a ratio:

```
Usage vs built-in tools (this repo):
  codegraph     0
  Grep          3
  Glob          1
  -> codegraph answered 0% of lookups in this repo
     (zero. The graph is not being reached for at all - that is the
      result, not a measurement error.)
```

That last line is the point of the whole exercise. Every other number this
project has produced measures whether the graph is CORRECT; this is the
first one that can come back and say the tool is not being used, in a form
that cannot be mistaken for a broken measurement.

Two deliberate limits:

- **Searching is matched wherever it happens, not only in the Grep tool.**
  The first real session measured - api-service, 19 codegraph calls -
  reported "not measured" for the Grep side. The transcript explained why:
  **0 Grep-tool calls, 34 Bash calls, 11 of them searches.** That session
  ran in auto mode, which routes searching through `grep` in the shell, so
  a matcher of `Grep|Glob` was pointed at a door nobody used. `Bash` is
  matched too and classified in the hook.

  A search tool in COMMAND position - start of a line, or after `&&`, `||`
  or `;` - is someone looking for code. After a single `|` it is filtering
  the output of something else (`npx vitest run | grep FAIL`), which
  codegraph was never a candidate for and must not inflate the comparison.
  Because shell `grep` matches line by line, a search inside a loop body
  counts, which a naive start-of-string rule would have missed.
- **It records the tool name and the repo, never the pattern, path or
  results.** These hooks run against work repositories. The question is
  "how often was Grep used"; storing what was searched for would put code,
  and occasionally a credential someone was hunting for, into a log for no
  analytical gain. A test asserts the pattern never reaches the file.
- **It logs only for repos that already have an index**, the same opt-in
  signal the re-index hook uses, and by the same cheap basename test so it
  costs nothing on a hook that fires on every Grep. A session can start
  anywhere; a tool that quietly accumulates records about unrelated
  projects is not one to leave switched on.

The log is global (one file, all repos) because it is appended to on every
Grep and must stay cheap; `cg_usage.py` filters by repo on read. The hook
is optional - with it absent the report says "not measured" rather than
implying nobody greps, which is a different claim.

### Called is not the same as useful

A tool can be reached for constantly and still be worthless, so the report
answers the two questions separately:

```
Is it being used?
  codegraph     0
  Grep          2
  Glob          1
  -> codegraph served 0% of lookups in this repo
     (zero. The graph is not being reached for at all - that is the
      result, not a measurement error.)

Is it useful?
  returned something        4/4 (100%)
  symbol did not resolve    2    (the caller and the graph disagree
                                  about what exists)
  grepped anyway            5 (28% within 60s)
```

Three signals, all from logs that already exist:

- **returned something** - the call came back with results. Calls with no
  notion of finding anything (`add_note`) are excluded rather than counted
  as successes, which would quietly inflate the rate.
- **symbol did not resolve** - reported on its own, because "found
  nothing" and "I cannot identify that symbol" are different failures. The
  first can be a true answer; the second means the caller and the graph
  disagree about what exists.
- **grepped anyway** - a Grep in the same repo within 60 seconds of a
  codegraph call. The sharpest signal available: the session asked the
  graph and went and grepped regardless. It is correlation, not proof - the
  Grep may be about something else - so it is printed as a rate to watch,
  with the window stated, not as a verdict. Over 50% prints a line saying
  the graph is being reached for but is not settling the question.

A Grep BEFORE a call is deliberately not counted: grepping first and then
reaching for the graph is the opposite story and must not be scored as the
graph failing.

### The first real reading

api-service, after one working session:

```
Is it being used?
  codegraph     21
  Bash:search   11
  -> codegraph served 66% of lookups in this repo

Is it useful?
  returned something        16/20 (80%)
  symbol did not resolve    3
  grepped anyway            0 (0% within 60s)
```

Symbols actually queried were `hydrateLookup`, `sanitizeInput`,
`sanitizeDbInput`, `getConfigByKey` - a session orienting in real
code, not a smoke test. `impacted_by` is the number to watch: 60%
found-anything across five blast-radius queries.

What this still cannot show: whether an answer was trusted, or
double-checked by reading the file rather than re-grepping. That needs
mining the session transcripts (`--sessions`), and is left alone until the
numbers above say the tool is being used at all.

## Two findings from an independent review

A reviewer tested the tool against api-service - cross-checking six
symbols' call sites against grep, forcing a re-index, and probing the
import graph - and reported it "trustworthy for symbol-level call tracing,
blind at the module/import level". Both findings were real.

Worth recording first: their grep baseline for `toRecordAlias` missed 4 of 8 call
sites, because TypeScript generics (`toRecordAlias<SessionItem>(...)`) defeat a
naive `\btoRecordAlias(` pattern. codegraph had all 8. The same thing happened
again while fixing finding 1 below - see the httpErrors numbers. Twice now
the hand-written baseline has been the thing that was wrong.

### Finding 1: imports never resolved to in-repo modules

Every `imports` edge targeted `external:<specifier>`, even when the
specifier named a file in this repo - **2,543 edges on api-service, not
one resolved**. Import *bindings* were resolved (that is how cross-file
call resolution works), but the edge itself never was. So
`neighbors(module, direction="in")` returned `incoming_total: 0`
universally, for a util file that 32 other files import, and `impacted_by`
on a module came back empty.

That last part is the one that mattered: a route handler imported by
`api/v1/assessment/router.ts` and wired up by reference showed
`impacted_by -> total: 0`. For a route serving production traffic, a false
negative in impact analysis is worse than no answer.

Parsers now emit a `PendingImport` (candidates, fallback, original
statement text) and pass 2 resolves it against the on-disk set - the same
place, and the same way, calls are resolved.

| | before | after |
|---|---|---|
| imports resolved to an in-repo module | 0 of 2,543 | **719** |
| `neighbors("server/utils/httpErrors.ts", "in")` | 0 | **33** |
| `neighbors("server/lib/dynamodb.ts", "in")` | 0 | **6** |
| `impacted_by(<the fileupload handler module>)` | 0 | **5**, with `router.ts` at depth 1 |

The 33 is one more than the reviewer's grep found, and the extra is real:
`test/server/httpErrors.logFailure.test.ts` does import it - the grep was
scoped to `server` and `handlers` and never looked in `test`.

This also surfaced a bug the change introduced: pointing imports at module
nodes means deleting a file leaves every importer's edge dangling. The
deleted-file path already downgraded `calls`; it now downgrades `imports`
too. Caught by the suite's dangling-edge check, not by inspection.

### Finding 2: a miss could not be told from a stale index

A function written an hour before the test (`sanitizeInput`) and a
symbol typed at random (`totallyFakeSymbolXyz123`) returned byte-identical
output: `{"error": "unresolved", "ambiguous_candidates": []}`. "Does not
exist" and "your index predates it" are very different answers, and the
second is the common one.

Unresolved results now carry an `index` block - when the index was built,
how many files have changed since, and what to do:

```json
{"error": "unresolved", "ambiguous_candidates": [],
 "index": {"indexed_at": "...", "files_changed_since_index": 12,
           "stale": true,
           "hint": "12 file(s) have changed since this index was built -
                    re-index before treating this as 'does not exist'."}}
```

Two deliberate restrictions. It runs only on the miss path, because it
stats the working tree and a successful lookup has no reason to pay for
it. And an AMBIGUOUS result does not get one: ambiguity is a complete
answer - the caller got a list to choose from - and muddying it with a
re-index suggestion would be noise.

**It was initially attached to `neighbors` and `impacted_by` only.** The
same reviewer caught that on the next pass: `search` returned
`{"results": [], "total": 0}` for a symbol written since the last index,
indistinguishable from one that never existed - and `search` is the
documented FIRST stop for "what is X / where does X live". The rule file
then told a reader to check a block that two of the four tools never sent.

Checking the other miss paths found two more the reviewer had not named:
`path_between` (both an unresolved endpoint and a null path - the code
connecting two symbols may simply post-date the index) and a refused
`add_note`. All of them carry it now, a test asserts every tool reports
the same repo state, and the rule file lists them explicitly rather than
saying "results" and leaving the reader to find the exceptions.

`~/.claude/rules/codegraph.md` was updated to match: re-index "after
pulling or switching branches, AND before trusting any unresolved or empty
result", and ask about the MODULE path for anything wired up by import
rather than called.

### Still true after both fixes

A symbol passed by reference (`router.on(..., handler)`) is not a call and
produces no call edge. Asking `impacted_by` about that handler SYMBOL
still returns little; asking about its MODULE now returns the router. That
is a real distinction a user has to know, so it is documented in the tool
descriptions rather than papered over.

## Two bugs from an adversarial pass

A second review went looking for NEW failure modes rather than re-checking
fixed ones, across parts of the repo not previously touched. It confirmed
Vue coverage exactly (81 edges against grep's 80 files, including a call at
line 610 of a 2,236-line SFC) and found two real bugs.

### `new X().method()` resolved to `external:<method>`

`scoring.service.ts` calls
`new ScoringRulesService().consistencyScore(...)`. The
module-level import edge was there; the method call was not. Not ambiguous,
not external-with-a-binding - the method simply looked uncalled:
`neighbors(method, "in")` returned only its own `defines` edge.

The cause is almost embarrassing: the receiver is a `new_expression`
(Python: a `Call` on a Name), not an identifier, so the attribute-call path
had no base to work with and fell through. **The type is written right
there in the expression** - this needed a lookup, not inference. The stored
form (`const s = new X(); s.method()`) always worked, which is exactly why
the gap survived: every test of instance calls used the shape that worked.

`PendingCall.ctor_type` now carries the class name as written, and pass 2
resolves it through the same import-binding/same-file/unique-class lookup
used for base classes - so `helper().run()`, where `helper` is a function,
is not mistaken for a construction. A class that is not in this repo still
produces `external:`.

This also corrected an older test that asserted `Helper().assist()` must
stay external "because it needs return-type inference". That reasoning was
wrong: `Helper()` is a construction with a written type. The case that
genuinely needs return-type inference - `get_helper().assist()`, where the
receiver is a function's result - still stays external, and now has its own
test saying so.

### `path_between` routed through shared `external:` nodes

Every `.includes()` call in a repo collapses to one `external:includes`
node. Asked for the path between a Vue composable and an unrelated scoring
method, the tool returned a confident multi-hop path through it, because
two files that share nothing both happen to call a built-in. The same
applies to `.map`, `.find`, `.filter` - any common method name.

An external node is a SINK: something left the repo there. It can be a
destination; it is never a conduit between two things inside the repo. The
BFS now expands only through symbols that have a `nodes` row. `neighbors`
is untouched - an external call is still a real fact about a function, and
suppressing a bogus path must not cost a true edge.

### Verified on the reported symbols

```
consistencyScore  callers=['scoring.service.ts::computeScore']
confidenceRating  callers=['scoring.service.ts::computeScore']
path(useThemeColor -> consistencyScore)  None (no path within 6 hops)
```

Known and unchanged: `external:` targets have no `nodes` row, so they
cannot be named as a `path_between` endpoint at all. "What calls this
built-in" is a fair question this tool does not answer; a test states that
rather than leaving it to be discovered.

## The standing limitation: one repo, one graph

Everything here is scoped to a single repository, and that is architectural
rather than a gap to be patched. Worth stating exactly where the line falls,
because "cannot see across repos" is not quite right.

**Visible today.** Every cross-repo import lands as an `external:` edge, so
package-level coupling is already answerable by querying the 13 indexes:

| shared package | imports | repos |
|---|---|---|
| `@acme/node-logger` | 262 | 6 |
| `@acme/design-system` | 410 | 1 |
| `@acme/schema/mongoose` | 54 | 2 |
| `@acme/schema/dynamodb` | 9 | 2 |

**Not visible, and this is the real limitation.** What flows THROUGH those
packages. `acme-schema/dynamodb` is a name with no structure behind it -
no table, no key shape, no attribute. Same for job payloads and queue
messages. Two services that must agree on a DynamoDB item shape have no
edge between them at all, and nothing here would notice a change breaking
that agreement.

That is a different kind of graph, not a bigger version of this one. Call
and import edges fall out of the AST for free; a data-contract edge needs a
schema as a source of truth and a way to tie both sides to it. Inferring
one from code would be guessing - the one thing this tool has consistently
refused to do, and the reason its "ambiguous" and "external" results are
worth anything.

So: use it for "who calls this, what imports this, what breaks inside this
repo". For a cross-service contract, it can tell you WHICH repos touch the
shared package and from where - which narrows the search - and nothing
more. Do not read a clean `impacted_by` as evidence that a schema change is
safe.

## Go support

The first language added after Python and TS/Vue. It differs from both in a
way that reaches into pass 2: **an import binds to a DIRECTORY, not a
file.** `scoring.Compute()` may be defined in any `.go` file of the
`internal/scoring` package, so the existing `(file, name)` symbol lookup
cannot resolve it. `load_symbol_maps` now also builds `(package_dir, name)`,
and a package directory counts as a resolvable binding target alongside a
file.

In one respect Go is *easier* than TypeScript: `func (r *Rules) Apply()`
states the receiver's type outright, so instance-method calls resolve from
a declaration rather than the inference TS needed.

A `package` node per directory was added so "what imports this package" has
a subject at all - Go's unit of importability is the package, not the file.

### The bug the real corpus found

Indexing gorilla/mux and cross-checking against grep surfaced a call the
graph could not see: `newRequest(...)` at `route_test.go:141`, inside
`t.Run("get metadata map", func(t *testing.T) { ... })`.

The parser stopped at `func_literal` so a literal's calls would not be
misattributed to the enclosing function - correct - but, unlike the TS
parser, never created a node for the literal. So the calls were not
reattributed. They were **discarded**.

That is not a corner case in Go. `t.Run(name, func(t *testing.T){...})` is
the standard subtest idiom, and `defer func(){}()`, `go func(){}()` and
`http.HandlerFunc(func(w, r){...})` are everywhere. Function literals now
get a `<closure:LINE>` node with both a `defines` and a `calls` edge, the
same treatment the TS parser already gave anonymous callbacks, and a
call-free literal still gets no node.

| gorilla/mux (17 files) | before | after |
|---|---|---|
| calls resolved | 380 | **522** |
| nodes | 278 | **435** |
| parse errors | 0 | 0 |
| ambiguous | 0 | 0 |

### A note on the grep baseline

Four apparent mismatches against grep were checked individually and **all
four were the baseline, not the tool**: two were calls inside comments
(`doc.go` package docs, commented-out lines in `route.go`), one was a
same-file caller my comparison excluded, and one was a regex whose
`(?<![\w.])` guard deliberately skipped the `mux.NewRouter(` form the graph
correctly resolves.

That is the third review in a row where a hand-written grep baseline was
the thing that was wrong. It is a reason to trust the graph over a quick
pattern - and a reason not to report a grep diff as a finding without
opening each case.

### Stated limits

Struct FIELD types are not inferred, so `r.field.Method()` stays external
rather than being guessed. A test asserts that rather than leaving it to be
discovered.

## Status / next steps

v1 scope: Python + TS/JS/TSX + `.vue` (`<script>` AND `<template>`, not
`<style>`), single repo, no doc/PDF linking — tested against synthetic
edge cases in all languages (including `.vue`-specific cases:
`<script setup>`, a plain options-API `<script>`, a template-only file
with no script, a malformed script alongside a sibling that still
indexes, and template call extraction itself: an explicit `{{ call() }}`
and a bare `@click="handler"`), a real 16.9K-node Python corpus (stdlib),
and webapp's full real corpus (3,995 files including `.vue`
templates), with import-aware call resolution and four rounds of
real-bug fixes on top of the original name-based version, all 5 skipped
files root-caused, and `impacted_by` verified end-to-end to now surface
a real component's template as a caller of its own methods (the specific
gap this was built to close).

Since then: dangling edges, the Vue attribute regex gap, TS/JS anonymous
callbacks + `require()`, and lightweight self/instance-method type
inference are all built and verified (see "Four more fixes" above) —
the four items previously flagged as known limitations, done in
increasing order of complexity/risk with before/after evidence for each.

Ranked next steps: (1) actually use this day-to-day against webapp
in real Claude Code sessions and see if `search`/`impacted_by` save time
vs. Grep in practice, now with `usage.jsonl` + `cg_usage.py` in place to
turn that into data instead of impressions — real usage is still the
thing that hasn't happened yet, only the instrumentation to measure it
has; (2) doc/PDF linking only once daily use proves the current version
earns its keep; (3) go beyond this session's deliberately narrow type
inference (multi-level attribute chains like `self.a.b.method()`,
return-type inference, anything needing real control/data-flow analysis)
only if the narrow version proves insufficient in practice — worth a
deliberate decision, not a default next step.
