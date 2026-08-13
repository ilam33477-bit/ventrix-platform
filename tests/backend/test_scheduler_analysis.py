from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from itertools import pairwise

import pytest
from sqlalchemy import func, select, update

from services.backend.analysis.budget import ConservativeTokenEstimator, ModelInputBudget
from services.backend.analysis.preprocessing import (
    AnalysisBatchBuilder,
    PreparedDialog,
    compact_messages,
    local_features,
    pack_dialog_payloads,
)
from services.backend.analysis.schema import parse_analysis_response
from services.backend.analysis.service import AnalysisPipelineService, canonical_problem_type
from services.backend.jobs.queue import SQLiteJobQueue
from services.backend.models import (
    AnalysisBatch,
    AnalysisRun,
    BackgroundJob,
    EncryptedSecret,
    Report,
    TelegramConnection,
    TelegramDialog,
    TelegramMessage,
    Tenant,
    TenantAnalysisSchedule,
    TenantSettings,
)
from services.backend.scheduler.service import TenantAnalysisScheduler, next_analysis_time
from services.backend.services.encryption import EncryptionService
from services.backend.timezones import normalize_timezone


@pytest.mark.parametrize("legacy", ["Moscow", "MSK", "Москва", " moscow "])
def test_legacy_timezones_are_normalized(legacy: str) -> None:
    assert normalize_timezone(legacy) == "Europe/Moscow"


def test_invalid_timezone_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid IANA timezone"):
        normalize_timezone("Mars/Olympus")


@pytest.mark.parametrize(
    ("raw_type", "expected"),
    [
        ("unanswered_customer", "client_without_answer"),
        ("negative_feedback", "customer_complaint"),
        ("broken-promise", "overdue_commitment"),
        ("invoice payment delay", "payment_risk"),
        ("unexpected free form label", "operational_risk"),
    ],
)
def test_problem_types_are_canonicalized(raw_type: str, expected: str) -> None:
    assert canonical_problem_type(raw_type) == expected


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


@pytest.mark.asyncio
async def test_existing_schedule_tracks_subscription_extension(
    session_factory, make_service, tenant_payload
) -> None:
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        tenant.subscription_expires_at = now.date()
        await session.commit()
    scheduler = TenantAnalysisScheduler(session_factory)
    await scheduler.ensure_schedules(now)
    async with session_factory() as session:
        tenant = await session.get(Tenant, tenant.id)
        tenant.subscription_expires_at = now.date() + timedelta(days=30)
        await session.commit()
    await scheduler.ensure_schedules(now + timedelta(minutes=1))
    async with session_factory() as session:
        schedule = await session.scalar(
            select(TenantAnalysisSchedule).where(TenantAnalysisSchedule.tenant_id == tenant.id)
        )
    assert schedule.access_status == "active"
    assert schedule.access_expires_at.date() == now.date() + timedelta(days=30)


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


def test_compaction_keeps_fresh_tail_and_relevant_historical_evidence() -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    messages = [
        TelegramMessage(
            telegram_message_id=1,
            sender_id=10,
            sent_at=now - timedelta(days=3),
            outgoing=True,
            body_text="Обещаю отправить подписанный договор клиенту.",
            attachments_json=[],
        ),
        *[
            TelegramMessage(
                telegram_message_id=index,
                sender_id=10,
                sent_at=now - timedelta(hours=20 - index),
                outgoing=False,
                body_text=f"Обычное старое сообщение {index}",
                attachments_json=[],
            )
            for index in range(2, 8)
        ],
        TelegramMessage(
            telegram_message_id=8,
            sender_id=20,
            sent_at=now - timedelta(minutes=2),
            outgoing=False,
            body_text="Последнее важное сообщение клиента",
            attachments_json=[],
        ),
        TelegramMessage(
            telegram_message_id=9,
            sender_id=10,
            sent_at=now - timedelta(minutes=1),
            outgoing=True,
            body_text="Самый свежий ответ сотрудника",
            attachments_json=[],
        ),
    ]
    compact = compact_messages(messages, max_chars=140, latest_count=2)
    ids = [item["id"] for item in compact]
    assert ids[-2:] == [8, 9]
    assert 1 in ids


