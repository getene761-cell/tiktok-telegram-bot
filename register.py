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
from typing import Any, Callable, Iterable, Optional

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
            last_error: Optional[Exception] = None
            for attempt in range(3):
                try:
                    email, email_password, token = mail.create()
                    break
                except TempMailError as exc:
                    last_error = exc
                    # mail.tm режет частые создания ящиков (429) — ждём и пробуем снова
                    self.progress(f"📮 mail.tm занят ({exc}), пауза 20 с…")
                    time.sleep(20)
            else:
                raise RuntimeError(f"mail.tm не дал ящик: {last_error}")
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
        # Прямой URL: /signup отдаёт лендинг и уводит на вкладку «телефон».
        page.goto(
            "https://www.tiktok.com/signup/phone-or-email/email",
            wait_until="domcontentloaded",
            timeout=config.NAV_TIMEOUT,
        )
        self._settle(page, 3000)
        self._dismiss_overlays(page)
        self._handle_captcha(page)
        try:
            page.locator('input[name="email"]').first.wait_for(state="visible", timeout=20000)
            return True
        except (PWError, PWTimeout):
            self._dump_screenshot(page, 0, "signup_open")
            return False

    def _click_text(self, page: Page, texts: Iterable[str], timeout: int = 4000) -> bool:
        """Жмёт кнопку по тексту: у TikTok кнопки без submit и без data-e2e."""
        for text in texts:
            for locator in (
                page.get_by_role("button", name=text, exact=False),
                page.get_by_text(text, exact=False),
            ):
                try:
                    if locator.count():
                        locator.first.click(timeout=timeout, force=True)
                        return True
                except (PWError, PWTimeout):
                    continue
        return False

    def _select_combo(
        self,
        page: Page,
        combo: Any,
        wanted: Optional[Iterable[str]] = None,
        by_index: int = 0,
    ) -> bool:
        """Раскрывает комбобокс и жмёт видимую опцию: по тексту или по позиции."""
        try:
            combo.click(timeout=4000, force=True)
            page.wait_for_timeout(900)
        except (PWError, PWTimeout):
            return False

        options = page.get_by_role("option")
        visible: list[Any] = []
        for index in range(min(options.count(), 400)):
            try:
                option = options.nth(index)
                if option.is_visible():
                    visible.append(option)
            except (PWError, PWTimeout):
                continue
        if not visible:
            try:
                page.keyboard.press("Escape")
            except PWError:
                pass
            return False

        if wanted:
            for option in visible:
                try:
                    text = (option.inner_text() or "").strip()
                except (PWError, PWTimeout):
                    continue
                if text in wanted:
                    try:
                        option.click(timeout=2500, force=True)
                        return True
                    except (PWError, PWTimeout):
                        break

        target = visible[min(max(by_index, 0), len(visible) - 1)]
        try:
            target.click(timeout=2500, force=True)
            return True
        except (PWError, PWTimeout):
            try:
                page.keyboard.press("Escape")
            except PWError:
                pass
            return False

    def _pick_birthday(self, page: Page) -> None:
        """Три комбобокса: Месяц, День, Год. Год — взрослый, по тексту опции."""
        combos = page.locator('[role="combobox"]')
        if combos.count() < 3:
            return
        year, month, day = _random_birthday()
        if self.stop_event.is_set():
            return
        self._select_combo(page, combos.nth(0), by_index=int(month) - 1)
        self._select_combo(page, combos.nth(1), by_index=int(day) - 1)
        self._select_combo(
            page,
            combos.nth(2),
            wanted=[str(value) for value in range(1990, 2004)],
            by_index=3,  # первые видимые годы — 2025, 2024, 2023, 2022
        )

    def _fill_form(self, page: Page, email: str, password: str) -> bool:
        self.progress("✍️ Заполняю форму регистрации…")
        self._dismiss_overlays(page)

        email_field = self._first_visible(page, EMAIL_SELECTORS)
        if email_field is None:
            return False
        self._type_into(email_field, email)
        self._close_autocomplete(page, email_field)

        self._pick_birthday(page)
        self._handle_captcha(page)

        password_field = self._first_visible(page, PASSWORD_SELECTORS)
        if password_field is None:
            return False
        self._type_into(password_field, password)

        try:
            checkbox = page.locator('input[type="checkbox"]').first
            if checkbox.count() and not checkbox.is_checked():
                checkbox.check(force=True, timeout=3000)
        except (PWError, PWTimeout):
            pass

        if not self._click_text(page, ("Отправить код", "Зарегистрироваться", "Далее")):
            self._click_first(page, PRIMARY_SELECTORS)
        page.wait_for_timeout(random.randint(2500, 4000))
        self._handle_captcha(page)

        for _ in range(6):
            if self._first_visible(page, CODE_SELECTORS) is not None:
                return True
            if self._is_logged_in(page):
                return True
            page.wait_for_timeout(1500)
            self._handle_captcha(page)
        return True

    def _obtain_code(
        self,
        page: Page,
        mail: Optional[TempMail],
        token: str,
        email: str,
    ) -> Optional[str]:
        """Код: из временного ящика, иначе — вопросом в Telegram."""
        if self._first_visible(page, CODE_SELECTORS) is None:
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
        for attempt in range(4):
            self._dismiss_overlays(page)
            self._handle_captcha(page)
            field = self._first_visible(page, CODE_SELECTORS)
            if field is None:
                if self._is_logged_in(page):
                    return True
                page.wait_for_timeout(1500)
                continue
            self._type_into(field, code)
            page.wait_for_timeout(500)
            if not self._click_text(
                page, ("Отправить код", "Зарегистрироваться", "Далее", "Продолжить")
            ):
                self._click_first(page, PRIMARY_SELECTORS)
            page.wait_for_timeout(random.randint(2500, 4000))
            self._handle_captcha(page)
            if self._is_logged_in(page):
                return True
            if attempt >= 2:
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
