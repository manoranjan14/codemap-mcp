"""
TypeScript/JavaScript parsing for codegraph, via tree-sitter. Produces the
SAME graph_lib.ParseResult / PendingCall / ImportBinding shapes as the
Python parser, so cg_index.py's pass-2 resolution, the query layer, and
the MCP server all work unchanged regardless of source language.

Scope (v1, deliberately - see SKILL.md):
  - .ts / .tsx / .js / .jsx, plus .vue Single File Components: only the
    <script>/<script setup> block(s) are parsed (extracted and handed to
    the same TS/JS grammar), NOT <template> or <style>. Concretely: a
    method only referenced from the template (`@click="foo"`,
    `{{ bar() }}`) is NOT recorded as called from anywhere - v1 doesn't
    parse the Vue template compiler's AST, only script-block JS/TS. This
    matters a lot in practice, since Vue components' methods are mostly
    invoked FROM the template, not from other script code - see
    parse_vue_file()'s docstring and SKILL.md for the concrete effect on
    impacted_by/neighbors results.
  - Definitions tracked: function declarations, class declarations,
    class methods, and NAMED arrow/function expressions (`const x = ()
    => {...}`, a class field `x = () => {...}`). Anonymous
    arrow/function expressions (inline callbacks, e.g. `useEffect(() =>
    {...})`) get a synthetic `<closure:LINE>` node (kind="closure") IF
    they make at least one direct call - the enclosing function gets
    both a `defines` edge (for display) and a `calls` edge (so
    query_lib.impacted_by, which only traverses calls/calls_external/
    imports, actually reaches through it) to the closure node, and
    nested callbacks chain the same way. A callback with no direct call
    in it (`.map(x => x.id)`) is deliberately skipped - no node created
    - since there's nothing for the graph to resolve through it and
    creating one would just be bloat. This differs from how Python
    lambdas are handled (never tracked at all, no node, no attribution)
    because JS/TS callbacks are used pervasively for real control flow
    (useEffect, promise chains, event handlers) where dropping the call
    entirely was a much bigger accuracy gap.
  - Imports: `import x from 'y'`, `import { a, b as c } from 'y'`,
    `import * as x from 'y'`, and CommonJS `const x = require('y')` /
    `const { a, b: c } = require('y')` bindings (a bare `require('y')`
    with no assignment records the same `imports` edge but, like a
    side-effect-only ESM import, creates no local binding since there's
    no name to bind).
  - Module resolution: relative imports (./ ../) resolved with the
    standard extension/index-file guesses; tsconfig.json
    `compilerOptions.paths` aliases (e.g. `@/*` -> `src/*`) are read
    once per repo and applied - a real check found 281 files in
    webapp use `@/` imports, so skipping alias resolution would
    have made import-aware resolution nearly useless on that repo.
    Bare package imports (react, lodash, ...) are left as plain
    external edges with no binding - resolving node_modules is out of
    scope.
"""
from __future__ import annotations

import json
import os
import re

from . import graph_lib as gl
from .graph_lib import (ImportBinding, ParseResult, PendingCall, PendingBase,
                        PendingAttrType, PendingImport)

# tree-sitter is an OPTIONAL dependency, imported lazily rather than at
# module import time. cg_index.py imports this module unconditionally (it
# dispatches by file extension), so a top-level `from tree_sitter_languages
# import get_parser` made an uninstallable/absent TS toolchain abort
# indexing of a pure-Python repo entirely - even though the Python path
# needs nothing but the stdlib. Now the import only happens when a
# .ts/.tsx/.js/.jsx/.vue file is actually reached, and `available()` lets
# the caller skip those files with one clear warning instead.
_parser_factory = None      # cached get_parser from whichever pack loaded
_parser_backend: str | None = None
_import_error: str | None = None

# Tried in order; the first that imports wins. In practice only one can be
# installed at a time (they pin incompatible `tree-sitter` versions), so
# this is really "whichever the environment has" - requirements.txt picks
# per Python version.
#
# The order is MEASURED, not assumed. tree_sitter_languages is abandoned at
# 1.10.2 and cannot install past cp312, so tree_sitter_language_pack is the
# only option on Python 3.13+. But on a real 4,113-file TypeScript/Vue repo
# the newer pack's grammars are a net REGRESSION: they fix 3 files the old
# one rejected (a regex literal beginning `<!--`, `import("mod").Type`,
# `new:` as a type-literal key) and break 8 that it accepted (notably
# `importOriginal<typeof import("mod")>()`), taking skipped files from 5 to
# 10. Reproduced identically on language-pack 0.9.0, 1.0.0 and 1.21.0, so
# it is a grammar-lineage difference, not a recent bug.
#
# Hence: prefer the older pack where it installs at all, and fall through
# to the newer one where it does not. Re-measure before flipping this.
_PARSER_BACKENDS = ("tree_sitter_languages", "tree_sitter_language_pack")


class TreeSitterUnavailable(RuntimeError):
    """Raised by get_parser() when the optional tree-sitter deps are absent."""


def _load_parser_factory():
    """Import a grammar pack once, caching either the factory or the
    failure. Returns the factory, or None if every candidate failed."""
    global _parser_factory, _parser_backend, _import_error
    if _parser_factory is None and _import_error is None:
        errors = []
        for mod_name in _PARSER_BACKENDS:
            try:
                mod = __import__(mod_name, fromlist=["get_parser"])
                _gp = mod.get_parser
            except Exception as e:  # ImportError, or a binary/ABI mismatch at load
                errors.append(f"{mod_name}: {type(e).__name__}: {e}")
            else:
                _parser_factory, _parser_backend = _gp, mod_name
                break
        if _parser_factory is None:
            _import_error = "; ".join(errors)
    return _parser_factory


def backend() -> str | None:
    """Which grammar pack is actually in use, or None if none loaded. The
    two disagree about which TypeScript is valid, so this is worth being
    able to see and assert on."""
    _load_parser_factory()
    return _parser_backend


def available() -> bool:
    """True if TypeScript/JavaScript/Vue files can be parsed in this
    environment. False means tree-sitter isn't installed (or failed to
    load); callers should skip those files rather than fail the run."""
    return _load_parser_factory() is not None


def unavailable_reason() -> str:
    """Why available() is False - the underlying import error, for a single
    actionable warning instead of a traceback."""
    _load_parser_factory()
    return _import_error or ""