def test_model_budget_reserves_prompt_output_and_safety() -> None:
    estimator = ConservativeTokenEstimator()
    budget = ModelInputBudget(
        context_window=20_000,
        max_output_tokens=2_000,
        safety_margin_tokens=3_000,
    )
    prompt_tokens = estimator.text("важная системная инструкция")
    assert budget.usable_input_tokens(prompt_tokens) == 15_000 - prompt_tokens
    assert estimator.text("данные") > 1


def test_greedy_packing_keeps_dialog_boundaries_and_never_exceeds_budget() -> None:
    estimator = ConservativeTokenEstimator()
    budget = ModelInputBudget(
        context_window=12_000,
        max_output_tokens=1_000,
        safety_margin_tokens=1_000,
        max_dialogs_per_request=12,
    )
    common = {"schema_version": "1.0", "tenant": {"name": "test"}}
    prepared = []
    for dialog_id, count in (("dialog-a", 10), ("dialog-b", 12)):
        dialog = TelegramDialog(id=dialog_id, title=dialog_id, dialog_type="personal")
        payload = {
            "id": dialog_id,
            "messages": [
                {"id": index, "sent_at": f"2026-08-09T10:{index:02d}:00+00:00", "text": "ok"}
                for index in range(count)
            ],
            "state_version": 1,
        }
        prepared.append(
            PreparedDialog(
                dialog=dialog,
                payload=payload,
                features={},
                route_name="fast",
                model="deepseek-test",
                estimated_tokens=estimator.payload(payload),
            )
        )
    packs = pack_dialog_payloads(
        prepared,
        common_payload=common,
        budget=budget,
        estimator=estimator,
        system_prompt="analyze dialogs independently",
    )
    assert len(packs) == 1
    assert [item.dialog.id for item in packs[0]] == ["dialog-a", "dialog-b"]
    payload = {**common, "dialogs": [item.payload for item in packs[0]]}
    usable = budget.usable_input_tokens(estimator.text("analyze dialogs independently"))
    assert estimator.payload(payload) <= usable


def test_long_dialog_is_split_chronologically_with_bounded_overlap(session_factory) -> None:
    budget = ModelInputBudget(overlap_tokens=120)
    builder = AnalysisBatchBuilder(session_factory, model_budget=budget)
    base = {"id": "dialog-a", "state_version": 7, "historical_summary": "old state"}
    messages = [
        {"id": index, "sent_at": f"2026-08-09T10:{index:02d}:00+00:00", "text": "я" * 100}
        for index in range(30)
    ]
    segments = builder._split_dialog_messages(base, messages, token_budget=900)
    assert len(segments) > 1
    assert segments[0]["messages"][0]["id"] == 0
    assert segments[-1]["messages"][-1]["id"] == 29
    assert all(builder.estimator.payload(segment) <= 900 for segment in segments)
    for previous, current in pairwise(segments):
        assert previous["messages"][-1]["id"] <= current["messages"][0]["id"]


