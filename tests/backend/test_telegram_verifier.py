import httpx
import pytest

from services.backend.services.telegram import BotTokenVerificationError, TelegramBotVerifier


@pytest.mark.asyncio
async def test_get_me_response_is_mapped_without_returning_token() -> None:
    token = "mock-telegram-token-must-remain-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/bot{token}/getMe")
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"id": 42, "username": "client_bot", "first_name": "Client"},
            },
        )

    verifier = TelegramBotVerifier(transport=httpx.MockTransport(handler))
    result = await verifier.verify(token)
    assert result.bot_id == 42
    assert result.username == "client_bot"
    assert token not in repr(result)


@pytest.mark.asyncio
async def test_rejected_token_raises_sanitized_error() -> None:
    token = "123456789:bad-secret-token"
    verifier = TelegramBotVerifier(
        transport=httpx.MockTransport(lambda _: httpx.Response(401, json={"ok": False}))
    )
    with pytest.raises(BotTokenVerificationError) as caught:
        await verifier.verify(token)
    assert token not in str(caught.value)
