# language: Python 3.9+, file: temp_mail.py
"""Одноразовая почта: 1secmail (основной) и mail.tm (резерв).

Только stdlib — worker живёт в отдельном потоке, ему aiohttp не нужен.

Почему два провайдера: mail.tm отдаёт один домен и режет частые запросы (429),
из-за этого код подтверждения не забрать. 1secmail даёт десяток доменов и не
требует регистрации ящика: адрес существует, как только на него пришло письмо.

Токен несёт провайдера — "1sec:<login>@<domain>" или "mtm:<token>", поэтому
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
ONE_SEC_API = "https://www.1secmail.com/api/v1/"


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

    def __init__(self, timeout: int = 15) -> None:
        self.timeout = timeout

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
        request = urllib.request.Request(self.BASE + path, method=method, data=data)
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

    # -------------------------------------------------------------- 1secmail
    def _create_1sec(self) -> tuple[str, str]:
        domains = self._get_json(ONE_SEC_API + "?action=getDomainList")
        if not isinstance(domains, list) or not domains:
            raise TempMailError("1secmail: пустой список доменов")
        preferred = [d for d in domains if d in ("1secmail.com", "1secmail.org", "1secmail.net")]
        domain = (preferred or domains)[0]
        local = "tt" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        log.info("временный ящик (1secmail): %s@%s", local, domain)
        return f"{local}@{domain}", f"{local}@{domain}"

    def _wait_1sec(self, mailbox: str, timeout: int, interval: int) -> Optional[str]:
        login, _, domain = mailbox.partition("@")
        params = urllib.parse.urlencode({"action": "getMessages", "login": login, "domain": domain})
        deadline = time.time() + timeout
        seen: set[str] = set()
        while time.time() < deadline:
            try:
                messages = self._get_json(ONE_SEC_API + "?" + params)
            except TempMailError as exc:
                log.warning("1secmail: %s", exc)
                time.sleep(interval)
                continue
            for message in messages if isinstance(messages, list) else []:
                message_id = str(message.get("id"))
                if message_id in seen:
                    continue
                seen.add(message_id)
                code = extract_code(str(message.get("subject", "")))
                if code:
                    return code
                read = urllib.parse.urlencode(
                    {"action": "readMessage", "login": login, "domain": domain, "id": message_id}
                )
                try:
                    full = self._get_json(ONE_SEC_API + "?" + read)
                except TempMailError:
                    continue
                body = f"{full.get('subject', '')} {full.get('textBody', '')} {full.get('body', '')}"
                code = extract_code(body)
                if code:
                    return code
            time.sleep(interval)
        return None

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

    # --------------------------------------------------------------- внешнее
    def create(self) -> tuple[str, str, str]:
        """Создаёт ящик. Возвращает (адрес, пароль, token с провайдером)."""
        error: Optional[Exception] = None
        for _ in range(2):
            try:
                address, marker = self._create_1sec()
                return address, "", f"1sec:{marker}"
            except TempMailError as exc:
                error = exc
                log.warning("1secmail недоступен: %s", exc)
                time.sleep(3)
        log.warning("перехожу на mail.tm: %s", error)
        address, password, token = self._create_mtm()
        return address, password, f"mtm:{token}"

    def wait_for_code(self, token: str, timeout: int = 240, interval: int = 6) -> Optional[str]:
        provider, _, value = token.partition(":")
        if provider == "1sec":
            return self._wait_1sec(value, timeout, interval)
        return self._wait_mtm(value, timeout, interval)
