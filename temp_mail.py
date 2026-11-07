# language: Python 3.9+, file: temp_mail.py
"""Одноразовая почта: mail.gw (основной) и mail.tm (резерв).

Только stdlib — worker живёт в отдельном потоке, ему aiohttp не нужен.

Почему два провайдера: mail.tm отдаёт один домен и режет частые запросы (429).
mails.gw — тот же API, но другая инфраструктура и другой набор доменов.

Токен несёт провайдера — "gw:<token>" или "mtm:<token>", поэтому
wait_for_code знает, куда стучаться.
"""
from __future__ import annotations

import json
import logging
import random
import re
import string
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

log = logging.getLogger(__name__)

CODE_RE = re.compile(r"\b(\d{6})\b")
MAILGW_API = "https://api.mail.gw"


class TempMailError(RuntimeError):
    pass


def _members(payload: Any) -> list[dict[str, Any]]:
    """mail.tm в разных версиях отдаёт то список, то hydra-обёртку."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("hydra:member", "member", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def gen_password(length: int = 14) -> str:
    """Пароль с гарантией букв/цифр/символа — TikTok режет простые."""
    alphabet = string.ascii_letters + string.digits
    specials = "!@#$%^&*"
    body = [random.choice(alphabet) for _ in range(max(8, length - 3))]
    body.append(random.choice(specials))
    body.append(random.choice(string.ascii_uppercase))
    body.append(random.choice(string.digits))
    random.shuffle(body)
    return "".join(body)


def extract_code(text: str) -> Optional[str]:
    match = CODE_RE.search(text or "")
    return match.group(1) if match else None


class TempMail:
    BASE = "https://api.mail.tm"

    def __init__(self, timeout: int = 15, base: str = "") -> None:
        self.timeout = timeout
        self.base = base or self.BASE

    # ------------------------------------------------------------------ http
    def _get_json(self, url: str) -> Any:
        request = urllib.request.Request(url)
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", "Mozilla/5.0")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200].decode("utf-8", "ignore")
            raise TempMailError(f"{exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TempMailError(str(exc)) from exc
        return json.loads(raw) if raw.strip() else {}

    def _req(
        self,
        method: str,
        path: str,
        payload: Optional[dict[str, Any]] = None,
        token: str = "",
    ) -> Any:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.base + path, method=method, data=data)
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "ignore")
            raise TempMailError(f"{exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TempMailError(str(exc)) from exc
        return json.loads(raw) if raw.strip() else {}

    # ----------------------------------------------------------------- mail.gw
    def _create_gw(self) -> tuple[str, str, str]:
        return TempMail(self.timeout, base=MAILGW_API)._create_mtm()

    # ----------------------------------------------------------------- mail.tm
    def _create_mtm(self) -> tuple[str, str, str]:
        domains = self._req("GET", "/domains")
        members = _members(domains)
        if not members:
            raise TempMailError("mail.tm не отдал домены")
        domain = members[0]["domain"]
        local = "tt" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        address = f"{local}@{domain}"
        password = gen_password(16)
        self._req("POST", "/accounts", {"address": address, "password": password})
        token = self._req("POST", "/token", {"address": address, "password": password})["token"]
        log.info("временный ящик (mail.tm): %s", address)
        return address, password, token

    def _wait_mtm(self, token: str, timeout: int, interval: int) -> Optional[str]:
        deadline = time.time() + timeout
        seen: set[str] = set()
        while time.time() < deadline:
            try:
                inbox = self._req("GET", "/messages", token=token)
            except TempMailError as exc:
                log.warning("mail.tm: %s", exc)
                time.sleep(interval)
                continue
            for message in _members(inbox):
                message_id = str(message.get("id"))
                if message_id in seen:
                    continue
                seen.add(message_id)
                preview = f"{message.get('subject', '')} {message.get('intro', '')}"
                code = extract_code(preview)
                if code:
                    return code
                try:
                    full = self._req("GET", f"/messages/{message_id}", token=token)
                except TempMailError:
                    continue
                body = (full.get("text") or "") + " " + " ".join(full.get("html") or [])
                code = extract_code(body)
                if code:
                    return code
            time.sleep(interval)
        return None

    # ------------------------------------------------------------ guerrillamail
    GM_API = "https://api.guerrillamail.com/ajax.php"

    def _create_gm(self) -> tuple[str, str, str]:
        data = self._get_json(self.GM_API + "?f=get_email_address")
        address = data.get("email_addr")
        sid = data.get("sid_token")
        if not address or not sid:
            raise TempMailError("guerrillamail не выдал адрес")
        log.info("временный ящик (guerrillamail): %s", address)
        return address, "", sid

    def _wait_gm(self, token: str, timeout: int, interval: int) -> Optional[str]:
        sid, _, address = token.partition("|")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                inbox = self._get_json(
                    f"{self.GM_API}?f=check_email&seq=0&sid_token={sid}"
                )
            except TempMailError as exc:
                log.warning("guerrillamail: %s", exc)
                time.sleep(interval)
                continue
            for message in inbox.get("list") or []:
                mail_id = message.get("mail_id")
                if not mail_id:
                    continue
                try:
                    full = self._get_json(
                        f"{self.GM_API}?f=fetch_email&email_id={mail_id}&sid_token={sid}"
                    )
                except TempMailError:
                    continue
                blob = " ".join(
                    str(full.get(key, ""))
                    for key in ("mail_subject", "mail_body", "mail_excerpt")
                )
                code = extract_code(blob)
                if code:
                    return code
            time.sleep(interval)
        return None

    # --------------------------------------------------------------- внешнее
    def create(self) -> tuple[str, str, str]:
        """Ящик на mail.gw, при недоступности — mail.tm. В токене — провайдер."""
        error: Optional[Exception] = None
        try:
            address, password, sid = self._create_gm()
            return address, password, f"gm:{sid}|{address}"
        except TempMailError as exc:
            error = exc
            log.warning("guerrillamail недоступен: %s", exc)
        for _ in range(2):
            try:
                address, password, token = self._create_gw()
                return address, password, f"gw:{token}"
            except TempMailError as exc:
                error = exc
                log.warning("mail.gw недоступен: %s", exc)
                time.sleep(3)
        log.warning("перехожу на mail.tm: %s", error)
        address, password, token = self._create_mtm()
        return address, password, f"mtm:{token}"

    def wait_for_code(self, token: str, timeout: int = 240, interval: int = 6) -> Optional[str]:
        provider, _, value = token.partition(":")
        if provider == "gm":
            return self._wait_gm(value, timeout, interval)
        if provider == "gw":
            return TempMail(self.timeout, base=MAILGW_API)._wait_mtm(value, timeout, interval)
        return self._wait_mtm(value, timeout, interval)
