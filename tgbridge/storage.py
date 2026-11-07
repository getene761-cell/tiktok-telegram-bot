# language: Python 3.9+, file: tgbridge/storage.py
"""Хранилище бота-переходника: SQLite, без внешних зависимостей.

Таблицы:
    users    — кто запустил бота, когда, подписался ли, откуда пришёл
    settings — канал, приветствие, контент после подписки
    events   — простые счётчики действий (для статистики по дням)
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT DEFAULT '',
    first_name  TEXT DEFAULT '',
    started_at  INTEGER DEFAULT 0,
    last_seen   INTEGER DEFAULT 0,
    subscribed  INTEGER DEFAULT 0,
    subscribed_at INTEGER DEFAULT 0,
    source      TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER,
    kind     TEXT,
    created  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, created);
"""


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ users
    def touch_user(
        self,
        user_id: int,
        username: str = "",
        first_name: str = "",
        source: str = "",
    ) -> bool:
        """Возвращает True, если пользователь новый."""
        now = int(time.time())
        row = self._conn.execute(
            "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row:
            self._conn.execute(
                "UPDATE users SET last_seen = ?, username = ?, first_name = ? WHERE user_id = ?",
                (now, username, first_name, user_id),
            )
            self._conn.commit()
            return False
        self._conn.execute(
            "INSERT INTO users (user_id, username, first_name, started_at, last_seen, source)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, first_name, now, now, source),
        )
        self._conn.commit()
        self.log_event(user_id, "start")
        return True

    def mark_subscribed(self, user_id: int) -> None:
        self._conn.execute(
            "UPDATE users SET subscribed = 1, subscribed_at = ? WHERE user_id = ? AND subscribed = 0",
            (int(time.time()), user_id),
        )
        self._conn.commit()

    def get_user(self, user_id: int) -> Optional[dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def log_event(self, user_id: int, kind: str) -> None:
        self._conn.execute(
            "INSERT INTO events (user_id, kind, created) VALUES (?, ?, ?)",
            (user_id, kind, int(time.time())),
        )
        self._conn.commit()

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict[str, Any]:
        now = int(time.time())
        day = now - 86400
        week = now - 7 * 86400
        conn = self._conn
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        today = conn.execute("SELECT COUNT(*) FROM users WHERE started_at >= ?", (day,)).fetchone()[0]
        week_new = conn.execute("SELECT COUNT(*) FROM users WHERE started_at >= ?", (week,)).fetchone()[0]
        subscribed = conn.execute("SELECT COUNT(*) FROM users WHERE subscribed = 1").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM users WHERE last_seen >= ?", (day,)).fetchone()[0]
        conversion = round(subscribed * 100 / total, 1) if total else 0.0
        return {
            "total": total,
            "today": today,
            "week": week_new,
            "subscribed": subscribed,
            "active24": active,
            "conversion": conversion,
        }

    def daily(self, days: int = 7) -> list[tuple[str, int]]:
        since = int(time.time()) - days * 86400
        rows = self._conn.execute(
            "SELECT date(started_at, 'unixepoch', 'localtime') AS d, COUNT(*)"
            " FROM users WHERE started_at >= ? GROUP BY d ORDER BY d",
            (since,),
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def recent(self, limit: int = 5) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM users ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def all_user_ids(self) -> list[int]:
        rows = self._conn.execute("SELECT user_id FROM users").fetchall()
        return [row[0] for row in rows]

    def export_csv(self, path: Path) -> Path:
        rows = self._conn.execute(
            "SELECT user_id, username, first_name, started_at, subscribed, source FROM users"
        ).fetchall()
        lines = ["user_id,username,first_name,started_at,subscribed,source"]
        for row in rows:
            lines.append(",".join('"' + str(value).replace('"', '""') + '"' for value in row))
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

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

    def close(self) -> None:
        self._conn.close()
