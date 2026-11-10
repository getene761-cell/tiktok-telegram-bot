# language: Python 3.9+, file: tg-finder/bot.py
"""Telegram-бот: ищет группы по критериям и выдаёт ссылки с числом участников.

Управление — только у ADMIN_ID. Поиск идёт user-аккаунтом (Telethon), результат
складывается в SQLite и приходит тебе в личку пачками. Кнопки: критерии, поиск,
последние, CSV, очистка, стоп.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import BaseFilter, Command, CommandStart
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
from db import Store
from finder import Finder

log = logging.getLogger("tgbot")
router = Router()
store = Store(config.DB_FILE)
BOT: Bot
STOP = threading.Event()
RUNNING: dict[str, bool] = {"now": False}


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and user.id == config.ADMIN_ID)


class St(StatesGroup):
    keywords = State()
    exclude = State()
    members = State()
    limit = State()


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


# --------------------------------------------------------------------------- #
#                                  Клавиатуры                                 #
# --------------------------------------------------------------------------- #
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Искать", callback_data="run")],
            [InlineKeyboardButton(text="⚙️ Критерии", callback_data="crit")],
            [
                InlineKeyboardButton(text="📋 Последние", callback_data="last"),
                InlineKeyboardButton(text="📤 CSV", callback_data="csv"),
            ],
            [
                InlineKeyboardButton(text="⏹ Стоп", callback_data="stop"),
                InlineKeyboardButton(text="🗑 Очистить", callback_data="clear"),
            ],
        ]
    )


def kb_criteria() -> InlineKeyboardMarkup:
    crit = store.criteria(config.DEFAULT_CRITERIA)
    post = "✅" if crit["posting_only"] == "1" else "❌"
    nick = "✅" if crit["require_username"] == "1" else "❌"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Ключевые слова", callback_data="set:keywords")],
            [InlineKeyboardButton(text="🚫 Исключить слова", callback_data="set:exclude")],
            [InlineKeyboardButton(
                text=f"👥 Участники: {crit['min_members']}–{crit['max_members']}",
                callback_data="set:members",
            )],
            [InlineKeyboardButton(text=f"🎛 Тип: {crit['kind']}", callback_data="toggle:kind")],
            [InlineKeyboardButton(text=f"📝 Только где можно писать {post}", callback_data="toggle:posting")],
            [InlineKeyboardButton(text=f"🔗 Только с юзернеймом {nick}", callback_data="toggle:username")],
            [InlineKeyboardButton(text=f"🎯 Результатов за прогон: {crit['limit']}", callback_data="set:limit")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="main")],
        ]
    )


def criteria_text() -> str:
    crit = store.criteria(config.DEFAULT_CRITERIA)
    return (
        "⚙️ Критерии поиска\n\n"
        f"Ключевые слова: {crit['keywords'] or '—'}\n"
        f"Исключить: {crit['exclude'] or '—'}\n"
        f"Участники: {crit['min_members']}–{crit['max_members']}\n"
        f"Тип: {crit['kind']} (group / channel / any)\n"
        f"Только где можно писать: {'да' if crit['posting_only'] == '1' else 'нет'}\n"
        f"Только с юзернеймом: {'да' if crit['require_username'] == '1' else 'нет'}\n"
        f"Результатов за прогон: {crit['limit']}\n\n"
        f"В базе найдено чатов: {store.count()}"
    )


def format_chat(chat: dict) -> str:
    kind = "канал" if chat["kind"] == "channel" else "чат"
    mark = "✅ можно писать" if chat["can_post"] else "⚠️ писать нельзя"
    link = chat["link"] or "ссылки нет (закрытый чат)"
    about = (chat["about"] or "").replace("\n", " ")[:90]
    return (
        f"👥 {chat['members']} · {kind} · {mark}\n"
        f"{chat['title']}\n"
        f"{link}"
        + (f"\n{about}" if about else "")
    )


# --------------------------------------------------------------------------- #
#                                  Хендлеры                                   #
# --------------------------------------------------------------------------- #
@router.message(CommandStart(), IsAdmin())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🔎 Поиск чатов Telegram\n\n" + criteria_text(),
        reply_markup=kb_main(),
    )


@router.callback_query(F.data == "main", IsAdmin())
async def cb_main(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Главное меню:", reply_markup=kb_main())
    await callback.answer()


@router.callback_query(F.data == "crit", IsAdmin())
async def cb_criteria(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(criteria_text(), reply_markup=kb_criteria())
    await callback.answer()


@router.callback_query(F.data.startswith("set:"), IsAdmin())
async def cb_set(callback: CallbackQuery, state: FSMContext) -> None:
    field = str(callback.data).split(":", 1)[1]
    prompts = {
        "keywords": "Пришли ключевые слова через запятую:\nкрипта, инвестиции, бизнес",
        "exclude": "Слова, при которых чат пропускаем (через запятую):\nскам, казино",
        "members": "Диапазон участников: минимум-максимум\n500-20000\n(0 в максимуме = без верхней границы)",
        "limit": "Сколько результатов собирать за прогон (1–200):\n40",
    }
    if field not in prompts:
        await callback.answer("неизвестное поле", show_alert=True)
        return
    await state.set_state(getattr(St, field))
    await callback.message.edit_text(prompts[field], reply_markup=kb_criteria())
    await callback.answer()


@router.message(St.keywords, F.text, IsAdmin())
async def set_keywords(message: Message, state: FSMContext) -> None:
    store.set("keywords", (message.text or "").strip())
    await state.clear()
    await message.answer("✅ Ключевые слова сохранены\n\n" + criteria_text(),
                         reply_markup=kb_criteria())


@router.message(St.exclude, F.text, IsAdmin())
async def set_exclude(message: Message, state: FSMContext) -> None:
    store.set("exclude", (message.text or "").strip())
    await state.clear()
    await message.answer("✅ Исключения сохранены\n\n" + criteria_text(),
                         reply_markup=kb_criteria())


@router.message(St.members, F.text, IsAdmin())
async def set_members(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").replace(" ", "")
    low, _, high = raw.partition("-")
    try:
        min_members = max(0, int(low))
        max_members = max(0, int(high or 0))
    except ValueError:
        await message.answer("Нужен формат 500-20000")
        return
    if max_members and max_members < min_members:
        min_members, max_members = max_members, min_members
    store.set("min_members", str(min_members))
    store.set("max_members", str(max_members))
    await state.clear()
    await message.answer("✅ Диапазон сохранён\n\n" + criteria_text(), reply_markup=kb_criteria())


@router.message(St.limit, F.text, IsAdmin())
async def set_limit(message: Message, state: FSMContext) -> None:
    try:
        value = max(1, min(200, int((message.text or "").strip())))
    except ValueError:
        await message.answer("Нужно число от 1 до 200")
        return
    store.set("limit", str(value))
    await state.clear()
    await message.answer(f"✅ Лимит: {value}\n\n" + criteria_text(), reply_markup=kb_criteria())


@router.callback_query(F.data.startswith("toggle:"), IsAdmin())
async def cb_toggle(callback: CallbackQuery) -> None:
    what = str(callback.data).split(":", 1)[1]
    if what == "kind":
        order = ["group", "channel", "any"]
        current = store.get("kind", "group")
        store.set("kind", order[(order.index(current) + 1) % 3] if current in order else "group")
    elif what == "posting":
        store.set("posting_only", "0" if store.get("posting_only", "1") == "1" else "1")
    elif what == "username":
        store.set("require_username", "0" if store.get("require_username", "1") == "1" else "1")
    else:
        await callback.answer("неизвестный переключатель", show_alert=True)
        return
    await callback.message.edit_text(criteria_text(), reply_markup=kb_criteria())
    await callback.answer("готово")


@router.callback_query(F.data == "stop", IsAdmin())
async def cb_stop(callback: CallbackQuery) -> None:
    STOP.set()
    await callback.answer("Останавливаю после текущего запроса")
    await callback.message.edit_text("⏹ Флаг остановки выставлен", reply_markup=kb_main())


@router.callback_query(F.data == "clear", IsAdmin())
async def cb_clear(callback: CallbackQuery) -> None:
    store.clear()
    await callback.message.edit_text("🗑 База очищена", reply_markup=kb_main())
    await callback.answer()


@router.callback_query(F.data == "last", IsAdmin())
async def cb_last(callback: CallbackQuery) -> None:
    rows = store.recent(10)
    if not rows:
        await callback.message.edit_text("Пока пусто — запусти поиск.", reply_markup=kb_main())
        await callback.answer()
        return
    chunks = [format_chat(chat) for chat in rows]
    await callback.message.edit_text(
        f"📋 Последние {len(rows)} (в базе {store.count()}):\n\n" + "\n\n".join(chunks)[:3800],
        reply_markup=kb_main(),
    )
    await callback.answer()


@router.callback_query(F.data == "csv", IsAdmin())
async def cb_csv(callback: CallbackQuery) -> None:
    path = store.export_csv(config.BASE_DIR / "chats.csv")
    document = BufferedInputFile(path.read_bytes(), filename="chats.csv")
    await callback.message.answer_document(document, caption=f"📤 {store.count()} чатов")
    await callback.answer("готово")


@router.callback_query(F.data == "run", IsAdmin())
async def cb_run(callback: CallbackQuery) -> None:
    if RUNNING["now"]:
        await callback.answer("Поиск уже идёт", show_alert=True)
        return
    crit = store.criteria(config.DEFAULT_CRITERIA)
    if not crit["keywords"]:
        await callback.answer("Сначала задай ключевые слова", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        f"🔎 Ищу: {crit['keywords']}\nУчастники: {crit['min_members']}–{crit['max_members']}\n"
        f"Только где можно писать: {'да' if crit['posting_only'] == '1' else 'нет'}",
        reply_markup=kb_main(),
    )
    STOP.clear()
    RUNNING["now"] = True
    chat_id = callback.message.chat.id
    loop = asyncio.get_running_loop()
    run_id = store.start_run(crit["keywords"])

    def progress(text: str) -> None:
        asyncio.run_coroutine_threadsafe(
            BOT.send_message(chat_id, text), loop
        )

    async def worker() -> None:
        try:
            def job() -> list[dict]:
                with Finder(
                    config.API_ID,
                    config.API_HASH,
                    str(config.SESSION_FILE),
                    limit=config.SEARCH_LIMIT,
                    delay=config.REQUEST_DELAY,
                    progress=progress,
                ) as finder:
                    return finder.run(crit, STOP)

            results = await asyncio.to_thread(job)
        except Exception as exc:  # noqa: BLE001
            log.exception("поиск упал")
            await BOT.send_message(chat_id, f"❌ Поиск упал: {exc}", reply_markup=kb_main())
            RUNNING["now"] = False
            return

        new_count = sum(1 for chat in results if store.save_chat(chat))
        store.finish_run(run_id, new_count)
        RUNNING["now"] = False

        if not results:
            await BOT.send_message(
                chat_id,
                "😕 Ничего не нашлось. Ослабь фильтры: диапазон участников, "
                "«только где можно писать», другие ключевые слова.",
                reply_markup=kb_main(),
            )
            return

        await BOT.send_message(
            chat_id,
            f"✅ Нашёл {len(results)} чатов, новых в базе: {new_count}.\n"
            "Ссылки — в сообщениях ниже и в 📤 CSV.",
        )
        for chat in results[:10]:
            await BOT.send_message(chat_id, format_chat(chat))
        if len(results) > 10:
            await BOT.send_message(
                chat_id,
                f"Ещё {len(results) - 10} — кнопкой 📋 Последние или 📤 CSV.",
                reply_markup=kb_main(),
            )

    asyncio.create_task(worker())


@router.message()
async def fallback(message: Message) -> None:
    if message.from_user and message.from_user.id == config.ADMIN_ID:
        await message.answer("Меню:", reply_markup=kb_main())
    else:
        await message.answer(f"⛔️ Доступ закрыт. Твой id: {message.from_user.id}")


async def main() -> None:
    global BOT
    setup_logging()
    if not config.BOT_TOKEN:
        raise SystemExit("FINDER_BOT_TOKEN не задан в .env")
    if not config.ADMIN_ID:
        raise SystemExit("FINDER_ADMIN_ID не задан в .env")
    if not config.API_ID or not config.API_HASH:
        raise SystemExit("TG_API_ID / TG_API_HASH не заданы (my.telegram.org)")

    BOT = Bot(config.BOT_TOKEN)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    await BOT.delete_webhook(drop_pending_updates=True)
    log.info("бот-поисковик чатов запущен, админ %s", config.ADMIN_ID)
    await dispatcher.start_polling(BOT)
    await BOT.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code:
            print(exc.code, file=sys.stderr)
