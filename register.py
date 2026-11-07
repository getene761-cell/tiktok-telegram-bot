# language: Python 3.9+, file: register.py
"""Авторегистрация аккаунтов TikTok с прохождением капчи через Telegram.

Логика одного аккаунта:
    1. ящик: mail.tm (временный) либо твой e-mail (код придёт тебе);
    2. форма: e-mail → дата рождения → пароль; TikTok подсовывает шаги по-разному,
       поэтому работает «драйвер формы»: на каждом шаге ищем видимое поле, которое
       ещё не заполнено, заполняем, жмём primary-кнопку;
    3. капча: тот же `_handle_captcha` из TikTokWorker-а (скриншот в Telegram);
    4. код из письма: mail.tm опрашивается сам; если ящик твой — бот спросит код
       у тебя; не пришёл — снова спросит;
    5. после успеха storage_state сохраняется в cookies/reg_<email>.json,
       аккаунт дописывается в accounts.json.

Честно про ограничения: TikTok может потребовать телефон на верификации, поменять
шаги формы или показать капчу, которую нельзя пройти без людей. Тогда регистрация
падает с понятной ошибкой и скриншотом в captcha_shots/ — это не баг сборки.
"""
from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime
from typing import Any, Callable, Optional

from playwright.sync_api import Error as PWError, Page, TimeoutError as PWTimeout, sync_playwright

import config
from temp_mail import TempMail, TempMailError, gen_password
from tiktok_worker import TikTokWorker, load_accounts, save_accounts

log = logging.getLogger(__name__)

Progress = Callable[[str], None]

SIGNUP_URLS = (
    "https://www.tiktok.com/signup",
    "https://www.tiktok.com/signup/phone-or-email/email",
)

EMAIL_SELECTORS = (
    'input[name="email"]',
    'input[type="email"]',
    'input[placeholder*="mail"]',
    'input[autocomplete="email"]',
)
PASSWORD_SELECTORS = (
    'input[name="password"]',
    'input[type="password"]',
)
CODE_SELECTORS = (
    'input[name="code"]',
    'input[name="verificationCode"]',
    'input[inputmode="numeric"]',
    'input[placeholder*="code"]',
    'input[autocomplete="one-time-code"]',
)
PRIMARY_SELECTORS = (
    'button[type="submit"]',
    'button[data-e2e="signup-button"]',
    'button[class*="Button"]',
)
EMAIL_TAB_SELECTORS = (
    'a[href*="phone-or-email/email"]',
    'div[data-e2e="email-tab"]',
    'span:has-text("Email")',
    'div:has-text("Использовать телефон или почту")',
)


def _random_birthday() -> tuple[str, str, str]:
    """Взрослый возраст, чтобы не отбило на первом же шаге."""
    year = random.randint(1990, 2003)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return str(year), str(month).zfill(2), str(day).zfill(2)


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


