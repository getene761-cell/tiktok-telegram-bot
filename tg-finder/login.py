# language: Python 3.9+, file: tg-finder/login.py
"""Одноразовый вход в user-аккаунт Telegram для MTProto-поиска.

Запускать один раз:  python login.py
Спросит номер и код из Telegram (и пароль, если включена двухфакторка).
Создаст файл сессии `finder_session.session` рядом — боту больше входить не нужно.
"""
from __future__ import annotations

import asyncio

from telethon import TelegramClient

import config


async def main() -> None:
    if not config.API_ID or not config.API_HASH:
        raise SystemExit(
            "TG_API_ID / TG_API_HASH не заданы в .env.\n"
            "Возьми их на https://my.telegram.org -> API development tools"
        )
    client = TelegramClient(str(config.SESSION_FILE), config.API_ID, config.API_HASH)
    await client.start(phone=config.PHONE or None)
    me = await client.get_me()
    print(f"Вход выполнен: {me.first_name} @{me.username} (id {me.id})")
    print("Сессия сохранена в", config.SESSION_FILE.with_suffix(".session"))
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
