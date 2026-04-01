"""FastAPI backend for Notemaster (notes CRUD + tagging + search).

This API serves a lightweight notes application backed by SQLite.
It supports:
- Create/read/update/delete notes
- List notes with filters (query, tags, archived)
- Basic tagging (attach/detach tags, list tags)
- Full-text search when SQLite FTS5 is available (falls back to LIKE)

Environment variables:
- SQLITE_DB: Path to the SQLite database file (provided by the database container)

Run:
- uvicorn src.api.main:app --host 0.0.0.0 --port 3001
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

OPENAPI_TAGS = [
    {"name": "Health", "description": "Service health and diagnostics."},
    {"name": "Notes", "description": "CRUD and listing for notes."},
    {"name": "Tags", "description": "Tag listing and management."},
]

app = FastAPI(
    title="Notemaster Notes API",
    description="REST API for a notes app with search and basic tagging (SQLite-backed).",
    version="0.2.0",
    openapi_tags=OPENAPI_TAGS,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For local dev; tighten in production.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _utc_now_iso() -> str:
    """Return current UTC time in ISO-8601 with Z suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _get_db_path() -> str:
    """Resolve DB path from environment."""
    db_path = os.getenv("SQLITE_DB")
    if not db_path:
        # IMPORTANT: orchestrator should set SQLITE_DB in the backend container .env
        # mapped to the database container path.
        raise RuntimeError("SQLITE_DB environment variable is not set.")
    return db_path


