"""Go parsing for codegraph, via tree-sitter.

Produces the same graph_lib.ParseResult shape as the Python and TS parsers,
so pass-2 resolution, the query layer and the MCP server are unchanged.

Go differs from both in one way that reaches into pass 2: **an import binds
to a DIRECTORY, not a file.** `scoring.Compute()` may be defined in any .go
file of the `internal/scoring` package, so the existing (file, name) symbol
lookup cannot resolve it. cg_index builds an additional (package_dir, name)
map for this.

In one respect Go is easier than TypeScript. `func (r *Rules) Apply()`
states the receiver's type outright, so an instance-method call resolves
from a declaration rather than the inference TS needed.

Scope of this first pass, stated rather than left to be discovered:
  - functions, methods (qualified by receiver type), struct and interface
    types, and the package each file belongs to
  - calls: bare (same package, across its files), package-qualified via an
    import binding, and receiver-qualified
  - imports resolved through go.mod's module path; anything outside the
    module (stdlib, third-party) stays external
  - NOT inferred: struct FIELD types. `r.field.Method()` stays external
    rather than being guessed, consistent with how every other unknown is
    treated here.
"""
from __future__ import annotations

import os
import re

from . import graph_lib as gl
from .graph_lib import (ImportBinding, ParseResult, PendingCall, PendingBase,
                        PendingImport)
from .ts_parser import get_parser   # shared lazy tree-sitter loader

SOURCE_EXTS = (".go",)

_COMPLEX_BASE = "<complex>"


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def module_path(repo_root: str) -> str | None:
    """The module path from go.mod - e.g. `github.com/acme/app`.

    This is what turns an import path into a repo-relative directory: an
    import of `github.com/acme/app/internal/scoring` is the directory
    `internal/scoring`. Without go.mod every import looks third-party,
    which is correct behaviour for a directory that is not a Go module."""
    p = os.path.join(repo_root, "go.mod")
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"\s*module\s+(\S+)", line)
                if m:
                    return m.group(1)
    except OSError:
        return None
    return None


def package_dir(rel_file: str) -> str:
    """The package a file belongs to: its directory, or '.' at the root."""
    d = os.path.dirname(rel_file)
    return d if d else "."


def _receiver_type(method_node, src: bytes) -> str | None:
    """The type name in `func (r *Rules) Apply()` -> "Rules".

    Takes the FIRST parameter_list, which for a method_declaration is the
    receiver. Pointer and value receivers name the same type, so the `*` is
    stripped."""
    for child in method_node.children:
        if child.type == "parameter_list":
            for decl in child.named_children:
                if decl.type != "parameter_declaration":
                    continue
                t = decl.child_by_field_name("type")
                while t is not None and t.type == "pointer_type":
                    inner = [c for c in t.named_children]
                    t = inner[0] if inner else None
                if t is not None and t.type in ("type_identifier", "generic_type"):
                    if t.type == "generic_type":
                        base = t.child_by_field_name("type")
                        return _text(base, src) if base else None
                    return _text(t, src)
            return None
    return None


def _direct_calls(scope_node, src: bytes):
    """Calls made directly in this scope, not inside a nested function
    literal (which is visited and attributed on its own). Same rule as the
    other two parsers."""
    if scope_node is None:
        return []
    calls, stack = [], [scope_node]
    while stack:
        n = stack.pop()
        if n is not scope_node and n.type in ("func_literal", "function_declaration",
                                              "method_declaration"):
            continue
        if n.type == "call_expression":
            calls.append(n)
        stack.extend(n.children)
    return calls


def _call_target(call_node, src: bytes):
    """(base_name, called_name) for a call.

    base_name is the qualifier in `pkg.Foo()` / `recv.Method()`, or None for
    a bare `foo()`. A non-trivial base (`get().Method()`) reports
    _COMPLEX_BASE so it routes through the safer attribute path rather than
    being mistaken for a bare call."""
    fn = call_node.child_by_field_name("function")
    if fn is None:
        return None, None
    if fn.type == "identifier":
        return None, _text(fn, src)
    if fn.type == "selector_expression":
        operand = fn.child_by_field_name("operand")
        field = fn.child_by_field_name("field")
        if field is None:
            return None, None
        base = _text(operand, src) if operand is not None and operand.type == "identifier" \
            else _COMPLEX_BASE
        return base, _text(field, src)
    return None, None


def _import_specs(root, src: bytes):
    """(alias_or_None, import_path, lineno) for every import spec."""
    out = []
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "import_spec":
            path_node = n.child_by_field_name("path")
            if path_node is not None:
                path = _text(path_node, src).strip('"`')
                name_node = n.child_by_field_name("name")
                alias = _text(name_node, src) if name_node is not None else None
                out.append((alias, path, n.start_point[0] + 1))
            continue
        stack.extend(n.children)
    return out


