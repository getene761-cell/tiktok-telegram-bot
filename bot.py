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

from datetime import datetime

import config
from captcha_solver import CaptchaSolver
from imap_mail import ImapMail
from proxy_pool import POOL
from register import AccountRegistrar, append_account
from tiktok_search import (
    DATE_PRESETS,
    VideoHit,
    VideoSearcher,
    load_results,
    save_results,
    since_from_custom,
)
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

# Состояние поиска: идёт ли, что искали, каким аккаунтом.
SEARCH: dict[str, Any] = {"running": False, "query": "", "label": "", "account": 0}
# Очередь найденных видео (словари VideoHit.as_dict()).
TARGETS: list[dict[str, Any]] = []


def _restore_targets() -> None:
    data = load_results()
    videos = data.get("videos")
    if isinstance(videos, list):
        TARGETS.clear()
        TARGETS.extend(v for v in videos if isinstance(v, dict) and v.get("url"))
        SEARCH["query"] = str(data.get("query") or "")
        SEARCH["label"] = str(data.get("label") or "")
        if TARGETS:
            log.info("восстановлена очередь из search_results.json: %d видео", len(TARGETS))


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
    search_query = State()
    search_custom_date = State()
    reg_count = State()
    reg_custom = State()


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
            [InlineKeyboardButton(text="🔎 Поиск видео", callback_data="search")],
            [InlineKeyboardButton(text="🆕 Регистрация аккаунта", callback_data="reg")],
            [InlineKeyboardButton(text="🌐 Прокси", callback_data="proxy")],
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
    try:
        pool = POOL.stats()
        lines.append(f"Прокси в пуле: {pool['alive']}/{pool['total']}")
    except Exception:  # noqa: BLE001
        pass
    if st.last_error:
        lines += ["", f"⚠️ Последняя ошибка: {st.last_error}"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#                             Запуск и остановка                              #
# --------------------------------------------------------------------------- #
async def launch_worker(
    account: dict[str, Any],
    chat_id: int,
    run_settings: Optional[dict[str, Any]] = None,
) -> None:
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
        settings=dict(run_settings if run_settings is not None else SETTINGS),
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


# --------------------------------------------------------------------------- #
#                            Поиск видео по дате                              #
# --------------------------------------------------------------------------- #
def _video_line(index: int, video: dict[str, Any]) -> str:
    author = video.get("author") or "?"
    created = int(video.get("create_time") or 0)
    try:
        date = datetime.fromtimestamp(created).strftime("%d.%m.%Y") if created else "дата ?"
    except (OverflowError, OSError, ValueError):
        date = "дата ?"
    desc = (video.get("desc") or "").replace("\n", " ")[:60]
    comments = int(video.get("comments") or 0)
    return f"{index + 1}. @{author} · {date} · 💬{comments}\n   {desc}"


def _results_keyboard(count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"▶️ Комментировать {count} видео", callback_data="runfound")],
            [InlineKeyboardButton(text=f"📋 Все {count} видео", callback_data="spage:0")],
            [
                InlineKeyboardButton(text="🔎 Новый поиск", callback_data="search"),
                InlineKeyboardButton(text="⬅️ В меню", callback_data="main"),
            ],
        ]
    )


async def run_search(
    account_index: int,
    query: str,
    since_ts: float,
    label: str,
    chat_id: int,
) -> None:
    if SEARCH["running"]:
        await bot_send(chat_id, "⚠️ Поиск уже идёт")
        return
    accounts = load_accounts()
    if account_index >= len(accounts):
        await bot_send(chat_id, "❌ Аккаунт не найден, обнови список", kb_back())
        return

    SEARCH.update(
        running=True, query=query, label=label, account=account_index, since_ts=since_ts
    )
    searcher = VideoSearcher(
        solver,
        asyncio.get_running_loop(),
        dict(SETTINGS),
        config.Status(),
        config.STOP_EVENT,
    )
    task = asyncio.create_task(
        asyncio.to_thread(searcher.search, accounts[account_index], query, since_ts)
    )
    task.add_done_callback(lambda t: asyncio.create_task(_on_search_done(t, chat_id, label)))


