from __future__ import annotations

import pytest

from services.backend.telegram_sessions.gateway import TelethonGateway


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
