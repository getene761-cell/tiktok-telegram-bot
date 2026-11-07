# language: Python 3.9+, file: imap_mail.py
"""Чтение кода подтверждения из настоящего ящика по IMAP.

Зачем: одноразовые сервисы (mail.tm, mail.gw, 1secmail) либо режут по IP (429),
либо в чёрных списках TikTok. Адреса вида `user+tt01@gmail.com` (плюс-алиас)
или `tt01@свой-домен` доставляются в обычный ящик, который читается по IMAP —
бот становится автономным: сам придумывает адрес, сам забирает код.

Работает с Gmail (app password, 2FA обязательна), Yandex (пароль приложения),
своим доменом (catch-all → этот же ящик). Hotmail/Outlook не подойдёт: Microsoft
отключила basic auth для IMAP, нужен OAuth2.

Настройка в .env:
    IMAP_HOST=imap.gmail.com
    IMAP_PORT=993
    IMAP_USER=you@gmail.com
    IMAP_PASSWORD=app-password-16-chars
    EMAIL_DOMAIN=            # пусто = взять из IMAP_USER
    ALIAS_PREFIX=tt          # адрес будет you+tt01@gmail.com
"""
from __future__ import annotations

import email
import imaplib
import logging
import random
import re
import string
import time
from email.header import decode_header
from typing import Any, Optional

import config

log = logging.getLogger(__name__)

CODE_RE = re.compile(r"\b(\d{6})\b")


class ImapMailError(RuntimeError):
    pass


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "ignore")
    if not value:
        return ""
    parts: list[str] = []
    for chunk, encoding in decode_header(str(value)):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(encoding or "utf-8", "ignore"))
        else:
            parts.append(chunk)
    return " ".join(parts)


class ImapMail:
    """Тот же интерфейс, что у TempMail.wait_for_code — регистрация не различает."""

    def __init__(self, alias: str = "") -> None:
        self.alias = alias

    @staticmethod
    def ready() -> bool:
        return bool(config.IMAP_HOST and config.IMAP_USER and config.IMAP_PASSWORD)

    @staticmethod
    def mint_alias(index: int = 0) -> str:
        """Создаёт адрес: plus-алиас или local-part на своём домене."""
        domain = config.EMAIL_DOMAIN
        local = config.IMAP_USER.split("@")[0]
        if not domain:
            domain = config.IMAP_USER.split("@")[-1]
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
        tag = f"{config.ALIAS_PREFIX}{index + 1:02d}{suffix}"
        if config.EMAIL_MODE == "subdomain":
            # tt01abc@свой-домен — для catch-all на домене
            return f"{tag}@{domain}"
        return f"{local}+{tag}@{domain}"

    def wait_for_code(self, token: str = "", timeout: int = 240, interval: int = 6) -> Optional[str]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            code = self._poll_once()
            if code:
                return code
            time.sleep(interval)
        return None

    def _poll_once(self) -> Optional[str]:
        try:
            client = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT)
        except OSError as exc:
            log.warning("IMAP: не подключиться (%s)", exc)
            return None
        try:
            client.login(config.IMAP_USER, config.IMAP_PASSWORD)
            client.select("INBOX")
            # письмо от TikTok обычно свежее: смотрим последние непрочитанные
            status, data = client.search(None, "UNSEEN")
            if status != "OK" or not data or not data[0]:
                status, data = client.search(None, "ALL")
            numbers = (data[0] or b"").split()[-15:] if data and data[0] else []
            for number in reversed(numbers):
                code = self._read_message(client, number)
                if code:
                    try:
                        client.store(number, "+FLAGS", "\\Seen")
                    except imaplib.IMAP4.error:
                        pass
                    return code
            return None
        except imaplib.IMAP4.error as exc:
            log.warning("IMAP ошибка: %s", exc)
            return None
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass

    def _read_message(self, client: imaplib.IMAP4, number: bytes) -> Optional[str]:
        try:
            status, parts = client.fetch(number, "(RFC822)")
        except imaplib.IMAP4.error:
            return None
        if status != "OK" or not parts:
            return None
        raw = b""
        for part in parts:
            if isinstance(part, tuple) and len(part) > 1:
                raw = part[1]
                break
        if not raw:
            return None
        try:
            message = email.message_from_bytes(raw)
        except Exception:  # noqa: BLE001
            return None

        recipients = " ".join(
            filter(None, [_decode(message.get("To")), _decode(message.get("Delivered-To"))])
        ).lower()
        # если алиас известен — берём только письма на него
        if self.alias and "@" in self.alias and recipients and self.alias.lower() not in recipients:
            return None

        subject = _decode(message.get("Subject"))
        code = CODE_RE.search(subject)
        if code:
            return code.group(1)

        body = self._body(message)
        code = CODE_RE.search(body)
        if code:
            return code.group(1)
        return None

    @staticmethod
    def _body(message: email.message.Message) -> str:
        chunks: list[str] = []
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_type() in ("text/plain", "text/html"):
                    payload = part.get_payload(decode=True) or b""
                    chunks.append(payload.decode(part.get_content_charset() or "utf-8", "ignore"))
        else:
            payload = message.get_payload(decode=True) or b""
            chunks.append(payload.decode(message.get_content_charset() or "utf-8", "ignore"))
        return "\n".join(chunks)
