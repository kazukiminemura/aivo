"""Optional local text history. No file is created until the first saved turn."""
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


class HistoryStore:
    def __init__(self, path):
        self.path = Path(path)

    @staticmethod
    def _session_id(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise ValueError("Conversation id must contain 1-128 characters")
        return value

    @staticmethod
    def _text(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 12000:
            raise ValueError("History text must contain 1-12000 characters")
        return value

    def _read(self):
        return sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)

    def append_turn(self, session_id, user_text, assistant_text):
        session_id = self._session_id(session_id)
        user_text, assistant_text = self._text(user_text), self._text(assistant_text)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
                updated_at TEXT NOT NULL, user_text TEXT NOT NULL, assistant_text TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS turns_session ON turns(session_id, id)")
            db.execute("INSERT INTO turns(session_id, updated_at, user_text, assistant_text) VALUES (?, ?, ?, ?)",
                       (session_id, datetime.now(timezone.utc).isoformat(), user_text, assistant_text))
        return session_id

    def list_conversations(self):
        if not self.path.is_file():
            return []
        with closing(self._read()) as db:
            rows = db.execute("""SELECT t.session_id, t.updated_at, t.user_text, groups.count
                FROM turns t JOIN (
                    SELECT session_id, MAX(id) AS last_id, COUNT(*) AS count FROM turns GROUP BY session_id
                ) groups ON t.id=groups.last_id ORDER BY t.id DESC LIMIT 100""").fetchall()
        return [dict(id=key, updated_at=updated, preview=text[:120], turns=count)
                for key, updated, text, count in rows]

    def get_conversation(self, session_id):
        session_id = self._session_id(session_id)
        if not self.path.is_file():
            return []
        with closing(self._read()) as db:
            rows = db.execute("SELECT user_text, assistant_text FROM turns WHERE session_id=? ORDER BY id DESC LIMIT 500",
                              (session_id,)).fetchall()
        return [message for user, assistant in reversed(rows)
                for message in ({"role": "user", "content": user}, {"role": "assistant", "content": assistant})]

    def delete_conversation(self, session_id):
        session_id = self._session_id(session_id)
        if not self.path.is_file():
            return
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("DELETE FROM turns WHERE session_id=?", (session_id,))

    def clear(self):
        if not self.path.is_file():
            return
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("DELETE FROM turns")
