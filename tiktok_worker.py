"""Playwright-логика TikTok.

Ключевое архитектурное решение: используется СИНХРОННЫЙ API Playwright и весь
воркер запускается в отдельном потоке через `asyncio.to_thread(...)`
(см. bot.py -> _launch). Поэтому event loop aiogram никогда не блокируется, а
ожидание ответа по капче организовано мостом `run_coroutine_threadsafe` из
`captcha_solver.py`.

Селекторы TikTok живут недолго. Все они собраны в константы ниже и почти везде
применяется «первый видимый из списка» — так проще править одну строку, а не
размазанную логику.
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PWError,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

import config
from captcha_solver import CaptchaSolver

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Минимальный stealth: убираем webdriver, правим languages/plugins/chrome,
# подменяем permissions.query для notifications. Глубже (CDP-патчи, canvas-шум)
# без отдельного драйвера лезть не стоит — риск сломать вёрстку.
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
window.chrome = window.chrome || { runtime: {} };
const _query = window.navigator.permissions && window.navigator.permissions.query;
if (_query) {
  window.navigator.permissions.query = (p) =>
    p && p.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : _query(p);
}
"""

LIKE_SELECTORS = (
    '[data-e2e="like-button"]',
    '[data-e2e="like-icon"]',
    'button[class*="LikeButton"]',
    'span[class*="LikeIcon"]',
)

COMMENT_ICON_SELECTORS = (
    '[data-e2e="comment-icon"]',
    '[data-e2e="browse-comment"]',
    'button[data-e2e="comment-button"]',
)

COMMENT_INPUT_SELECTORS = (
    '[data-e2e="comment-input"] div[contenteditable="true"]',
    'div[contenteditable="true"][data-e2e="comment-input"]',
    'div[class*="public-DraftEditor-content"]',
    'div[contenteditable="true"]',
)

COMMENT_POST_SELECTORS = (
    '[data-e2e="comment-post"]',
    'button[data-e2e="comment-post"]',
    'div[class*="CommentPost"]',
)

REPLY_ICON_SELECTORS = (
    '[data-e2e="comment-reply-icon"]',
    'span[data-e2e="comment-reply"]',
    'div[class*="ReplyIcon"]',
)

NEXT_ARROW_SELECTORS = (
    '[data-e2e="arrow-right"]',
    'button[data-e2e="arrow-right"]',
    '[data-e2e="feed-next"]',
)

CAPTCHA_CONTAINER_SELECTORS = (
    "#captcha-verify-image",
    'div[class*="captcha_verify_container"]',
    'div[id*="captcha-verify"]',
    'iframe[src*="captcha"]',
    'div[class*="Captcha"]',
)

SLIDER_SELECTORS = (
    ".captcha_verify_slide--slidebar",
    'div[class*="captcha_verify_slide"]',
    'div[class*="slidebar"]',
    'span[class*="slider"]',
    'div[class*="slider"]',
)

CAPTCHA_INPUT_SELECTORS = (
    "#captcha-verify-input",
    'input[name*="captcha"]',
    'input[class*="captcha"]',
    'input[placeholder*="code"]',
    'input[type="text"]',
)

CAPTCHA_SUBMIT_SELECTORS = (
    'button[class*="captcha"]',
    'button[type="submit"]',
    'button[class*="verify"]',
)

BLOCK_MARKERS = (
    "too many attempts",
    "try again later",
    "вы достигли предела",
    "аккаунт заблокирован",
    "account is suspended",
)


class AccountBlocked(RuntimeError):
    """Аккаунт заблокирован / превышен лимит попыток."""


class LoginFailed(RuntimeError):
    """Не удалось войти: нет сессии и пароль не подошёл."""


