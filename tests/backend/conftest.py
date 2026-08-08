from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import time

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.backend.config import Settings
from services.backend.database import Base, build_engine
from services.backend.schemas import TenantCreate
from services.backend.services.encryption import EncryptionService
from services.backend.services.foundation import FoundationService
from services.backend.services.telegram import VerifiedBot


class FakeVerifier:
    def __init__(self) -> None:
        self.tokens: list[str] = []

    async def verify(self, token: str) -> VerifiedBot:
        self.tokens.append(token)
        return VerifiedBot(bot_id=987654321, username="axiom_ops_bot", display_name="Axiom Ops")


@pytest.fixture
def encryption_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def settings(encryption_key: str) -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///./data/test.db",
        platform_owner_telegram_id=100200300,
        platform_owner_telegram_username="test_owner",
        telegram_owner_bot_token="123456:owner-bot-token-for-tests",
        owner_api_token="owner-api-token-at-least-24-characters",
        app_encryption_key=encryption_key,
        telegram_api_base_url="https://api.telegram.invalid",
    )


@pytest_asyncio.fixture
async def session_factory(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", 5000)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def verifier() -> FakeVerifier:
    return FakeVerifier()


@pytest.fixture
def tenant_payload() -> TenantCreate:
    return TenantCreate(
        name="Axiom Group",
        owner_name="Elena Kotova",
        owner_telegram_username="elena_axiom",
        owner_telegram_user_id=555000111,
        niche="B2B services",
        business_description="Implementation and support for retail companies",
        products_services="Implementation, Support",
        target_audience="Retail operations directors",
        working_hours={"description": "Mon-Fri 09:00-18:00"},
        timezone="Europe/Moscow",
        response_sla_minutes=60,
        critical_problem_criteria="Existing client mentions refund or is ignored",
        daily_report_time=time(9, 30),
        plan="trial",
        additional_ai_instructions="Use the term project instead of deal",
    )


@pytest.fixture
def make_service(settings: Settings, encryption_key: str, verifier: FakeVerifier):
    def factory(session: AsyncSession, source: str = "test") -> FoundationService:
        return FoundationService(
            session,
            settings.platform_owner_telegram_id,
            EncryptionService(encryption_key),
            verifier,
            source,
            settings.platform_owner_telegram_username,
        )

    return factory