def get_parser(lang: str):
    factory = _load_parser_factory()
    if factory is None:
        raise TreeSitterUnavailable(
            "TypeScript/JavaScript/Vue parsing needs the optional tree-sitter "
            f"dependencies, which could not be imported ({_import_error}). "
            "Install them with: python3 -m pip install -r requirements.txt"
        )
    return factory(lang)

SOURCE_EXTS = (".ts", ".tsx", ".js", ".jsx")
VUE_EXT = ".vue"

_LANG_FOR_EXT = {
    ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "javascript",
}
_RESOLUTION_SUFFIXES = ["", ".ts", ".tsx", ".js", ".jsx", ".vue",
                         "/index.ts", "/index.tsx", "/index.js", "/index.jsx", "/index.vue"]

_NAMED_DEF_TYPES = {"function_declaration", "class_declaration", "method_definition"}


def _strip_jsonc(raw: str) -> str:
    """Remove // and /* */ comments and trailing commas from JSONC, with a
    scanner that knows where string literals start and end.

    This was three regexes, and they could not tell a comment from a glob.
    TypeScript path aliases ARE globs - `"@/*": ["./*"]` next to
    `"include": ["**/*.ts"]` means `@/*` opens what the regex read as a
    block comment and `**/*.ts` closes it, deleting the entire paths block
    in between. json.loads then failed, the exception was swallowed, and
    the file looked like it had no aliases.

    Measured cost on one real Next.js repo: every `@/...` import went
    unresolved, resolution fell back to bare-name matching, and 9,078 call
    sites (15% of the repo) came out ambiguous and were dropped. Repos that
    worked did so by luck - webapp has `@/*` but no later `*/` to
    close the phantom comment."""
    out = []
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c == '"':                      # string literal: copy verbatim
            out.append(c)
            i += 1
            while i < n:
                ch = raw[i]
                out.append(ch)
                i += 1
                if ch == "\\" and i < n:   # escape: the next char is literal
                    out.append(raw[i])
                    i += 1
                elif ch == '"':
                    break
            continue
        if c == "/" and i + 1 < n and raw[i + 1] == "/":
            while i < n and raw[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and raw[i + 1] == "*":
            i += 2
            while i + 1 < n and not (raw[i] == "*" and raw[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if c == ",":                      # drop it only if it is trailing
            j = i + 1
            while j < n and raw[j] in " \t\r\n":
                j += 1
            if j < n and raw[j] in "}]":
                i += 1
                continue
        out.append(c)
        i += 1
    return "".join(out)


def load_tsconfig_aliases(repo_root: str) -> list[tuple[str, str]]:
    """Read compilerOptions.paths from tsconfig.json (JSONC-tolerant: a
    real tsconfig.json commonly has // and /* */ comments, which plain
    json.loads rejects). Returns [(prefix, target)] with trailing '/*'
    stripped, longest prefix first so a more specific alias wins."""
    path = os.path.join(repo_root, "tsconfig.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
        raw = _strip_jsonc(raw)
        data = json.loads(raw)
    except Exception:
        return []  # malformed/unreadable tsconfig - degrade to no aliases, don't crash indexing
    paths = (data.get("compilerOptions") or {}).get("paths") or {}
    out = []
    for prefix, targets in paths.items():
        if not targets:
            continue
        target = targets[0]
        out.append((prefix.rstrip("*"), target.rstrip("*")))
    out.sort(key=lambda pt: -len(pt[0]))  # longest/most-specific prefix first
    return out


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


# Both spellings are checked because they've been observed to differ by
# grammar/version: the pinned tree_sitter_languages build for TS/JS uses
# node.type "function" for a `function` (or `function name() {}`)
# expression - NOT "function_expression" as might be assumed - verified
# directly against the actual parse tree, not assumed (same discipline as
# the rest of this file's grammar-shape comments). Both names are kept
# here so this doesn't silently break again on a different grammar build.
_ANON_FUNC_TYPES = ("arrow_function", "function", "function_expression")


def _named_def(node, src: bytes):
    """Returns (kind, name) if this node is something we track as its own
    graph node, else None. kind is 'function'/'class'/'method', the
    sentinel 'func_or_method' for a named arrow/function expression whose
    real kind (function vs method) depends on nesting depth (same as the
    Python parser's rule), or 'closure' (name is None) for an ANONYMOUS
    arrow/function expression encountered directly - e.g. a callback
    argument (`useEffect(() => {...})`) or an array element - which
    _walk_one gives a synthetic name to. A named arrow/function
    expression's own node is never independently offered to this function
    in the first place (see _walk_one: it's consumed as the `value` of
    its variable_declarator/public_field_definition and only that
    wrapper's body is walked), so the 'closure' branch only ever matches
    a genuinely anonymous one."""
    t = node.type
    if t == "function_declaration":
        n = node.child_by_field_name("name")
        return ("function_or_method", n) if n else None
    if t == "class_declaration":
        n = node.child_by_field_name("name")
        return ("class", n) if n else None
    if t == "method_definition":
        n = node.child_by_field_name("name")
        return ("method", n) if n else None
    if t == "public_field_definition":
        value = node.child_by_field_name("value")
        n = node.child_by_field_name("name")
        if value is not None and value.type in _ANON_FUNC_TYPES and n is not None:
            return ("method", n)
        return None
    if t == "variable_declarator":
        value = node.child_by_field_name("value")
        n = node.child_by_field_name("name")
        if value is not None and value.type in _ANON_FUNC_TYPES \
                and n is not None and n.type == "identifier":
            return ("function_or_method", n)
        return None
    if t in _ANON_FUNC_TYPES:
        return ("closure", None)
    return None


_COMPLEX_BASE = "<complex>"  # sentinel: see graph_lib._call_target for rationale


def _target_from_node(node, src: bytes):
    """Shared core of _call_target: given an identifier/member-expression
    node, extract (base_name, called_name). Factored out so Vue template
    handling can reuse it for a bare event-handler reference (`@click="save"`
    - Vue calls `save` implicitly, there's no call_expression node at all
    to hand to _call_target)."""
    if node is None:
        return None, None
    if node.type == "identifier":
        return None, _text(node, src)
    if node.type == "member_expression":
        prop = node.child_by_field_name("property")
        obj = node.child_by_field_name("object")
        called_name = _text(prop, src) if prop else None
        base_name = _COMPLEX_BASE
        if obj is not None:
            if obj.type == "identifier":
                base_name = _text(obj, src)
            elif obj.type == "this":
                base_name = "this"
        else:
            base_name = None
        return base_name, called_name
    return None, None


def _ctor_type(func_node, src: bytes):
    """The class name in `new Scorer().method()`, or None.

    The receiver is a `new_expression`, not an identifier, so the ordinary
    attribute-call path had no base to work with and the call fell through
    to `external:<method>`. The type is written right there - this needs a
    lookup, not inference."""
    if func_node is None or func_node.type != "member_expression":
        return None
    obj = func_node.child_by_field_name("object")
    if obj is None:
        return None
    # `new Scorer().m()` parses the receiver as a new_expression; with
    # arguments it may be wrapped in parentheses.
    while obj is not None and obj.type == "parenthesized_expression":
        inner = [c for c in obj.named_children]
        obj = inner[0] if inner else None
    if obj is None or obj.type != "new_expression":
        return None
    ctor = obj.child_by_field_name("constructor")
    if ctor is None or ctor.type != "identifier":
        return None
    return _text(ctor, src)


def _this_attr_chain(func_node, src: bytes):
    """If func_node (a call's `function` field) is the two-level chain
    `this.<attr>.<method>` (e.g. `this.repo.save()`), return `<attr>`
    ("repo"); else None. Mirrors graph_lib._self_attr_chain for Python's
    `self.<attr>.<method>()`. The method name itself is already captured as
    `called_name` by _call_target/_target_from_node - this only extracts the
    extra attribute-name context needed to resolve THROUGH `this`'s
    attribute, the same lightweight type inference described in
    PendingAttrType's docstring."""
    if func_node is None or func_node.type != "member_expression":
        return None
    obj = func_node.child_by_field_name("object")
    if obj is None or obj.type != "member_expression":
        return None
    inner_obj = obj.child_by_field_name("object")
    if inner_obj is None or inner_obj.type != "this":
        return None
    prop = obj.child_by_field_name("property")
    return _text(prop, src) if prop is not None else None


def _type_ref(type_annotation_node, src: bytes):
    """Unwrap a `: Foo` / `: mod.Foo` TS type_annotation node into
    (module_alias_or_None, simple_type_name) - the same (base_name, ref_name)
    shape _target_from_node produces for a call/base-class reference, so
    resolution in cg_index.py's pass 2 can treat a class reference and a
    type-annotation reference identically. Returns None for anything that
    isn't a simple/dotted type reference (`number`, `string[]`, a union, a
    generic like `Array<Foo>`, ...) - skipped rather than guessed, verified
    directly against the parse tree (see the empirical shapes this was
    built from: type_identifier for a bare type, nested_type_identifier for
    a dotted one like `some.Thing`, both children of type_annotation
    alongside the anonymous ':' token)."""
    if type_annotation_node is None:
        return None
    inner = next((c for c in type_annotation_node.children if c.type != ":"), None)
    if inner is None:
        return None
    if inner.type == "type_identifier":
        return None, _text(inner, src)
    if inner.type == "nested_type_identifier":
        obj = next((c for c in inner.children if c.type == "identifier"), None)
        prop = next((c for c in inner.children if c.type == "type_identifier"), None)
        if prop is None:
            return None
        return (_text(obj, src) if obj is not None else None), _text(prop, src)
    return None


def _collect_type_assignments(scope_node, src: bytes):
    """TS/JS analog of graph_lib._collect_type_assignments: scan a
    function/method body (stopping at nested defs/closures, same rule as
    _direct_calls) for `this.<attr> = new Class(...)` and local
    `<var> = new Class(...)` / `let <var>: Class` shapes. Returns raw
    (scope_kind, var_name, base_name, ref_name, lineno) tuples - see
    PendingAttrType's docstring for the exact scope. Class field
    declarations and constructor parameter properties are NOT handled here
    (they're structural, not body statements) - see the class_declaration
    handling in _walk_one, which covers those directly from the class body
    once per class rather than per-method."""
    if scope_node is None:
        return []
    out = []
    stack = [scope_node]
    while stack:
        n = stack.pop()
        if _named_def(n, src) is not None:
            continue
        if n.type == "assignment_expression":
            left = n.child_by_field_name("left")
            right = n.child_by_field_name("right")
            if left is not None and left.type == "member_expression" and right is not None \
                    and right.type == "new_expression":
                obj = left.child_by_field_name("object")
                prop = left.child_by_field_name("property")
                if obj is not None and obj.type == "this" and prop is not None:
                    ref = _target_from_node(right.child_by_field_name("constructor"), src)
                    if ref[1]:
                        out.append(("self_attr", _text(prop, src), ref[0], ref[1],
                                    n.start_point[0] + 1))
        elif n.type == "variable_declarator":
            name_node = n.child_by_field_name("name")
            value_node = n.child_by_field_name("value")
            type_node = n.child_by_field_name("type")
            if name_node is not None and name_node.type == "identifier":
                ref = None
                if value_node is not None and value_node.type == "new_expression":
                    ref = _target_from_node(value_node.child_by_field_name("constructor"), src)
                elif type_node is not None:
                    ref = _type_ref(type_node, src)
                if ref and ref[1]:
                    out.append(("local_var", _text(name_node, src), ref[0], ref[1],
                                n.start_point[0] + 1))
            # do NOT descend further into a variable_declarator's own
            # subtree here - its value (if a closure) is walked normally
            # via the outer _walk/_named_def dispatch, not this scan
            continue
        stack.extend(n.children)
    return out


def _call_target(call_node, src: bytes):
    """Extract (base_name, called_name). base_name is a simple identifier
    ("res" in res.json(), "this" for this.method()) when resolvable,
    `_COMPLEX_BASE` when there's a base but it isn't a simple identifier
    (e.g. `res.status(503).json()` - the object is itself a call, not a
    name), and None only for a true bare/global call. Bug found via
    real-world testing on a large TS codebase: a chained call like
    `res.status(503).json(...)` previously came back with base_name=None
    (indistinguishable from a bare global call), which let it wrongly match
    an unrelated same-named method elsewhere in the repo (a real handler's
    `.json()` matched a test helper's `mockRes.json`) via the bare-call
    "exactly one repo-wide candidate" fallback. `_COMPLEX_BASE` is not a
    legal identifier, so it never coincidentally matches a real import
    binding, but being non-None correctly routes the call through the
    attribute-call path instead, which treats "no binding, no same-file
    match" as external/unknown rather than guessing."""
    return _target_from_node(call_node.child_by_field_name("function"), src)


def _direct_calls(scope_node, src: bytes):
    """Same rule as graph_lib._direct_calls: collect calls made directly
    in this scope, stopping at any NESTED node that will become its own
    tracked definition (a nested named function/method/class, or - now
    that _named_def recognizes them too - a nested anonymous closure) so
    its calls are attributed there instead, not double-counted here.

    Starts from `scope_node` ITSELF, not just its children - a concise
    (non-block) arrow body is the expression directly (`x => helper(x)`
    has body.type == 'call_expression', with no wrapping statement_block
    at all), so a version of this that only walked scope_node.children
    would silently never see that outermost call. Found by direct
    inspection of the parse tree while adding closure tracking, not
    assumed - this affected every concise-body arrow function, named or
    anonymous, not just the new closure case."""
    if scope_node is None:
        return []
    calls = []
    stack = [scope_node]
    while stack:
        n = stack.pop()
        if _named_def(n, src) is not None:
            continue
        if n.type == "call_expression":
            calls.append(n)
        stack.extend(n.children)
    return calls


def _resolve_module_candidates(importing_file: str, module_path: str, aliases: list[tuple[str, str]]) -> list[str]:
    """module_path is the raw string from the import statement (e.g.
    './foo', '@/components/Thing', 'lodash'). Returns candidate
    repo-relative file paths to check against on_disk, or [] for a bare
    package import we intentionally don't try to resolve (node_modules
    is out of scope) - callers treat an empty list as 'no binding'."""
    if module_path.startswith("."):
        base = os.path.normpath(os.path.join(os.path.dirname(importing_file), module_path))
    else:
        base = None
        for prefix, target in aliases:
            if module_path.startswith(prefix):
                base = target + module_path[len(prefix):]
                break
        if base is None:
            return []  # bare package import (react, lodash, ...) - not resolved, left external
        # Alias targets are written relative to the repo root and commonly
        # start with "./" - Next.js emits `"@/*": ["./*"]` meaning "the repo
        # root". Without normalising, `@/lib/payload` produced the candidate
        # `./lib/payload.ts` while every indexed file is keyed repo-relative
        # as `lib/payload.ts`, so the candidate could never match and the
        # import stayed unresolved even once the alias itself parsed.
        base = os.path.normpath(base)
    base = base.replace(os.sep, "/")
    return [f"{base}{suf}" for suf in _RESOLUTION_SUFFIXES]


class _Ctx:
    __slots__ = ("file", "src", "aliases", "result", "module_id")


def _handle_import(node, ctx: _Ctx):
    source = node.child_by_field_name("source")
    if source is None:
        return
    module_path = _text(source, ctx.src).strip("'\"")
    lineno = node.start_point[0] + 1
    # Target the module itself when it lives in this repo - see
    # graph_lib.PendingImport. Resolved in cg_index pass 2 against the
    # on-disk set; a bare package import has no candidates and stays
    # external, which is correct.
    ctx.result.pending_imports.append(PendingImport(
        candidates=_resolve_module_candidates(ctx.file, module_path, ctx.aliases),
        external=f"external:{module_path}",
        evidence=f"import from '{module_path}' (line {lineno})", lineno=lineno,
    ))
    # NOTE: "import_clause" is NOT exposed as a named field on this
    # grammar (only "source" is) - found via testing, not assumed. Must
    # search children by type instead of child_by_field_name, or every
    # named/default/namespace import silently produces zero bindings
    # while still looking like it worked (the generic import edge above
    # still gets created either way, which is what made this easy to miss).
    clause = next((c for c in node.children if c.type == "import_clause"), None)
    if clause is None:
        return  # side-effect-only import: `import './foo'`

    candidates = _resolve_module_candidates(ctx.file, module_path, ctx.aliases)
    if not candidates:
        return  # bare package import - no binding, legacy/external handling covers it

    for child in clause.children:
        if child.type == "identifier":
            # default import: `import Default from './x'`
            local = _text(child, ctx.src)
            ctx.result.import_bindings[local] = ImportBinding(
                local_name=local, symbol_candidates=candidates, symbol_name="default",
                module_candidates=candidates,
            )
        elif child.type == "namespace_import":
            ident = next((c for c in child.children if c.type == "identifier"), None)
            if ident is not None:
                local = _text(ident, ctx.src)
                ctx.result.import_bindings[local] = ImportBinding(
                    local_name=local, module_candidates=candidates,
                )
        elif child.type == "named_imports":
            for spec in child.children:
                if spec.type != "import_specifier":
                    continue
                name_node = spec.child_by_field_name("name")
                alias_node = spec.child_by_field_name("alias")
                if name_node is None:
                    continue
                exported_name = _text(name_node, ctx.src)
                local = _text(alias_node, ctx.src) if alias_node else exported_name
                ctx.result.import_bindings[local] = ImportBinding(
                    local_name=local, symbol_candidates=candidates, symbol_name=exported_name,
                    module_candidates=candidates,
                )


def _handle_require_declarator(node, ctx: _Ctx) -> bool:
    """`const x = require('./foo')` or `const { y, z: alias } =
    require('./foo')` - the CommonJS equivalent of an ESM import, bound to
    the same ImportBinding shape so call resolution treats `x.y()` /
    `y()` identically regardless of which import style a file uses.
    Returns True if `node` (a variable_declarator) was a require() call -
    the caller should not walk further into it (there's no nested
    definition or trackable call inside a require() expression) - False
    for an ordinary declarator, which falls through to normal handling."""
    value = node.child_by_field_name("value")
    if value is None or value.type != "call_expression":
        return False
    func = value.child_by_field_name("function")
    if func is None or func.type != "identifier" or _text(func, ctx.src) != "require":
        return False
    args = value.child_by_field_name("arguments")
    arg_nodes = args.named_children if args is not None else []
    if len(arg_nodes) != 1 or arg_nodes[0].type != "string":
        return False  # require(someVariable) or require() with 0/2+ args - not a resolvable static path
    module_path = _text(arg_nodes[0], ctx.src).strip("'\"")
    lineno = node.start_point[0] + 1
    candidates = _resolve_module_candidates(ctx.file, module_path, ctx.aliases)
    ctx.result.pending_imports.append(PendingImport(
        candidates=candidates, external=f"external:{module_path}",
        evidence=f"require('{module_path}') (line {lineno})", lineno=lineno,
    ))
    if not candidates:
        return True  # bare package require ('lodash') - not resolved, same as a bare ESM import

    name_node = node.child_by_field_name("name")
    if name_node is None:
        return True
    if name_node.type == "identifier":
        # const x = require('./foo') - x is bound to the WHOLE module
        # export object, same as `import * as x from './foo'`.
        local = _text(name_node, ctx.src)
        ctx.result.import_bindings[local] = ImportBinding(
            local_name=local, module_candidates=candidates,
        )
    elif name_node.type == "object_pattern":
        # const { y, z: alias } = require('./foo') - each destructured
        # name binds to that one named export, same as ESM `import { y,
        # z as alias } from './foo'`. Node types verified directly against
        # the pinned grammar's actual parse tree, not assumed (same
        # discipline as _handle_import's ESM named-import handling).
        for child in name_node.children:
            if child.type == "shorthand_property_identifier_pattern":
                local = _text(child, ctx.src)
                ctx.result.import_bindings[local] = ImportBinding(
                    local_name=local, symbol_candidates=candidates, symbol_name=local,
                    module_candidates=candidates,
                )
            elif child.type == "pair_pattern":
                key_node = child.child_by_field_name("key")
                value_node = child.child_by_field_name("value")
                if key_node is None:
                    continue
                exported_name = _text(key_node, ctx.src)
                local = _text(value_node, ctx.src) if value_node is not None else exported_name
                ctx.result.import_bindings[local] = ImportBinding(
                    local_name=local, symbol_candidates=candidates, symbol_name=exported_name,
                    module_candidates=candidates,
                )
    # Any other destructuring shape (array pattern, nested/default-valued
    # patterns) is left unbound rather than guessed - the `imports` edge
    # above still records that a require() happened here.
    return True


def _collect_class_structure(class_node, class_body, class_id: str, ctx: "_Ctx"):
    """Populate ctx.result.class_bases / attr_types for a class_declaration
    from its STRUCTURAL, declaration-only shapes (as opposed to statements
    inside a method body, which _collect_type_assignments covers):
      - `extends Base` / `extends mod.Base` heritage -> PendingBase.
      - a class field declaration with a type annotation (`service:
        UserService;`) -> PendingAttrType(scope_kind="self_attr").
      - a TS constructor PARAMETER PROPERTY (`constructor(private repo:
        Repo)` / `constructor(readonly repo: Repo)`) - a TS shorthand that
        both declares AND assigns the field from the constructor argument,
        with no `this.x = x` statement anywhere to scan for - also ->
        PendingAttrType(scope_kind="self_attr"). This is the single most
        common DI/composition pattern in idiomatic TS (Angular/NestJS-style
        `constructor(private userService: UserService)`), and specifically
        why field/parameter TYPE ANNOTATIONS, not just `new X()` assignment
        tracing, are read here - a DI-injected dependency is frequently
        never constructed with `new` anywhere in the file at all."""
    heritage = next((c for c in class_node.children if c.type == "class_heritage"), None)
    if heritage is not None:
        extends_clause = next((c for c in heritage.children if c.type == "extends_clause"), None)
        if extends_clause is not None:
            value = extends_clause.child_by_field_name("value")
            base_name, ref_name = _target_from_node(value, ctx.src)
            if ref_name:
                ctx.result.class_bases.append(PendingBase(
                    class_id=class_id, base_name=base_name, ref_name=ref_name,
                    file=ctx.file, lineno=extends_clause.start_point[0] + 1,
                ))

    if class_body is None:
        return
    for member in class_body.children:
        if member.type == "public_field_definition":
            name_node = member.child_by_field_name("name")
            type_node = member.child_by_field_name("type")
            if name_node is None or type_node is None:
                continue
            ref = _type_ref(type_node, ctx.src)
            if ref and ref[1]:
                ctx.result.attr_types.append(PendingAttrType(
                    scope_id=class_id, scope_kind="self_attr", var_name=_text(name_node, ctx.src),
                    base_name=ref[0], ref_name=ref[1], file=ctx.file,
                    lineno=member.start_point[0] + 1,
                ))
        elif member.type == "method_definition":
            name_node = member.child_by_field_name("name")
            if name_node is None or _text(name_node, ctx.src) != "constructor":
                continue
            params = member.child_by_field_name("parameters")
            if params is None:
                continue
            for p in params.children:
                if p.type != "required_parameter":
                    continue
                # a parameter property needs at least one of
                # public/private/protected (grammar node type
                # "accessibility_modifier") or "readonly" (its own sibling
                # node, NOT nested inside accessibility_modifier - verified
                # directly against the parse tree) - a plain parameter with
                # neither is just an ordinary argument, not a field.
                has_modifier = any(c.type in ("accessibility_modifier", "readonly") for c in p.children)
                if not has_modifier:
                    continue
                pattern = p.child_by_field_name("pattern")
                type_node = p.child_by_field_name("type")
                if pattern is None or pattern.type != "identifier" or type_node is None:
                    continue
                ref = _type_ref(type_node, ctx.src)
                if ref and ref[1]:
                    ctx.result.attr_types.append(PendingAttrType(
                        scope_id=class_id, scope_kind="self_attr", var_name=_text(pattern, ctx.src),
                        base_name=ref[0], ref_name=ref[1], file=ctx.file,
                        lineno=p.start_point[0] + 1,
                    ))


def _walk(node, stack, id_stack, ctx: _Ctx):
    for child in node.children:
        if child.type == "export_statement":
            decl = child.child_by_field_name("declaration")
            _walk_one(decl if decl is not None else child, stack, id_stack, ctx)
        else:
            _walk_one(child, stack, id_stack, ctx)


def _walk_one(node, stack, id_stack, ctx: _Ctx):
    if node.type == "import_statement":
        _handle_import(node, ctx)
        return
    if node.type == "variable_declarator" and _handle_require_declarator(node, ctx):
        return
    defn = _named_def(node, ctx.src)
    if defn is None:
        _walk(node, stack, id_stack, ctx)  # transparent pass-through, look deeper
        return

    kind_hint, name_node = defn

    if node.type in ("function_declaration", "method_definition", "class_declaration") \
            or node.type in _ANON_FUNC_TYPES:
        # class_declaration's "body" is its class_body (containing
        # method_definitions, handled independently); the others' "body"
        # is the function's own body - a statement_block, OR, for a
        # concise-body arrow (`x => helper(x)`), the bare expression
        # itself (see _direct_calls' docstring).
        scope_node = node.child_by_field_name("body")
    else:
        # variable_declarator / public_field_definition wrapping a NAMED
        # arrow/function expression: `node` is the wrapper, not the
        # function itself - drill into value.body directly (not just
        # `value`, the function node) so this scope_node means the exact
        # same thing (the function's own body) as every other branch.
        # Getting this wrong previously fed the whole function node into
        # _direct_calls, which - after _named_def started recognizing
        # closures - would have wrongly treated a NAMED arrow's own node
        # as a closure to skip over, silently zeroing out every named
        # arrow function's calls.
        wrapped = node.child_by_field_name("value")
        scope_node = wrapped.child_by_field_name("body") if wrapped is not None else None

    if kind_hint == "closure":
        # Anonymous arrow/function expression encountered directly (a
        # callback argument, an array element, ...). Only worth its own
        # node if something is actually called directly in its body -
        # otherwise (`.map(x => x.id)`, the overwhelmingly common case)
        # creating a node+edges for every trivial one-liner callback would
        # bloat the graph for no query value (nothing would ever be found
        # AS its outgoing call). A call-free closure is skipped as its
        # own node but still walked into, at the CURRENT scope, so any
        # named def or call-bearing closure nested inside it (rare, but
        # possible: `setTimeout(() => { arr.forEach(x => doWork(x)) })`)
        # is still found and attributed one level up rather than lost.
        direct_calls = _direct_calls(scope_node, ctx.src)
        if not direct_calls:
            _walk(node, stack, id_stack, ctx)
            return
        name = f"<closure:{node.start_point[0] + 1}>"
    else:
        direct_calls = None  # computed below, after we know we're creating the node
        name = _text(name_node, ctx.src)

    kind = "closure" if kind_hint == "closure" else (
        kind_hint if kind_hint != "function_or_method" else ("method" if stack else "function")
    )
    qn = ".".join(stack + [name]) if stack else name
    node_id = f"{ctx.file}::{qn}"
    end_line = node.end_point[0] + 1
    ctx.result.nodes.append({
        "id": node_id, "kind": kind, "name": name, "qualname": qn,
        "file": ctx.file, "lineno": node.start_point[0] + 1, "end_lineno": end_line,
    })
    parent_id = id_stack[-1]
    ctx.result.edges.append({
        "src": parent_id, "dst": node_id, "type": "defines",
        "evidence": f"{kind} {name} defined at {ctx.file}:{node.start_point[0] + 1}",
    })
    if kind == "closure":
        # ALSO a `calls` edge, not just `defines`: impacted_by/neighbors
        # only traverse calls/calls_external/imports (see query_lib.py),
        # so a defines-only link would make this closure - and anything
        # it calls - unreachable from the enclosing function's own
        # reverse-reachability chain. Semantically defensible too: the
        # enclosing function is what causes this closure to run, whether
        # via an event handler prop, a .then(), or a hook.
        ctx.result.edges.append({
            "src": parent_id, "dst": node_id, "type": "calls",
            "evidence": f"defines callback at {ctx.file}:{node.start_point[0] + 1}",
        })

    if kind == "class":
        _collect_class_structure(node, scope_node, node_id, ctx)

    # Classes are included for the same reason as graph_lib's Python side: a
    # call in a CLASS BODY - a property initialiser (`col = makeField()`), a
    # decorator, a computed heritage clause - runs at definition time and is
    # a real call. _direct_calls stops at every tracked definition, so a
    # method's own calls still belong to the method, not to its class.
    if kind in ("function", "method", "closure", "class"):
        if direct_calls is None:
            direct_calls = _direct_calls(scope_node, ctx.src)
        for call_node in direct_calls:
            func_node = call_node.child_by_field_name("function")
            base_name, called_name = _target_from_node(func_node, ctx.src)
            if called_name:
                ctx.result.pending_calls.append(PendingCall(
                    caller_id=node_id, called_name=called_name, base_name=base_name,
                    attr_base=_this_attr_chain(func_node, ctx.src),
                ctor_type=_ctor_type(func_node, ctx.src),
                    lineno=call_node.start_point[0] + 1, file=ctx.file,
                ))

    if kind in ("function", "method", "closure"):
        # lightweight type inference (see PendingAttrType docstring):
        # `this.<attr> = new Class()` is scoped to the ENCLOSING CLASS
        # (parent_id, only meaningful when kind == "method" - a bare
        # function has no `this` bound the same way); a local `x = new
        # Class()` / `let x: Class` is scoped to THIS function/method only.
        for scope_kind, var_name, base_name, ref_name, lineno in _collect_type_assignments(scope_node, ctx.src):
            if scope_kind == "self_attr":
                if kind != "method":
                    continue
                scope_id = parent_id
            else:
                scope_id = node_id
            ctx.result.attr_types.append(PendingAttrType(
                scope_id=scope_id, scope_kind=scope_kind, var_name=var_name,
                base_name=base_name, ref_name=ref_name, file=ctx.file, lineno=lineno,
            ))

    stack.append(name)
    id_stack.append(node_id)
    if scope_node is not None:
        _walk(scope_node, stack, id_stack, ctx)
    id_stack.pop()
    stack.pop()


def _record_module_level_calls(root_node, ctx: _Ctx) -> None:
    """Calls made at MODULE scope - outside any function, class or tracked
    closure. These used to be dropped entirely: pending calls were only
    collected in _walk_one's named-definition branch, which never fires for
    the top level of a file.

    That mattered far more than it sounds. In a composition-API codebase
    most wiring IS top-level: `const { a, b } = useThing()` in a
    <script setup> block, `defineStore(...)`/`createRouter(...)` in a .ts
    module. Measured on a real 4,113-file Vue/TS repo before this: 38,302
    top-level call expressions, 13.0% of every call expression in the repo,
    produced no edge at all, so a composable's caller list silently omitted
    every component that used it the normal way.

    _direct_calls already stops at anything that becomes its own tracked
    definition, so handing it the root node yields exactly the top-level
    calls and nothing a function/closure already owns."""
    for call_node in _direct_calls(root_node, ctx.src):
        func_node = call_node.child_by_field_name("function")
        base_name, called_name = _target_from_node(func_node, ctx.src)
        if called_name:
            ctx.result.pending_calls.append(PendingCall(
                caller_id=ctx.module_id, called_name=called_name, base_name=base_name,
                attr_base=_this_attr_chain(func_node, ctx.src),
                ctor_type=_ctor_type(func_node, ctx.src),
                lineno=call_node.start_point[0] + 1, file=ctx.file,
            ))


def parse_file(root: str, rel_file: str, aliases: list[tuple[str, str]]) -> ParseResult:
    ext = os.path.splitext(rel_file)[1]
    lang = _LANG_FOR_EXT.get(ext)
    if lang is None:
        raise ValueError(f"unsupported extension: {ext}")
    parser = get_parser(lang)
    abs_path = os.path.join(root, rel_file)
    with open(abs_path, "rb") as f:
        src = f.read()
    tree = parser.parse(src)
    if tree.root_node.has_error:
        # tree-sitter is error-tolerant (it produces a best-effort tree
        # even for invalid syntax) rather than raising - a real syntax
        # error would otherwise silently produce a partial/wrong graph.
        # Treat it the same way graph_lib.parse_file treats a Python
        # SyntaxError: surface it so cg_index.py skips just this file.
        raise SyntaxError(f"tree-sitter reported a parse error in {rel_file}")

    module_id = rel_file
    result = ParseResult()
    result.nodes.append({
        "id": module_id, "kind": "module", "name": os.path.basename(rel_file),
        "qualname": rel_file, "file": rel_file, "lineno": 1,
        "end_lineno": tree.root_node.end_point[0] + 1,
    })
    ctx = _Ctx()
    ctx.file, ctx.src, ctx.aliases, ctx.result, ctx.module_id = rel_file, src, aliases, result, module_id
    _walk(tree.root_node, [], [module_id], ctx)
    _record_module_level_calls(tree.root_node, ctx)
    return result


_VUE_SCRIPT_RE = re.compile(rb"<script\b([^>]*)>(.*?)</script\s*>", re.DOTALL | re.IGNORECASE)
_VUE_LANG_RE = re.compile(rb"""lang\s*=\s*["']([\w-]+)["']""", re.IGNORECASE)
_VUE_LANG_MAP = {
    "ts": "typescript", "typescript": "typescript", "tsx": "tsx",
    "js": "javascript", "javascript": "javascript", "jsx": "javascript",
}

# <template> is matched greedily (search, not finditer, and `.*` not
# `.*?`): from the FIRST <template> tag to the LAST </template> in the
# file. This is deliberate, not a mistake - a real SFC has exactly one
# top-level <template>...</template>, but commonly contains NESTED
# `<template v-slot:...>` wrapper tags for named/scoped slots, which
# share the same tag name. Greedy matching correctly folds those into
# the one outer template block instead of stopping at the first nested
# </template>. (Contrast with _VUE_SCRIPT_RE above, which uses finditer +
# lazy `.*?`, because a SFC CAN have two separate, non-nested top-level
# <script> blocks - setup and options - that must not be merged.)
_VUE_TEMPLATE_RE = re.compile(rb"<template\b[^>]*>(.*)</template\s*>", re.DOTALL | re.IGNORECASE)
_VUE_INTERP_RE = re.compile(rb"\{\{(.*?)\}\}", re.DOTALL)
# Any attribute whose name starts with @ (event shorthand), : (prop/attr
# binding shorthand), v- (a directive: v-if, v-show, v-for, v-model,
# v-on:click, v-bind:prop, ...), or # (v-slot shorthand: #default,
# #item.name, ...) has a JS expression as its value in Vue's template
# syntax - unlike a plain HTML attribute (class="...", id="..."), which is
# literal text, not JS, and is correctly NOT matched here. `#` is safe to
# treat as always-Vue: it's not a legal leading character for a plain HTML
# attribute name, so there's no plain-attribute case this could misfire
# on. The trailing character class also allows `[`/`]` for a dynamic
# argument (`:[propName]="expr"`, `@[eventName]="expr"`) - previously
# unmatched entirely (not just a parse failure: the attribute-name capture
# stopped dead at the `[`, so the whole attribute never matched and its
# value was never even attempted).
_VUE_ATTR_RE = re.compile(rb'''[\s]((?:@|:|v-|\#)[\w:.\-\[\]]*)\s*=\s*(?:"([^"]*)"|'([^']*)')''')
_VUE_EVENT_ATTR_RE = re.compile(rb"^(?:@|v-on:)", re.IGNORECASE)


def _template_pending_calls(content: bytes, tmpl_start: int, tmpl_end: int,
                             rel_file: str, template_node_id: str) -> list:
    """Extract calls made FROM a Vue <template> block: `{{ expr }}`
    interpolations and directive/bound-attribute values (`@click="..."`,
    `:prop="..."`, `v-if="..."`, etc.). This is what makes `impacted_by`
    meaningful for a Vue component's methods - in idiomatic Vue, a method
    is usually invoked BY the template, not by other script code, so
    without this the graph would (and, before this, did) show most
    component methods as having no callers at all.

    Each attribute value / interpolation body is extracted as an isolated
    text fragment and parsed independently as its own tiny JS program (no
    padding/offset trick like the <script> blocks need - each fragment
    gets its own byte-0-based tree, and only the outer expression's own
    line number, computed from its position in `content`, is attached to
    every call found inside it - sub-expression-level line precision
    isn't worth the complexity for what's evidence text, not resolution
    logic).

    Best-effort, deliberately: template expression syntax includes things
    that aren't quite standalone JS (`v-for="item in items"`, `v-slot="{
    item }"`), and a fragment that fails to parse is silently skipped
    rather than failing the whole file - there are dozens of these per
    real-world file, and one non-standard directive value must not blank
    out everything else found in the same template.
    """
    calls = []
    parser = get_parser("typescript")  # permissive enough for plain JS fragments too
    tmpl = content[tmpl_start:tmpl_end]

    def _line_at(abs_pos: int) -> int:
        return content.count(b"\n", 0, abs_pos) + 1

    def _root_expr(root_node):
        # Program -> expression_statement -> <expr>, unwrapping one level
        # of parens too, best-effort.
        node = root_node
        if node.named_child_count == 0:
            return None
        node = node.named_children[0]
        if node.type == "expression_statement" and node.named_child_count:
            node = node.named_children[0]
        if node.type == "parenthesized_expression" and node.named_child_count:
            node = node.named_children[0]
        return node

    def _collect_call_nodes(root_node):
        found = []
        stack = [root_node]
        while stack:
            n = stack.pop()
            if n.type == "call_expression":
                found.append(n)
            stack.extend(n.children)
        return found

    def _handle_fragment(expr_bytes: bytes, abs_pos: int, is_handler: bool):
        expr_bytes = expr_bytes.strip()
        if not expr_bytes:
            return
        tree = parser.parse(expr_bytes)
        if tree.root_node.has_error:
            return  # non-standard directive syntax or a genuine typo - skip, don't fail the file
        lineno = _line_at(abs_pos)
        call_nodes = _collect_call_nodes(tree.root_node)
        for cn in call_nodes:
            base_name, called_name = _call_target(cn, expr_bytes)
            if called_name:
                calls.append(PendingCall(
                    caller_id=template_node_id, called_name=called_name,
                    base_name=base_name, lineno=lineno, file=rel_file,
                    no_cross_file_guess=True,
                ))
        if is_handler and not call_nodes:
            # Bare handler reference with no explicit call, e.g.
            # `@click="save"` rather than `@click="save()"` - Vue calls it
            # implicitly, passing the event. Only meaningful when the
            # whole expression is just a name (identifier/member
            # expression) - `@click="x ? a : b"` or an inline arrow aren't
            # references to an existing named symbol.
            root = _root_expr(tree.root_node)
            if root is not None:
                base_name, called_name = _target_from_node(root, expr_bytes)
                if called_name:
                    calls.append(PendingCall(
                        caller_id=template_node_id, called_name=called_name,
                        base_name=base_name, lineno=lineno, file=rel_file,
                        no_cross_file_guess=True,
                    ))

    for m in _VUE_INTERP_RE.finditer(tmpl):
        _handle_fragment(m.group(1), tmpl_start + m.start(1), is_handler=False)

    for m in _VUE_ATTR_RE.finditer(tmpl):
        attr_name = m.group(1)
        group_idx = 2 if m.group(2) is not None else 3
        value = m.group(group_idx)
        _handle_fragment(value, tmpl_start + m.start(group_idx),
                          is_handler=bool(_VUE_EVENT_ATTR_RE.match(attr_name)))

    return calls


def parse_vue_file(root: str, rel_file: str, aliases: list[tuple[str, str]]) -> ParseResult:
    """Parse a Vue Single File Component's <script>/<script setup>
    block(s) with the same TS/JS grammar used for .ts/.js files (chosen
    per-block by that block's own `lang` attribute, default JS - Vue's
    own default). A .vue file can legally have BOTH a `<script setup>`
    and a plain `<script>` block (the latter typically just for
    `defineComponent({ name: ... })` or non-setup options); both are
    walked into the same module node.

    <template> IS also parsed (see _template_pending_calls) for calls made
    from `{{ }}` interpolations and directive/bound-attribute values
    (`@click="save"`, `:prop="expr"`, `v-if="cond"`, ...) - this is what
    lets `impacted_by`/`neighbors` see a component method's real callers,
    since in idiomatic Vue a method is usually invoked BY the template,
    not by other script code. It's still best-effort, not a real Vue
    template compiler: each expression fragment is parsed independently
    as standalone JS and a fragment that doesn't parse as one (some
    directive syntax, e.g. `v-slot="{ item }"`, isn't quite standalone
    JS) is silently skipped rather than failing the whole file. <style>
    is never parsed (not relevant to a call graph).

    Line numbers in the resulting nodes match the ORIGINAL .vue file, not
    the extracted script body: each extracted script is left-padded with
    the same number of newlines that preceded it in the real file, so
    tree-sitter's row numbers line up for free without separate offset
    bookkeeping in the (shared) _walk/_walk_one code. Template call sites
    use the position of their enclosing expression fragment (an
    interpolation or attribute value), which is exact for the common case
    of a short single-line expression.
    """
    abs_path = os.path.join(root, rel_file)
    with open(abs_path, "rb") as f:
        content = f.read()

    # Line count, not tree-sitter's own end_point (there may be several
    # trees, one per <script> block, none spanning the whole file): a
    # trailing newline must not count as an extra line.
    total_lines = content.count(b"\n") + (0 if content.endswith(b"\n") else 1)

    module_id = rel_file
    result = ParseResult()
    result.nodes.append({
        "id": module_id, "kind": "module", "name": os.path.basename(rel_file),
        "qualname": rel_file, "file": rel_file, "lineno": 1,
        "end_lineno": total_lines,
    })

    blocks_found = blocks_failed = 0
    for m in _VUE_SCRIPT_RE.finditer(content):
        blocks_found += 1
        attrs, body = m.group(1), m.group(2)
        lang_m = _VUE_LANG_RE.search(attrs)
        lang_attr = lang_m.group(1).decode("ascii", errors="replace").lower() if lang_m else "js"
        lang = _VUE_LANG_MAP.get(lang_attr, "javascript")  # unknown lang (e.g. coffeescript) -> best-effort JS parse

        body_start = m.start(2)
        prefix_newlines = content.count(b"\n", 0, body_start)
        padded_src = b"\n" * prefix_newlines + body

        parser = get_parser(lang)
        tree = parser.parse(padded_src)
        if tree.root_node.has_error:
            blocks_failed += 1
            continue  # one malformed <script> block shouldn't blank out a sibling block

        ctx = _Ctx()
        ctx.file, ctx.src, ctx.aliases, ctx.result, ctx.module_id = rel_file, padded_src, aliases, result, module_id
        _walk(tree.root_node, [], [module_id], ctx)
        # per block: a .vue file may legally have BOTH a plain <script> and
        # a <script setup>, and each block's top level is module scope.
        _record_module_level_calls(tree.root_node, ctx)

    if blocks_found > 0 and blocks_failed == blocks_found:
        # every <script> block present failed to parse - same treatment
        # as a .ts/.js syntax error (skip the file, don't index it as
        # silently empty). A .vue file with NO <script> tag at all
        # (template-only, e.g. a pure-markup partial) is not an error -
        # blocks_found stays 0 and it indexes as a bare module node.
        raise SyntaxError(f"tree-sitter reported a parse error in every <script> block of {rel_file}")

    tmpl_m = _VUE_TEMPLATE_RE.search(content)
    if tmpl_m is not None:
        # A synthetic node representing "the template" as a caller -
        # there's no real function to attribute template-driven calls to,
        # but without SOME node for them to originate from, they'd have
        # nowhere to attach and impacted_by would stay blind to them.
        # kind="template" (not "function"/"method") so load_symbol_maps()
        # never considers it a possible CALLEE - it's a source of calls
        # only, never a target.
        template_node_id = f"{rel_file}::<template>"
        result.nodes.append({
            "id": template_node_id, "kind": "template", "name": "<template>",
            "qualname": "<template>", "file": rel_file,
            "lineno": content.count(b"\n", 0, tmpl_m.start()) + 1,
            "end_lineno": content.count(b"\n", 0, tmpl_m.end()) + 1,
        })
        result.edges.append({
            "src": module_id, "dst": template_node_id, "type": "defines",
            "evidence": f"template block of {rel_file}",
        })
        result.pending_calls.extend(_template_pending_calls(
            content, tmpl_m.start(1), tmpl_m.end(1), rel_file, template_node_id
        ))
    return result
