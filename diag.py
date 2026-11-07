# language: Python 3.9+, file: diag.py
"""Диагностика: рендерится ли TikTok в текущем браузере и жива ли сессия.

Отдельный модуль, чтобы не плодить кавычки в bot.py: команда /diag просто
вызывает `run_diag(settings)` в потоке и печатает отчёт.
"""
from __future__ import annotations

import threading
from typing import Any

import config
from tiktok_worker import TikTokWorker, load_accounts


def run_diag(settings: dict[str, Any]) -> str:
    from playwright.sync_api import sync_playwright

    accounts = load_accounts()
    account = accounts[0] if accounts else {}
    worker = TikTokWorker(None, None, dict(settings), config.Status(), threading.Event())
    lines: list[str] = []
    with sync_playwright() as pw:
        browser = worker._launch(pw, {})
        try:
            context = worker._new_context(browser, account)
            page = context.new_page()
            page.goto(
                "https://www.tiktok.com/",
                wait_until="domcontentloaded",
                timeout=config.NAV_TIMEOUT,
            )
            page.wait_for_timeout(8000)
            body = len(page.inner_text("body"))
            logged = worker._api_logged_in(page)
            lines.append("Сессия (API): " + ("активна" if logged else "НЕТ"))
            lines.append(f"Текст страницы: {body} символов")
            if body < 50:
                lines.append("")
                lines.append("TikTok отдал пустую страницу — DOM не рендерится,")
                lines.append("кнопки комментария в ней нет. Что проверить:")
                lines.append("1) переключить сеть: Wi-Fi ↔ мобильный интернет;")
                lines.append("2) свой прокси в REG_PROXY (.env);")
                lines.append("3) запуск на ПК — там Chrome рендерит нормально.")
            else:
                lines.append("")
                lines.append("Рендер в порядке — можно комментировать.")
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
    return "\n".join(lines)