async def _on_search_done(task: "asyncio.Task", chat_id: int, label: str) -> None:
    SEARCH["running"] = False
    try:
        result = task.result()
    except Exception as exc:  # noqa: BLE001
        log.exception("поиск упал")
        await bot_send(chat_id, f"🔎 Поиск упал: {exc}", kb_back())
        return

    hits: list[VideoHit] = result["hits"]
    path = save_results(
        hits,
        {
            "query": SEARCH["query"],
            "label": label,
            "since_ts": SEARCH.get("since_ts", 0),
            "found": result["found"],
        },
    )
    TARGETS.clear()
    TARGETS.extend(hit.as_dict() for hit in hits)

    head = (
        f"🔎 «{SEARCH['query']}» — {label}\n"
        f"Найдено в выдаче: {result['found']}\n"
        f"После фильтра по дате: {result['filtered']}\n"
        f"В очереди на комментирование: {len(hits)}\n\n"
    )
    if not hits:
        await bot_send(chat_id, head + "Пусто. Возьми фильтр мягче или другой запрос.", kb_back())
        return

    preview = "\n".join(_video_line(i, hit.as_dict()) for i, hit in enumerate(hits[:8]))
    await bot_send(
        chat_id,
        head + preview + f"\n\nСохранено: {path.name}",
        _results_keyboard(len(hits)),
    )


@router.callback_query(F.data == "search", IsAdmin())
async def cb_search(callback: CallbackQuery, state: FSMContext) -> None:
    if SEARCH["running"]:
        await callback.answer("Поиск уже идёт", show_alert=True)
        return
    accounts = load_accounts()
    if not accounts:
        await callback.answer()
        await callback.message.edit_text("❌ Нет аккаунтов в accounts.json", reply_markup=kb_back())
        return
    await state.clear()
    rows = [
        [InlineKeyboardButton(
            text=f"👤 {acc.get('name') or acc.get('username') or f'account {i + 1}'}",
            callback_data=f"sacc:{i}",
        )]
        for i, acc in enumerate(accounts)
    ]
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="main")])
    await callback.message.edit_text(
        "🔎 Поиск видео\n\n"
        "Выбери аккаунт — его cookies нужны, чтобы TikTok отдал выдачу.\n"
        "Тот же аккаунт потом пойдёт комментировать найденное.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sacc:"), IsAdmin())