# --------------------------------------------------------------------------- #
#                              Аккаунты и cookies                             #
# --------------------------------------------------------------------------- #
def parse_spintax(text: str) -> str:
    """Раскрывает {a|b|c} и выбирает случайную строку из многострочного текста."""

    def _pick(match: re.Match[str]) -> str:
        return random.choice(match.group(1).split("|"))

    variants = [v for v in (text or "").splitlines() if v.strip()]
    if not variants:
        return ""
    chosen = random.choice(variants)
    return re.sub(r"\{([^{}]*)\}", _pick, chosen).strip()


def _norm_cookie(cookie: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Приводит cookie из разных форматов к тому, что ждёт Playwright."""
    name = cookie.get("name")
    value = cookie.get("value")
    if not name or value is None:
        return None
    out: dict[str, Any] = {
        "name": str(name),
        "value": str(value),
        "domain": cookie.get("domain") or ".tiktok.com",
        "path": cookie.get("path") or "/",
    }
    expires = cookie.get("expires", cookie.get("expirationDate"))
    if expires:
        try:
            out["expires"] = float(expires)
        except (TypeError, ValueError):
            pass
    same_site = cookie.get("sameSite")
    if isinstance(same_site, str):
        same_site = same_site.strip().lower()
        mapping = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None"}
        if same_site in mapping:
            out["sameSite"] = mapping[same_site]
    if "httpOnly" in cookie:
        out["httpOnly"] = bool(cookie["httpOnly"])
    if "secure" in cookie:
        out["secure"] = bool(cookie["secure"])
    return out


def parse_cookie_payload(raw: Any) -> list[dict[str, Any]]:
    """Принимает три формата:
    1) storage_state Playwright: {"cookies": [...]}
    2) обычный список cookie (экспорт расширения / DevTools)
    3) Netscape cookies.txt (строки через TAB)
    """
    if isinstance(raw, (dict, list)):
        data = raw
    else:
        text = str(raw).strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if data is None:
            cookies = []
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) != 7:
                    continue
                domain, _flag, path, secure, expiry, name, value = parts
                cookies.append(
                    {
                        "domain": domain,
                        "path": path,
                        "secure": secure.upper() == "TRUE",
                        "expires": expiry,
                        "name": name,
                        "value": value,
                    }
                )
            return [c for c in (_norm_cookie(x) for x in cookies) if c]

    if isinstance(data, dict) and "cookies" in data:
        data = data["cookies"]
    if not isinstance(data, list):
        return []
    return [c for c in (_norm_cookie(x) for x in data if isinstance(x, dict)) if c]


def read_cookie_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        log.warning("не читается cookie-файл %s: %s", path, exc)
        return []
    return parse_cookie_payload(raw)


def load_accounts() -> list[dict[str, Any]]:
    """accounts.json -> список аккаунтов. Fallback: acc.txt (login:password на строку)."""
    accounts: list[dict[str, Any]] = []
    if config.ACCOUNTS_FILE.exists():
        try:
            data = json.loads(config.ACCOUNTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data = data.get("accounts", [])
            for item in data:
                if not isinstance(item, dict):
                    continue
                if item.get("enabled", True) is False:
                    continue
                accounts.append(item)
        except (OSError, json.JSONDecodeError) as exc:
            log.error("accounts.json битый: %s", exc)

    if not accounts:
        legacy = config.BASE_DIR / "acc.txt"
        if legacy.exists():
            for line in legacy.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = re.split(r"[:;|\-]{1,4}", line, maxsplit=1)
                if len(parts) == 2 and parts[0] and parts[1]:
                    accounts.append({"name": parts[0], "username": parts[0], "password": parts[1]})
    return accounts


def save_accounts(accounts: list[dict[str, Any]]) -> None:
    config.ACCOUNTS_FILE.write_text(
        json.dumps({"accounts": accounts}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
#                                   Worker                                    #
# --------------------------------------------------------------------------- #
class TikTokWorker:
    def __init__(
        self,
        solver: CaptchaSolver,
        bot_loop: Any,
        settings: dict[str, Any],
        status: config.Status,
        stop_event: threading.Event,
    ) -> None:
        self.solver = solver
        self.bot_loop = bot_loop
        self.settings = settings
        self.status = status
        self.stop_event = stop_event
        self.page: Optional[Page] = None
        self._cookie_path = config.COOKIES_DIR / "session.json"

    # ------------------------------------------------------------- lifecycle
    def run(self, account: dict[str, Any]) -> None:
        name = str(account.get("name") or account.get("username") or "acc")
        # Счётчики обнуляем на старте, а не на финише: бот должен показать итог.
        self.status.reset()
        self.status.is_running = True
        self.status.account = name
        self.status.started_at = time.time()
        self.status.last_error = ""
        self.status.action = "запуск браузера"
        log.info("=== старт: %s ===", name)

        with sync_playwright() as pw:
            browser: Optional[Browser] = None
            context: Optional[BrowserContext] = None
            try:
                browser = self._launch(pw, account)
                context = self._new_context(browser, account)
                page = context.new_page()
                self.page = page
                self._cookie_path = self._cookie_target(account, name)

                if not self._ensure_logged_in(page, account):
                    raise LoginFailed("не удалось войти в аккаунт")

                self._save_cookies(context)
                self._run_cycles(page)
            except AccountBlocked as exc:
                self._fail(f"аккаунт заблокирован: {exc}")
            except LoginFailed as exc:
                self._fail(f"вход не выполнен: {exc}")
            except PWTimeout as exc:
                self._fail(f"таймаут Playwright: {exc}")
            except PWError as exc:
                self._fail(f"ошибка браузера: {exc}")
            except Exception as exc:  # noqa: BLE001 — воркер не должен ронять бота
                self._fail(f"непредвиденная ошибка: {exc}")
                log.exception("воркер упал")
            finally:
                self._save_cookies(context)
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:  # noqa: BLE001
                        pass
                self.status.is_running = False
                if self.status.action not in ("готово",):
                    self.status.action = "остановлено"
                log.info("=== стоп: %s ===", name)

    def _fail(self, message: str) -> None:
        self.status.last_error = message[:300]
        log.error(message)

    # --------------------------------------------------------------- browser
    def _launch(self, pw: Any, account: dict[str, Any]) -> Browser:
        kwargs: dict[str, Any] = {
            "headless": config.HEADLESS,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--mute-audio",
                "--no-first-run",
                f"--user-agent={USER_AGENT}",
            ],
        }
        if config.BROWSER_EXECUTABLE:
            kwargs["executable_path"] = config.BROWSER_EXECUTABLE
        proxy = account.get("proxy") or config.PROXY
        if proxy:
            kwargs["proxy"] = {"server": proxy}
        log.info("запуск chromium (headless=%s, exec=%s)", config.HEADLESS,
                 config.BROWSER_EXECUTABLE or "playwright-bundled")
        return pw.chromium.launch(**kwargs)

    def _new_context(self, browser: Browser, account: dict[str, Any]) -> BrowserContext:
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            ignore_https_errors=True,
            java_script_enabled=True,
        )
        context.add_init_script(STEALTH_JS)
        context.set_default_timeout(config.ACTION_TIMEOUT)
        context.set_default_navigation_timeout(config.NAV_TIMEOUT)

        cookies = self._collect_cookies(account)
        if cookies:
            try:
                context.add_cookies(cookies)
                log.info("подгружено cookies: %d", len(cookies))
            except PWError as exc:
                log.warning("cookies не приняты: %s", exc)
        return context

    def _cookie_target(self, account: dict[str, Any], name: str) -> Path:
        raw = account.get("cookies_file")
        if raw:
            path = Path(str(raw))
            if not path.is_absolute():
                path = config.BASE_DIR / path
            if path.suffix.lower() in (".json", ".txt"):
                return path
        safe = re.sub(r"[^\w.\-]+", "_", name)
        return config.COOKIES_DIR / f"{safe}.json"

    def _collect_cookies(self, account: dict[str, Any]) -> list[dict[str, Any]]:
        cookies = read_cookie_file(self._cookie_target(account, str(account.get("name", "acc"))))
        if cookies:
            return cookies
        inline = account.get("cookies")
        if inline:
            return parse_cookie_payload(inline)
        return []

    def _save_cookies(self, context: Optional[BrowserContext]) -> None:
        if context is None:
            return
        try:
            context.storage_state(path=str(self._cookie_path))
            log.info("cookies сохранены: %s", self._cookie_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("не удалось сохранить cookies: %s", exc)

    # ------------------------------------------------------------------ auth
    def _ensure_logged_in(self, page: Page, account: dict[str, Any]) -> bool:
        self.status.action = "вход в TikTok"
        page.goto("https://www.tiktok.com/", wait_until="domcontentloaded")
        self._settle(page)
        self._handle_captcha(page)

        if self._is_logged_in(page):
            log.info("сессия активна")
            return True

        username = str(account.get("username") or "").strip()
        password = str(account.get("password") or "").strip()
        if not username or not password:
            log.error("сессии нет, а логин/пароль не заданы")
            return False
        return self._login_password(page, username, password)

    def _is_logged_in(self, page: Page) -> bool:
        for sel in (
            '[data-e2e="profile-icon"]',
            '[data-e2e="profile-avatar"]',
            'a[data-e2e="profile-link"]',
        ):
            try:
                if page.locator(sel).first.is_visible(timeout=1500):
                    return True
            except (PWError, PWTimeout):
                continue
        try:
            if page.locator('[data-e2e="top-login-button"]').first.is_visible(timeout=1000):
                return False
        except (PWError, PWTimeout):
            pass
        return False

    def _login_password(self, page: Page, username: str, password: str) -> bool:
        log.info("вход по логину/паролю")
        self.status.action = "ввод логина"
        page.goto("https://www.tiktok.com/login/phone-or-email/email",
                  wait_until="domcontentloaded")
        self._settle(page)
        self._handle_captcha(page)

        user_input = self._first_visible(
            page, ('input[name="username"]', 'input[placeholder*="mail"]', 'input[type="text"]')
        )
        if user_input is None:
            log.error("поле логина не найдено")
            return False
        user_input.click()
        page.keyboard.type(username, delay=random.randint(50, 120))

        pass_input = self._first_visible(page, ('input[type="password"]', 'input[name="password"]'))
        if pass_input is None:
            log.error("поле пароля не найдено")
            return False
        pass_input.click()
        page.keyboard.type(password, delay=random.randint(50, 120))
        page.wait_for_timeout(random.randint(400, 900))

        self._click_first(
            page,
            ('button[type="submit"]', 'button[data-e2e="login-button"]', 'button[class*="LoginButton"]'),
        )
        page.wait_for_load_state("domcontentloaded")
        self._settle(page, 3000)
        self._handle_captcha(page)

        if self._is_logged_in(page):
            log.info("вход выполнен")
            self._save_cookies(page.context)
            return True
        log.error("после входа сессия не найдена")
        return False

    # --------------------------------------------------------------- actions
    def _run_cycles(self, page: Page) -> None:
        target = str(self.settings.get("target_url") or "").strip()
        use_url = self.settings.get("mode") == "url" and target

        if not use_url:
            self.status.action = "лента «Для тебя»"
            page.goto("https://www.tiktok.com/foryou", wait_until="domcontentloaded")
            self._settle(page, 2500)
            self._handle_captcha(page)

        total = max(1, int(self.settings.get("cycles") or 1))
        for index in range(total):
            if self.stop_event.is_set():
                log.info("остановка по флагу, цикл %d/%d", index, total)
                break
            self.status.progress = f"{index + 1}/{total}"
            log.info("цикл %d/%d (аккаунт %s)", index + 1, total, self.status.account)

            if use_url:
                self.status.action = "открываю видео"
                page.goto(target, wait_until="domcontentloaded")
                self._settle(page, 2500)
                self._handle_captcha(page)

            self._act_on_video(page)
            if index < total - 1 and not use_url:
                self._next_video(page)
            self._pause()

        self.status.action = "готово"
        log.info("циклы завершены: комментариев %d, лайков %d, капч %d",
                 self.status.comments_sent, self.status.likes_sent, self.status.captchas_seen)

    def _act_on_video(self, page: Page) -> None:
        self._check_blocked(page)
        if self.settings.get("do_like"):
            self._like(page)
            self._after_action(page)
        if self.settings.get("do_comment"):
            self._comment(page)
            self._after_action(page)
        if self.settings.get("do_reply"):
            self._reply(page)
            self._after_action(page)

    def _after_action(self, page: Page) -> None:
        # TikTok подкидывает капчу прямо посреди действия — ловим сразу.
        if self._captcha_visible(page):
            self._handle_captcha(page)

    def _like(self, page: Page) -> None:
        self.status.action = "лайк"
        btn = self._first_visible(page, LIKE_SELECTORS)
        if btn is None:
            log.warning("кнопка лайка не найдена")
            return
        try:
            if btn.get_attribute("aria-pressed") == "true":
                log.info("уже лайкнуто")
                return
        except PWError:
            pass
        try:
            btn.click(timeout=config.ACTION_TIMEOUT)
            self.status.likes_sent += 1
            log.info("лайк поставлен")
        except (PWError, PWTimeout) as exc:
            log.warning("лайк не удался: %s", exc)

    def _comment(self, page: Page) -> None:
        text = parse_spintax(str(self.settings.get("comment_text") or ""))
        if not text:
            log.warning("пустой текст комментария — пропуск")
            return
        self.status.action = f"комментарий: {text[:30]}"
        if not self._click_first(page, COMMENT_ICON_SELECTORS):
            log.warning("иконка комментариев не найдена")
            return
        page.wait_for_timeout(random.randint(600, 1200))

        box = self._first_visible(page, COMMENT_INPUT_SELECTORS)
        if box is None:
            log.warning("поле комментария не найдено")
            return
        try:
            box.click()
            page.keyboard.type(text, delay=random.randint(60, 150))
            page.wait_for_timeout(random.randint(300, 700))
            if not self._click_first(page, COMMENT_POST_SELECTORS):
                page.keyboard.press("Enter")
            page.wait_for_timeout(random.randint(800, 1600))
            self.status.comments_sent += 1
            log.info("комментарий отправлен: %s", text[:60])
        except (PWError, PWTimeout) as exc:
            log.warning("комментарий не отправлен: %s", exc)

    def _reply(self, page: Page) -> None:
        text = parse_spintax(str(self.settings.get("comment_text") or ""))
        if not text:
            return
        self.status.action = "ответ на комментарий"
        if not self._click_first(page, COMMENT_ICON_SELECTORS):
            return
        page.wait_for_timeout(random.randint(800, 1400))

        item = None
        for sel in ('[data-e2e="comment-level-1"]', 'div[class*="CommentItem"]', '[data-e2e="comment-item"]'):
            loc = self._first_visible(page, (sel,))
            if loc is not None:
                item = loc
                break
        if item is None:
            log.warning("комментарии для ответа не найдены")
            return
        try:
            item.hover()
            page.wait_for_timeout(300)
            if not self._click_first(page, REPLY_ICON_SELECTORS):
                log.warning("кнопка «ответить» не найдена")
                return
            page.wait_for_timeout(500)
            box = self._first_visible(page, COMMENT_INPUT_SELECTORS)
            if box is None:
                return
            box.click()
            page.keyboard.type(text, delay=random.randint(60, 150))
            if not self._click_first(page, COMMENT_POST_SELECTORS):
                page.keyboard.press("Enter")
            page.wait_for_timeout(random.randint(800, 1400))
            self.status.comments_sent += 1
            log.info("ответ отправлен")
        except (PWError, PWTimeout) as exc:
            log.warning("ответ не отправлен: %s", exc)

    def _next_video(self, page: Page) -> None:
        self.status.action = "следующее видео"
        # Стрелка надёжнее клика по кнопке: она работает и на /foryou, и в модалке.
        try:
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(random.randint(1200, 2200))
            return
        except PWError:
            pass
        self._click_first(page, NEXT_ARROW_SELECTORS)

    # ----------------------------------------------------------------- captcha
    def _captcha_visible(self, page: Page) -> bool:
        for sel in CAPTCHA_CONTAINER_SELECTORS:
            try:
                if page.locator(sel).first.is_visible(timeout=700):
                    return True
            except (PWError, PWTimeout):
                continue
        try:
            body = page.inner_text("body", timeout=1500).lower()
        except (PWError, PWTimeout):
            return False
        return any(marker in body for marker in ("drag the slider", "verify to continue",
                                                 "пройдите проверку", "подтвердите, что вы не робот"))

    def _captcha_kind(self, page: Page) -> str:
        if self._first_visible(page, SLIDER_SELECTORS) is not None:
            return "slide"
        if len(self._choice_elements(page)) >= 2:
            return "choice"
        return "text"

    def _choice_elements(self, page: Page) -> list[Any]:
        """Ищет кликабельные картинки-варианты (капча «выберите объекты»)."""
        found: list[Any] = []
        try:
            images = page.locator('div[class*="captcha"] img, div[id*="captcha"] img')
            for idx in range(min(images.count(), 9)):
                node = images.nth(idx)
                try:
                    box = node.bounding_box()
                except PWError:
                    continue
                if box and box["width"] >= 40 and box["height"] >= 40:
                    found.append(node)
        except PWError:
            return []
        return found

    def _captcha_screenshot(self, page: Page) -> Optional[bytes]:
        shot: Optional[bytes] = None
        for sel in CAPTCHA_CONTAINER_SELECTORS:
            try:
                node = page.locator(sel).first
                if node.is_visible(timeout=700):
                    shot = node.screenshot(type="png", timeout=6000)
                    break
            except (PWError, PWTimeout):
                continue
        if shot is None:
            try:
                shot = page.screenshot(type="png", full_page=False)
            except (PWError, PWTimeout) as exc:
                log.error("скриншот капчи не получен: %s", exc)
                return None
        try:
            (config.CAPTCHA_DIR / f"captcha_{int(time.time())}.png").write_bytes(shot)
        except OSError:
            pass
        return shot

    def _handle_captcha(self, page: Page, attempts: int = 3) -> bool:
        """Отправляет капчу в Telegram и применяет ответ. True — капча ушла."""
        for _ in range(attempts):
            if not self._captcha_visible(page):
                return True
            if self.stop_event.is_set():
                return False

            self.status.captchas_seen += 1
            self.status.action = "капча — жду ответ в Telegram"
            log.warning("обнаружена капча, слот #%d", self.status.captchas_seen)

            shot = self._captcha_screenshot(page)
            if shot is None:
                return False

            kind = self._captcha_kind(page)
            if kind == "slide":
                answer = self.solver.request_sync(
                    self.bot_loop,
                    shot,
                    "🧩 Слайдер-капча TikTok.\nОтправь число: на сколько пикселей "
                    "сдвинуть ползунок вправо (например 120).",
                    kind="text",
                )
                if answer and answer.strip().isdigit():
                    self._drag_slider(page, int(answer.strip()))
            elif kind == "choice":
                options = [f"Вариант {i + 1}" for i in range(len(self._choice_elements(page)))]
                answer = self.solver.request_sync(
                    self.bot_loop,
                    shot,
                    "🖼 Капча с вариантами. Какие картинки выбрать? Нажми кнопку ниже "
                    "(порядок слева-направо, сверху вниз).",
                    options=options,
                    kind="choice",
                )
                if answer is not None and answer.strip().isdigit():
                    self._click_choice(page, int(answer.strip()))
            else:
                answer = self.solver.request_sync(
                    self.bot_loop,
                    shot,
                    "🔐 Капча TikTok.\nВведи текст с картинки одним сообщением.",
                    kind="text",
                )
                if answer:
                    self._fill_captcha(page, answer)

            page.wait_for_timeout(random.randint(2200, 3200))
            self._settle(page, 1200)
            if not self._captcha_visible(page):
                log.info("капча пройдена")
                return True

        log.error("капча не пройдена за %d попытки", attempts)
        return False

    def _fill_captcha(self, page: Page, answer: str) -> None:
        for sel in CAPTCHA_INPUT_SELECTORS:
            try:
                node = page.locator(sel).first
                if node.is_visible(timeout=1200):
                    node.click()
                    page.keyboard.type(answer, delay=random.randint(40, 100))
                    page.wait_for_timeout(300)
                    if not self._click_first(page, CAPTCHA_SUBMIT_SELECTORS):
                        page.keyboard.press("Enter")
                    log.info("ответ капчи введён в поле %s", sel)
                    return
            except (PWError, PWTimeout):
                continue
        # Поля нет — жмём вслепую, некоторые варианты капчи принимают Enter.
        log.warning("поле капчи не найдено, ввод с клавиатуры")
        page.keyboard.type(answer)
        page.keyboard.press("Enter")

    def _click_choice(self, page: Page, index: int) -> None:
        nodes = self._choice_elements(page)
        if index >= len(nodes):
            log.warning("вариант %d вне диапазона (%d)", index, len(nodes))
            return
        try:
            nodes[index].click(timeout=config.ACTION_TIMEOUT)
        except (PWError, PWTimeout) as exc:
            log.warning("клик по варианту не удался: %s", exc)

    def _drag_slider(self, page: Page, offset: int) -> None:
        node = self._first_visible(page, SLIDER_SELECTORS)
        if node is None:
            log.warning("ползунок не найден")
            return
        try:
            box = node.bounding_box()
            if not box:
                return
            start_x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2
            page.mouse.move(start_x, y)
            page.mouse.down()
            steps = max(12, min(80, offset // 3))
            for step in range(1, steps + 1):
                x = start_x + offset * step / steps
                page.mouse.move(x, y + random.uniform(-1.5, 1.5))
                time.sleep(random.uniform(0.004, 0.012))
            page.mouse.up()
            log.info("ползунок сдвинут на %d px", offset)
        except (PWError, PWTimeout) as exc:
            log.warning("драг ползунка не удался: %s", exc)

    # ----------------------------------------------------------------- helpers
    def _first_visible(self, page: Page, selectors: Iterable[str]) -> Any:
        for sel in selectors:
            try:
                node = page.locator(sel).first
                if node.is_visible(timeout=1200):
                    return node
            except (PWError, PWTimeout):
                continue
        return None

    def _click_first(self, page: Page, selectors: Iterable[str]) -> bool:
        node = self._first_visible(page, selectors)
        if node is None:
            return False
        try:
            node.click(timeout=config.ACTION_TIMEOUT)
            return True
        except (PWError, PWTimeout) as exc:
            log.debug("клик по %s не удался: %s", selectors, exc)
            return False

    def _check_blocked(self, page: Page) -> None:
        try:
            body = page.inner_text("body", timeout=1500).lower()
        except (PWError, PWTimeout):
            return
        for marker in BLOCK_MARKERS:
            if marker in body:
                raise AccountBlocked(marker)

    def _settle(self, page: Page, ms: int = 1200) -> None:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=config.NAV_TIMEOUT)
        except (PWError, PWTimeout):
            pass
        page.wait_for_timeout(ms)

    def _pause(self) -> None:
        lo = float(self.settings.get("delay_min") or 5)
        hi = float(self.settings.get("delay_max") or lo + 5)
        if hi < lo:
            lo, hi = hi, lo
        total = random.uniform(lo, hi)
        self.status.action = f"пауза {total:.0f} с"
        log.info("пауза %.1f с", total)
        deadline = time.time() + total
        while time.time() < deadline:
            if self.stop_event.is_set():
                return
            time.sleep(0.5)
