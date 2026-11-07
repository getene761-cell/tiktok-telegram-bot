# language: Python 3.9+, file: smoke_signup.py
"""Дымовой прогон формы регистрации без бота и без реальной почты.

Открывает страницу регистрации тем же браузером и теми же шагами, что и
AccountRegistrar, и печатает, где именно остановился. Капчу не решает —
подставляет заглушку. Ничего не создаёт: e-mail фейковый, код не вводится.

Запуск:  PYTHONPATH=. python smoke_signup.py
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from playwright.sync_api import sync_playwright

import config
from register import AccountRegistrar


class DummySolver:
    has_pending = False

    def request_sync(self, loop: Any, image: Any, caption: str, options: Any = None, kind: str = "text") -> Optional[str]:
        print(f"[капча] {caption[:70]!r}")
        return None

    def ask_sync(self, loop: Any, caption: str, timeout: Any = None) -> Optional[str]:
        print(f"[вопрос] {caption[:70]!r}")
        return None


def main() -> None:
    status = config.Status()
    stop = threading.Event()
    registrar = AccountRegistrar(
        DummySolver(), None, dict(config.DEFAULT_SETTINGS), status, stop,
        progress=lambda message: print("[prog]", message),
    )

    with sync_playwright() as pw:
        browser = registrar._launch(pw, {})
        context = registrar._new_context(browser, {})
        page = context.new_page()
        page.set_default_timeout(config.ACTION_TIMEOUT)

        opened = registrar._open_signup(page)
        print("страница открыта:", opened, "| url:", page.url)

        filled = registrar._fill_form(page, "smoketest12345@example.com", "SmokeTest!2345")
        print("форма заполнена:", filled)
        print("inputs:", page.locator("input").count())
        print("url после формы:", page.url)

        page.screenshot(path=str(config.CAPTCHA_DIR / "smoke_signup.png"))
        print("скриншот:", config.CAPTCHA_DIR / "smoke_signup.png")

        context.close()
        browser.close()


if __name__ == "__main__":
    main()
