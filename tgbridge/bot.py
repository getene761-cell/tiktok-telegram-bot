# language: Python 3.9+, file: tgbridge/bot.py
"""Бот-переходник на Telegram-канал.

Кто пришёл: /start → приветствие + кнопка на канал → «Я подписался» → проверка
подписки через get_chat_member → выдача контента (текст/ссылка), который ты
задаёшь в админке.

Личное управление: для ADMIN_ID в боте открывается панель — статистика
(сколько всего запустило бота, новые за сутки/неделю, подписались, конверсия),
последние пользователи, настройка канала и текстов, рассылка, экспорт CSV.

Данные — SQLite рядом с ботом, ничего внешнего не нужно.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config
from storage import Storage

log = logging.getLogger("bridge")

router = Router()
db = Storage(config.DB_FILE)
BOT: Bot

SUBSCRIBED_STATUSES = ("member", "administrator", "creator")

DEFAULT_WELCOME = (
    "👋 Привет!\n\n"
    "Чтобы получить доступ, подпишись на канал — кнопка ниже. "
    "Потом нажми «Я подписался»."
)
DEFAULT_CONTENT = "✅ Готово! Держи то, за чем пришёл:\n\nhttps://t.me/"


class St(StatesGroup):
    channel = State()
    url = State()
    welcome = State()
    content = State()
    broadcast = State()


# --------------------------------------------------------------------------- #
#                                  Утилиты                                    #
# --------------------------------------------------------------------------- #
def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(config.LOG_FILE, maxBytes=1_000_000, backupCount=2,
                                       encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    logging.getLogger("aiogram").setLevel(logging.WARNING)


def is_admin(user_id: int) -> bool:
    return user_id == config.ADMIN_ID


def welcome_text() -> str:
    return db.get("welcome", DEFAULT_WELCOME)


def content_text() -> str:
    return db.get("content", DEFAULT_CONTENT)


def channel_id() -> str:
    return db.get("channel_id", config.CHANNEL_ID)


def channel_url() -> str:
    return db.get("channel_url", config.CHANNEL_URL)


def kb_user() -> InlineKeyboardMarkup:
    url = channel_url()
    rows: list[list[InlineKeyboardButton]] = []
    if url:
        rows.append([InlineKeyboardButton(text="📢 Перейти в канал", url=url)])
    rows.append([InlineKeyboardButton(text="✅ Я подписался", callback_data="check")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
            [InlineKeyboardButton(text="👥 Последние", callback_data="adm:recent")],
            [InlineKeyboardButton(text="📢 ID канала", callback_data="adm:channel"),
             InlineKeyboardButton(text="🔗 Ссылка", callback_data="adm:url")],
            [InlineKeyboardButton(text="✍️ Приветствие", callback_data="adm:welcome")],
            [InlineKeyboardButton(text="🎁 Контент после подписки", callback_data="adm:content")],
            [InlineKeyboardButton(text="📨 Рассылка", callback_data="adm:broadcast")],
            [InlineKeyboardButton(text="📤 Экспорт CSV", callback_data="adm:export")],
        ]
    )


async def subscribed(user_id: int) -> bool:
    chat = channel_id()
    if not chat:
        return True  # канал не настроен — не блокируем
    try:
        member = await BOT.get_chat_member(chat_id=chat, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 — бот не админ канала или id битый
        log.warning("get_chat_member: %s", exc)
        return True
    return str(member.status) in SUBSCRIBED_STATUSES


def stats_text() -> str:
    stats = db.stats()
    lines = [
        "📊 Статистика бота-переходника",
        "",
        f"Всего запустило: {stats['total']}",
        f"Новых за сутки: {stats['today']}",
        f"Новых за неделю: {stats['week']}",
        f"Активны за сутки: {stats['active24']}",
        f"Подписались: {stats['subscribed']}",
        f"Конверсия в подписку: {stats['conversion']}%",
    ]
    daily = db.daily(7)
    if daily:
        lines += ["", "По дням:"]
        for day, count in daily:
            lines.append(f"  {day}: {'█' * min(count, 20)} {count}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#                                  Хендлеры                                   #
# --------------------------------------------------------------------------- #
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    if not user:
        return
    source = ""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        source = parts[1][:64]
    is_new = db.touch_user(user.id, user.username or "", user.first_name or "", source)
    if is_new:
        log.info("новый пользователь %s (@%s)", user.id, user.username or "—")

    if is_admin(user.id):
        await message.answer(
            "👑 Админ-панель\n\n" + stats_text(),
            reply_markup=kb_admin(),
        )
        return

    await message.answer(welcome_text(), reply_markup=kb_user())


@router.callback_query(F.data == "check")
async def cb_check(callback: CallbackQuery) -> None:
    user = callback.from_user
    if await subscribed(user.id):
        db.mark_subscribed(user.id)
        db.log_event(user.id, "subscribed")
        await callback.message.edit_text(content_text())
    else:
        await callback.answer("Пока не вижу подписку. Подпишись и нажми ещё раз.", show_alert=True)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    await message.answer(stats_text(), reply_markup=kb_admin())


@router.callback_query(F.data.startswith("adm:"))
async def cb_admin(callback: CallbackQuery, state: FSMContext) -> None:
    user = callback.from_user
    if not is_admin(user.id):
        await callback.answer("Это только для владельца", show_alert=True)
        return
    action = str(callback.data).split(":", 1)[1]
    await state.clear()

    if action == "stats":
        await callback.message.edit_text(stats_text(), reply_markup=kb_admin())
    elif action == "recent":
        rows = db.recent(10)
        if not rows:
            text = "Пользователей пока нет."
        else:
            lines = ["👥 Последние запуски:", ""]
            for row in rows:
                name = row["username"] and "@" + row["username"] or row["first_name"] or "—"
                mark = "✅" if row["subscribed"] else "◻️"
                lines.append(f"{mark} {name} · id {row['user_id']}")
            text = "\n".join(lines)
        await callback.message.edit_text(text, reply_markup=kb_admin())
    elif action == "channel":
        await state.set_state(St.channel)
        await callback.message.edit_text(
            "📢 ID канала\n\n"
            f"Сейчас: {db.get('channel_id') or 'не задан (проверка подписки выключена)'}\n\n"
            "Пришли id вида -1001234567890.\n"
            "Бота нужно добавить админом канала, иначе проверка не сработает.\n"
            "Пришли - чтобы выключить проверку.",
            reply_markup=kb_admin(),
        )
    elif action == "url":
        await state.set_state(St.url)
        await callback.message.edit_text(
            "🔗 Ссылка на канал\n\n"
            f"Сейчас: {db.get('channel_url') or 'не задана'}\n\n"
            "Пришли ссылку вида https://t.me/имя или https://t.me/+invite\n"
            "Пришли - чтобы убрать кнопку.",
            reply_markup=kb_admin(),
        )
    elif action == "welcome":
        await state.set_state(St.welcome)
        await callback.message.edit_text(
            "Пришли новый текст приветствия:", reply_markup=kb_admin()
        )
    elif action == "content":
        await state.set_state(St.content)
        await callback.message.edit_text(
            "Пришли текст, который бот выдаст после подписки:", reply_markup=kb_admin()
        )
    elif action == "broadcast":
        await state.set_state(St.broadcast)
        await callback.message.edit_text(
            "Пришли текст рассылки всем пользователям:", reply_markup=kb_admin()
        )
    elif action == "export":
        path = db.export_csv(config.BASE_DIR / "export_users.csv")
        document = BufferedInputFile(path.read_bytes(), filename="export_users.csv")
        await callback.message.answer_document(document, caption="📤 Пользователи бота")
        await callback.answer("готово")
        return
    await callback.answer()


@router.message(St.channel, F.text)
async def set_channel(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if "|" in raw:  # старый формат id|url
        chat, _, url = raw.partition("|")
        db.set("channel_id", "" if chat.strip() in ("-", "") else chat.strip())
        db.set("channel_url", "" if url.strip() in ("-", "") else url.strip())
    else:
        db.set("channel_id", "" if raw in ("-", "") else raw)
    await state.clear()
    await message.answer(
        "✅ Сохранено.\n"
        f"📢 ID: {db.get('channel_id') or 'проверка выключена'}\n"
        f"🔗 Ссылка: {db.get('channel_url') or '—'}",
        reply_markup=kb_admin(),
    )


@router.message(St.url, F.text)
async def set_url(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    db.set("channel_url", "" if raw in ("-", "") else raw)
    await state.clear()
    await message.answer(
        f"✅ Ссылка: {db.get('channel_url') or 'убрана'}", reply_markup=kb_admin()
    )


@router.message(St.welcome, F.text)
async def set_welcome(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    db.set("welcome", message.text or "")
    await state.clear()
    await message.answer("✅ Приветствие обновлено", reply_markup=kb_admin())


@router.message(St.content, F.text)
async def set_content(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    db.set("content", message.text or "")
    await state.clear()
    await message.answer("✅ Контент после подписки обновлён", reply_markup=kb_admin())


@router.message(St.broadcast, F.text)
async def do_broadcast(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    text = message.text or ""
    await state.clear()
    user_ids = db.all_user_ids()
    await message.answer(f"📨 Рассылаю {len(user_ids)} пользователям…")

    sent = failed = 0
    for user_id in user_ids:
        try:
            await BOT.send_message(user_id, text)
            sent += 1
        except Exception:  # noqa: BLE001 — заблокировал бота
            failed += 1
        await asyncio.sleep(0.05)

    await message.answer(
        f"✅ Рассылка: доставлено {sent}, не дошло {failed}", reply_markup=kb_admin()
    )


@router.message()
async def fallback(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    if is_admin(user.id):
        await message.answer("Панель:", reply_markup=kb_admin())
        return
    await message.answer(welcome_text(), reply_markup=kb_user())


async def main() -> None:
    global BOT
    setup_logging()
    if not config.BOT_TOKEN or ":" not in config.BOT_TOKEN:
        raise SystemExit("BRIDGE_BOT_TOKEN не задан в tgbridge/.env")
    if not config.ADMIN_ID:
        raise SystemExit("BRIDGE_ADMIN_ID не задан в tgbridge/.env")

    BOT = Bot(config.BOT_TOKEN)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    await BOT.delete_webhook(drop_pending_updates=True)
    log.info("бот-переходник запущен, админ %s", config.ADMIN_ID)
    try:
        await BOT.send_message(config.ADMIN_ID, "🤖 Переходник запущен.\n\n" + stats_text())
    except Exception as exc:  # noqa: BLE001
        log.warning("не смог написать админу: %s", exc)
    try:
        await dispatcher.start_polling(BOT)
    finally:
        await BOT.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code:
            print(exc.code, file=sys.stderr)
