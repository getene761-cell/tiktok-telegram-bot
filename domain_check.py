# language: Python 3.9+, file: domain_check.py
"""Проверка домена почты: мусорный ли он и как его видит TikTok.

Две проверки:
    1. `disposable_email_blocklist` — публичный список одноразовых доменов
       (mail.tm, guerrillamail, 1secmail и тысячи других). Кэш на диск, TTL 7 дней.
    2. Живой пробник TikTok: открываем форму регистрации с адресом
       `probe<random>@домен`, проходим дату рождения/пароль, жмём «Отправить код»
       и читаем ответ страницы. Так видно: TikTok принял домен, отбил его или
       включил IP-троттлинг («Слишком много попыток»).

Пробник ничего не создаёт: адрес фейковый, код не вводится.
"""
from __future__ import annotations

import logging
import random
import string
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

import config

log = logging.getLogger(__name__)

DISPOSABLE_URL = (
    "https://raw.githubusercontent.com/disposable-email-domains/"
    "disposable-email-domains/master/disposable_email_blocklist.conf"
)
CACHE_FILE = config.BASE_DIR / "disposable_domains.txt"
CACHE_TTL = 7 * 24 * 3600

BLOCK_MARKERS = (
    "слишком много попыток",
    "too many attempts",
    "too many requests",
)
REJECT_MARKERS = (
    "недействительн",
    "не поддерживается",
    "используйте другой",
    "invalid",
    "not supported",
    "cannot be used",
    "unsupported email",
)


class _SilentSolver:
    """Пробнику не нужен человек: капчу просто пропускаем."""

    has_pending = False

    def request_sync(self, *args: Any, **kwargs: Any) -> Optional[str]:
        return None

    def ask_sync(self, *args: Any, **kwargs: Any) -> Optional[str]:
        return None


def disposable_set(refresh: bool = False) -> set[str]:
    if CACHE_FILE.exists() and not refresh:
        age = time.time() - CACHE_FILE.stat().st_mtime
        if age < CACHE_TTL:
            return {
                line.strip().lower()
                for line in CACHE_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
                if line.strip() and not line.startswith("#")
            }
    try:
        request = urllib.request.Request(DISPOSABLE_URL)
        request.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8", "ignore")
        CACHE_FILE.write_text(raw, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — без списка живём, просто не предупреждаем
        log.warning("список одноразовых доменов не скачался: %s", exc)
        return set()
    return {
        line.strip().lower()
        for line in raw.splitlines()
        if line.strip() and not line.startswith("#")
    }


def is_disposable(domain: str) -> bool:
    domain = domain.strip().lower().lstrip("@")
    domains = disposable_set()
    if not domains:
        return False
    if domain in domains:
        return True
    # поддомены мусорных зон: x.mail.tm, foo.guerrillamail.com
    return any(domain.endswith("." + bad) for bad in domains)


def probe_tiktok(domain: str, proxy: str = "") -> dict[str, Any]:
    """Открывает форму регистрации с фейковым адресом и возвращает вердикт TikTok."""
    from playwright.sync_api import sync_playwright

    from register import AccountRegistrar
    from temp_mail import gen_password

    domain = domain.strip().lower().lstrip("@")
    address = (
        "probe" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8)) + "@" + domain
    )
    status = config.Status()
    stop = threading.Event()
    registrar = AccountRegistrar(
        _SilentSolver(),
        None,
        dict(config.DEFAULT_SETTINGS),
        status,
        stop,
        progress=lambda message: log.info("probe: %s", message),
    )

    if not proxy:
        try:
            from proxy_pool import POOL

            proxy = POOL.acquire() or ""
        except Exception:  # noqa: BLE001
            proxy = ""
    launch_account: dict[str, Any] = {}
    if proxy:
        launch_account["proxy"] = proxy

    with sync_playwright() as pw:
        browser = registrar._launch(pw, launch_account)
        try:
            context = registrar._new_context(browser, {})
            page = context.new_page()
            if not registrar._open_signup(page):
                return {
                    "ok": False,
                    "verdict": "форма не открылась (прокси медленный или IP в блоке)",
                    "address": address,
                    "proxy": proxy,
                }
            registrar._fill_form(page, address, gen_password(14))
            try:
                body = page.inner_text("body", timeout=4000).lower()
            except Exception:  # noqa: BLE001
                body = ""
            if any(marker in body for marker in BLOCK_MARKERS):
                verdict = "TikTok режет по IP («Слишком много попыток») — домен тут ни при чём"
            elif any(marker in body for marker in REJECT_MARKERS):
                verdict = "TikTok отказал домену"
            elif any(marker in body for marker in ("код", "code", "письм", "email")):
                verdict = "похоже, TikTok принял домен (код отправлен)"
            else:
                verdict = "ответ не распознан, смотри скриншот"
            return {
                "ok": True,
                "verdict": verdict,
                "address": address,
                "proxy": proxy,
                "body": body[:300],
            }
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else ""
    if not target:
        raise SystemExit("usage: python domain_check.py example.com")
    print("мусорный домен:", is_disposable(target))
    print(probe_tiktok(target))