def _connect() -> sqlite3.Connection:
    """Get a new DB connection with row_factory set."""
    conn = sqlite3.connect(_get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _fts5_available(conn: sqlite3.Connection) -> bool:
    """Return True if the notes_fts table exists (and thus FTS is enabled)."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='notes_fts';"
    ).fetchone()
    return row is not None


def _fetch_note_tags(conn: sqlite3.Connection, note_id: int) -> List[str]:
    """Fetch tag names for a note."""
    rows = conn.execute(
        """
        SELECT t.name
        FROM tags t
        JOIN note_tags nt ON nt.tag_id = t.id
        WHERE nt.note_id = ?
        ORDER BY t.name ASC;
        """,
        (note_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def _note_row_to_model(conn: sqlite3.Connection, row: sqlite3.Row) -> "Note":
    """Convert a notes row to the API model including tags."""
    return Note(
        id=row["id"],
        title=row["title"],
        content=row["content"],
        tags=_fetch_note_tags(conn, row["id"]),
        is_archived=bool(row["is_archived"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class NoteBase(BaseModel):
    """Common note fields."""

    title: str = Field(..., min_length=1, max_length=500, description="Note title.")
    content: str = Field("", description="Note content (supports plain text).")
    tags: List[str] = Field(default_factory=list, description="List of tag names.")


class NoteCreate(NoteBase):
    """Payload for creating a note."""


class NoteUpdate(BaseModel):
    """Payload for updating a note (all fields optional)."""

    title: Optional[str] = Field(None, min_length=1, max_length=500, description="Note title.")
    content: Optional[str] = Field(None, description="Note content.")
    tags: Optional[List[str]] = Field(None, description="Replace the note's tags with this list.")
    is_archived: Optional[bool] = Field(None, description="Archive/unarchive the note.")


class Note(NoteBase):
    """A note with metadata."""

    id: int = Field(..., description="Note ID.")
    is_archived: bool = Field(False, description="Whether the note is archived.")
    created_at: str = Field(..., description="UTC timestamp when created (ISO-8601).")
    updated_at: str = Field(..., description="UTC timestamp when last updated (ISO-8601).")


class NotesListResponse(BaseModel):
    """Response wrapper for listing notes."""

    items: List[Note] = Field(..., description="Notes for this page/filter.")
    total: int = Field(..., description="Total notes matching the filters (ignores paging).")


class Tag(BaseModel):
    """Tag model."""

    id: int = Field(..., description="Tag ID.")
    name: str = Field(..., description="Unique tag name.")
    note_count: int = Field(..., description="Number of notes currently using this tag.")


class TagsListResponse(BaseModel):
    """Response wrapper for listing tags."""

    items: List[Tag] = Field(..., description="Tags.")


# PUBLIC_INTERFACE
@app.get(
    "/",
    tags=["Health"],
    summary="Health check",
    description="Basic health check endpoint.",
    operation_id="health_check",
)
def health_check() -> Dict[str, str]:
    """Return a simple health response."""
    return {"message": "Healthy"}


# PUBLIC_INTERFACE
@app.get(
    "/notes",
    response_model=NotesListResponse,
    tags=["Notes"],
    summary="List notes",
    description=(
        "List notes with optional filters: full-text query, tag filter, archived flag, and pagination."
    ),
    operation_id="list_notes",
)
def list_notes(
    q: Optional[str] = Query(None, description="Search query (full-text if available; otherwise LIKE)."),
    tag: Optional[str] = Query(None, description="Filter notes that have this tag name."),
    archived: Optional[bool] = Query(None, description="Filter by archived state."),
    limit: int = Query(50, ge=1, le=200, description="Max items to return."),
    offset: int = Query(0, ge=0, description="Offset for pagination."),
) -> NotesListResponse:
    """List notes with filters and pagination."""
    with _connect() as conn:
        fts_enabled = _fts5_available(conn)

        where_parts: List[str] = []
        params: List[Any] = []

        join_tag = False
        if tag:
            join_tag = True
            where_parts.append("t.name = ?")
            params.append(tag.strip())

        if archived is not None:
            where_parts.append("n.is_archived = ?")
            params.append(1 if archived else 0)

        if q:
            q_norm = q.strip()
            if fts_enabled:
                where_parts.append("n.id IN (SELECT note_id FROM notes_fts WHERE notes_fts MATCH ?)")
                params.append(q_norm)
            else:
                where_parts.append("(n.title LIKE ? OR n.content LIKE ?)")
                like = f"%{q_norm}%"
                params.extend([like, like])

        where_clause = "WHERE " + " AND ".join(where_parts) if where_parts else ""

        if join_tag:
            base_from = """
                FROM notes n
                JOIN note_tags nt ON nt.note_id = n.id
                JOIN tags t ON t.id = nt.tag_id
            """
        else:
            base_from = "FROM notes n"

        total_row = conn.execute(
            f"SELECT COUNT(DISTINCT n.id) AS c {base_from} {where_clause};",
            params,
        ).fetchone()
        total = int(total_row["c"]) if total_row else 0

        rows = conn.execute(
            f"""
            SELECT DISTINCT n.*
            {base_from}
            {where_clause}
            ORDER BY n.updated_at DESC
            LIMIT ? OFFSET ?;
            """,
            [*params, limit, offset],
        ).fetchall()

        items = [_note_row_to_model(conn, r) for r in rows]
        return NotesListResponse(items=items, total=total)


def _ensure_tag(conn: sqlite3.Connection, name: str) -> int:
    """Create tag if needed; return tag id."""
    tag_name = name.strip()
    if not tag_name:
        raise ValueError("Tag name cannot be empty.")
    conn.execute("INSERT OR IGNORE INTO tags(name) VALUES(?);", (tag_name,))
    row = conn.execute("SELECT id FROM tags WHERE name = ?;", (tag_name,)).fetchone()
    if not row:
        raise RuntimeError("Failed to upsert tag.")
    return int(row["id"])


def _replace_note_tags(conn: sqlite3.Connection, note_id: int, tags: List[str]) -> None:
    """Replace note tags with a given list."""
    # Normalize, de-dup, drop empties.
    normalized: List[str] = []
    seen = set()
    for t in tags:
        t2 = (t or "").strip()
        if not t2:
            continue
        if t2.lower() in seen:
            continue
        seen.add(t2.lower())
        normalized.append(t2)

    conn.execute("DELETE FROM note_tags WHERE note_id = ?;", (note_id,))
    for t in normalized:
        tag_id = _ensure_tag(conn, t)
        conn.execute(
            "INSERT OR IGNORE INTO note_tags(note_id, tag_id) VALUES(?, ?);",
            (note_id, tag_id),
        )


# PUBLIC_INTERFACE
@app.post(
    "/notes",
    response_model=Note,
    tags=["Notes"],
    summary="Create a note",
    description="Create a new note with optional tags.",
    operation_id="create_note",
)
def create_note(payload: NoteCreate) -> Note:
    """Create a note and return it."""
    with _connect() as conn:
        now = _utc_now_iso()
        cur = conn.execute(
            """
            INSERT INTO notes(title, content, created_at, updated_at)
            VALUES(?, ?, ?, ?);
            """,
            (payload.title.strip(), payload.content or "", now, now),
        )
        note_id = int(cur.lastrowid)
        _replace_note_tags(conn, note_id, payload.tags)
        conn.commit()

        row = conn.execute("SELECT * FROM notes WHERE id = ?;", (note_id,)).fetchone()
        return _note_row_to_model(conn, row)


# PUBLIC_INTERFACE
@app.get(
    "/notes/{note_id}",
    response_model=Note,
    tags=["Notes"],
    summary="Get a note",
    description="Fetch a single note by ID (including its tags).",
    operation_id="get_note",
)
def get_note(note_id: int) -> Note:
    """Get a note by id."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM notes WHERE id = ?;", (note_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Note not found")
        return _note_row_to_model(conn, row)


# PUBLIC_INTERFACE
@app.put(
    "/notes/{note_id}",
    response_model=Note,
    tags=["Notes"],
    summary="Update a note",
    description="Update note fields. Tags, if provided, replace existing tags.",
    operation_id="update_note",
)
def update_note(note_id: int, payload: NoteUpdate) -> Note:
    """Update a note and return it."""
    with _connect() as conn:
        existing = conn.execute("SELECT * FROM notes WHERE id = ?;", (note_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Note not found")

        fields: List[str] = []
        params: List[Any] = []
        if payload.title is not None:
            fields.append("title = ?")
            params.append(payload.title.strip())
        if payload.content is not None:
            fields.append("content = ?")
            params.append(payload.content)
        if payload.is_archived is not None:
            fields.append("is_archived = ?")
            params.append(1 if payload.is_archived else 0)

        # Always bump updated_at for any successful update (including tag-only updates).
        fields.append("updated_at = ?")
        params.append(_utc_now_iso())

        if fields:
            conn.execute(
                f"UPDATE notes SET {', '.join(fields)} WHERE id = ?;",
                (*params, note_id),
            )

        if payload.tags is not None:
            _replace_note_tags(conn, note_id, payload.tags)

        conn.commit()
        row = conn.execute("SELECT * FROM notes WHERE id = ?;", (note_id,)).fetchone()
        return _note_row_to_model(conn, row)


# PUBLIC_INTERFACE
@app.delete(
    "/notes/{note_id}",
    tags=["Notes"],
    summary="Delete a note",
    description="Delete a note by ID (tag links are removed via cascade).",
    operation_id="delete_note",
)
def delete_note(note_id: int) -> Dict[str, Any]:
    """Delete a note."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM notes WHERE id = ?;", (note_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Note not found")
        conn.commit()
        return {"deleted": True, "id": note_id}


# PUBLIC_INTERFACE
@app.get(
    "/tags",
    response_model=TagsListResponse,
    tags=["Tags"],
    summary="List tags",
    description="List tags ordered by usage count (desc) then name.",
    operation_id="list_tags",
)
def list_tags() -> TagsListResponse:
    """List all tags and their usage counts."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
              t.id,
              t.name,
              COUNT(nt.note_id) AS note_count
            FROM tags t
            LEFT JOIN note_tags nt ON nt.tag_id = t.id
            GROUP BY t.id, t.name
            ORDER BY note_count DESC, t.name ASC;
            """
        ).fetchall()

        items = [Tag(id=int(r["id"]), name=r["name"], note_count=int(r["note_count"])) for r in rows]
        return TagsListResponse(items=items)
