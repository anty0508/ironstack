"""SQLite storage for the document library, meetings, transcripts and settings.

Access uses short-lived connections (one per call) so it is safe to call from
the Qt thread, the answer-worker thread and the transcriber thread.
"""

import sqlite3
from contextlib import contextmanager

import config

# Module-level so tests can point it at a temporary file before init_db().
DB_PATH = config.DB_PATH

DOCUMENT_KINDS = ("resume", "jd", "project", "reference")


@contextmanager
def _connect():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                kind            TEXT NOT NULL,
                title           TEXT NOT NULL,
                source_filename TEXT,
                content         TEXT NOT NULL,
                created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS meetings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                company     TEXT,
                created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS meeting_documents (
                meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                PRIMARY KEY (meeting_id, document_id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )


# --- documents ---

def add_document(kind, title, content, source_filename=None):
    if kind not in DOCUMENT_KINDS:
        raise ValueError(f"unknown document kind: {kind}")
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO documents (kind, title, source_filename, content) "
            "VALUES (?, ?, ?, ?)",
            (kind, title, source_filename, content),
        )
        return cur.lastrowid


def list_documents(kind=None):
    """Document rows (without the heavy `content` field), newest first."""
    sql = "SELECT id, kind, title, source_filename, created_at FROM documents"
    params = ()
    if kind is not None:
        sql += " WHERE kind = ?"
        params = (kind,)
    sql += " ORDER BY created_at DESC, id DESC"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def get_document(doc_id):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return dict(row) if row else None


def delete_document(doc_id):
    with _connect() as conn:
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))


# --- meetings ---

def create_meeting(title, company, document_ids):
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO meetings (title, company) VALUES (?, ?)",
            (title, company),
        )
        meeting_id = cur.lastrowid
        conn.executemany(
            "INSERT OR IGNORE INTO meeting_documents (meeting_id, document_id) "
            "VALUES (?, ?)",
            [(meeting_id, did) for did in document_ids],
        )
        return meeting_id


def list_meetings():
    with _connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT id, title, company, created_at FROM meetings "
                "ORDER BY created_at DESC, id DESC"
            )
        ]


def delete_meeting(meeting_id):
    # foreign_keys=ON + ON DELETE CASCADE removes the meeting's messages and
    # meeting_documents rows too (the source documents are left untouched).
    with _connect() as conn:
        conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))


def get_meeting_documents(meeting_id):
    """Full document rows (with content) linked to a meeting."""
    with _connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT d.* FROM documents d "
                "JOIN meeting_documents md ON md.document_id = d.id "
                "WHERE md.meeting_id = ? ORDER BY d.kind, d.id",
                (meeting_id,),
            )
        ]


# --- messages (transcript) ---

def add_message(meeting_id, role, content):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO messages (meeting_id, role, content) VALUES (?, ?, ?)",
            (meeting_id, role, content),
        )


def list_messages(meeting_id):
    with _connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT role, content, created_at FROM messages "
                "WHERE meeting_id = ? ORDER BY id",
                (meeting_id,),
            )
        ]


# --- settings ---

def get_setting(key, default=""):
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
