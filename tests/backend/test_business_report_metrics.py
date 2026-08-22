from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from services.backend.reporting.business_metrics import build_employee_business_performance


def message(dialog_id: str, remote_id: int, minute: int, *, outgoing: bool) -> SimpleNamespace:
    return SimpleNamespace(
        dialog_id=dialog_id,
        telegram_message_id=remote_id,
        sent_at=datetime(2026, 8, 22, 9, 0, tzinfo=UTC) + timedelta(minutes=minute),
        outgoing=outgoing,
    )


def test_business_metrics_require_explicit_matching_evidence() -> None:
    employee = SimpleNamespace(
        id="employee-1",
        display_name="Мария",
        telegram_username="maria",
    )
    result = build_employee_business_performance(
        current_messages=[
            message("dialog-1", 10, 0, outgoing=False),
            message("dialog-1", 11, 15, outgoing=True),
            message("dialog-1", 12, 20, outgoing=True),
        ],
        previous_messages=[
            message("dialog-1", 1, 0, outgoing=False),
            message("dialog-1", 2, 30, outgoing=True),
        ],
        dialog_employee_ids={"dialog-1": "employee-1"},
        employees=[employee],
        dialog_outcomes={
            "dialog-1": [
                {
                    "outcome_type": "call_scheduled",
                    "explicitly_supported": True,
                    "confidence": 0.95,
                    "source_message_ids": [11],
                    "summary": "Созвон подтверждён на 15:00.",
                },
                {
                    "outcome_type": "sale_confirmed",
                    "explicitly_supported": False,
                    "confidence": 0.99,
                    "source_message_ids": [12],
                    "summary": "Была только презентация цены.",
                    "amount": 100000,
                    "currency": "RUB",
                },
                {
                    "outcome_type": "sale_confirmed",
                    "explicitly_supported": True,
                    "confidence": 0.99,
                    "source_message_ids": [999],
                    "summary": "Ссылка на несуществующее сообщение.",
                    "amount": 100000,
                    "currency": "RUB",
                },
            ]
        },
    )

    row = result["rows"][0]
    assert row["average_response_minutes"] == 15
    assert row["response_time_change_percent"] == -50
    assert row["calls_scheduled"] == 1
    assert row["sales_confirmed"] == 0
    assert row["confirmed_sales_amounts"] == {}
