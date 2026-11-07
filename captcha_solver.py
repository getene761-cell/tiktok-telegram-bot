"""Отправка капчи в Telegram и ожидание ответа администратора.

Идея: один ожидающий запрос в каждый момент времени.
    * поток Playwright (sync API) вызывает request_sync(...) — блокируется;
    * корутина request(...) исполняется в event loop бота, шлёт фото и ждёт Future;
    * хендлер бота (текст или inline-кнопка) вызывает submit_text / submit_choice.

Никакого внешнего сервиса: только глаза администратора.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


class CaptchaSolver:
    def __init__(self, bot: Any, admin_id: int, timeout: int = 300) -> None:
        self.bot = bot
        self.admin_id = admin_id
        self.timeout = timeout
        self._future: Optional[asyncio.Future] = None
        self._options: list[str] = []
        self._kind: str = "text"

    # ------------------------------------------------------------------ state
    @property
    def has_pending(self) -> bool:
        return self._future is not None and not self._future.done()

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def options(self) -> list[str]:
        return list(self._options)

    # --------------------------------------------------------------- outbound
    async def _send(self, image: bytes, caption: str, options: list[str] | None) -> None:
        from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

        markup = None
        if options:
            # Каждая кнопка несёт номер варианта — по нему воркер найдёт элемент на странице.
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=f"{i + 1}. {opt[:30]}", callback_data=f"cap:{i}")]
                    for i, opt in enumerate(options)
                ]
            )
        await self.bot.send_photo(
            chat_id=self.admin_id,
            photo=BufferedInputFile(image, filename="captcha.png"),
            caption=caption,
            reply_markup=markup,
            parse_mode=None,
        )

    # ------------------------------------------------------------------ await
    async def request(
        self,
        image: bytes,
        caption: str,
        options: list[str] | None = None,
        kind: str = "text",
    ) -> Optional[str]:
        """Шлёт скриншот, ждёт ответа. Возвращает строку ответа или None по таймауту."""
        if self.has_pending:
            log.warning("капча: уже есть ожидающий запрос, новый проигнорирован")
            return None

        loop = asyncio.get_running_loop()
        self._future = loop.create_future()
        self._options = list(options or [])
        self._kind = kind
        try:
            await self._send(image, caption, options)
            answer = await asyncio.wait_for(self._future, timeout=self.timeout)
            log.info("капча: получен ответ %r", answer)
            return answer
        except asyncio.TimeoutError:
            log.warning("капча: таймаут %s с", self.timeout)
            try:
                await self.bot.send_message(
                    self.admin_id,
                    f"⏳ Время ожидания капчи истекло ({self.timeout} с). "
                    "Автоматизация остановлена на этом шаге.",
                )
            except Exception:  # noqa: BLE001 — телеграм мог отвалиться, это не критично
                pass
            return None
        finally:
            self._future = None
            self._options = []
            self._kind = "text"

    def request_sync(
        self,
        bot_loop: asyncio.AbstractEventLoop,
        image: bytes,
        caption: str,
        options: list[str] | None = None,
        kind: str = "text",
    ) -> Optional[str]:
        """Мост из потока Playwright в event loop бота (блокирующее ожидание)."""
        future = asyncio.run_coroutine_threadsafe(
            self.request(image, caption, options, kind), bot_loop
        )
        try:
            # Небольшой запас сверх собственного таймаута корутины.
            return future.result(timeout=self.timeout + 30)
        except asyncio.TimeoutError:
            future.cancel()
            log.error("капча: request_sync не дождался корутины")
            return None
        except Exception as exc:  # noqa: BLE001
            log.exception("капча: ошибка запроса: %s", exc)
            return None

    # ---------------------------------------------------------------- inbound
    def submit_text(self, text: str) -> bool:
        if self.has_pending and self._future is not None:
            self._future.set_result(text.strip())
            return True
        return False

    def submit_choice(self, index: int) -> bool:
        if not self.has_pending or self._future is None:
            return False
        if 0 <= index < len(self._options):
            self._future.set_result(str(index))
            return True
        return False

    def cancel(self) -> None:
        """Снять ожидание, не дожидаясь таймаута (например, по кнопке «Стоп»)."""
        if self._future is not None and not self._future.done():
            self._future.set_result(None)
