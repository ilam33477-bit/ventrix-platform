from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from packages.ops_core.problems import ProblemStatus
from services.backend.intelligence.feedback_learning import TenantFeedbackLearningService
from services.backend.intelligence.problem_lifecycle import (
    ProblemLifecycleService,
    TransitionRequest,
)
from services.backend.intelligence.remediation import RemediationVerifier
from services.backend.jobs.queue import SQLiteJobQueue
from services.backend.models import (
    Employee,
    OperationalProblem,
    ProblemTransition,
    Signal,
    TelegramConnection,
    TelegramDialog,
    TelegramMessage,
    TenantAIFeedbackProfile,
)


class FeedbackProvider:
    async def generate_json(self, **_kwargs):
        return (
            json.dumps(
                {
                    "summary": "Закрывающие фразы не требуют реакции.",
                    "patterns_to_suppress": ["Вежливое завершение диалога"],
                    "patterns_to_keep": ["Прямой вопрос клиента"],
                    "classification_recommendations": ["Проверять, есть ли открытое действие"],
                },
                ensure_ascii=False,
            ),
            {"input_tokens": 120, "output_tokens": 40},
        )


async def _problem_fixture(session_factory, make_service, tenant_payload):
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        employee = Employee(tenant_id=tenant.id, display_name="Мария")
        session.add(employee)
        await session.flush()
        connection = TelegramConnection(
            tenant_id=tenant.id,
            assigned_employee_id=employee.id,
            status="ready",
        )
        session.add(connection)
        await session.flush()
        dialog = TelegramDialog(
            tenant_id=tenant.id,
            connection_id=connection.id,
            telegram_dialog_id=88001,
            title="Клиент",
            dialog_type="personal",
            source="folder",
            selected=True,
        )
        session.add(dialog)
        await session.flush()
        message = TelegramMessage(
            tenant_id=tenant.id,
            connection_id=connection.id,
            dialog_id=dialog.id,
            telegram_message_id=1,
            sent_at=datetime.now(UTC),
            outgoing=False,
            body_text="Когда вы отправите договор?",
            attachments_json=[],
        )
        session.add(message)
        await session.flush()
        problem = OperationalProblem(
            tenant_id=tenant.id,
            connection_id=connection.id,
            dialog_id=dialog.id,
            source_message_id=message.id,
            fingerprint="fsm-problem",
            problem_type="customer_question",
            priority="high",
            confidence=0.9,
            evidence=message.body_text,
            explanation="Клиент ждёт договор.",
            recommended_action="Отправить договор клиенту.",
            occurred_at=message.sent_at,
        )
        session.add(problem)
        await session.commit()
        return tenant, employee, connection, dialog, message, problem


@pytest.mark.asyncio
async def test_persistent_problem_fsm_rejects_invalid_and_audits_valid_transitions(
    session_factory, make_service, tenant_payload
) -> None:
    tenant, employee, _connection, _dialog, _message, problem = await _problem_fixture(
        session_factory, make_service, tenant_payload
    )
    lifecycle = ProblemLifecycleService(session_factory)
    with pytest.raises(ValueError, match="illegal problem transition"):
        await lifecycle.transition(
            tenant.id,
            problem.id,
            TransitionRequest(ProblemStatus.RESOLVED, "membership", None, "Закрыть"),
        )

    requests = (
        TransitionRequest(ProblemStatus.ACKNOWLEDGED, "membership", None, "Подтверждено"),
        TransitionRequest(
            ProblemStatus.ASSIGNED,
            "membership",
            None,
            "Назначена Мария",
            responsible_employee_id=employee.id,
            deadline_at=datetime.now(UTC) + timedelta(hours=1),
        ),
        TransitionRequest(ProblemStatus.IN_PROGRESS, "membership", None, "Начали работу"),
        TransitionRequest(ProblemStatus.WAITING, "membership", None, "Ждём документ"),
        TransitionRequest(ProblemStatus.IN_PROGRESS, "membership", None, "Документ получен"),
        TransitionRequest(
            ProblemStatus.RESOLVED,
            "membership",
            None,
            "Договор отправлен",
            evidence="Сообщение с приложенным договором",
        ),
        TransitionRequest(ProblemStatus.REOPENED, "membership", None, "Клиент не получил файл"),
    )
    for request in requests:
        problem = await lifecycle.transition(tenant.id, problem.id, request)

    async with session_factory() as session:
        transition_count = await session.scalar(select(func.count(ProblemTransition.id)))
    assert problem.status == "reopened"
    assert problem.responsible_employee_id == employee.id
    assert problem.resolved_at is None
    assert transition_count == len(requests)