async def cb_search_account(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        index = int(str(callback.data).split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("битые данные", show_alert=True)
        return
    if index >= len(load_accounts()):
        await callback.answer("аккаунт исчез", show_alert=True)
        return
    await state.set_state(St.search_query)
    await state.update_data(account=index)
    await callback.message.edit_text(
        "🔎 Пришли ключевое слово или фразу.\n"
        "Например: рецепт пасты, обзор наушников, кот приколы",
        reply_markup=kb_back(),
    )
    await callback.answer()


@router.message(St.search_query, F.text, IsAdmin())
async def search_query_input(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    if len(query) < 2:
        await message.answer("Слишком коротко, нужно хотя бы 2 символа")
        return
    await state.update_data(query=query)
    await state.set_state(St.search_date)
    await message.answer(
        f"Запрос: «{query}»\n\nЗа какой период искать видео?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🕐 24 часа", callback_data="sf:24h"),
                    InlineKeyboardButton(text="📅 7 дней", callback_data="sf:7d"),
                    InlineKeyboardButton(text="🗓 30 дней", callback_data="sf:30d"),
                ],
                [InlineKeyboardButton(text="♾ Любая дата", callback_data="sf:any")],
                [InlineKeyboardButton(text="✍️ Своя дата", callback_data="sf:custom")],
                [InlineKeyboardButton(text="⬅️ В меню", callback_data="main")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("sf:"), IsAdmin())
async def cb_search_date(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    query = str(data.get("query") or SEARCH["query"] or "")
    account_index = int(data.get("account") or 0)
    if not query:
        await callback.answer("Сначала ключевое слово", show_alert=True)
        return

    key = str(callback.data).split(":", 1)[1]
    if key == "custom":
        await state.set_state(St.search_custom_date)
        await callback.message.edit_text(
            "Пришли дату в формате YYYY-MM-DD.\n"
            "Будут искать видео, опубликованные не раньше неё.",
            reply_markup=kb_back(),
        )
        await callback.answer()
        return

    label, seconds = DATE_PRESETS.get(key, DATE_PRESETS["any"])
    since_ts = time.time() - seconds if seconds else 0.0
    await state.clear()
    await callback.message.edit_text(f"🔎 Ищу «{query}» ({label})…\nЭто займёт до минуты.")
    await callback.answer()
    await run_search(account_index, query, since_ts, label, callback.message.chat.id)


@router.message(St.search_custom_date, F.text, IsAdmin())
async def search_custom_date(message: Message, state: FSMContext) -> None:
    since_ts = since_from_custom(message.text or "")
    if since_ts is None:
        await message.answer("Не разобрал дату. Нужен формат YYYY-MM-DD, например 2026-10-01")
        return
    data = await state.get_data()
    query = str(data.get("query") or "")
    account_index = int(data.get("account") or 0)
    await state.clear()
    label = f"с {datetime.fromtimestamp(since_ts).strftime('%d.%m.%Y')}"
    await message.answer(f"🔎 Ищу «{query}» ({label})…")
    await run_search(account_index, query, since_ts, label, message.chat.id)


@router.callback_query(F.data == "runfound", IsAdmin())
async def cb_run_found(callback: CallbackQuery) -> None:
    if not TARGETS:
        await callback.answer("Очередь пуста — сначала поиск", show_alert=True)
        return
    if config.STATUS.is_running:
        await callback.answer("Автоматизация уже работает", show_alert=True)
        return

    accounts = load_accounts()
    index = int(SEARCH.get("account") or 0)
    if index >= len(accounts):
        await callback.answer("Аккаунт поиска пропал, запусти поиск заново", show_alert=True)
        return

    urls = [str(item.get("url")) for item in TARGETS if item.get("url")]
    run_settings = dict(SETTINGS)
    run_settings["mode"] = "search"
    run_settings["targets"] = urls

    name = accounts[index].get("name") or accounts[index].get("username") or index + 1
    await callback.message.edit_text(
        f"▶️ Аккаунт {name}\n"
        f"Режим: найденные видео\n"
        f"В очереди: {len(urls)}\n"
        f"Лимит за запуск: {run_settings['cycles']} (меняется в настройках)"
    )
    await callback.answer()
    await launch_worker(accounts[index], callback.message.chat.id, run_settings)


@router.callback_query(F.data.startswith("spage:"), IsAdmin())
async def cb_search_page(callback: CallbackQuery) -> None:
    if not TARGETS:
        await callback.answer("Очередь пуста", show_alert=True)
        return
    try:
        page_index = int(str(callback.data).split(":", 1)[1])
    except (IndexError, ValueError):
        page_index = 0

    per_page = 10
    pages = max(1, (len(TARGETS) + per_page - 1) // per_page)
    page_index = max(0, min(page_index, pages - 1))
    chunk = TARGETS[page_index * per_page:(page_index + 1) * per_page]
    start = page_index * per_page

    lines = "\n".join(_video_line(start + i, video) for i, video in enumerate(chunk))
    text = (
        f"🔎 «{SEARCH['query']}» — {SEARCH['label']}\n"
        f"Всего: {len(TARGETS)} · страница {page_index + 1}/{pages}\n\n{lines}"
    )

    nav: list[InlineKeyboardButton] = []
    if page_index > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"spage:{page_index - 1}"))
    if page_index < pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"spage:{page_index + 1}"))

    rows = []
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(
        text=f"▶️ Комментировать {len(TARGETS)} видео", callback_data="runfound"
    )])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="main")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


# --------------------------------------------------------------------------- #
#                                   Прокси                                    #
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "proxy", IsAdmin())
async def cb_proxy(callback: CallbackQuery) -> None:
    stats = POOL.stats()
    await callback.message.edit_text(
        "🌐 Пул бесплатных прокси\n\n"
        f"Рабочих: {stats['alive']} из {stats['total']}\n\n"
        "Сбор: proxyscrape, TheSpeedX, monosans, clarketm, roosterkid, openproxylist, geonode.\n"
        "Проверка: ipify (живой + чужой IP) → tiktok.com (не 403/429).\n"
        "Регистрация берёт рабочий прокси сама, если REG_PROXY пуст.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить пул", callback_data="proxyrefresh")],
                [InlineKeyboardButton(text="⬅️ В меню", callback_data="main")],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "proxyrefresh", IsAdmin())
