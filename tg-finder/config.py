# language: Python 3.9+, file: tg-finder/config.py
"""Конфиг бота-поисковика чатов. Секреты — в .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default


# --- Telegram Bot (управление) ----------------------------------------------
BOT_TOKEN: str = os.getenv("FINDER_BOT_TOKEN", "").strip()
ADMIN_ID: int = _int("FINDER_ADMIN_ID", 0)

# --- Telegram user-аккаунт (поиск по MTProto) -------------------------------
# api_id/api_hash: https://my.telegram.org -> API development tools
API_ID: int = _int("TG_API_ID", 0)
API_HASH: str = os.getenv("TG_API_HASH", "").strip()
PHONE: str = os.getenv("TG_PHONE", "").strip()
SESSION_FILE: Path = BASE_DIR / "finder_session"

DB_FILE: Path = BASE_DIR / "finder.db"
LOG_FILE: Path = BASE_DIR / "finder.log"

# Сколько чатов максимум запрашивать одним поисковым запросом (лимит Telegram)
SEARCH_LIMIT: int = _int("SEARCH_LIMIT", 100)
# Пауза между запросами полной инфы о чате, сек (бережёт от FloodWait)
REQUEST_DELAY: float = float(os.getenv("REQUEST_DELAY", "0.5"))

DEFAULT_CRITERIA: dict[str, str] = {
    "keywords": "",        # через запятую: "крипта, инвестиции"
    "exclude": "",         # слова, при которых чат пропускаем
    "min_members": "500",
    "max_members": "500000",
    "kind": "group",       # group | channel | any
    "require_username": "1",   # только публичные чаты (t.me/username)
    "posting_only": "1",       # только там, где можно писать
    "limit": "40",             # сколько результатов за прогон
}
