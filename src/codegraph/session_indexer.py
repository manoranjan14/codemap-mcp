"""
Layer 3: index Claude Code's own session transcripts for a repo, so past
sessions are searchable (`search_sessions`) instead of re-derived from
scratch each time. Opt-in (see cg_index.py's --sessions flag) - separate
from the code graph (Layer 1) and curated session_notes (Layer 2, added
by `add_note`): this indexes the RAW conversation history automatically.

Where transcripts live: Claude Code CLI stores one JSONL file per session
under ~/.claude/projects/<project-dir>/, where <project-dir> is the
repo's absolute working-directory path at session start with every
non-alphanumeric character replaced by "-" (truncated + hashed past 200
chars). This is derived from how Claude Code names these directories, not
a documented/versioned API - see the parsing notes below for how this
module stays defensive against format drift.

IMPORTANT, stated up front rather than discovered later:
  - The JSONL schema is Claude Code's internal format and is NOT a stable,
    documented API - fields can change between Claude Code versions. Every
    read here goes through .get() with a fallback, and a line/file that
    doesn't parse as expected is skipped, never a hard failure (mirrors
    cg_index.py's own per-file error isolation).
  - Session transcripts can contain anything discussed in that session:
    file contents, commands run, secrets pasted, business context. Indexing
    them makes that content searchable via the MCP server to whatever asks
    it. This is exactly what --sessions opts into - stated here so it's a
    deliberate choice, not a surprise.
  - A session's cwd can change mid-session (rare, but possible - e.g. the
    user cd's around). Association with a repo is by which project
    directory the transcript file lives under (fixed at session start),
    not re-checked per line. last_cwd on session_files records what was
    last seen, for visibility, but chunks aren't filtered by it.
  - Only top-level *.jsonl files directly under the project directory are
    read - not subdirectories (some Claude Code deployments nest
    subagent-task transcripts there; those are a different, less stable
    shape and out of scope for v1).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from . import graph_lib as gl

# Content long enough to be a real message but not so long it dominates the
# DB or a search result - truncated with a marker, never silently cut with
# no indication.
_MAX_CHUNK_CHARS = 4000

# Assistant tool_use calls worth a lightweight "action" chunk (what was
# done to which file/command), vs. everything else which is skipped as
# implementation noise (e.g. internal bookkeeping tool calls). Keyed by
# tool name -> the input field(s) that make a useful one-line summary.
_ACTION_TOOLS = {
    "Edit": ("file_path",), "Write": ("file_path",), "Read": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Bash": ("command",),
    "Grep": ("pattern", "path"), "Glob": ("pattern", "path"),
}


def claude_projects_dir() -> str:
    """Where Claude Code stores per-project session transcripts. Overridable
    via $CLAUDE_CONFIG_DIR (matches Claude Code's own env var for relocating
    its whole config directory), for anyone running a non-default setup."""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def project_dir_name(repo_root: str) -> str:
    """Reproduce Claude Code CLI's own directory-naming scheme for a
    project: the absolute cwd path with every non-alphanumeric character
    replaced by '-', truncated (+ a hash of the full path appended) past
    200 characters to avoid an OS filename-length error. If a future
    Claude Code version names these differently, this simply won't find
    the directory - index_sessions() handles that as "nothing to index"
    rather than an error (see its docstring)."""
    abs_root = os.path.normpath(os.path.abspath(repo_root))
    name = re.sub(r"[^A-Za-z0-9]", "-", abs_root)
    if len(name) > 200:
        import hashlib as _hashlib
        name = name[:200] + "-" + _hashlib.sha256(abs_root.encode("utf-8")).hexdigest()[:8]
    return name


@dataclass
class Chunk:
    session_id: str
    ts: str | None
    role: str   # user | assistant | summary
    kind: str   # message | action | summary
    text: str
    line_no: int


def _truncate(text: str) -> str:
    text = text.strip()
    if len(text) > _MAX_CHUNK_CHARS:
        return text[:_MAX_CHUNK_CHARS] + f"... [truncated, {len(text)} chars total]"
    return text


def _extract_user_text(content) -> str | None:
    """A real user-typed message is either a plain string, or (when mixed
    with an image/file attachment) a list containing a 'text' block. A
    'user' entry whose content is a list of ONLY tool_result blocks is not
    something the human typed - it's a tool result being fed back to the
    model - and is skipped entirely (that's already visible as an 'action'
    chunk on the assistant's preceding tool_use, without duplicating
    potentially large tool output here)."""
    if isinstance(content, str):
        return content if content.strip() else None
    if isinstance(content, list):
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        joined = "\n".join(t for t in texts if t.strip())
        return joined if joined.strip() else None
    return None


def _action_summary(name: str, tool_input: dict) -> str | None:
    fields = _ACTION_TOOLS.get(name)
    if fields is None:
        return None
    parts = []
    for f in fields:
        v = tool_input.get(f) if isinstance(tool_input, dict) else None
        if v:
            parts.append(str(v))
    if not parts:
        return None
    detail = " ".join(parts)
    if len(detail) > 300:
        detail = detail[:300] + "..."
    return f"{name}: {detail}"


def _parse_line(line: str, line_no: int) -> list[Chunk]:
    """Best-effort parse of one JSONL line into zero or more searchable
    chunks. Never raises - a malformed or unrecognized line yields no
    chunks rather than aborting the file (see module docstring)."""
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(d, dict):
        return []

    entry_type = d.get("type")
    session_id = d.get("sessionId") or ""
    ts = d.get("timestamp")
    chunks: list[Chunk] = []

    if entry_type == "user":
        message = d.get("message") or {}
        text = _extract_user_text(message.get("content"))
        if text:
            chunks.append(Chunk(session_id, ts, "user", "message", _truncate(text), line_no))

    elif entry_type == "assistant":
        message = d.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and block.get("text", "").strip():
                    chunks.append(Chunk(session_id, ts, "assistant", "message",
                                         _truncate(block["text"]), line_no))
                elif btype == "tool_use":
                    summary = _action_summary(block.get("name", ""), block.get("input") or {})
                    if summary:
                        chunks.append(Chunk(session_id, ts, "assistant", "action", summary, line_no))

    elif entry_type == "summary":
        # Claude Code's own compaction/session summaries - a condensed
        # recap of earlier conversation, valuable to keep since it covers
        # ground that may otherwise scroll out of any single transcript.
        text = d.get("summary") or d.get("text")
        if isinstance(text, str) and text.strip():
            chunks.append(Chunk(session_id, ts, "summary", "summary", _truncate(text), line_no))

    # Everything else (attachment, system, cost-state, mode, and any
    # future/unknown type) is Claude Code product bookkeeping, not
    # conversation content - intentionally skipped, not an error.
    return chunks


def find_session_files(repo_root: str, projects_dir: str | None = None) -> list[str]:
    """Top-level *.jsonl files for this repo's project directory, oldest
    first. Returns [] if Claude Code has no session history for this repo
    yet (or the directory-naming scheme has changed - see project_dir_name)
    rather than raising, since "no sessions indexed yet" is a normal state,
    not a failure."""
    base = projects_dir or claude_projects_dir()
    proj_dir = os.path.join(base, project_dir_name(repo_root))
    if not os.path.isdir(proj_dir):
        return []
    files = [
        os.path.join(proj_dir, f) for f in os.listdir(proj_dir)
        if f.endswith(".jsonl") and os.path.isfile(os.path.join(proj_dir, f))
    ]
    files.sort(key=lambda p: os.path.getmtime(p))
    return files


def index_sessions(conn, repo_root: str, projects_dir: str | None = None, force: bool = False) -> dict:
    """Incrementally index every session transcript for repo_root into
    transcript_chunks. Safe to call often: a file whose (size, mtime)
    hasn't changed since the last run is skipped with no I/O beyond a
    stat + one query. A grown file (the normal case for a session still
    appending) seeks to session_files.bytes_indexed and reads only the
    new bytes, rather than re-reading the whole transcript every run - a
    long-lived session's file only gets more expensive to index by the
    size of what's new, never by its total size so far. A SHRUNK file
    (edited/replaced, or --force) is treated as changed and fully
    re-indexed from byte 0 after purging its old chunks. Returns summary
    counts for the caller to print."""
    import sys as _sys

    files = find_session_files(repo_root, projects_dir)
    files_scanned = files_reindexed = files_skipped_unchanged = 0
    chunks_added = 0

    for path in files:
        try:
            st = os.stat(path)
        except OSError:
            continue
        size, mtime = st.st_size, st.st_mtime
        row = conn.execute(
            "SELECT size, mtime, bytes_indexed, lines_indexed FROM session_files WHERE path = ?", (path,)
        ).fetchone()

        if row and not force and row[0] == size and row[1] == mtime:
            files_skipped_unchanged += 1
            continue

        start_byte, start_line = 0, 0
        if row and not force and size >= row[0]:
            # Grown (or unchanged-but-forced with growth): resume after
            # what we've already parsed rather than re-reading from byte 0.
            start_byte, start_line = row[2], row[3]
        elif row:
            # Shrunk, replaced, or --force: this file's previous chunks are
            # now potentially stale/duplicated - purge and start clean.
            conn.execute("DELETE FROM transcript_chunks WHERE source_file = ?", (path,))

        files_reindexed += 1
        last_cwd = None
        session_id_seen = os.path.splitext(os.path.basename(path))[0]
        line_no = start_line
        new_rows = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                if start_byte:
                    f.seek(start_byte)
                for raw in f:
                    for chunk in _parse_line(raw, line_no):
                        new_rows.append((
                            chunk.session_id or session_id_seen, chunk.ts, chunk.role,
                            chunk.kind, chunk.text, path, chunk.line_no,
                        ))
                    try:
                        d = json.loads(raw)
                        if isinstance(d, dict) and d.get("cwd"):
                            last_cwd = d["cwd"]
                    except (json.JSONDecodeError, ValueError):
                        pass
                    line_no += 1
                end_byte = f.tell()
        except OSError as e:
            print(f"  ! skipping session file {path}: {e}", file=_sys.stderr)
            continue

        if new_rows:
            conn.executemany(
                "INSERT INTO transcript_chunks (session_id, ts, role, kind, text, source_file, line_no) "
                "VALUES (?,?,?,?,?,?,?)",
                new_rows,
            )
            chunks_added += len(new_rows)

        conn.execute(
            "INSERT INTO session_files (path, session_id, size, mtime, bytes_indexed, lines_indexed, last_cwd, indexed_at) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, "
            "bytes_indexed=excluded.bytes_indexed, lines_indexed=excluded.lines_indexed, "
            "last_cwd=COALESCE(excluded.last_cwd, session_files.last_cwd), indexed_at=excluded.indexed_at",
            (path, session_id_seen, size, mtime, end_byte, line_no, last_cwd, gl.now_iso()),
        )
        files_scanned += 1

    conn.commit()
    return {
        "session_files_found": len(files),
        "session_files_reindexed": files_reindexed,
        "session_files_unchanged": files_skipped_unchanged,
        "chunks_added": chunks_added,
    }
