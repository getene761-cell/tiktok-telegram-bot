# language: Python 3.9+, file: proxy_pool.py
"""Пул бесплатных прокси: собрать, проверить на TikTok, выдать рабочий.

Главный урок бесплатных списков: 90% — это plain-HTTP прокси, которые на
`CONNECT www.tiktok.com:443` отвечают `400`. TikTok по HTTPS через них не открыть,
поэтому проверка не «GET через прокси», а именно CONNECT-туннель + TLS-рукопожатие
до tiktok.com. SOCKS5 проверяются своим рукопожатием — они туннелируют нативно.

Источники: proxyscrape, TheSpeedX, monosans, clarketm, roosterkid, openproxylist,
geonode, hookzof. Без ключей и регистрации.

Честно про класс ресурса: дата-центр-IP, которые видели тысячи автоматизаций.
Для пары аккаунтов хватит, для потока — нет: TikTok склеит по подсетям.
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
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

import config

log = logging.getLogger(__name__)

HOST_PORT_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})\b")

# (тип, url). Тип задаёт и способ проверки, и схему для Playwright.
SOURCES: tuple[tuple[str, str], ...] = (
    ("http", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all"),
    ("http", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
    ("http", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("http", "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt"),
    ("http", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt"),
    ("http", "https://api.openproxylist.xyz/http.txt"),
    ("http", "https://proxylist.geonode.com/api/proxy-list?limit=200&page=1&sort_by=lastChecked&sort_type=desc&protocols=http,https"),
    ("socks5", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"),
    ("socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"),
    ("socks5", "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt"),
    ("socks5", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt"),
    ("socks5", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=10000&country=all"),
)


class ProxyPool:
    def __init__(
        self,
        path: Optional[Path] = None,
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
    def fetch(self) -> list[tuple[str, str]]:
        """Возвращает [(scheme, host:port)] из всех источников."""
        found: dict[str, str] = {}

        def grab(source: tuple[str, str]) -> list[tuple[str, str]]:
            scheme, url = source
            request = urllib.request.Request(url)
            request.add_header("User-Agent", "Mozilla/5.0")
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    raw = response.read().decode("utf-8", "ignore")
            except Exception as exc:  # noqa: BLE001 — источник мог умереть
                log.info("источник недоступен (%s): %s", url[:60], exc)
                return []
            if "api/proxy-list" in url:  # JSON геонода
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    return []
                rows = data.get("data") if isinstance(data, dict) else []
                return [
                    (scheme, f"{row['ip']}:{row['port']}")
                    for row in rows
                    if isinstance(row, dict) and row.get("ip") and row.get("port")
                ]
            return [(scheme, f"{host}:{port}") for host, port in HOST_PORT_RE.findall(raw)]

        with ThreadPoolExecutor(max_workers=len(SOURCES)) as executor:
            for chunk in executor.map(grab, SOURCES):
                for scheme, addr in chunk:
                    found.setdefault(addr, scheme)
        log.info("собрано адресов: %d", len(found))
        return [(scheme, addr) for addr, scheme in found.items()]

    # --------------------------------------------------------------- checks
    def _check_http(self, addr: str, host: str = "www.tiktok.com", port: int = 443) -> Optional[float]:
        host_part, _, port_part = addr.partition(":")
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
            if b" 200" not in head.split(b"\r\n", 1)[0]:
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

    def _check_socks5(self, addr: str, host: str = "www.tiktok.com", port: int = 443) -> Optional[float]:
        host_part, _, port_part = addr.partition(":")
        if not host_part or not port_part.isdigit():
            return None
        started = time.time()
        try:
            sock = socket.create_connection((host_part, int(port_part)), timeout=self.timeout)
        except OSError:
            return None
        try:
            sock.settimeout(self.timeout)
            sock.sendall(b"\x05\x01\x00")  # версия 5, один метод — без авторизации
            if sock.recv(2) != b"\x05\x00":
                return None
            host_bytes = host.encode()
            sock.sendall(
                b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + port.to_bytes(2, "big")
            )
            reply = sock.recv(4)
            if len(reply) < 4 or reply[1] != 0:
                return None
            atyp = reply[3]
            if atyp == 1:
                sock.recv(4 + 2)
            elif atyp == 3:
                length = sock.recv(1)[0]
                sock.recv(length + 2)
            elif atyp == 4:
                sock.recv(16 + 2)
            context = ssl.create_default_context()
            tls = context.wrap_socket(sock, server_hostname=host)
            tls.close()
        except (OSError, ssl.SSLError, IndexError):
            return None
        finally:
            try:
                sock.close()
            except OSError:
                pass
        return round(time.time() - started, 2)

    def check(self, scheme: str, addr: str) -> dict[str, Any]:
        latency = self._check_socks5(addr) if scheme == "socks5" else self._check_http(addr)
        if latency is None:
            return {"addr": addr, "scheme": scheme, "ok": False, "latency": 0.0, "fails": 1}
        return {"addr": addr, "scheme": scheme, "ok": True, "latency": latency, "fails": 0}

    # ---------------------------------------------------------------- pool
    def refresh(self, max_check: int = 400) -> int:
        """Пересобирает пул: скачать списки, проверить, оставить живые."""
        candidates = self.fetch()
        random.shuffle(candidates)
        if max_check:
            # половину выборки отдаём socks5 — они туннелируют надёжнее
            socks = [item for item in candidates if item[0] == "socks5"]
            http = [item for item in candidates if item[0] == "http"]
            half = max_check // 2
            candidates = socks[:half] + http[: max_check - len(socks[:half])]
        if not candidates:
            return 0

        checked: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            for result in executor.map(lambda item: self.check(item[0], item[1]), candidates):
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
        updated = 0
        if self.path.exists():
            try:
                updated = int(json.loads(self.path.read_text(encoding="utf-8")).get("updated", 0))
            except (OSError, json.JSONDecodeError, ValueError):
                updated = 0
        if len(fresh) < min_alive or (time.time() - updated) > max_age:
            return self.refresh()
        return len(fresh)

    # ------------------------------------------------------------- acquire
    def acquire(self) -> Optional[str]:
        """Рабочий прокси, ещё не занятый в этом прогоне. None — пул пуст."""
        with self._lock:
            usable = sorted(
                (row for row in self.proxies if row.get("ok")),
                key=lambda row: row.get("latency", 9.0),
            )
            for row in usable:
                key = f"{row.get('scheme', 'http')}://{row['addr']}"
                if key not in self._used:
                    self._used.add(key)
                    return key
        if self.refresh() == 0:
            return None
        return self.acquire()

    def mark_bad(self, proxy: str) -> None:
        scheme, _, addr = proxy.partition("://")
        with self._lock:
            for row in self.proxies:
                if row.get("addr") == addr:
                    row["ok"] = False
                    row["fails"] = int(row.get("fails", 0)) + 1
            self._save()

    def stats(self) -> dict[str, int]:
        alive = [row for row in self.proxies if row.get("ok")]
        socks = [row for row in alive if row.get("scheme") == "socks5"]
        return {"alive": len(alive), "total": len(self.proxies), "socks": len(socks)}


POOL = ProxyPool()
