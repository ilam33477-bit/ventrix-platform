from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..database import get_session
from ..services.encryption import EncryptionService
from ..services.foundation import FoundationService
from ..services.telegram import TelegramBotVerifier


def require_owner_api_token(
    x_owner_token: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),  # noqa: B008 - FastAPI dependency declaration
) -> None:
    expected = settings.owner_api_token.get_secret_value()
    if x_owner_token is None or not hmac.compare_digest(x_owner_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="owner authentication required"
        )


def build_service(
    session: AsyncSession,
    settings: Settings,
    source: str = "api",
) -> FoundationService:
    return FoundationService(
        session=session,
        owner_telegram_id=settings.platform_owner_telegram_id,
        encryption=EncryptionService(settings.app_encryption_key.get_secret_value()),
        verifier=TelegramBotVerifier(settings.telegram_api_base_url),
        source=source,
        owner_username=settings.platform_owner_telegram_username,
    )


async def get_foundation_service(
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI dependency declaration
    settings: Settings = Depends(get_settings),  # noqa: B008 - FastAPI dependency declaration
) -> FoundationService:
    return build_service(session, settings)
