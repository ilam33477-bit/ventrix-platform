from __future__ import annotations

from types import SimpleNamespace

import pytest
from telethon.sessions import MemorySession

from services.backend.telegram_sessions.gateway import TelegramLoginRestarted, TelethonGateway


class FakeClient:
    def __init__(self) -> None:
        self.connected = False
        self.connects = 0
        self.disconnects = 0

    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True
        self.connects += 1

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnects += 1

    async def __call__(self, _request):
        return SimpleNamespace(filters=[SimpleNamespace(id=7, title="Работа")])


class FakeLoginClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.session = MemorySession()
        self.requested_phone: str | None = None
        self.sign_in_calls: list[dict[str, object]] = []

    async def send_code_request(self, phone: str):
        self.requested_phone = phone
        return SimpleNamespace(
            phone_code_hash="hash-1",
            type=SimpleNamespace(),
            next_type=None,
            timeout=30,
        )

    async def sign_in(self, phone=None, *, code=None, phone_code_hash=None, password=None):
        self.sign_in_calls.append(
            {
                "phone": phone,
                "code": code,
                "phone_code_hash": phone_code_hash,
                "password": password,
            }
        )

    async def get_me(self):
        return SimpleNamespace(
            id=42,
            first_name="Test",
            last_name="User",
            username="test_user",
        )


@pytest.mark.asyncio
async def test_gateway_reuses_and_closes_account_connection(monkeypatch) -> None:
    gateway = TelethonGateway(1, "hash")
    client = FakeClient()
    monkeypatch.setattr(gateway, "client", lambda _session=None: client)

    async with gateway.connected_client("encrypted-session") as first:
        assert first is client
    async with gateway.connected_client("encrypted-session") as second:
        assert second is client

    assert client.connects == 1
    assert client.disconnects == 0
    await gateway.close()
    assert client.disconnects == 1


@pytest.mark.asyncio
async def test_gateway_accepts_telethon_dialog_filters_container(monkeypatch) -> None:
    gateway = TelethonGateway(1, "hash")
    client = FakeClient()
    monkeypatch.setattr(gateway, "client", lambda _session=None: client)

    folders = await gateway.list_folders("encrypted-session")

    assert [(item.id, item.title) for item in folders] == [(7, "Работа")]


@pytest.mark.asyncio
async def test_login_keeps_same_connected_client_until_authorized(monkeypatch) -> None:
    gateway = TelethonGateway(1, "hash")
    client = FakeLoginClient()
    monkeypatch.setattr(gateway, "client", lambda _session=None: client)

    challenge = await gateway.begin_login("+79099412079")

    assert client.connected is True
    assert client.disconnects == 0
    result = await gateway.complete_login(
        challenge.session_string,
        "+79099412079",
        challenge.phone_code_hash,
        code="12 3-45",
    )
    assert result.status == "connected"
    assert client.sign_in_calls[0]["code"] == "12345"
    assert client.disconnects == 1


@pytest.mark.asyncio
async def test_login_requires_restart_when_temporary_client_is_lost(monkeypatch) -> None:
    gateway = TelethonGateway(1, "hash")
    client = FakeLoginClient()
    monkeypatch.setattr(gateway, "client", lambda _session=None: client)
    challenge = await gateway.begin_login("+79099412079")
    await gateway.cancel_login(challenge.session_string)

    with pytest.raises(TelegramLoginRestarted):
        await gateway.complete_login(
            challenge.session_string,
            "+79099412079",
            challenge.phone_code_hash,
            code="12345",
        )
