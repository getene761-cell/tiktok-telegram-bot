# language: Python 3.9+, file: tiktok_search.py
"""Поиск видео TikTok по ключевым словам с фильтром по дате публикации.

Как получаем дату честно, а не «на глаз»:
    Экран поиска подгружает результаты XHR-запросами `/api/search/*`. Мы слушаем
    ответы страницы (`page.on("response")`) и разбираем JSON — там есть `createTime`
    (unix), автор, описание и счётчик комментариев. Никаких подписей (X-Bogus)
    руками не считаем: браузер делает запрос сам, мы только читаем ответ.

Если API ничего не отдал (например, урезанная выдача без логина), есть fallback:
    * вытащить ссылки `/video/` из DOM;
    * для отфильтрованных по дате — открыть видео и вытащить `createTime` из
      `__UNIVERSAL_DATA_FOR_REHYDRATION__` регуляркой.

Работает поверх `TikTokWorker`: наследуем запуск браузера, stealth, cookies и
обработку капчи через Telegram — поведение одно и то же в поиске и в действиях.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from playwright.sync_api import Error as PWError, Page, sync_playwright

import config
from tiktok_worker import LoginFailed, TikTokWorker

log = logging.getLogger(__name__)

# Пресеты фильтра по дате: ключ -> (человеческая подпись, сколько секунд назад).
DATE_PRESETS: dict[str, tuple[str, int]] = {
    "24h": ("за 24 часа", 24 * 3600),
    "7d": ("за 7 дней", 7 * 24 * 3600),
    "30d": ("за 30 дней", 30 * 24 * 3600),
    "any": ("без фильтра по дате", 0),
}


@dataclass
class VideoHit:
    id: str
    url: str
    author: str = ""
    desc: str = ""
    create_time: int = 0
    comments: int = 0
    likes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def date_str(self) -> str:
        if not self.create_time:
            return "дата ?"
        try:
            return datetime.fromtimestamp(self.create_time).strftime("%d.%m.%Y")
        except (OverflowError, OSError, ValueError):
            return "дата ?"


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _hit_from_item(item: dict[str, Any]) -> Optional[VideoHit]:
    if not isinstance(item, dict):
        return None
    video_id = str(item.get("id") or item.get("videoId") or item.get("aweme_id") or "")
    if not video_id.isdigit():
        return None

    author = ""
    raw_author = item.get("author")
    if isinstance(raw_author, dict):
        author = str(
            raw_author.get("uniqueId") or raw_author.get("unique_id") or raw_author.get("nickname") or ""
        )

    stats = item.get("stats") or item.get("statsV2") or {}
    if not isinstance(stats, dict):
        stats = {}

    create_time = _int(
        item.get("createTime")
        or item.get("create_time")
        or stats.get("createTime")
        or stats.get("create_time")
        or 0
    )
    url = (
        f"https://www.tiktok.com/@{author}/video/{video_id}"
        if author
        else f"https://www.tiktok.com/video/{video_id}"
    )
    return VideoHit(
        id=video_id,
        url=url,
        author=author,
        desc=str(item.get("desc") or "").replace("\n", " ").strip(),
        create_time=create_time,
        comments=_int(stats.get("commentCount") or stats.get("comment_count") or 0),
        likes=_int(stats.get("diggCount") or stats.get("digg_count") or 0),
    )


def parse_search_payload(payload: Any) -> list[VideoHit]:
    """Разбирает оба вида ответа TikTok: itemList (item/full) и data[].item (general/full)."""
    hits: list[VideoHit] = []
    if not isinstance(payload, dict):
        return hits

    raw = payload.get("itemList") or payload.get("item_list")
    if isinstance(raw, list):
        for item in raw:
            hit = _hit_from_item(item)
            if hit:
                hits.append(hit)

    data = payload.get("data")
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            item = entry.get("item") or entry.get("aweme_info")
            hit = _hit_from_item(item) if isinstance(item, dict) else None
            if hit:
                hits.append(hit)
    return hits


def save_results(hits: list[VideoHit], meta: dict[str, Any]) -> Path:
    """Пишет очередь на диск, чтобы бот пережил перезапуск."""
    payload = {
        **meta,
        "found": int(meta.get("found", len(hits))),
        "filtered": len(hits),
        "created_at": int(time.time()),
        "videos": [hit.as_dict() for hit in hits],
    }
    config.SEARCH_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return config.SEARCH_FILE


def load_results() -> dict[str, Any]:
    if not config.SEARCH_FILE.exists():
        return {}
    try:
        data = json.loads(config.SEARCH_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("search_results.json не читается: %s", exc)
        return {}


def since_from_custom(date_text: str) -> Optional[float]:
    """'YYYY-MM-DD' -> unix-тайм начала дня в локальной таймзоне."""
    try:
        return datetime.strptime(date_text.strip(), "%Y-%m-%d").timestamp()
    except ValueError:
        return None


class VideoSearcher(TikTokWorker):
    """Поиск. Использует те же browser/stealth/captcha-механизмы, что и воркер."""

    def search(
        self,
        account: dict[str, Any],
        query: str,
        since_ts: float = 0.0,
        scan_limit: int = 0,
    ) -> dict[str, Any]:
        scan_limit = scan_limit or config.SEARCH_SCAN_LIMIT
        self.status.reset()
        self.status.is_running = True
        self.status.action = f"поиск: {query[:40]}"
        self.status.started_at = time.time()
        log.info("=== поиск «%s» (фильтр с %s) ===", query, since_ts or "без даты")

        collected: list[VideoHit] = []
        with sync_playwright() as pw:
            browser = self._launch(pw, account)
            try:
                context = self._new_context(browser, account)
                page = context.new_page()
                self.page = page
                # Поиск работает и без логина: не валим затею из-за протухших cookies,
                # просто предупреждаем — выдача может быть беднее.
                if not self._ensure_logged_in(page, account):
                    log.warning("поиск: вход не выполнен, пробую выдачу без логина")
                collected = self._scan(page, query, scan_limit)
                if since_ts and any(not hit.create_time for hit in collected):
                    self._enrich_dates(page, collected)
            finally:
                try:
                    browser.close()
                except PWError:
                    pass
                self.status.is_running = False
                self.status.action = "поиск завершён"

        filtered = [hit for hit in collected if not since_ts or hit.create_time >= since_ts]
        filtered.sort(key=lambda hit: hit.create_time, reverse=True)
        log.info("поиск: собрано %d, после фильтра %d", len(collected), len(filtered))
        return {"found": len(collected), "filtered": len(filtered), "hits": filtered}

    # ------------------------------------------------------------------ scan
    def _scan(self, page: Page, query: str, limit: int) -> list[VideoHit]:
        found: dict[str, VideoHit] = {}

        def on_response(response: Any) -> None:
            if "/api/search/" not in response.url:
                return
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001 — не-JSON ответы не наша забота
                return
            for hit in parse_search_payload(payload):
                found.setdefault(hit.id, hit)

        page.on("response", on_response)
        try:
            page.goto(
                "https://www.tiktok.com/search/video?q=" + quote(query),
                wait_until="domcontentloaded",
            )
            self._settle(page, 3000)
            self._handle_captcha(page)

            empty_rounds = 0
            for _ in range(24):
                if self.stop_event.is_set() or len(found) >= limit:
                    break
                before = len(found)
                page.mouse.wheel(0, random.randint(2000, 2600))
                page.wait_for_timeout(random.randint(1400, 2400))
                if self._captcha_visible(page):
                    self._handle_captcha(page)
                if len(found) == before:
                    empty_rounds += 1
                else:
                    empty_rounds = 0
                if empty_rounds >= 3:
                    break
        finally:
            try:
                page.remove_listener("response", on_response)
            except PWError:
                pass

        hits = list(found.values())
        if hits:
            return hits
        log.warning("API поиска молчит, разбираю DOM")
        return self._scan_dom(page)

    def _scan_dom(self, page: Page) -> list[VideoHit]:
        hits: list[VideoHit] = []
        seen: set[str] = set()
        try:
            anchors = page.locator('a[href*="/video/"]')
            total = min(anchors.count(), config.SEARCH_SCAN_LIMIT)
        except PWError:
            return hits

        for index in range(total):
            try:
                node = anchors.nth(index)
                href = node.get_attribute("href") or ""
                text = ""
                try:
                    text = (node.inner_text(timeout=1000) or "").strip()
                except PWError:
                    pass
            except PWError:
                continue
            match = re.search(r"/@([^/]+)/video/(\d+)", href)
            if not match or match.group(2) in seen:
                continue
            seen.add(match.group(2))
            hits.append(
                VideoHit(
                    id=match.group(2),
                    url=f"https://www.tiktok.com/@{match.group(1)}/video/{match.group(2)}",
                    author=match.group(1),
                    desc=text[:120],
                )
            )
        return hits

    def _enrich_dates(self, page: Page, hits: list[VideoHit], max_n: int = 12) -> None:
        """Даты из DOM-выдачи не видны — открываем видео и читаем createTime из JSON страницы."""
        done = 0
        for hit in hits:
            if done >= max_n or self.stop_event.is_set():
                return
            if hit.create_time:
                continue
            done += 1
            try:
                page.goto(hit.url, wait_until="domcontentloaded")
                self._settle(page, 1200)
                self._handle_captcha(page)
                html = page.content()
            except PWError as exc:
                log.debug("не открылось %s: %s", hit.url, exc)
                continue
            match = re.search(r'"createTime":"?(\d{9,11})', html)
            if match:
                hit.create_time = int(match.group(1))
            match = re.search(r'"commentCount":(\d+)', html)
            if match and not hit.comments:
                hit.comments = int(match.group(1))
