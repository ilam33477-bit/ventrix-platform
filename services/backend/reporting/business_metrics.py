from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _response_latencies(messages: list[Any]) -> list[float]:
    waiting_since: datetime | None = None
    result: list[float] = []
    for message in sorted(
        messages, key=lambda item: (_aware(item.sent_at), item.telegram_message_id)
    ):
        if message.outgoing:
            if waiting_since is not None:
                result.append(
                    max(0.0, (_aware(message.sent_at) - waiting_since).total_seconds() / 60)
                )
                waiting_since = None
        elif waiting_since is None:
            waiting_since = _aware(message.sent_at)
    return result


def build_employee_business_performance(
    *,
    current_messages: Iterable[Any],
    previous_messages: Iterable[Any],
    dialog_employee_ids: dict[str, str | None],
    employees: Iterable[Any],
    dialog_outcomes: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Build evidence-backed employee metrics without inventing business outcomes."""

    employee_by_id = {item.id: item for item in employees}
    current_by_employee: dict[str, list[Any]] = defaultdict(list)
    previous_by_employee: dict[str, list[Any]] = defaultdict(list)
    current_message_ids: dict[str, set[str]] = defaultdict(set)
    for message in current_messages:
        employee_id = dialog_employee_ids.get(message.dialog_id)
        if employee_id:
            current_by_employee[employee_id].append(message)
            current_message_ids[message.dialog_id].add(str(message.telegram_message_id))
    for message in previous_messages:
        employee_id = dialog_employee_ids.get(message.dialog_id)
        if employee_id:
            previous_by_employee[employee_id].append(message)

    outcomes_by_employee: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_outcomes: set[tuple[str, str, tuple[str, ...]]] = set()
    for dialog_id, outcomes in dialog_outcomes.items():
        employee_id = dialog_employee_ids.get(dialog_id)
        if not employee_id:
            continue
        valid_ids = current_message_ids.get(dialog_id, set())
        for outcome in outcomes:
            evidence_ids = tuple(
                sorted(str(item) for item in outcome.get("source_message_ids", []))
            )
            key = (dialog_id, str(outcome.get("outcome_type")), evidence_ids)
            if (
                key in seen_outcomes
                or not outcome.get("explicitly_supported")
                or float(outcome.get("confidence", 0)) < 0.8
                or not evidence_ids
                or not set(evidence_ids) <= valid_ids
            ):
                continue
            seen_outcomes.add(key)
            outcomes_by_employee[employee_id].append(outcome)

    rows: list[dict[str, Any]] = []
    for employee_id, employee in employee_by_id.items():
        current = current_by_employee.get(employee_id, [])
        previous = previous_by_employee.get(employee_id, [])
        current_latencies = _response_latencies(current)
        previous_latencies = _response_latencies(previous)
        outgoing = [item for item in current if item.outgoing]
        active_dates = {_aware(item.sent_at).date() for item in outgoing}
        activity_windows = []
        for active_date in active_dates:
            daily = [
                _aware(item.sent_at)
                for item in outgoing
                if _aware(item.sent_at).date() == active_date
            ]
            if daily:
                activity_windows.append((max(daily) - min(daily)).total_seconds() / 60)
        outcomes = outcomes_by_employee.get(employee_id, [])
        calls = [item for item in outcomes if item.get("outcome_type") == "call_scheduled"]
        sales = [item for item in outcomes if item.get("outcome_type") == "sale_confirmed"]
        amounts: dict[str, float] = defaultdict(float)
        for outcome in sales:
            amount = outcome.get("amount")
            currency = str(outcome.get("currency") or "").upper()
            if amount is not None and currency:
                amounts[currency] += float(amount)
        avg_response = (
            sum(current_latencies) / len(current_latencies) if current_latencies else None
        )
        previous_avg = (
            sum(previous_latencies) / len(previous_latencies) if previous_latencies else None
        )
        response_change = None
        if avg_response is not None and previous_avg is not None and previous_avg != 0:
            response_change = round((avg_response - previous_avg) / previous_avg * 100, 1)
        rows.append(
            {
                "employee_id": employee_id,
                "name": employee.display_name,
                "username": employee.telegram_username,
                "messages_sent": len(outgoing),
                "active_dialogs": len({item.dialog_id for item in current}),
                "average_response_minutes": round(avg_response, 1)
                if avg_response is not None
                else None,
                "previous_average_response_minutes": round(previous_avg, 1)
                if previous_avg is not None
                else None,
                "response_time_change_percent": response_change,
                "active_days": len(active_dates),
                "average_daily_activity_window_minutes": (
                    round(sum(activity_windows) / len(activity_windows), 1)
                    if activity_windows
                    else None
                ),
                "calls_scheduled": len(calls),
                "sales_confirmed": len(sales),
                "confirmed_sales_amounts": dict(amounts),
                "business_outcomes": outcomes[:12],
            }
        )
    return {
        "rows": rows,
        "methodology": (
            "Созвоны и продажи учитываются только при явном подтверждении в переписке. "
            "Окно активности не является учётом рабочего времени."
        ),
    }
