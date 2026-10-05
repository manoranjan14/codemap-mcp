"""codegraph - a local, queryable map of your codebase.

Three layers over one SQLite database, kept entirely outside the repo it
indexes:

  1. a parsed code graph (modules, classes, functions, calls, imports)
  2. durable session notes - what a past session learned
  3. optional full-text search over past Claude Code transcripts

Exposed to an editor as MCP tools (`codegraph.cg_mcp_server`) and to a
terminal as CLIs (`cg_index`, `cg_query`, `cg_usage`).

The indexer and query layer depend on nothing but the standard library.
TypeScript/JavaScript/Vue parsing needs the optional `ts` extra, and the
MCP server needs the optional `server` extra - see pyproject.toml.
"""

__version__ = "0.1.0"
