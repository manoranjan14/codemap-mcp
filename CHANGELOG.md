# Changelog

## 0.2.0

**Go support.** The first language after Python and TypeScript/Vue.

Go resolves imports differently from both, in a way that reaches into
pass 2: an import binds to a *directory*, not a file. `scoring.Compute()`
may live in any `.go` file of the `internal/scoring` package, so the
existing `(file, name)` lookup could not resolve it. Symbol maps now also
carry `(package_dir, name)`, and a package directory counts as a
resolvable binding target alongside a file. A `package` node per directory
was added so "what imports this package" has a subject — Go's unit of
importability is the package, not the file.

In one respect Go is easier than TypeScript: `func (r *Rules) Apply()`
states the receiver's type outright, so instance-method calls resolve from
a declaration rather than the inference TypeScript needed.

Function literals are attributed. Indexing gorilla/mux showed calls inside
`func(w, r) { ... }` were being dropped — the parser stopped at the literal
boundary but created nothing for it. Each call-bearing literal now gets a
`<closure:LINE>` node carrying both the `defines` and `calls` edges, which
took resolved calls on that corpus from 380 to 522. This matters more than
it sounds: most of a Go test's body, and most HTTP handler code, lives in a
literal.

Known gap: struct field types are not inferred, so `r.field.Method()` stays
`external:`. Resolving it needs type propagation this deliberately does not
do.

24 Go tests. 301 total, up from 288.

## 0.1.0

First release. Python, TypeScript, JavaScript and Vue — including `.vue`
`<template>` bindings. Code graph, session memory, and opt-in session
history search, exposed as MCP tools.
