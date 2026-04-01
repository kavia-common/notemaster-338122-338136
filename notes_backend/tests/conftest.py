import sqlite3
import sys
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient


def _init_sqlite_schema(db_path: str) -> None:
    """
    Initialize a minimal SQLite schema required by src.api.main endpoints.

    The app expects tables:
    - notes
    - tags
    - note_tags
    And optionally notes_fts for FTS-backed search.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")

        conn.execute(
            """
            CREATE TABLE notes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                is_archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

        conn.execute(
            """
            CREATE TABLE tags(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            """
        )

        conn.execute(
            """
            CREATE TABLE note_tags(
                note_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                PRIMARY KEY(note_id, tag_id),
                FOREIGN KEY(note_id) REFERENCES notes(id) ON DELETE CASCADE,
                FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE
            );
            """
        )

        # Enable FTS search path used by GET /notes?q=...
        conn.execute(
            """
            CREATE VIRTUAL TABLE notes_fts
            USING fts5(
                title,
                content,
                note_id UNINDEXED,
                tokenize = 'unicode61'
            );
            """
        )

        # Keep notes_fts in sync with notes.
        conn.execute(
            """
            CREATE TRIGGER notes_ai AFTER INSERT ON notes BEGIN
                INSERT INTO notes_fts(rowid, title, content, note_id)
                VALUES (new.id, new.title, new.content, new.id);
            END;
            """
        )
        conn.execute(
            """
            CREATE TRIGGER notes_ad AFTER DELETE ON notes BEGIN
                DELETE FROM notes_fts WHERE rowid = old.id;
            END;
            """
        )
        conn.execute(
            """
            CREATE TRIGGER notes_au AFTER UPDATE ON notes BEGIN
                UPDATE notes_fts SET title = new.title, content = new.content
                WHERE rowid = new.id;
            END;
            """
        )

        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="session")
def _ensure_src_on_path() -> None:
    """
    Ensure `import src...` works when pytest runs from container root.

    This avoids requiring extra pytest plugins (e.g., pytest-pythonpath).
    """
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _ensure_src_on_path: None) -> Iterator[TestClient]:
    """
    Provide a FastAPI TestClient backed by an isolated temporary SQLite DB.

    We set SQLITE_DB to point at a temp file and create the required schema,
    so tests do not depend on any external DB container state.
    """
    db_path = str(tmp_path / "test_notes.db")
    _init_sqlite_schema(db_path)

    monkeypatch.setenv("SQLITE_DB", db_path)

    from src.api.main import app

    with TestClient(app) as c:
        yield c