async def cb_proxy_refresh(callback: CallbackQuery) -> None:
    await callback.answer("Собираю и проверяю, это пара минут")
    await callback.message.edit_text("🌐 Обновляю пул: скачиваю списки и проверяю через TikTok…")
    alive = await asyncio.to_thread(POOL.refresh)
    stats = POOL.stats()
    await callback.message.edit_text(
        f"🌐 Пул обновлён.\nРабочих: {alive} (в базе {stats['total']})",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="main")]]
        ),
    )


# --------------------------------------------------------------------------- #
#                                Авторегистрация                              #
# --------------------------------------------------------------------------- #
async def run_registration(chat_id: int, count: int, mode: str, email: str) -> None:
    if config.STATUS.is_running:
        await bot_send(chat_id, "⚠️ Сейчас уже идёт работа. Останови и повтори.")
        return
    loop = asyncio.get_running_loop()

    def progress(text: str) -> None:
        asyncio.run_coroutine_threadsafe(bot_send(chat_id, text), loop)

    config.STOP_EVENT.clear()
    config.STATUS.reset()
    config.STATUS.is_running = True
    config.STATUS.account = "регистрация"

    registrar = AccountRegistrar(
        solver,
        loop,
        dict(SETTINGS),
        config.STATUS,
        config.STOP_EVENT,
        progress=progress,
    )
    task = asyncio.create_task(
        asyncio.to_thread(registrar.register_many, count, mode, email)
    )
    config.STATUS.task = task
    task.add_done_callback(lambda t: asyncio.create_task(_on_registration_done(t, chat_id)))


async def _on_registration_done(task: "asyncio.Task", chat_id: int) -> None:
    st = config.STATUS
    st.is_running = False
    st.task = None
    try:
        accounts = task.result()
    except Exception as exc:  # noqa: BLE001
        log.exception("регистрация упала")
        await bot_send(chat_id, f"❌ Регистрация упала: {exc}", kb_main())
        return

    if not accounts:
        await bot_send(
            chat_id,
            "❌ Ни один аккаунт не зарегистрирован.\n"
            "Смотри /logs и captcha_shots/ — там скриншот шага, на котором встали.",
            kb_main(),
        )
        return

    saved = 0
    for account in accounts:
        try:
            append_account(account)
            saved += 1
        except OSError as exc:
            log.error("аккаунт не записался: %s", exc)

    lines = [f"✅ Зарегистрировано: {len(accounts)} · в accounts.json: {saved}", ""]
    for account in accounts:
        lines.append(f"📧 {account['email']}")
        lines.append(f"🔑 {account['password']}")
        lines.append(f"🍪 {account['cookies_file']}")
        lines.append("")
    lines.append("Пароль от ящика тоже сохранён в accounts.json (email_password).")
    await bot_send(chat_id, "\n".join(lines), kb_main())


@router.callback_query(F.data == "reg", IsAdmin())
async def cb_registration(callback: CallbackQuery, state: FSMContext) -> None:
    if config.STATUS.is_running:
        await callback.answer("Сейчас идёт работа", show_alert=True)
        return
    await state.clear()
    await state.set_state(St.reg_count)
    await callback.message.edit_text(
        "🆕 Авторегистрация TikTok\n\n"
        "Сколько аккаунтов зарегистрировать? Пришли число (1–20).\n\n"
        "Капчу буду присылать тебе в этот чат, как и раньше.",
        reply_markup=kb_back(),
    )
    await callback.answer()