@pytest.mark.asyncio
async def test_multi_account_analysis_uses_one_tenant_aggregation_barrier(
    session_factory, make_service, tenant_payload, encryption_key
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        for index in (1, 2):
            secret = EncryptedSecret(
                tenant_id=tenant.id,
                kind="telegram_session",
                ciphertext=b"encrypted-session",
                fingerprint=f"session-{index}",
            )
            session.add(secret)
            await session.flush()
            connection = TelegramConnection(
                tenant_id=tenant.id,
                session_secret_id=secret.id,
                telegram_user_id=9000 + index,
                status="ready",
            )
            session.add(connection)
            await session.flush()
            dialog = TelegramDialog(
                tenant_id=tenant.id,
                connection_id=connection.id,
                telegram_dialog_id=7000 + index,
                title=f"Клиент {index}",
                dialog_type="personal",
                source="folder",
                selected=True,
            )
            session.add(dialog)
            await session.flush()
            session.add(
                TelegramMessage(
                    tenant_id=tenant.id,
                    connection_id=connection.id,
                    dialog_id=dialog.id,
                    telegram_message_id=1,
                    sent_at=datetime.now(UTC),
                    outgoing=False,
                    body_text="Когда будет договор?",
                    attachments_json=[],
                )
            )
        await session.commit()

    queue = SQLiteJobQueue(session_factory)
    pipeline = AnalysisPipelineService(
        session_factory,
        EncryptionService(encryption_key),
        queue=queue,
    )
    root_id = await queue.enqueue(
        "analysis.pipeline",
        {"trigger": "scheduled", "history_window_days": 7},
        tenant_id=tenant.id,
        category="analysis",
        correlation_id="tenant-cycle-1",
    )
    root = await queue.claim_next("analysis-root", allowed_categories=frozenset({"analysis"}))
    assert root is not None and root.id == root_id
    await pipeline.pipeline(root)
    await queue.complete(root)

    while True:
        child = await queue.claim_next(
            "analysis-account", allowed_categories=frozenset({"analysis"})
        )
        if child is None:
            break
        assert child.job_type == "analysis.connection"
        await pipeline.pipeline(child)
        await queue.complete(child)

    async with session_factory() as session:
        runs = list(await session.scalars(select(AnalysisRun)))
        aggregate_jobs = list(
            await session.scalars(
                select(BackgroundJob).where(BackgroundJob.job_type == "analysis.aggregate")
            )
        )
        per_account_reports = await session.scalar(
            select(func.count(BackgroundJob.id)).where(
                BackgroundJob.job_type == "report_generation"
            )
        )
    assert len(runs) == 3
    assert sum(item.telegram_account_id is None for item in runs) == 1
    assert len(aggregate_jobs) == 1
    assert per_account_reports == 0


@pytest.mark.asyncio
async def test_builder_packs_short_dialogs_in_one_tenant_safe_request(
    session_factory, make_service, tenant_payload
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        connection = TelegramConnection(
            tenant_id=tenant.id,
            telegram_user_id=9010,
            status="ready",
        )
        session.add(connection)
        await session.flush()
        run = AnalysisRun(
            tenant_id=tenant.id,
            telegram_account_id=connection.id,
            trigger="manual",
            status="running",
            correlation_id="batch-packing-test",
        )
        session.add(run)
        await session.flush()
        dialog_ids: list[str] = []
        for dialog_index in range(2):
            dialog = TelegramDialog(
                tenant_id=tenant.id,
                connection_id=connection.id,
                telegram_dialog_id=7100 + dialog_index,
                title=f"Диалог {dialog_index}",
                dialog_type="personal",
                source="personal",
                selected=True,
            )
            session.add(dialog)
            await session.flush()
            dialog_ids.append(dialog.id)
            for message_index in range(5):
                session.add(
                    TelegramMessage(
                        tenant_id=tenant.id,
                        connection_id=connection.id,
                        dialog_id=dialog.id,
                        telegram_message_id=message_index + 1,
                        sent_at=datetime.now(UTC) + timedelta(seconds=message_index),
                        outgoing=bool(message_index % 2),
                        body_text=f"Сообщение {message_index}",
                        attachments_json=[],
                    )
                )
        await session.commit()
        run_id = run.id
        scoped_run = AnalysisRun(
            tenant_id=tenant.id,
            telegram_account_id=connection.id,
            trigger="signal_escalation",
            status="running",
            correlation_id="scoped-batch-test",
        )
        session.add(scoped_run)
        await session.commit()
        scoped_run_id = scoped_run.id

    batch_ids = await AnalysisBatchBuilder(
        session_factory,
        model_budget=ModelInputBudget(
            context_window=16_000,
            max_output_tokens=2_000,
            safety_margin_tokens=2_000,
        ),
        system_prompt="independent dialogs",
    ).build(run_id)
    async with session_factory() as session:
        batches = list(
            await session.scalars(select(AnalysisBatch).where(AnalysisBatch.id.in_(batch_ids)))
        )
    assert len(batches) == 1
    assert batches[0].tenant_id == tenant.id
    assert batches[0].dialogs_count == 2
    assert len(batches[0].payload_json["dialogs"]) == 2
    assert batches[0].estimated_input_tokens <= batches[0].input_budget

    scoped_batch_ids = await AnalysisBatchBuilder(
        session_factory,
        system_prompt="independent dialogs",
    ).build(scoped_run_id, dialog_ids={dialog_ids[0]})
    async with session_factory() as session:
        scoped_batch = await session.get(AnalysisBatch, scoped_batch_ids[0])
    assert scoped_batch.dialogs_count == 1
    assert [item["id"] for item in scoped_batch.payload_json["dialogs"]] == [dialog_ids[0]]


@pytest.mark.asyncio
async def test_signal_deep_analysis_is_dialog_scoped_and_does_not_create_report_job(
    session_factory, make_service, tenant_payload, encryption_key
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        secret = EncryptedSecret(
            tenant_id=tenant.id,
            kind="telegram_session",
            ciphertext=b"encrypted-session",
            fingerprint="deep-analysis-session",
        )
        session.add(secret)
        await session.flush()
        connection = TelegramConnection(
            tenant_id=tenant.id,
            session_secret_id=secret.id,
            telegram_user_id=9020,
            status="ready",
        )
        session.add(connection)
        await session.flush()
        dialogs: list[TelegramDialog] = []
        for index in range(2):
            dialog = TelegramDialog(
                tenant_id=tenant.id,
                connection_id=connection.id,
                telegram_dialog_id=7200 + index,
                title=f"Deep dialog {index}",
                dialog_type="personal",
                source="personal",
                selected=True,
            )
            session.add(dialog)
            await session.flush()
            dialogs.append(dialog)
            session.add(
                TelegramMessage(
                    tenant_id=tenant.id,
                    connection_id=connection.id,
                    dialog_id=dialog.id,
                    telegram_message_id=1,
                    sent_at=datetime.now(UTC),
                    outgoing=False,
                    body_text="Клиент ждёт подтверждение срока.",
                    attachments_json=[],
                )
            )
        await session.commit()

    queue = SQLiteJobQueue(session_factory)
    job_id = await queue.enqueue(
        "analysis.deep",
        {"trigger": "signal_escalation", "history_window_days": 7},
        tenant_id=tenant.id,
        telegram_account_id=connection.id,
        dialog_id=dialogs[0].id,
        category="ai_heavy",
        is_heavy=True,
    )
    lease = await queue.claim_next(
        "deep-analysis-worker",
        allowed_categories=frozenset({"ai_heavy"}),
    )
    assert lease is not None and lease.id == job_id
    result = await AnalysisPipelineService(
        session_factory,
        EncryptionService(encryption_key),
        queue=queue,
    ).pipeline(lease)

    async with session_factory() as session:
        batch = await session.scalar(
            select(AnalysisBatch).where(AnalysisBatch.run_id == result["analysis_run_id"])
        )
        report_jobs = int(
            await session.scalar(
                select(func.count(BackgroundJob.id)).where(
                    BackgroundJob.job_type == "report_generation"
                )
            )
            or 0
        )
    assert result["report_suppressed"] is True
    assert batch.dialogs_count == 1
    assert [item["id"] for item in batch.payload_json["dialogs"]] == [dialogs[0].id]
    assert report_jobs == 0


@pytest.mark.asyncio
async def test_legacy_signal_escalation_report_job_is_suppressed(
    session_factory, make_service, tenant_payload, encryption_key
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        run = AnalysisRun(
            tenant_id=tenant.id,
            trigger="signal_escalation",
            status="running",
            correlation_id="legacy-signal-report",
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    queue = SQLiteJobQueue(session_factory)
    job_id = await queue.enqueue(
        "report_generation",
        {"analysis_run_id": run_id},
        tenant_id=tenant.id,
        category="report",
    )
    lease = await queue.claim_next(
        "report-worker",
        allowed_categories=frozenset({"report"}),
    )
    assert lease is not None and lease.id == job_id
    result = await AnalysisPipelineService(
        session_factory,
        EncryptionService(encryption_key),
        queue=queue,
    ).generate_report(lease)

    async with session_factory() as session:
        stored_run = await session.get(AnalysisRun, run_id)
        reports = int(await session.scalar(select(func.count(Report.id))) or 0)
    assert result["report_suppressed"] is True
    assert stored_run.status == "completed"
    assert reports == 0
