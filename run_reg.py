# language: Python 3.9+, file: run_reg.py
"""Автономный прогон регистрации без основного бота.

Зачем: бот на устройстве держит getUpdates, второй поллер получит 409.
Поэтому основной процесс на время прогона останавливается, а этот скрипт:
    * сам поллит Telegram и превращает твои ответы в ответы на капчу/коды;
    * запускает AccountRegistrar в отдельном потоке;
    * дописывает аккаунты в accounts.json.

Запуск:  PYTHONPATH=. python run_reg.py 2 temp
         PYTHONPATH=. python run_reg.py 1 custom someone@example.com
"""
from __future__ import annotations

import asyncio
import sys
import threading

from aiogram import Bot

import config
from captcha_solver import CaptchaSolver
from register import AccountRegistrar, append_account


async def poll_updates(bot: Bot, solver: CaptchaSolver, stop: threading.Event) -> None:
    offset = 0
    while not stop.is_set():
        try:
            updates = await bot.get_updates(offset=offset, timeout=5)
        except Exception as exc:  # noqa: BLE001 — сеть/409, просто ждём дальше
            print("poll:", exc, flush=True)
            await asyncio.sleep(3)
            continue
        for update in updates:
            offset = update.update_id + 1
            message = update.message
            if message and message.text and message.from_user:
                if message.from_user.id == config.ADMIN_ID and solver.has_pending:
                    ok = solver.submit_text(message.text)
                    await bot.send_message(
                        config.ADMIN_ID,
                        "✅ принято" if ok else "⚠️ капча уже не ждёт ответа",
                    )
            callback = update.callback_query
            if callback and callback.data and callback.from_user:
                if callback.from_user.id == config.ADMIN_ID and callback.data.startswith("cap:"):
                    if solver.has_pending:
                        solver.submit_choice(int(callback.data.split(":", 1)[1]))
                try:
                    await callback.answer()
                except Exception:  # noqa: BLE001
                    pass


async def main() -> None:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    mode = sys.argv[2] if len(sys.argv) > 2 else "temp"
    custom_email = sys.argv[3] if len(sys.argv) > 3 else ""

    bot = Bot(config.BOT_TOKEN)
    solver = CaptchaSolver(bot, config.ADMIN_ID, config.CAPTCHA_TIMEOUT)
    loop = asyncio.get_running_loop()
    stop = threading.Event()
    poller = asyncio.create_task(poll_updates(bot, solver, stop))

    def progress(text: str) -> None:
        print("[prog]", text.replace("\n", " | "), flush=True)
        asyncio.run_coroutine_threadsafe(
            bot.send_message(config.ADMIN_ID, text), loop
        )

    registrar = AccountRegistrar(
        solver,
        loop,
        dict(config.DEFAULT_SETTINGS),
        config.Status(),
        config.STOP_EVENT,
        progress=progress,
    )
    print(f"старт: {count} акк., режим {mode}", flush=True)
    accounts = await asyncio.to_thread(registrar.register_many, count, mode, custom_email)

    saved = 0
    for account in accounts:
        try:
            append_account(account)
            saved += 1
        except Exception as exc:  # noqa: BLE001
            print("save fail:", exc, flush=True)

    print(f"итог: зарегистрировано {len(accounts)}, в accounts.json {saved}", flush=True)
    for account in accounts:
        print(
            f"{account['email']} | {account['password']} | {account['cookies_file']}",
            flush=True,
        )

    stop.set()
    poller.cancel()
    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