@router.message(St.reg_count, F.text, IsAdmin())
async def reg_count_input(message: Message, state: FSMContext) -> None:
    try:
        count = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно число, например 1")
        return
    count = max(1, min(20, count))
    await state.update_data(reg_count=count)

    rows = []
    if ImapMail.ready():
        rows.append([InlineKeyboardButton(text="📬 Мой ящик по IMAP (код сам)", callback_data="regm:imap")])
    rows.extend([
        [InlineKeyboardButton(text="📮 Временный ящик mail.tm", callback_data="regm:temp")],
        [InlineKeyboardButton(text="✉️ Свой e-mail (код пришлю в бота)", callback_data="regm:custom")],
        [InlineKeyboardButton(text="✍️ Свой e-mail, введу код вручную", callback_data="regm:manual")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="main")],
    ])
    hint = "" if ImapMail.ready() else (
        "\n\nℹ️ Чтобы бот сам читал код из твоего ящика, добавь в .env:\n"
        "IMAP_HOST, IMAP_USER, IMAP_PASSWORD (Gmail app password или Yandex)."
    )
    await message.answer(
        f"Сколько: {count}.\n\nОткуда брать почту для регистрации?{hint}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("regm:"), IsAdmin())
async def cb_reg_mode(callback: CallbackQuery, state: FSMContext) -> None:
    mode = str(callback.data).split(":", 1)[1]
    data = await state.get_data()
    count = int(data.get("reg_count") or 1)

    if mode == "imap":
        await state.clear()
        await callback.message.edit_text(
            f"🆕 Регистрирую {count} аккаунт(ов) на своём ящике по IMAP.\n"
            "Адреса чиню сам, код читаю сам — писать ничего не нужно, кроме капчи."
        )
        await callback.answer()
        await run_registration(callback.message.chat.id, count, "imap", "")
        return

    if mode == "temp":
        await state.clear()
        await callback.message.edit_text(
            f"🆕 Регистрирую {count} аккаунт(ов) на временные ящики mail.tm.\n"
            "Капча и возможные вопросы придут сюда."
        )
        await callback.answer()
        await run_registration(callback.message.chat.id, count, "temp", "")
        return

    await state.set_state(St.reg_custom)
    await state.update_data(reg_mode=mode)
    await callback.message.edit_text(
        "Пришли e-mail, на который регистрируем.\n"
        "Код из письма придёт туда — пришли его сюда, когда спрошу.",
        reply_markup=kb_back(),
    )
    await callback.answer()


@router.message(St.reg_custom, F.text, IsAdmin())
async def reg_custom_email(message: Message, state: FSMContext) -> None:
    email = (message.text or "").strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        await message.answer("Не похоже на e-mail. Пример: name@example.com")
        return
    data = await state.get_data()
    mode = str(data.get("reg_mode") or "custom")
    count = int(data.get("reg_count") or 1)
    await state.clear()
    await message.answer(f"🆕 Регистрирую {count} аккаунт(ов) на {email}…")
    await run_registration(message.chat.id, count, mode, email)


@router.callback_query(F.data == "stop", IsAdmin())
async def cb_stop(callback: CallbackQuery) -> None:
    config.STOP_EVENT.set()
    if solver and solver.has_pending:
        solver.cancel()
    if config.STATUS.is_running or SEARCH["running"]:
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
        "/search — поиск видео по ключу и дате\n"
        "/help — эта справка"
    )


@router.message(Command("search"), IsAdmin())
async def cmd_search(message: Message, state: FSMContext) -> None:
    if SEARCH["running"]:
        await message.answer("⚠️ Поиск уже идёт")
        return
    accounts = load_accounts()
    if not accounts:
        await message.answer("❌ Нет аккаунтов в accounts.json")
        return
    await state.clear()
    rows = [
        [InlineKeyboardButton(
            text=f"👤 {acc.get('name') or acc.get('username') or f'account {i + 1}'}",
            callback_data=f"sacc:{i}",
        )]
        for i, acc in enumerate(accounts)
    ]
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="main")])
    await message.answer(
        "🔎 Поиск видео. Выбери аккаунт (его cookies нужны для выдачи):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
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


async def _proxy_keepalive() -> None:
    """Раз в 30 минут: добирает пул, если рабочих мало или данные устарели."""
    while True:
        try:
            await asyncio.to_thread(POOL.refresh_if_stale)
        except Exception:  # noqa: BLE001
            log.exception("обновление пула прокси")
        await asyncio.sleep(1800)


async def main() -> None:
    global solver, _BOT

    setup_logging()
    _restore_targets()
    asyncio.create_task(_proxy_keepalive())

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
