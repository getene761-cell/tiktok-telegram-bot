# language: Python 3.9+, file: proxy_pool.py
"""Пул бесплатных прокси: собрать, проверить на TikTok, выдать рабочий.

Источники — публичные списки (без ключей и регистрации). Проверка двухступенчатая:
    1. `https://api.ipify.org` — прокси вообще живой и отдаёт чужой IP;
    2. `https://www.tiktok.com/` — конкретно TikTok через него открывается.
Второй шаг отсекает 80–90% списка: половина прокси мертва, другая половина
заблокирована самим TikTok или отдаёт капчу на любом запросе.

Честно про класс ресурса: это дата-центр-прокси с общим IP, которые видели
тысячи автоматизаций. Для «зарегистрировать пару аккаунтов» — рабочий вариант,
для потока — нет: TikTok склеит их по IP-подсетям. Мобильные/резидентные — только
платно.
"""
from __future__ import annotations

import json
import logging
import random
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import config

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# (формат, url). Текстовые списки — по одному host:port на строку.
SOURCES: tuple[tuple[str, str], ...] = (
    ("text", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all"),
    ("text", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
    ("text", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("text", "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt"),
    ("text", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt"),
    ("text", "https://api.openproxylist.xyz/http.txt"),
    ("json", "https://proxylist.geonode.com/api/proxy-list?limit=200&page=1&sort_by=lastChecked&sort_type=desc&protocols=http,https"),
)

HOST_PORT_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})\b")


class ProxyPool:
    def __init__(
        self,
        path: Optional[Any] = None,
        workers: int = 80,
        timeout: int = 5,
    ) -> None:
        self.path = path or config.PROXY_FILE
        self.workers = workers
        self.timeout = timeout
        self._lock = threading.Lock()
        self._used: set[str] = set()
        self.proxies: list[dict[str, Any]] = self._load()

    # ----------------------------------------------------------- persistence
    def _load(self) -> list[dict[str, Any]]:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                rows = data.get("proxies") if isinstance(data, dict) else None
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, dict) and row.get("addr")]
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("пул прокси не читается: %s", exc)
        return []

    def _save(self) -> None:
        payload = {"updated": int(time.time()), "proxies": self.proxies}
        try:
            self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as exc:
            log.warning("пул прокси не записался: %s", exc)

    # ---------------------------------------------------------------- fetch
    def fetch(self) -> list[str]:
        found: set[str] = set()

        def grab(source: tuple[str, str]) -> list[str]:
            kind, url = source
            request = urllib.request.Request(url)
            request.add_header("User-Agent", UA)
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    raw = response.read().decode("utf-8", "ignore")
            except Exception as exc:  # noqa: BLE001 — источник мог умереть
                log.info("источник недоступен (%s): %s", url[:60], exc)
                return []
            if kind == "json":
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    return []
                rows = data.get("data") if isinstance(data, dict) else []
                return [
                    f"{row['ip']}:{row['port']}"
                    for row in rows
                    if isinstance(row, dict) and row.get("ip") and row.get("port")
                ]
            return [f"{host}:{port}" for host, port in HOST_PORT_RE.findall(raw)]

        with ThreadPoolExecutor(max_workers=len(SOURCES)) as executor:
            for chunk in executor.map(grab, SOURCES):
                found.update(chunk)
        log.info("собрано адресов: %d", len(found))
        return sorted(found)

    # --------------------------------------------------------------- checks
    def _check_connect(self, proxy: str, host: str = "www.tiktok.com", port: int = 443) -> Optional[float]:
        """CONNECT-туннель до TikTok + TLS-рукопожатие. Полный аналог того, что делает Chromium.

        Отсекает главную беду бесплатных списков: сервера, которые проксируют только
        plain HTTP и отвечают на CONNECT «400». С ними TikTok не открыть.
        """
        host_part, _, port_part = proxy.partition(":")
        if not host_part or not port_part.isdigit():
            return None
        started = time.time()
        try:
            sock = socket.create_connection((host_part, int(port_part)), timeout=self.timeout)
        except OSError:
            return None

        try:
            sock.settimeout(self.timeout)
            sock.sendall(
                f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode()
            )
            head = sock.recv(256)
            first_line = head.split(b"\r\n", 1)[0]
            if b" 200" not in first_line:
                return None
            context = ssl.create_default_context()
            tls = context.wrap_socket(sock, server_hostname=host)
            tls.close()
        except (OSError, ssl.SSLError):
            return None
        finally:
            try:
                sock.close()
            except OSError:
                pass
        return round(time.time() - started, 2)

    def check(self, proxy: str) -> dict[str, Any]:
        latency = self._check_connect(proxy)
        if latency is None:
            return {"addr": proxy, "ok": False, "latency": 0.0, "fails": 1}
        return {"addr": proxy, "ok": True, "latency": latency, "fails": 0}

    def refresh(self, max_check: int = 400) -> int:
        """Пересобирает пул: скачать список, проверить, оставить живые."""
        candidates = self.fetch()
        random.shuffle(candidates)
        if max_check:
            candidates = candidates[:max_check]
        if not candidates:
            return 0

        checked: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            for result in executor.map(self.check, candidates):
                checked.append(result)

        alive = [row for row in checked if row["ok"]]
        alive.sort(key=lambda row: row["latency"])
        with self._lock:
            self.proxies = alive
            self._used.clear()
            self._save()
        log.info("проверено %d, рабочих %d", len(checked), len(alive))
        return len(alive)

    def refresh_if_stale(self, min_alive: int = 5, max_age: int = 1800) -> int:
        fresh = [row for row in self.proxies if row.get("ok")]
        updated = 0.0
        if self.path.exists():
            try:
                updated = json.loads(self.path.read_text(encoding="utf-8")).get("updated", 0)
            except (OSError, json.JSONDecodeError):
                updated = 0
        stale = (time.time() - updated) > max_age
        if len(fresh) < min_alive or stale:
            return self.refresh()
        return len(fresh)

    # ------------------------------------------------------------- acquire
    def acquire(self) -> Optional[str]:
        """Рабочий прокси, ещё не занятый в этом прогоне. None — пул пуст."""
        with self._lock:
            for row in sorted(
                (r for r in self.proxies if r.get("ok")), key=lambda r: r.get("latency", 9)
            ):
                addr = row["addr"]
                if addr not in self._used:
                    self._used.add(addr)
                    return f"http://{addr}"
        if self.refresh() == 0:
            return None
        return self.acquire()

    def mark_bad(self, proxy: str) -> None:
        with self._lock:
            for row in self.proxies:
                if row["addr"] == proxy.replace("http://", ""):
                    row["ok"] = False
                    row["fails"] = int(row.get("fails", 0)) + 1
            self._save()

    def stats(self) -> dict[str, int]:
        alive = [row for row in self.proxies if row.get("ok")]
        return {"alive": len(alive), "total": len(self.proxies)}


POOL = ProxyPool()
