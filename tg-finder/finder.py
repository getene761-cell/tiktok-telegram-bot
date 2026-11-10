# language: Python 3.9+, file: tg-finder/finder.py
"""Поиск Telegram-чатов по критериям через MTProto (Telethon, user-аккаунт).

Bot API глобальный поиск не умеет: искать можно только своим аккаунтом.
Поэтому здесь два источника:
    1. contacts.SearchRequest — поиск по названию/юзернейму;
    2. messages.SearchGlobalRequest — поиск по сообщениям, оттуда тоже вытаскиваем чаты.

Для каждого чата берём полную инфу: число участников, описание, права на отправку
сообщений и заявку на вступление. Поэтому в выдачу попадают только чаты, где
реально можно писать, а не read-only каналы.

Лимиты Telegram: поиск отдаёт максимум ~100 результатов на запрос, дальше нужен
offset; вступления/просмотры дёргают FloodWait — поэтому пауза между запросами и
обработка FloodWaitError.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from telethon import TelegramClient, functions
from telethon.errors import FloodWaitError, RPCError
from telethon.tl import types

log = logging.getLogger("finder")

Progress = Callable[[str], None]


def _stable_id(entity: Any) -> Optional[int]:
    paid = getattr(entity, "id", None)
    if paid is None:
        return None
    channel_id = getattr(entity, "channel_id", None)
    return int(channel_id or paid)


class Finder:
    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session: str,
        limit: int = 100,
        delay: float = 0.5,
        progress: Optional[Progress] = None,
    ) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.session = session
        self.limit = limit
        self.delay = delay
        self.progress = progress or (lambda text: log.info(text))
        self.client: Optional[TelegramClient] = None
        self._seen: set[int] = set()

    # --------------------------------------------------------------- lifecycle
    def __enter__(self) -> "Finder":
        self.client = TelegramClient(self.session, self.api_id, self.api_hash)
        self.client.connect()
        if not self.client.is_user_authorized():
            raise RuntimeError(
                "Нет авторизации: запусти один раз python login.py и войди кодом из Telegram"
            )
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------- api
    def _call(self, request: Any, retries: int = 2) -> Any:
        assert self.client is not None
        for attempt in range(retries + 1):
            try:
                return self.client(request)
            except FloodWaitError as exc:
                wait = int(getattr(exc, "seconds", 30)) + 2
                self.progress(f"⏳ Telegram просит подождать {wait} с (FloodWait)")
                time.sleep(wait)
            except RPCError as exc:
                log.warning("RPC ошибка: %s", exc)
                return None
        return None

    def search_peers(self, query: str) -> list[Any]:
        found: list[Any] = []
        result = self._call(functions.contacts.SearchRequest(q=query, limit=self.limit))
        if result is not None:
            found.extend(result.chats)
        time.sleep(self.delay)

        global_result = self._call(
            functions.messages.SearchGlobalRequest(
                q=query,
                filter=types.InputMessagesFilterEmpty(),
                min_date=None,
                max_date=None,
                offset_rate=0,
                offset_peer=types.InputPeerEmpty(),
                offset_id=0,
                limit=self.limit,
            )
        )
        if global_result is not None:
            for chat in getattr(global_result, "chats", []) or []:
                found.append(chat)
        time.sleep(self.delay)
        return found

    # ------------------------------------------------------------------ info
    def chat_info(self, entity: Any, query: str) -> Optional[dict[str, Any]]:
        assert self.client is not None
        kind = "channel" if getattr(entity, "broadcast", False) else "group"
        username = str(getattr(entity, "username", "") or "")
        title = str(getattr(entity, "title", "") or "")

        members = 0
        about = ""
        can_post = False
        join_request = False

        if isinstance(entity, types.Channel):
            full = self._call(
                functions.channels.GetFullChannelRequest(channel=entity), retries=1
            )
            if full is None:
                return None
            full_chat = full.full_chat
            members = int(getattr(full_chat, "participants_count", 0) or 0)
            about = str(getattr(full_chat, "about", "") or "")
            join_request = bool(getattr(full_chat, "join_request", False))
            banned = getattr(full_chat, "banned_rights", None)
            default_banned = getattr(entity, "default_banned_rights", None)
        else:
            full = self._call(
                functions.messages.GetFullChatRequest(chat_id=entity.id), retries=1
            )
            if full is not None:
                full_chat = full.full_chat
                members = int(getattr(full_chat, "participants_count", 0) or 0)
                about = str(getattr(full_chat, "about", "") or "")
                banned = getattr(full_chat, "banned_rights", None)
                default_banned = getattr(entity, "default_banned_rights", None)
            else:
                banned = None
                default_banned = None

        blocked = False
        for rights in (banned, default_banned):
            if rights is not None and getattr(rights, "send_messages", False):
                blocked = True
        can_post = (kind == "group") and not blocked and not join_request

        self._sleep()
        return {
            "chat_id": _stable_id(entity) or 0,
            "title": title,
            "username": username,
            "link": f"https://t.me/{username}" if username else "",
            "members": members,
            "kind": kind,
            "can_post": can_post,
            "about": about,
            "query": query,
        }

    def _sleep(self) -> None:
        if self.delay:
            time.sleep(self.delay)

    # ------------------------------------------------------------------- run
    def run(self, criteria: dict[str, str], stop_flag: Any = None) -> list[dict[str, Any]]:
        keywords = [word.strip() for word in criteria["keywords"].split(",") if word.strip()]
        exclude = [word.strip().lower() for word in criteria["exclude"].split(",") if word.strip()]
        min_members = int(criteria.get("min_members") or 0)
        max_members = int(criteria.get("max_members") or 0)
        kind_wanted = criteria.get("kind") or "any"
        require_username = criteria.get("require_username") == "1"
        posting_only = criteria.get("posting_only") == "1"
        limit = int(criteria.get("limit") or 40)

        if not keywords:
            raise RuntimeError("Не заданы ключевые слова")

        results: list[dict[str, Any]] = []
        seen: set[int] = set()

        for keyword in keywords:
            if stop_flag is not None and stop_flag.is_set():
                break
            if len(results) >= limit:
                break
            self.progress(f"🔎 Ищу «{keyword}»…")
            peers = self.search_peers(keyword)
            self.progress(f"   кандидатов: {len(peers)}, уже найдено: {len(results)}")

            for entity in peers:
                if stop_flag is not None and stop_flag.is_set():
                    break
                if len(results) >= limit:
                    break
                entity_id = _stable_id(entity)
                if entity_id is None or entity_id in seen:
                    continue
                seen.add(entity_id)

                title = str(getattr(entity, "title", "") or "")
                username = str(getattr(entity, "username", "") or "")
                kind = "channel" if getattr(entity, "broadcast", False) else "group"

                if kind_wanted != "any" and kind != kind_wanted:
                    continue
                if require_username and not username:
                    continue
                haystack = f"{title} {username} {keyword}".lower()
                if exclude and any(word in haystack for word in exclude):
                    continue

                info = self.chat_info(entity, keyword)
                if info is None:
                    continue
                if min_members and info["members"] < min_members:
                    continue
                if max_members and info["members"] > max_members:
                    continue
                if posting_only and not info["can_post"]:
                    continue
                # ключевое слово должно реально встречаться в названии или описании
                blob = f"{info['title']} {info['about']} {info['username']}".lower()
                if keyword.lower() not in blob:
                    continue

                results.append(info)
                self.progress(
                    f"✅ {info['title']} · {info['members']} чел."
                    + ("" if info["can_post"] else " · писать нельзя")
                )

        results.sort(key=lambda item: item["members"], reverse=True)
        return results
