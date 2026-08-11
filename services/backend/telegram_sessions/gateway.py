from __future__ import annotations

import asyncio
import hashlib
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from telethon import TelegramClient, errors, functions
from telethon.sessions import StringSession


class TelegramFloodWait(RuntimeError):
    def __init__(self, seconds: int) -> None:
        super().__init__("Telegram requested a temporary flood wait")
        self.retry_after_seconds = max(1, seconds)


class TelegramSessionRevoked(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LoginChallenge:
    session_string: str
    phone_code_hash: str
    delivery_type: str = "telegram_app"
    next_delivery_type: str | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class LoginResult:
    status: str
    session_string: str
    telegram_user_id: int | None = None
    username: str | None = None
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteFolder:
    id: int
    title: str


@dataclass(frozen=True, slots=True)
class RemoteDialog:
    id: int
    title: str
    username: str | None
    dialog_type: str
    folder_id: int | None
    participants_count: int | None = None
    last_message_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RemoteMessage:
    id: int
    sender_id: int | None
    sender_username: str | None
    sent_at: datetime
    edited_at: datetime | None
    outgoing: bool
    text: str | None
    attachments: list[dict[str, str | int | None]]


@dataclass(frozen=True, slots=True)
class MessageBatch:
    messages: list[RemoteMessage]
    next_offset_id: int
    has_more: bool


class TelegramUserGateway(Protocol):
    async def begin_login(self, phone: str) -> LoginChallenge: ...
    async def resend_login(
        self, session_string: str, phone: str, phone_code_hash: str
    ) -> LoginChallenge: ...
    async def complete_login(
        self,
        session_string: str,
        phone: str,
        phone_code_hash: str,
        *,
        code: str | None = None,
        password: str | None = None,
    ) -> LoginResult: ...
    async def list_folders(self, session_string: str) -> list[RemoteFolder]: ...
    async def list_dialogs(self, session_string: str) -> list[RemoteDialog]: ...
    async def fetch_messages(
        self,
        session_string: str,
        dialog_id: int,
        *,
        offset_id: int,
        limit: int,
    ) -> MessageBatch: ...
    async def fetch_new_messages(
        self,
        session_string: str,
        dialog_id: int,
        *,
        after_id: int,
        limit: int,
    ) -> MessageBatch: ...
    async def terminate_session(self, session_string: str) -> None: ...
    async def check_session(self, session_string: str) -> LoginResult: ...


class TelethonGateway:
    def __init__(self, api_id: int, api_hash: str, *, idle_ttl_seconds: float = 300) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.idle_ttl_seconds = idle_ttl_seconds
        self._clients: dict[str, TelegramClient] = {}
        self._client_locks: dict[str, asyncio.Lock] = {}
        self._last_used: dict[str, float] = {}

    def client(
        self, session: str | None = None, *, receive_updates: bool = False
    ) -> TelegramClient:
        return TelegramClient(
            StringSession(session or ""),
            self.api_id,
            self.api_hash,
            receive_updates=receive_updates,
            flood_sleep_threshold=0,
            request_retries=2,
        )

    @staticmethod
    def _session_key(session_string: str) -> str:
        return hashlib.sha256(session_string.encode()).hexdigest()

    @asynccontextmanager
    async def connected_client(self, session_string: str):
        """Reuse one serialized Telethon connection per account in this worker."""
        key = self._session_key(session_string)
        await self._evict_idle(exclude=key)
        lock = self._client_locks.setdefault(key, asyncio.Lock())
        async with lock:
            client = self._clients.get(key)
            if client is None:
                client = self.client(session_string)
                self._clients[key] = client
            try:
                if not client.is_connected():
                    await client.connect()
                yield client
            except (errors.AuthKeyUnregisteredError, errors.SessionRevokedError):
                await self._discard_client(key)
                raise TelegramSessionRevoked("Telegram session was revoked") from None
            except (ConnectionError, OSError):
                await self._discard_client(key)
                raise
            finally:
                if key in self._clients:
                    self._last_used[key] = time.monotonic()

    async def _evict_idle(self, *, exclude: str | None = None) -> None:
        cutoff = time.monotonic() - self.idle_ttl_seconds
        stale = [
            key
            for key, last_used in self._last_used.items()
            if key != exclude and last_used < cutoff and not self._client_locks[key].locked()
        ]
        for key in stale:
            await self._discard_client(key)

    async def _discard_client(self, key: str) -> None:
        client = self._clients.pop(key, None)
        self._last_used.pop(key, None)
        if client is not None and client.is_connected():
            await client.disconnect()

    async def close(self) -> None:
        for key in list(self._clients):
            await self._discard_client(key)

    async def begin_login(self, phone: str) -> LoginChallenge:
        client = self.client()
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
            return self._login_challenge(client, sent)
        except errors.FloodWaitError as exc:
            raise TelegramFloodWait(exc.seconds) from None
        finally:
            await client.disconnect()

    async def resend_login(
        self, session_string: str, phone: str, phone_code_hash: str
    ) -> LoginChallenge:
        client = self.client(session_string)
        try:
            await client.connect()
            sent = await client(
                functions.auth.ResendCodeRequest(
                    phone_number=phone,
                    phone_code_hash=phone_code_hash,
                )
            )
            return self._login_challenge(client, sent, fallback_hash=phone_code_hash)
        except errors.FloodWaitError as exc:
            raise TelegramFloodWait(exc.seconds) from None
        finally:
            await client.disconnect()

    @staticmethod
    def _login_challenge(
        client: TelegramClient, sent: object, *, fallback_hash: str = ""
    ) -> LoginChallenge:
        def delivery_name(value: object | None) -> str | None:
            if value is None:
                return None
            name = type(value).__name__
            return {
                "SentCodeTypeApp": "telegram_app",
                "SentCodeTypeSms": "sms",
                "SentCodeTypeCall": "call",
                "SentCodeTypeFlashCall": "flash_call",
                "SentCodeTypeMissedCall": "missed_call",
                "CodeTypeSms": "sms",
                "CodeTypeCall": "call",
                "CodeTypeFlashCall": "flash_call",
                "CodeTypeMissedCall": "missed_call",
            }.get(name, name)

        return LoginChallenge(
            StringSession.save(client.session),
            str(getattr(sent, "phone_code_hash", None) or fallback_hash),
            delivery_name(getattr(sent, "type", None)) or "telegram_app",
            delivery_name(getattr(sent, "next_type", None)),
            getattr(sent, "timeout", None),
        )

    async def complete_login(
        self,
        session_string: str,
        phone: str,
        phone_code_hash: str,
        *,
        code: str | None = None,
        password: str | None = None,
    ) -> LoginResult:
        client = self.client(session_string)
        try:
            await client.connect()
            try:
                if password is not None:
                    await client.sign_in(password=password)
                else:
                    await client.sign_in(phone, code=code, phone_code_hash=phone_code_hash)
            except errors.SessionPasswordNeededError:
                return LoginResult("awaiting_2fa", StringSession.save(client.session))
            me = await client.get_me()
            display_name = " ".join(part for part in (me.first_name, me.last_name) if part)
            return LoginResult(
                "connected",
                StringSession.save(client.session),
                int(me.id),
                me.username,
                display_name or None,
            )
        except errors.FloodWaitError as exc:
            raise TelegramFloodWait(exc.seconds) from None
        finally:
            await client.disconnect()

    async def list_folders(self, session_string: str) -> list[RemoteFolder]:
        try:
            async with self.connected_client(session_string) as client:
                response = await client(functions.messages.GetDialogFiltersRequest())
                filters = getattr(response, "filters", response)
                return [
                    RemoteFolder(int(item.id), str(getattr(item, "title", "Рабочая папка")))
                    for item in filters
                    if getattr(item, "id", 0)
                ]
        except errors.FloodWaitError as exc:
            raise TelegramFloodWait(exc.seconds) from None

    async def list_dialogs(self, session_string: str) -> list[RemoteDialog]:
        result: list[RemoteDialog] = []
        try:
            async with self.connected_client(session_string) as client:
                async for dialog in client.iter_dialogs():
                    entity = dialog.entity
                    if dialog.is_user:
                        kind = "personal"
                    elif dialog.is_channel and getattr(entity, "broadcast", False):
                        kind = "channel"
                    else:
                        kind = "group"
                    raw = getattr(dialog, "dialog", None)
                    result.append(
                        RemoteDialog(
                            int(dialog.id),
                            str(dialog.name or "Без названия"),
                            getattr(entity, "username", None),
                            kind,
                            getattr(raw, "folder_id", None),
                            getattr(entity, "participants_count", None),
                            getattr(dialog.message, "date", None),
                        )
                    )
            return result
        except errors.FloodWaitError as exc:
            raise TelegramFloodWait(exc.seconds) from None

    async def fetch_messages(
        self,
        session_string: str,
        dialog_id: int,
        *,
        offset_id: int,
        limit: int,
    ) -> MessageBatch:
        messages: list[RemoteMessage] = []
        try:
            async with self.connected_client(session_string) as client:
                async for item in client.iter_messages(dialog_id, limit=limit, offset_id=offset_id):
                    sender = await item.get_sender() if item.sender_id else None
                    attachments: list[dict[str, str | int | None]] = []
                    if item.media:
                        file = getattr(item, "file", None)
                        attachments.append(
                            {
                                "kind": type(item.media).__name__,
                                "name": getattr(file, "name", None),
                                "size": getattr(file, "size", None),
                                "mime_type": getattr(file, "mime_type", None),
                            }
                        )
                    messages.append(
                        RemoteMessage(
                            int(item.id),
                            int(item.sender_id) if item.sender_id else None,
                            getattr(sender, "username", None),
                            item.date,
                            item.edit_date,
                            bool(item.out),
                            item.message or None,
                            attachments,
                        )
                    )
            next_offset = min((item.id for item in messages), default=offset_id)
            return MessageBatch(messages, next_offset, len(messages) == limit)
        except errors.FloodWaitError as exc:
            raise TelegramFloodWait(exc.seconds) from None

    async def fetch_new_messages(
        self,
        session_string: str,
        dialog_id: int,
        *,
        after_id: int,
        limit: int,
    ) -> MessageBatch:
        messages: list[RemoteMessage] = []
        try:
            async with self.connected_client(session_string) as client:
                async for item in client.iter_messages(
                    dialog_id,
                    min_id=after_id,
                    reverse=True,
                    limit=limit,
                ):
                    sender = await item.get_sender() if item.sender_id else None
                    attachments: list[dict[str, str | int | None]] = []
                    if item.media:
                        file = getattr(item, "file", None)
                        attachments.append(
                            {
                                "kind": type(item.media).__name__,
                                "name": getattr(file, "name", None),
                                "size": getattr(file, "size", None),
                                "mime_type": getattr(file, "mime_type", None),
                            }
                        )
                    messages.append(
                        RemoteMessage(
                            int(item.id),
                            int(item.sender_id) if item.sender_id else None,
                            getattr(sender, "username", None),
                            item.date,
                            item.edit_date,
                            bool(item.out),
                            item.message or None,
                            attachments,
                        )
                    )
            newest = max((item.id for item in messages), default=after_id)
            return MessageBatch(messages, newest, len(messages) == limit)
        except errors.FloodWaitError as exc:
            raise TelegramFloodWait(exc.seconds) from None

    async def terminate_session(self, session_string: str) -> None:
        key = self._session_key(session_string)
        client = self._clients.pop(key, None) or self.client(session_string)
        try:
            if not client.is_connected():
                await client.connect()
            await client.log_out()
        finally:
            await client.disconnect()
            self._last_used.pop(key, None)
            self._client_locks.pop(key, None)

    async def check_session(self, session_string: str) -> LoginResult:
        try:
            async with self.connected_client(session_string) as client:
                if not await client.is_user_authorized():
                    raise TelegramSessionRevoked("Telegram session is no longer authorized")
                me = await client.get_me()
                display_name = " ".join(part for part in (me.first_name, me.last_name) if part)
                return LoginResult(
                    "connected",
                    StringSession.save(client.session),
                    int(me.id),
                    me.username,
                    display_name or None,
                )
        except (errors.AuthKeyUnregisteredError, errors.SessionRevokedError):
            raise TelegramSessionRevoked("Telegram session was revoked") from None
