from __future__ import annotations

from dataclasses import dataclass

import httpx


class BotTokenVerificationError(RuntimeError):
    """Safe error that never contains the submitted BotFather token."""


@dataclass(frozen=True, slots=True)
class VerifiedBot:
    bot_id: int
    username: str
    display_name: str


class TelegramBotVerifier:
    def __init__(
        self,
        base_url: str = "https://api.telegram.org",
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport

    async def verify(self, token: str) -> VerifiedBot:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.get(f"{self.base_url}/bot{token}/getMe")
            if response.status_code != 200:
                raise BotTokenVerificationError("Telegram rejected the bot token")
            payload = response.json()
            result = payload.get("result") if payload.get("ok") is True else None
            if not isinstance(result, dict) or not result.get("id") or not result.get("username"):
                raise BotTokenVerificationError("Telegram returned an invalid getMe response")
            return VerifiedBot(
                bot_id=int(result["id"]),
                username=str(result["username"]),
                display_name=str(result.get("first_name") or result["username"]),
            )
        except BotTokenVerificationError:
            raise
        except Exception:  # noqa: BLE001 - sanitize all transport/client errors at trust boundary
            raise BotTokenVerificationError("Telegram token verification is unavailable") from None
