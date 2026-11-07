# language: Python 3.9+, file: config.py
"""Центральная конфигурация проекта.

Все секреты (BOT_TOKEN, ADMIN_ID) читаются из .env и НЕ попадают в git.
Пути строятся от каталога файла, поэтому запускать можно из любого cwd.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "1" if default else "0")).strip().lower()
    return raw not in ("0", "false", "no", "off", "")


# --- Telegram ---------------------------------------------------------------
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID: int = _int("ADMIN_ID", 0)

# --- Playwright / браузер ---------------------------------------------------
# На Termux системный chromium ставится пакетом, поэтому путь можно задать вручную.
BROWSER_EXECUTABLE: str = os.getenv("BROWSER_EXECUTABLE", "").strip() or ""
HEADLESS: bool = _bool("HEADLESS", True)
# Android часто не тянет полноценный Chromium в headless — таймауты в мс.
NAV_TIMEOUT: int = _int("NAV_TIMEOUT", 60000)
ACTION_TIMEOUT: int = _int("ACTION_TIMEOUT", 8000)
PROXY: str = os.getenv("PROXY", "").strip() or ""

# --- Капча ------------------------------------------------------------------
# Сколько секунд бот ждёт ответа администратора по капче, прежде чем сдаться.
CAPTCHA_TIMEOUT: int = _int("CAPTCHA_TIMEOUT", 300)

# --- Пути -------------------------------------------------------------------
ACCOUNTS_FILE: Path = BASE_DIR / "accounts.json"
COOKIES_DIR: Path = BASE_DIR / "cookies"
CAPTCHA_DIR: Path = BASE_DIR / "captcha_shots"
LOG_FILE: Path = BASE_DIR / "bot.log"

for _p in (COOKIES_DIR, CAPTCHA_DIR):
    _p.mkdir(parents=True, exist_ok=True)


# --- Настройки автоматизации по умолчанию -----------------------------------
# Живут в памяти процесса; меняются из меню настроек бота.
DEFAULT_SETTINGS: dict[str, Any] = {
    "comment_text": "🔥",          # текст комментария; поддерживает {a|b|c} и строки
    "delay_min": 8,                # пауза между действиями, сек
    "delay_max": 20,
    "cycles": 10,                  # сколько видео обработать за запуск
    "mode": "feed",                # feed — лента «Для тебя», url — конкретное видео
    "target_url": "",              # используется при mode == "url"
    "do_like": True,
    "do_comment": True,
    "do_reply": False,
    "max_comments_per_account": 30,  # защита от «too many attempts»
}


class Status:
    """Разделяемое состояние для меню «Статус».

    Мутируется из потока Playwright, читается из event loop бота.
    Отдельные поля-атомы, поэтому GIL здесь достаточная защита.
    """

    def __init__(self) -> None:
        self.is_running: bool = False
        self.account: str = "—"
        self.action: str = "idle"
        self.progress: str = "0/0"
        self.comments_sent: int = 0
        self.likes_sent: int = 0
        self.captchas_seen: int = 0
        self.started_at: float | None = None
        self.last_error: str = ""
        self.task: Any = None

    def uptime(self) -> str:
        if not self.started_at:
            return "—"
        sec = int(time.time() - self.started_at)
        return f"{sec // 60:02d}:{sec % 60:02d}"

    def reset(self) -> None:
        self.is_running = False
        self.account = "—"
        self.action = "idle"
        self.progress = "0/0"
        self.comments_sent = 0
        self.likes_sent = 0
        self.captchas_seen = 0
        self.started_at = None
        self.task = None


STATUS = Status()

# Глобальный флаг остановки: threading.Event, потому что его читает поток Playwright.
import threading  # noqa: E402  (осознанно ниже, рядом с использованием)

STOP_EVENT = threading.Event()