@pytest.mark.asyncio
async def test_quick_resolve_uses_session_owner_and_audits_full_path(
    session_factory, make_service, tenant_payload
) -> None:
    tenant, employee, _connection, _dialog, _message, problem = await _problem_fixture(
        session_factory, make_service, tenant_payload
    )

    result = await ProblemLifecycleService(session_factory).resolve_by_human(
        tenant.id,
        problem.id,
        actor_id="owner-membership",
        reason="Ситуация завершена владельцем проекта.",
    )

    async with session_factory() as session:
        transitions = list(
            await session.scalars(
                select(ProblemTransition)
                .where(ProblemTransition.problem_id == problem.id)
                .order_by(ProblemTransition.occurred_at)
            )
        )
    assert result.status == "resolved"
    assert result.responsible_employee_id == employee.id
    assert [item.to_status for item in transitions] == [
        "acknowledged",
        "assigned",
        "in_progress",
        "resolved",
    ]


@pytest.mark.asyncio
async def test_remediation_verifier_does_not_treat_acknowledgement_as_fix(
    session_factory, make_service, tenant_payload
) -> None:
    _tenant, _employee, connection, dialog, source, problem = await _problem_fixture(
        session_factory, make_service, tenant_payload
    )
    verifier = RemediationVerifier()
    acknowledgement = TelegramMessage(
        tenant_id=problem.tenant_id,
        connection_id=connection.id,
        dialog_id=dialog.id,
        telegram_message_id=source.telegram_message_id + 1,
        sent_at=datetime.now(UTC),
        outgoing=True,
        body_text="Ок",
        attachments_json=[],
    )
    weak = await verifier.verify(problem, [acknowledgement])
    assert weak.outcome == "not_fixed"
    assert weak.confidence >= 0.9

    completed = TelegramMessage(
        tenant_id=problem.tenant_id,
        connection_id=connection.id,
        dialog_id=dialog.id,
        telegram_message_id=source.telegram_message_id + 2,
        sent_at=datetime.now(UTC),
        outgoing=True,
        body_text="Готово, отправил договор клиенту.",
        attachments_json=[{"kind": "document"}],
    )
    strong = await verifier.verify(problem, [acknowledgement, completed])
    assert strong.outcome == "fixed"
    assert strong.confidence >= verifier.auto_close_confidence


@pytest.mark.asyncio
async def test_assigned_problem_can_be_marked_false_positive(
    session_factory, make_service, tenant_payload
) -> None:
    tenant, employee, _connection, _dialog, _message, problem = await _problem_fixture(
        session_factory, make_service, tenant_payload
    )
    lifecycle = ProblemLifecycleService(session_factory)
    await lifecycle.transition(
        tenant.id,
        problem.id,
        TransitionRequest(ProblemStatus.ACKNOWLEDGED, "membership", None, "Проверено"),
    )
    await lifecycle.transition(
        tenant.id,
        problem.id,
        TransitionRequest(
            ProblemStatus.ASSIGNED,
            "membership",
            None,
            "Назначено",
            responsible_employee_id=employee.id,
        ),
    )
    result = await lifecycle.transition(
        tenant.id,
        problem.id,
        TransitionRequest(
            ProblemStatus.FALSE_POSITIVE,
            "membership",
            None,
            "Диалог уже завершён",
        ),
    )
    assert result.status == "false_positive"