def parse_file(root: str, rel_file: str, mod_path: str | None) -> ParseResult:
    src = open(os.path.join(root, rel_file), "rb").read()
    tree = get_parser("go").parse(src)
    if tree.root_node.has_error:
        raise SyntaxError(f"tree-sitter reported a parse error in {rel_file}")

    result = ParseResult()
    module_id = rel_file
    pkg_id = package_dir(rel_file)

    result.nodes.append({
        "id": module_id, "kind": "module", "name": os.path.basename(rel_file),
        "qualname": rel_file, "file": rel_file, "lineno": 1,
        "end_lineno": tree.root_node.end_point[0] + 1,
    })
    # A package node per directory. Go's unit of importability is the
    # package, so without this "what imports internal/scoring" has no
    # subject - the same gap that made module imports unanswerable before.
    result.nodes.append({
        "id": pkg_id, "kind": "package", "name": os.path.basename(pkg_id) or pkg_id,
        "qualname": pkg_id, "file": rel_file, "lineno": 1, "end_lineno": 1,
    })
    result.edges.append({
        "src": pkg_id, "dst": module_id, "type": "defines",
        "evidence": f"file of package {pkg_id}",
    })

    # --- imports
    for alias, path, lineno in _import_specs(tree.root_node, src):
        local = alias or path.rstrip("/").split("/")[-1]
        candidates = []
        if mod_path and (path == mod_path or path.startswith(mod_path + "/")):
            rel = path[len(mod_path):].lstrip("/")
            candidates = [rel or "."]
        if local not in ("_", "."):
            result.import_bindings[local] = ImportBinding(
                local_name=local, module_candidates=candidates,
            )
        result.pending_imports.append(PendingImport(
            candidates=candidates, external=f"external:{path}",
            evidence=f'import "{path}" (line {lineno})', lineno=lineno,
        ))

    # --- declarations
    for node in tree.root_node.named_children:
        if node.type == "type_declaration":
            for spec in node.named_children:
                if spec.type != "type_spec":
                    continue
                name_node = spec.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _text(name_node, src)
                nid = f"{rel_file}::{name}"
                result.nodes.append({
                    "id": nid, "kind": "class", "name": name, "qualname": name,
                    "file": rel_file, "lineno": spec.start_point[0] + 1,
                    "end_lineno": spec.end_point[0] + 1,
                })
                result.edges.append({
                    "src": module_id, "dst": nid, "type": "defines",
                    "evidence": f"type {name} defined at {rel_file}:{spec.start_point[0] + 1}",
                })

        elif node.type in ("function_declaration", "method_declaration"):
            if node.type == "method_declaration":
                recv = _receiver_type(node, src)
                name_node = node.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _text(name_node, src)
                qual = f"{recv}.{name}" if recv else name
                kind = "method"
            else:
                name_node = node.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _text(name_node, src)
                qual, kind, recv = name, "function", None

            nid = f"{rel_file}::{qual}"
            result.nodes.append({
                "id": nid, "kind": kind, "name": name, "qualname": qual,
                "file": rel_file, "lineno": node.start_point[0] + 1,
                "end_lineno": node.end_point[0] + 1,
            })
            result.edges.append({
                "src": module_id, "dst": nid, "type": "defines",
                "evidence": f"{kind} {name} defined at {rel_file}:{node.start_point[0] + 1}",
            })
            # A method's receiver type is written in the declaration, so the
            # owning type is known outright - no inference, unlike TS.
            if recv:
                result.class_bases.append(PendingBase(
                    class_id=nid, base_name=None, ref_name=recv,
                    file=rel_file, lineno=node.start_point[0] + 1,
                ))

            body = node.child_by_field_name("body")
            _record_scope(result, nid, qual, body, src, rel_file)

    return result


def _func_literals(scope_node):
    """Function literals declared directly in this scope, not inside a
    nested one (which collects its own)."""
    if scope_node is None:
        return []
    out, stack = [], list(scope_node.children)
    while stack:
        n = stack.pop()
        if n.type == "func_literal":
            out.append(n)
            continue          # its body is walked when we recurse into it
        stack.extend(n.children)
    return out


def _record_scope(result, owner_id: str, owner_qual: str, body, src: bytes, rel_file: str):
    """Attribute the calls made directly in `body` to `owner_id`, then do
    the same for every function literal inside it.

    Function literals are load-bearing in Go - `t.Run(name, func(t *testing.T){...})`
    is the standard subtest idiom, and `defer func(){}()`, `go func(){}()`
    and `http.HandlerFunc(func(w, r){...})` are everywhere. _direct_calls
    stops at a literal so its calls are not misattributed to the enclosing
    function, but without a node to attribute them to they were simply
    DISCARDED. Found on gorilla/mux: a real `newRequest` call inside a
    subtest was invisible to the graph.

    A literal that makes no call gets no node, for the same reason the TS
    parser skips `.map(x => x.id)` - there is nothing to resolve through
    it, so a node would be bloat."""
    for call in _direct_calls(body, src):
        base_name, called = _call_target(call, src)
        if called:
            result.pending_calls.append(PendingCall(
                caller_id=owner_id, called_name=called, base_name=base_name,
                lineno=call.start_point[0] + 1, file=rel_file,
            ))

    for lit in _func_literals(body):
        lit_body = lit.child_by_field_name("body")
        if not _direct_calls(lit_body, src) and not _func_literals(lit_body):
            continue
        line = lit.start_point[0] + 1
        lit_qual = f"{owner_qual}.<closure:{line}>"
        lit_id = f"{rel_file}::{lit_qual}"
        result.nodes.append({
            "id": lit_id, "kind": "closure", "name": f"<closure:{line}>",
            "qualname": lit_qual, "file": rel_file,
            "lineno": line, "end_lineno": lit.end_point[0] + 1,
        })
        result.edges.append({
            "src": owner_id, "dst": lit_id, "type": "defines",
            "evidence": f"function literal at {rel_file}:{line}",
        })
        # ALSO a calls edge: impacted_by traverses calls/imports, so a
        # defines-only link would leave everything inside the literal
        # unreachable from the function that actually runs it.
        result.edges.append({
            "src": owner_id, "dst": lit_id, "type": "calls",
            "evidence": f"function literal at {rel_file}:{line}",
        })
        _record_scope(result, lit_id, lit_qual, lit_body, src, rel_file)
