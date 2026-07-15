"""Memory store — SQLite-backed session and event logging."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


DB_PATH = Path(__file__).parent / "watch_buddy.db"


class MemoryStore:
    """Persists watch sessions and events to SQLite."""

    def __init__(self, db_path: str | Path = DB_PATH):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_tables()
        self._current_session_id: int | None = None

    def _init_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS watch_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_title TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT
            );
            CREATE TABLE IF NOT EXISTS watch_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                summary TEXT NOT NULL,
                tags TEXT DEFAULT '',
                FOREIGN KEY (session_id) REFERENCES watch_sessions(id)
            );
        """)
        self._conn.commit()

    def start_session(self, stream_title: str) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            "INSERT INTO watch_sessions (stream_title, started_at) VALUES (?, ?)",
            (stream_title, now),
        )
        self._conn.commit()
        self._current_session_id = cur.lastrowid
        return self._current_session_id

    def end_session(self):
        if self._current_session_id is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE watch_sessions SET ended_at = ? WHERE id = ?",
            (now, self._current_session_id),
        )
        self._conn.commit()
        self._current_session_id = None

    def log_event(self, summary: str, tags: list[str] | None = None):
        if self._current_session_id is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        tag_str = ",".join(tags) if tags else ""
        self._conn.execute(
            "INSERT INTO watch_events (session_id, timestamp, summary, tags) VALUES (?, ?, ?, ?)",
            (self._current_session_id, now, summary, tag_str),
        )
        self._conn.commit()

    def get_recent_events(self, n: int = 10) -> list[dict]:
        rows = self._conn.execute(
            "SELECT timestamp, summary, tags FROM watch_events ORDER BY id DESC LIMIT ?",
            (n,),
        ).fetchall()
        return [dict(r) for r in rows]

    def search_events(self, query: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT timestamp, summary, tags FROM watch_events WHERE summary LIKE ? ORDER BY id DESC LIMIT 20",
            (f"%{query}%",),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self._conn.close()
