# language: Python 3.9+, file: tg-finder/db.py
"""SQLite: найденные чаты, критерии, история поисков."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id     INTEGER PRIMARY KEY,
    title       TEXT,
    username    TEXT,
    link        TEXT,
    members     INTEGER DEFAULT 0,
    kind        TEXT,
    can_post    INTEGER DEFAULT 0,
    about       TEXT,
    query       TEXT,
    found_at    INTEGER
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started    INTEGER,
    finished   INTEGER,
    found      INTEGER DEFAULT 0,
    queries    TEXT
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ chats
    def save_chat(self, chat: dict[str, Any]) -> bool:
        """True — чат новый, False — уже был."""
        row = self._conn.execute(
            "SELECT chat_id FROM chats WHERE chat_id = ?", (chat["chat_id"],)
        ).fetchone()
        if row:
            self._conn.execute(
                "UPDATE chats SET members = ?, can_post = ?, found_at = ? WHERE chat_id = ?",
                (chat.get("members", 0), int(bool(chat.get("can_post"))), int(time.time()),
                 chat["chat_id"]),
            )
            self._conn.commit()
            return False
        self._conn.execute(
            "INSERT INTO chats (chat_id, title, username, link, members, kind, can_post,"
            " about, query, found_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                chat["chat_id"],
                chat.get("title", ""),
                chat.get("username", ""),
                chat.get("link", ""),
                chat.get("members", 0),
                chat.get("kind", ""),
                int(bool(chat.get("can_post"))),
                (chat.get("about") or "")[:500],
                chat.get("query", ""),
                int(time.time()),
            ),
        )
        self._conn.commit()
        return True

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0]

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM chats ORDER BY found_at DESC, members DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def all_chats(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM chats ORDER BY members DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def export_csv(self, path: Path) -> Path:
        rows = self.all_chats()
        lines = ["title,link,username,members,type,can_post,about,found"]
        for row in rows:
            values = [row["title"], row["link"], row["username"], row["members"],
                      row["kind"], row["can_post"], row["about"], row["found_at"]]
            lines.append(",".join('"' + str(v).replace('"', '""') + '"' for v in values))
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def clear(self) -> None:
        self._conn.execute("DELETE FROM chats")
        self._conn.commit()

    # --------------------------------------------------------------- settings
    def get(self, key: str, default: str = "") -> str:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row and row[0] is not None else default

    def set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def criteria(self, defaults: dict[str, str]) -> dict[str, str]:
        return {key: self.get(key, default) for key, default in defaults.items()}

    def set_criteria(self, values: dict[str, str]) -> None:
        for key, value in values.items():
            self.set(key, value)

    # ------------------------------------------------------------------- runs
    def start_run(self, queries: str) -> int:
        cursor = self._conn.execute(
            "INSERT INTO runs (started, queries) VALUES (?, ?)", (int(time.time()), queries)
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, found: int) -> None:
        self._conn.execute(
            "UPDATE runs SET finished = ?, found = ? WHERE id = ?",
            (int(time.time()), found, run_id),
        )
        self._conn.commit()

    def last_runs(self, limit: int = 5) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
