# language: Python 3.9+, file: bot.py
"""Точка входа: асинхронный Telegram-бот (aiogram 3.x) + менеджер Playwright-воркера.

Схема работы:
    1. aiogram крутит свой event loop.
    2. По кнопке «Запустить» создаётся задача
       `asyncio.to_thread(worker.run, account)` — Playwright (sync API) живёт
       в отдельном потоке и не блокирует event loop.
    3. Капча: воркер через `captcha_solver.request_sync` уходит в event loop бота,
       бот шлёт фото админу и ждёт ответа (текст или inline-кнопку).
    4. «Стоп» выставляет threading.Event, который воркер проверяет в паузах и циклах.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config
from captcha_solver import CaptchaSolver
from tiktok_worker import (
    TikTokWorker,
    load_accounts,
    parse_cookie_payload,
    save_accounts,
)

log = logging.getLogger("bot")

router = Router()
solver: Optional[CaptchaSolver] = None
SETTINGS: dict[str, Any] = dict(config.DEFAULT_SETTINGS)
LAST_COOKIE: dict[str, Any] = {"path": ""}


# --------------------------------------------------------------------------- #
#                                  Утилиты                                    #
# --------------------------------------------------------------------------- #
def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        config.LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and user.id == config.ADMIN_ID)


class PendingCaptcha(BaseFilter):
    """Текст от админа становится ответом на капчу, только если она реально ждёт."""

    async def __call__(self, message: Message) -> bool:
        return bool(solver and solver.has_pending)


class St(StatesGroup):
    settings_value = State()
    cookies_input = State()


def tail_log(lines: int = 30) -> str:
    if not config.LOG_FILE.exists():
        return "лог пуст"
    try:
        content = config.LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError as exc:
        return f"лог недоступен: {exc}"
    return "\n".join(content[-lines:]) or "лог пуст"


# --------------------------------------------------------------------------- #
#                                 Клавиатуры                                  #
# --------------------------------------------------------------------------- #
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Запустить автоматизацию", callback_data="run")],
            [
                InlineKeyboardButton(text="⏹ Остановить", callback_data="stop"),
                InlineKeyboardButton(text="📊 Статус", callback_data="status"),
            ],
            [InlineKeyboardButton(text="📁 Загрузить cookies", callback_data="cookies")],
            [
                InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings"),
                InlineKeyboardButton(text="📋 Логи", callback_data="logs"),
            ],
        ]
    )


def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="main")]]
    )


def kb_accounts() -> InlineKeyboardMarkup:
    accounts = load_accounts()
    rows = [
        [InlineKeyboardButton(
            text=f"👤 {acc.get('name') or acc.get('username') or f'account {i + 1}'}",
            callback_data=f"acc:{i}",
        )]
        for i, acc in enumerate(accounts)
    ]
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_settings() -> InlineKeyboardMarkup:
    like = "✅" if SETTINGS["do_like"] else "❌"
    comment = "✅" if SETTINGS["do_comment"] else "❌"
    reply = "✅" if SETTINGS["do_reply"] else "❌"
    mode = "лента" if SETTINGS["mode"] == "feed" else "URL"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Текст комментария", callback_data="set:comment")],
            [InlineKeyboardButton(text="⏱ Задержки", callback_data="set:delay")],
            [
                InlineKeyboardButton(text="🔁 Циклы", callback_data="set:cycles"),
                InlineKeyboardButton(text="🔗 URL видео", callback_data="set:url"),
            ],
            [InlineKeyboardButton(text=f"Режим: {mode}", callback_data="toggle:mode")],
            [
                InlineKeyboardButton(text=f"Лайк {like}", callback_data="toggle:like"),
                InlineKeyboardButton(text=f"Коммент {comment}", callback_data="toggle:comment"),
            ],
            [InlineKeyboardButton(text=f"Ответ на комментарий {reply}", callback_data="toggle:reply")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="main")],
        ]
    )


def settings_text() -> str:
    return (
        "⚙️ Настройки\n\n"
        f"Текст комментария: {SETTINGS['comment_text']!r}\n"
        f"Задержка между действиями: {SETTINGS['delay_min']}–{SETTINGS['delay_max']} с\n"
        f"Циклов за запуск: {SETTINGS['cycles']}\n"
        f"Режим: {SETTINGS['mode']} ({SETTINGS['target_url'] or 'без URL'})\n"
        f"Лайк: {'да' if SETTINGS['do_like'] else 'нет'} | "
        f"Комментарий: {'да' if SETTINGS['do_comment'] else 'нет'} | "
        f"Ответ: {'да' if SETTINGS['do_reply'] else 'нет'}"
    )


def status_text() -> str:
    st = config.STATUS
    lines = [
        "📊 Статус",
        "",
        f"Состояние: {'🟢 работает' if st.is_running else '⚪️ простой'}",
        f"Аккаунт: {st.account}",
        f"Действие: {st.action}",
        f"Прогресс: {st.progress}",
        f"Комментариев: {st.comments_sent}",
        f"Лайков: {st.likes_sent}",
        f"Капч: {st.captchas_seen}",
        f"В работе: {st.uptime()}",
    ]
    if st.last_error:
        lines += ["", f"⚠️ Последняя ошибка: {st.last_error}"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#                             Запуск и остановка                              #
# --------------------------------------------------------------------------- #
async def launch_worker(account: dict[str, Any], chat_id: int) -> None:
    global solver
    current = config.STATUS.task
    if current and not current.done():
        await bot_send(chat_id, "⚠️ Автоматизация уже запущена")
        return

    config.STOP_EVENT.clear()
    config.STATUS.reset()
    config.STATUS.is_running = True
    config.STATUS.account = str(account.get("name") or account.get("username") or "acc")

    worker = TikTokWorker(
        solver=solver,
        bot_loop=asyncio.get_running_loop(),
        settings=dict(SETTINGS),
        status=config.STATUS,
        stop_event=config.STOP_EVENT,
    )
    # asyncio.to_thread -> отдельный поток, event loop бота свободен.
    task = asyncio.create_task(asyncio.to_thread(worker.run, account))
    config.STATUS.task = task
    task.add_done_callback(lambda t: asyncio.create_task(_on_worker_done(t, chat_id)))


async def bot_send(chat_id: int, text: str, markup: Any = None) -> None:
    try:
        await _BOT.send_message(chat_id, text, reply_markup=markup)
    except Exception as exc:  # noqa: BLE001
        log.warning("не удалось отправить сообщение: %s", exc)


async def _on_worker_done(task: asyncio.Task, chat_id: int) -> None:
    st = config.STATUS
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # noqa: BLE001
        log.exception("воркер завершился с ошибкой")
        st.last_error = str(exc)[:300]

    st.is_running = False
    st.task = None
    report = [
        "🏁 Автоматизация завершена",
        f"Аккаунт: {st.account}",
        f"Комментариев: {st.comments_sent} | Лайков: {st.likes_sent} | Капч: {st.captchas_seen}",
    ]
    if st.last_error:
        report.append(f"⚠️ Ошибка: {st.last_error}")
    await bot_send(chat_id, "\n".join(report), kb_main())


_BOT: Bot  # проставляется в main()


# --------------------------------------------------------------------------- #
#                                Хендлеры: капча                               #
# --------------------------------------------------------------------------- #
@router.message(PendingCaptcha(), F.text, IsAdmin())
async def captcha_text(message: Message) -> None:
    assert solver is not None
    ok = solver.submit_text(message.text or "")
    await message.reply("✅ Ответ на капчу принят" if ok else "⚠️ Капча уже не ждёт ответа")


@router.callback_query(F.data.startswith("cap:"), IsAdmin())
async def captcha_choice(callback: CallbackQuery) -> None:
    assert solver is not None
    try:
        index = int(str(callback.data).split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("битые данные кнопки", show_alert=True)
        return
    ok = solver.submit_choice(index)
    await callback.answer("✅ Принято" if ok else "Опоздал: капча снята")


# --------------------------------------------------------------------------- #
#                            Хендлеры: меню и статус                           #
# --------------------------------------------------------------------------- #
@router.message(CommandStart(), IsAdmin())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "👋 Бот управления TikTok.\n\n"
        f"Аккаунтов в accounts.json: {len(load_accounts())}\n"
        "Выбери действие:",
        reply_markup=kb_main(),
    )


@router.message(Command("menu"), IsAdmin())
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Главное меню:", reply_markup=kb_main())


@router.callback_query(F.data == "main", IsAdmin())
async def cb_main(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Главное меню:", reply_markup=kb_main())
    await callback.answer()


@router.callback_query(F.data == "run", IsAdmin())
async def cb_run(callback: CallbackQuery, state: FSMContext) -> None:
    if config.STATUS.is_running:
        await callback.answer("Уже работает", show_alert=True)
        return
    accounts = load_accounts()
    if not accounts:
        await callback.answer()
        await callback.message.edit_text(
            "❌ В accounts.json нет активных аккаунтов.\n"
            "Заполни accounts.json или загрузи cookies через меню.",
            reply_markup=kb_back(),
        )
        return
    await state.clear()
    await callback.message.edit_text("Выбери аккаунт для запуска:", reply_markup=kb_accounts())
    await callback.answer()


@router.callback_query(F.data.startswith("acc:"), IsAdmin())
async def cb_account(callback: CallbackQuery) -> None:
    try:
        index = int(str(callback.data).split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("битые данные", show_alert=True)
        return
    accounts = load_accounts()
    if index >= len(accounts):
        await callback.answer("аккаунт исчез, обнови список", show_alert=True)
        return
    account = accounts[index]
    name = account.get("name") or account.get("username") or f"account {index + 1}"
    await callback.message.edit_text(
        f"▶️ Запускаю аккаунт {name}\nРежим: {SETTINGS['mode']}, циклов: {SETTINGS['cycles']}"
    )
    await callback.answer()
    await launch_worker(account, callback.message.chat.id)


@router.callback_query(F.data == "stop", IsAdmin())
async def cb_stop(callback: CallbackQuery) -> None:
    config.STOP_EVENT.set()
    if solver and solver.has_pending:
        solver.cancel()
    if config.STATUS.is_running:
        await callback.message.edit_text("⏹ Останавливаю…", reply_markup=kb_back())
    else:
        await callback.message.edit_text("⚪️ Автоматизация и не была запущена.", reply_markup=kb_back())
    await callback.answer()


@router.callback_query(F.data == "status", IsAdmin())
async def cb_status(callback: CallbackQuery) -> None:
    await callback.message.edit_text(status_text(), reply_markup=kb_back())
    await callback.answer()


@router.callback_query(F.data == "logs", IsAdmin())
async def cb_logs(callback: CallbackQuery) -> None:
    await callback.message.edit_text("📋 Последние строки лога:\n\n" + tail_log(25), reply_markup=kb_back())
    await callback.answer()


# --------------------------------------------------------------------------- #
#                                  Настройки                                  #
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "settings", IsAdmin())
async def cb_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(settings_text(), reply_markup=kb_settings())
    await callback.answer()


@router.callback_query(F.data.startswith("set:"), IsAdmin())
async def cb_set_value(callback: CallbackQuery, state: FSMContext) -> None:
    field = str(callback.data).split(":", 1)[1]
    prompts = {
        "comment": "Пришли текст комментария.\n"
                   "Можно несколько вариантов — каждый с новой строки.\n"
                   "Поддерживается спинтаксис: {привет|хай|ку} бро",
        "delay": "Пришли задержку в секундах: min-max (например 10-25)",
        "cycles": "Сколько видео обработать за запуск? (число)",
        "url": "Пришли ссылку на видео TikTok (https://www.tiktok.com/@user/video/...)",
    }
    if field not in prompts:
        await callback.answer("неизвестное поле", show_alert=True)
        return
    await state.set_state(St.settings_value)
    await state.update_data(field=field)
    await callback.message.edit_text(prompts[field], reply_markup=kb_back())
    await callback.answer()


@router.message(St.settings_value, F.text, IsAdmin())
async def apply_setting(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    field = data.get("field")
    text = (message.text or "").strip()
    await state.clear()

    try:
        if field == "comment":
            if not text:
                raise ValueError("пустой текст")
            SETTINGS["comment_text"] = text
        elif field == "delay":
            lo, _, hi = text.replace(" ", "").partition("-")
            SETTINGS["delay_min"] = max(1, int(lo))
            SETTINGS["delay_max"] = max(SETTINGS["delay_min"], int(hi or lo))
        elif field == "cycles":
            SETTINGS["cycles"] = max(1, min(500, int(text)))
        elif field == "url":
            if not text.startswith("http"):
                raise ValueError("нужна ссылка http(s)")
            SETTINGS["target_url"] = text
            SETTINGS["mode"] = "url"
    except ValueError as exc:
        await message.answer(f"⚠️ Не принял: {exc}", reply_markup=kb_settings())
        return

    await message.answer("✅ Сохранено.\n\n" + settings_text(), reply_markup=kb_settings())


@router.callback_query(F.data.startswith("toggle:"), IsAdmin())
async def cb_toggle(callback: CallbackQuery) -> None:
    what = str(callback.data).split(":", 1)[1]
    if what == "mode":
        SETTINGS["mode"] = "url" if SETTINGS["mode"] == "feed" else "feed"
        if SETTINGS["mode"] == "url" and not SETTINGS["target_url"]:
            await callback.answer("Сначала задай URL видео", show_alert=True)
            SETTINGS["mode"] = "feed"
            return
    elif what == "like":
        SETTINGS["do_like"] = not SETTINGS["do_like"]
    elif what == "comment":
        SETTINGS["do_comment"] = not SETTINGS["do_comment"]
    elif what == "reply":
        SETTINGS["do_reply"] = not SETTINGS["do_reply"]
    else:
        await callback.answer("неизвестный переключатель", show_alert=True)
        return
    await callback.message.edit_text(settings_text(), reply_markup=kb_settings())
    await callback.answer("готово")


# --------------------------------------------------------------------------- #
#                                   Cookies                                   #
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "cookies", IsAdmin())
async def cb_cookies(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(St.cookies_input)
    await callback.message.edit_text(
        "📁 Загрузка cookies\n\n"
        "Пришли любым из способов:\n"
        "• файлом .json (storage_state Playwright или список cookie)\n"
        "• файлом .txt (Netscape cookies.txt из расширения)\n"
        "• текстом JSON\n\n"
        "Файл сохранится в cookies/, затем можно привязать его к аккаунту.",
        reply_markup=kb_back(),
    )
    await callback.answer()


def _store_cookies(raw: str, source: str, chat_id: int) -> None:
    cookies = parse_cookie_payload(raw)
    if not cookies:
        asyncio.create_task(bot_send(chat_id, "❌ Не нашёл ни одного cookie в присланном. "
                                                "Проверь формат.", kb_back()))
        return
    path = config.COOKIES_DIR / f"upload_{int(time.time())}.json"
    try:
        path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        asyncio.create_task(bot_send(chat_id, f"❌ Не записался файл: {exc}", kb_back()))
        return
    LAST_COOKIE["path"] = str(path)

    accounts = load_accounts()
    rows = [
        [InlineKeyboardButton(
            text=f"🔗 Привязать к {acc.get('name') or acc.get('username') or i + 1}",
            callback_data=f"ckbind:{i}",
        )]
        for i, acc in enumerate(accounts)
    ]
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="main")])
    markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else kb_back()

    asyncio.create_task(bot_send(
        chat_id,
        f"✅ Получено cookie ({source}): {len(cookies)} шт.\n"
        f"Файл: {path.relative_to(config.BASE_DIR)}\n\n"
        "Привязать к аккаунту из accounts.json?",
        markup,
    ))


@router.message(St.cookies_input, IsAdmin(), F.document)
async def cookies_document(message: Message) -> None:
    document = message.document
    if document.file_size and document.file_size > 2_000_000:
        await message.reply("❌ Файл больше 2 МБ, так не пойдёт")
        return
    buffer = await _BOT.download(document.file_id)
    raw = buffer.read()
    try:
        text = raw.decode("utf-8", "ignore")
    except Exception as exc:  # noqa: BLE001
        await message.reply(f"❌ Не читается файл: {exc}")
        return
    _store_cookies(text, document.file_name or "document", message.chat.id)


@router.message(St.cookies_input, IsAdmin(), F.text)
async def cookies_text(message: Message, state: FSMContext) -> None:
    await state.set_state(St.cookies_input)  # остаёмся в режиме, можно дослать ещё
    _store_cookies(message.text or "", "text", message.chat.id)


@router.callback_query(F.data.startswith("ckbind:"), IsAdmin())
async def cb_bind_cookies(callback: CallbackQuery, state: FSMContext) -> None:
    path = LAST_COOKIE.get("path")
    if not path:
        await callback.answer("сначала загрузи cookies", show_alert=True)
        return
    try:
        index = int(str(callback.data).split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("битые данные", show_alert=True)
        return
    accounts = load_accounts()
    if index >= len(accounts):
        await callback.answer("аккаунт не найден", show_alert=True)
        return

    rel = str(Path(path).relative_to(config.BASE_DIR))
    accounts[index]["cookies_file"] = rel
    try:
        save_accounts(accounts)
    except OSError as exc:
        await callback.answer(f"не записалось: {exc}", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        f"✅ cookies привязаны к аккаунту {accounts[index].get('name') or index + 1}\n"
        f"Файл: {rel}",
        reply_markup=kb_main(),
    )
    await callback.answer("готово")


# --------------------------------------------------------------------------- #
#                              Прочее и запуск                                #
# --------------------------------------------------------------------------- #
@router.message(Command("help"), IsAdmin())
async def cmd_help(message: Message) -> None:
    await message.answer(
        "/start — меню\n"
        "/menu — меню\n"
        "/status — статус\n"
        "/logs — последние строки лога\n"
        "/stop — остановить\n"
        "/help — эта справка"
    )


@router.message(Command("status"), IsAdmin())
async def cmd_status(message: Message) -> None:
    await message.answer(status_text(), reply_markup=kb_main())


@router.message(Command("stop"), IsAdmin())
async def cmd_stop(message: Message) -> None:
    config.STOP_EVENT.set()
    if solver and solver.has_pending:
        solver.cancel()
    await message.answer("⏹ Флаг остановки выставлен", reply_markup=kb_main())


@router.message(Command("logs"), IsAdmin())
async def cmd_logs(message: Message) -> None:
    await message.answer("📋 Лог:\n\n" + tail_log(25))


@router.message()
async def deny_everyone(message: Message) -> None:
    if message.from_user and message.from_user.id != config.ADMIN_ID:
        await message.answer(f"⛔️ Доступ закрыт. Твой id: {message.from_user.id}")
    else:
        await message.answer("Не понял команду. /menu")


async def main() -> None:
    global solver, _BOT

    setup_logging()

    if not config.BOT_TOKEN or ":" not in config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан. Скопируй .env.example в .env и вставь токен.")
    if not config.ADMIN_ID:
        raise SystemExit("ADMIN_ID не задан. Узнай свой id через @userinfobot.")

    _BOT = Bot(config.BOT_TOKEN)
    solver = CaptchaSolver(_BOT, config.ADMIN_ID, config.CAPTCHA_TIMEOUT)

    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)

    await _BOT.delete_webhook(drop_pending_updates=True)
    log.info("бот запущен, админ %s, лог %s", config.ADMIN_ID, config.LOG_FILE)
    try:
        await _BOT.send_message(config.ADMIN_ID, "🤖 Бот запущен. /menu")
    except Exception as exc:  # noqa: BLE001 — админ мог не начать диалог с ботом
        log.warning("не смог написать админу: %s", exc)

    try:
        await dispatcher.start_polling(_BOT)
    finally:
        config.STOP_EVENT.set()
        if solver:
            solver.cancel()
        await _BOT.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code:
            print(exc.code, file=sys.stderr)
        else:
            print("остановлено", file=sys.stderr)