class AccountRegistrar(TikTokWorker):
    def __init__(self, *args: Any, progress: Optional[Progress] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.progress = progress or (lambda message: log.info(message))
        self.registered: list[dict[str, Any]] = []

    # ------------------------------------------------------------- entrypoint
    def register_many(
        self,
        count: int,
        email_mode: str = "temp",
        custom_email: str = "",
    ) -> list[dict[str, Any]]:
        """email_mode: temp (mail.tm) | custom (свой ящик) | manual (код спросим в боте)."""
        self.status.reset()
        self.status.is_running = True
        self.status.started_at = time.time()
        total = max(1, min(20, int(count)))
        for index in range(total):
            if self.stop_event.is_set():
                log.info("регистрация остановлена на %d/%d", index, total)
                break
            self.status.progress = f"{index + 1}/{total}"
            self.status.action = f"регистрация {index + 1}/{total}"
            self.progress(f"🆕 Регистрация {index + 1}/{total}: готовлю почту…")
            try:
                account = self._register_one(index, email_mode, custom_email)
            except Exception as exc:  # noqa: BLE001 — один провал не рушит пачку
                log.exception("регистрация %d провалилась", index + 1)
                self.progress(f"❌ Аккаунт {index + 1}: {exc}")
                continue
            if account:
                self.registered.append(account)
                self.status.comments_sent = len(self.registered)  # переиспользуем счётчик
                self.progress(
                    f"✅ Аккаунт {index + 1}: {account['email']}\n"
                    f"пароль: {account['password']}"
                )
            time.sleep(random.uniform(3, 7))
        self.status.action = "готово"
        self.status.is_running = False
        return self.registered

    # --------------------------------------------------------------- one pass
    def _register_one(
        self, index: int, email_mode: str, custom_email: str
    ) -> Optional[dict[str, Any]]:
        mail: Optional[TempMail] = None
        email = ""
        email_password = ""
        token = ""

        if email_mode == "temp":
            mail = TempMail()
            self.progress("📮 Создаю временный ящик mail.tm…")
            email, email_password, token = mail.create()
        elif email_mode == "custom":
            email = custom_email.strip()
            if not email or "@" not in email:
                raise RuntimeError("не задан свой e-mail")
        else:  # manual — почта твоя, код пришлёшь в бота
            email = custom_email.strip()
            if not email or "@" not in email:
                raise RuntimeError("не задан e-mail для ручного режима")

        password = gen_password(14)

        with sync_playwright() as pw:
            browser = self._launch(pw, {})
            context = None
            try:
                context = self._new_context(browser, {})
                page = context.new_page()
                self.page = page
                # Никаких старых cookies: регистрация всегда с чистого листа.
                try:
                    context.clear_cookies()
                except PWError:
                    pass

                if not self._open_signup(page):
                    raise RuntimeError("не открылась страница регистрации")

                if not self._fill_form(page, email, password):
                    self._dump_screenshot(page, index, "form")
                    raise RuntimeError("форма не заполнилась")

                code = self._obtain_code(page, mail, token, email)
                if not code:
                    self._dump_screenshot(page, index, "code")
                    raise RuntimeError("код подтверждения не получен")

                if not self._enter_code(page, code):
                    self._dump_screenshot(page, index, "enter_code")
                    raise RuntimeError("код не принят")

                page.wait_for_timeout(3000)
                self._handle_captcha(page)
                if not self._is_logged_in(page):
                    self._dump_screenshot(page, index, "after_signup")
                    raise RuntimeError("после регистрации нет сессии — вероятно, нужен телефон")

                cookies_path = config.COOKIES_DIR / f"reg_{_safe(email)}.json"
                context.storage_state(path=str(cookies_path))
                return {
                    "name": _safe(email.split("@")[0])[:20] or f"reg{index + 1}",
                    "username": email,
                    "password": password,
                    "email": email,
                    "email_password": email_password,
                    "cookies_file": str(cookies_path.relative_to(config.BASE_DIR)),
                    "proxy": None,
                    "enabled": True,
                    "registered_at": int(time.time()),
                }
            finally:
                if context is not None:
                    try:
                        context.close()
                    except PWError:
                        pass
                try:
                    browser.close()
                except PWError:
                    pass

    # ------------------------------------------------------------------ steps
    def _open_signup(self, page: Page) -> bool:
        for url in SIGNUP_URLS:
            try:
                page.goto(url, wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001
                continue
            self._settle(page, 2500)
            self._handle_captcha(page)
            self._dismiss_overlays(page)
            if page.locator("input").count():
                break
        # иногда сначала предлагают «телефон», надо переключиться на e-mail
        if self._first_visible(page, EMAIL_SELECTORS) is None:
            self._click_first(page, EMAIL_TAB_SELECTORS)
            page.wait_for_timeout(1500)
        return self._first_visible(page, EMAIL_SELECTORS) is not None or page.locator(
            "input"
        ).count() > 0

    def _fill_form(self, page: Page, email: str, password: str) -> bool:
        self.progress("✍️ Заполняю форму регистрации…")
        email_done = password_done = birthday_done = False

        for step in range(14):
            if self.stop_event.is_set():
                return False
            self._dismiss_overlays(page)
            self._handle_captcha(page)

            # дата рождения — три селекта, TikTok может прятать их за dropdown
            if not birthday_done:
                selects = page.locator("select")
                if selects.count() >= 3:
                    year, month, day = _random_birthday()
                    try:
                        selects.nth(0).select_option(label=_month_name(month))
                    except PWError:
                        try:
                            selects.nth(0).select_option(month)
                        except PWError:
                            pass
                    for idx, value in ((1, day), (2, year)):
                        try:
                            selects.nth(idx).select_option(value)
                        except PWError:
                            pass
                    birthday_done = True

            if not email_done:
                field = self._first_visible(page, EMAIL_SELECTORS)
                if field is not None and not (field.input_value() or "").strip():
                    # fill() фокусирует поле без клика мышью: выпадашка автокомплита
                    # и cookie-баннер больше не перехватывают pointer events.
                    self._type_into(field, email)
                    self._close_autocomplete(page, field)
                    email_done = True

            if not password_done:
                field = self._first_visible(page, PASSWORD_SELECTORS)
                if field is not None and not (field.input_value() or "").strip():
                    self._dismiss_overlays(page)
                    self._type_into(field, password)
                    password_done = True

            # уже просят код — форма заполнена
            if self._first_visible(page, CODE_SELECTORS) is not None:
                return True

            if email_done and password_done:
                # всё введено, но кнопки/шаги ещё могут понадобиться
                if not self._click_first(page, PRIMARY_SELECTORS):
                    page.keyboard.press("Enter")
            else:
                if email_done or birthday_done:
                    self._click_first(page, PRIMARY_SELECTORS)

            page.wait_for_timeout(random.randint(1200, 2200))
            if self._captcha_visible(page):
                self._handle_captcha(page)
            if self._first_visible(page, CODE_SELECTORS) is not None:
                return True

        return email_done and password_done

    def _obtain_code(
        self,
        page: Page,
        mail: Optional[TempMail],
        token: str,
        email: str,
    ) -> Optional[str]:
        if self._first_visible(page, CODE_SELECTORS) is None:
            # код просят не сразу — дадим странице секунду
            page.wait_for_timeout(2000)

        if mail is not None and token:
            self.progress("📬 Жду письмо с кодом (до 4 минут)…")
            code = mail.wait_for_code(token, timeout=240)
            if code:
                return code
            self.progress("⚠️ Письмо не пришло, спрашиваю код у тебя")

        code = self.solver.ask_sync(
            self.bot_loop,
            f"📨 Введи код подтверждения для {email}.\n"
            "Если письма нет — напиши «нет».",
        )
        if not code or code.strip().lower() in ("нет", "no", "none"):
            return None

        digits = _digits(code)
        return digits if len(digits) >= 4 else code.strip()

    def _enter_code(self, page: Page, code: str) -> bool:
        self.progress("🔑 Ввожу код подтверждения…")
        for _ in range(4):
            self._handle_captcha(page)
            field = self._first_visible(page, CODE_SELECTORS)
            if field is None:
                page.wait_for_timeout(1500)
                continue
            try:
                self._dismiss_overlays(page)
                self._type_into(field, code)
            except PWError:
                continue
            page.wait_for_timeout(600)
            if not self._click_first(page, PRIMARY_SELECTORS):
                page.keyboard.press("Enter")
            page.wait_for_timeout(random.randint(2500, 4000))
            self._handle_captcha(page)
            if self._is_logged_in(page):
                return True
            # код могли не принять — покажем ошибку и попробуем ещё раз, если поле живо
            if page.locator("input").count() == 0:
                break
        return self._is_logged_in(page)

    # ------------------------------------------------------------------ utils
    def _dismiss_overlays(self, page: Page) -> None:
        """Сносит cookie-баннер: он перехватывает клики поверх формы."""
        try:
            page.evaluate(
                "() => document.querySelectorAll('tiktok-cookie-banner')"
                ".forEach(node => node.remove())"
            )
        except PWError:
            pass
        for selector in (
            "tiktok-cookie-banner button",
            "button:has-text('Принять все')",
            "button:has-text('Принять')",
            "button:has-text('Accept all')",
        ):
            try:
                node = page.locator(selector).first
                if node.is_visible(timeout=600):
                    node.click(timeout=1500, force=True)
                    break
            except (PWError, PWTimeout):
                continue

    def _type_into(self, field: Any, text: str) -> None:
        """fill() -> фокус и вставка без мыши; fallback — клик с force и посимвольный ввод."""
        try:
            field.fill(text, timeout=5000)
            return
        except (PWError, PWTimeout):
            pass
        try:
            field.click(timeout=2000, force=True)
        except (PWError, PWTimeout):
            pass
        field.type(text, delay=random.randint(40, 100))

    def _close_autocomplete(self, page: Page, field: Any) -> None:
        try:
            page.keyboard.press("Escape")
            field.evaluate("node => node.blur()")
        except (PWError, PWTimeout):
            pass
        page.wait_for_timeout(300)

    def _dump_screenshot(self, page: Page, index: int, tag: str) -> None:
        try:
            path = config.CAPTCHA_DIR / f"reg_{index + 1}_{tag}_{int(time.time())}.png"
            page.screenshot(path=str(path), full_page=False)
            log.info("скриншот шага: %s", path)
            self.progress(f"🖼 Скриншот шага сохранён: {path.name}")
        except PWError:
            pass


def _safe(text: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", text)[:40]


def _month_name(month: str) -> str:
    try:
        return datetime.strptime(month, "%m").strftime("%B")
    except ValueError:
        return month


def append_account(account: dict[str, Any]) -> None:
    """Дописывает зарегистрированный аккаунт в accounts.json, не теряя старые."""
    accounts = load_accounts()
    accounts = [acc for acc in accounts if acc.get("username") != account.get("username")]
    accounts.append(account)
    save_accounts(accounts)
    log.info("аккаунт добавлен в accounts.json: %s", account.get("username"))