@pytest.mark.asyncio
async def test_acknowledged_problem_can_be_marked_false_positive(
    session_factory, make_service, tenant_payload
) -> None:
    tenant, _employee, _connection, _dialog, _message, problem = await _problem_fixture(
        session_factory, make_service, tenant_payload
    )
    lifecycle = ProblemLifecycleService(session_factory)
    await lifecycle.transition(
        tenant.id,
        problem.id,
        TransitionRequest(ProblemStatus.ACKNOWLEDGED, "membership", None, "Проверено"),
    )

    result = await lifecycle.transition(
        tenant.id,
        problem.id,
        TransitionRequest(
            ProblemStatus.FALSE_POSITIVE,
            "membership",
            None,
            "Диалог завершён без упущенной возможности",
        ),
    )

    assert result.status == "false_positive"


@pytest.mark.asyncio
async def test_false_positive_can_be_restored_with_signal(
    session_factory, make_service, tenant_payload
) -> None:
    tenant, _employee, _connection, _dialog, _message, problem = await _problem_fixture(
        session_factory, make_service, tenant_payload
    )
    async with session_factory() as session:
        stored = await session.get(OperationalProblem, problem.id)
        signal = Signal(
            tenant_id=stored.tenant_id,
            telegram_connection_id=stored.connection_id,
            dialog_id=stored.dialog_id,
            source_message_id=stored.source_message_id,
            fingerprint="restorable-signal",
            signal_type="customer_question",
            local_score=80,
            criticality=80,
            reason="Клиент ждёт ответа",
            detected_at=datetime.now(UTC),
        )
        session.add(signal)
        await session.flush()
        stored.signal_id = signal.id
        await session.commit()
        problem.signal_id = signal.id
    lifecycle = ProblemLifecycleService(session_factory)
    await lifecycle.transition(
        tenant.id,
        problem.id,
        TransitionRequest(ProblemStatus.ACKNOWLEDGED, "membership", None, "Проверено"),
    )
    await lifecycle.transition(
        tenant.id,
        problem.id,
        TransitionRequest(
            ProblemStatus.FALSE_POSITIVE,
            "membership",
            None,
            "Ошибочно исключили",
        ),
    )

    restored = await lifecycle.transition(
        tenant.id,
        problem.id,
        TransitionRequest(
            ProblemStatus.REOPENED,
            "membership",
            None,
            "Вернуть как проблему",
        ),
    )

    assert restored.status == "reopened"
    async with session_factory() as session:
        signal = await session.get(Signal, problem.signal_id)
        assert signal.status == "problem_created"
        assert signal.metadata_json["tenant_feedback"]["verdict"] == "restored"


@pytest.mark.asyncio
async def test_false_positive_feedback_is_synthesized_into_tenant_guidance(
    session_factory, make_service, tenant_payload
) -> None:
    tenant, _employee, _connection, _dialog, _message, problem = await _problem_fixture(
        session_factory, make_service, tenant_payload
    )
    lifecycle = ProblemLifecycleService(session_factory)
    await lifecycle.transition(
        tenant.id,
        problem.id,
        TransitionRequest(ProblemStatus.ACKNOWLEDGED, "membership", None, "Проверено"),
    )
    await lifecycle.transition(
        tenant.id,
        problem.id,
        TransitionRequest(
            ProblemStatus.FALSE_POSITIVE,
            "membership",
            None,
            "Диалог уже был завершён",
        ),
    )
    queue = SQLiteJobQueue(session_factory)
    await queue.enqueue(
        "feedback.synthesize",
        {},
        tenant_id=tenant.id,
        category="ai",
        cost_class="standard",
    )
    lease = await queue.claim_next("feedback-test", allowed_categories=frozenset({"ai"}))
    assert lease is not None

    result = await TenantFeedbackLearningService(
        session_factory,
        FeedbackProvider(),
        model="deepseek-chat",
    ).synthesize(lease)

    assert result["status"] == "learned"
    async with session_factory() as session:
        profile = await session.scalar(
            select(TenantAIFeedbackProfile).where(TenantAIFeedbackProfile.tenant_id == tenant.id)
        )
        assert profile.version == 1
        assert profile.source_count == 1
        assert profile.guidance_json["patterns_to_suppress"]
