from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import func, select, update

from services.backend.analysis.preprocessing import local_features
from services.backend.analysis.schema import parse_analysis_response
from services.backend.jobs.queue import SQLiteJobQueue
from services.backend.models import (
    TelegramMessage,
    TenantAnalysisSchedule,
    TenantSettings,
)
from services.backend.scheduler.service import TenantAnalysisScheduler, next_analysis_time
from services.backend.timezones import normalize_timezone


@pytest.mark.parametrize("legacy", ["Moscow", "MSK", "Москва", " moscow "])
def test_legacy_timezones_are_normalized(legacy: str) -> None:
    assert normalize_timezone(legacy) == "Europe/Moscow"


def test_invalid_timezone_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid IANA timezone"):
        normalize_timezone("Mars/Olympus")


def test_next_analysis_time_respects_timezone_days_and_advance() -> None:
    now = datetime(2026, 8, 3, 5, 0, tzinfo=UTC)  # Monday, 08:00 in Moscow
    planned = next_analysis_time(
        now=now,
        timezone="Europe/Moscow",
        report_time=time(9, 0),
        enabled_days=[0, 1, 2, 3, 4],
        advance_minutes=15,
    )
    assert planned == datetime(2026, 8, 3, 5, 45, tzinfo=UTC)


@pytest.mark.asyncio
async def test_queue_fairness_and_one_heavy_job_per_tenant(
    session_factory, make_service, tenant_payload
) -> None:
    async with session_factory() as session:
        first = await make_service(session).create_tenant(tenant_payload)
        second_payload = tenant_payload.model_copy(
            update={
                "name": "Second tenant",
                "owner_telegram_username": "second_owner",
                "owner_telegram_user_id": 555000222,
            }
        )
        second = await make_service(session).create_tenant(second_payload)

    queue = SQLiteJobQueue(session_factory)
    first_one = await queue.enqueue(
        "analysis.pipeline", {}, tenant_id=first.id, category="analysis"
    )
    first_two = await queue.enqueue(
        "analysis.pipeline", {}, tenant_id=first.id, category="analysis"
    )
    second_one = await queue.enqueue(
        "analysis.pipeline", {}, tenant_id=second.id, category="analysis"
    )

    lease_one = await queue.claim_next("worker-1")
    lease_two = await queue.claim_next("worker-2")
    assert lease_one is not None and lease_one.id == first_one
    assert lease_two is not None and lease_two.id == second_one
    assert await queue.complete(lease_one)

    lease_three = await queue.claim_next("worker-1")
    assert lease_three is not None and lease_three.id == first_two


@pytest.mark.asyncio
async def test_scheduler_skips_one_invalid_tenant_and_continues(
    session_factory, make_service, tenant_payload, monkeypatch
) -> None:
    logged: list[str] = []
    monkeypatch.setattr(
        "services.backend.scheduler.service.logger.error",
        lambda message, *args, **kwargs: logged.append(message),
    )
    async with session_factory() as session:
        valid = await make_service(session).create_tenant(tenant_payload)
        invalid_payload = tenant_payload.model_copy(
            update={
                "name": "Invalid timezone tenant",
                "owner_telegram_username": "invalid_timezone_owner",
                "owner_telegram_user_id": 555000333,
            }
        )
        invalid = await make_service(session).create_tenant(invalid_payload)
        await session.execute(
            update(TenantSettings)
            .where(TenantSettings.tenant_id == invalid.id)
            .values(timezone="Mars/Olympus")
        )
        await session.commit()

    created = await TenantAnalysisScheduler(session_factory).ensure_schedules(
        datetime(2026, 8, 3, 5, 0, tzinfo=UTC)
    )
    async with session_factory() as session:
        schedules = list(await session.scalars(select(TenantAnalysisSchedule)))
        count = await session.scalar(select(func.count(TenantAnalysisSchedule.id)))
    assert created == 1 and count == 1
    assert schedules[0].tenant_id == valid.id
    assert logged == ["Skipping tenant with invalid schedule timezone"]


@pytest.mark.asyncio
async def test_legacy_timezone_is_stored_as_iana(
    session_factory, make_service, tenant_payload
) -> None:
    legacy_payload = tenant_payload.model_copy(update={"timezone": "MSK"})
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(legacy_payload)
        stored = await session.scalar(
            select(TenantSettings).where(TenantSettings.tenant_id == tenant.id)
        )
    assert stored.timezone == "Europe/Moscow"


def test_preprocessing_and_controlled_json_repair() -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    messages = [
        TelegramMessage(
            telegram_message_id=1,
            sender_id=10,
            sent_at=now - timedelta(hours=2),
            outgoing=False,
            body_text="Созвон завтра, бюджет 250 000 руб https://example.com",
            attachments_json=[],
        ),
        TelegramMessage(
            telegram_message_id=2,
            sender_id=10,
            sent_at=now - timedelta(hours=1),
            outgoing=False,
            body_text="Когда ответите?",
            attachments_json=[],
        ),
    ]
    features = local_features(messages, now)
    assert features["message_count"] == 2
    assert features["minutes_without_answer"] == 60
    assert features["amounts"] == ["250 000"]
    assert features["call_mentions"] == 1

    raw = """```json
    {"schema_version":"1.0","tenant_id":"t1","batch_id":"b1",
    "dialog_results":[],"usage":{"input_tokens":1,"output_tokens":2},}
    ```"""
    parsed, repaired = parse_analysis_response(raw)
    assert repaired is True
    assert parsed.batch_id == "b1"
