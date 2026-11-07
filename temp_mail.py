# language: Python 3.9+, file: temp_mail.py
"""Одноразовая почта через mail.tm — чтобы принимать код подтверждения TikTok.

API: https://api.mail.tm  (бесплатно, регистрация邮箱 не нужна).
Только stdlib: worker живёт в отдельном потоке, ему aiohttp не нужен.

Этика сервиса: адрес живёт, пока хотя бы раз в неделю к нему обращаются.
После регистрации TikTok аккаунт привязан к этому адресу — пароль от него
сохраняется в accounts.json вместе с аккаунтом, чтобы можно было восстановить.
"""
from __future__ import annotations

import json
import logging
import random
import re
import string
import time
import urllib.error
import urllib.request
from typing import Any, Optional

log = logging.getLogger(__name__)

CODE_RE = re.compile(r"\b(\d{6})\b")


class TempMailError(RuntimeError):
    pass


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

    # --------------------------------------------------------------- mailbox
    def create(self) -> tuple[str, str, str]:
        """Создаёт ящик. Возвращает (адрес, пароль, token)."""
        domains = self._req("GET", "/domains")
        members = domains.get("hydra:member") or []
        if not members:
            raise TempMailError("mail.tm не отдал домены")
        domain = members[0]["domain"]
        local = "tt" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        address = f"{local}@{domain}"
        password = gen_password(16)
        self._req("POST", "/accounts", {"address": address, "password": password})
        token = self._req("POST", "/token", {"address": address, "password": password})["token"]
        log.info("временный ящик: %s", address)
        return address, password, token

    def wait_for_code(self, token: str, timeout: int = 240, interval: int = 6) -> Optional[str]:
        """Ждёт письмо с 6-значным кодом. None — не дождались."""
        deadline = time.time() + timeout
        seen: set[str] = set()
        while time.time() < deadline:
            try:
                inbox = self._req("GET", "/messages", token=token)
            except TempMailError as exc:
                log.warning("mail.tm: %s", exc)
                time.sleep(interval)
                continue
            for message in inbox.get("hydra:member") or []:
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
