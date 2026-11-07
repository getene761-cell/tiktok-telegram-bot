# language: Python 3.9+, file: tgbridge/config.py
"""Конфиг бота-переходника на канал. Секреты — в .env."""
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


BOT_TOKEN: str = os.getenv("BRIDGE_BOT_TOKEN", "").strip()
ADMIN_ID: int = _int("BRIDGE_ADMIN_ID", 0)

# Канал, на который ведём. Пусто — задаётся из админки через бота.
CHANNEL_ID: str = os.getenv("BRIDGE_CHANNEL_ID", "").strip()
# Публичная ссылка на канал (для кнопки).
CHANNEL_URL: str = os.getenv("BRIDGE_CHANNEL_URL", "").strip()

DB_FILE: Path = BASE_DIR / "bridge.db"
LOG_FILE: Path = BASE_DIR / "bridge.log"

# Показывать ли админ-панель сразу при /start админу.
ADMIN_PANEL: bool = True
