from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import func, or_, select

from services.backend.database import get_session_factory
from services.backend.intelligence.problem_lifecycle import (
    TERMINAL_PROBLEM_STATUSES,
    ProblemLifecycleService,
)
from services.backend.models import Employee, OperationalProblem, TelegramConnection, Tenant
from services.backend.services.employee_access import sync_employee_membership


async def run(tenant_name: str, *, apply: bool) -> int:
    session_factory = get_session_factory()
    async with session_factory() as session:
        tenants = list(
            await session.scalars(
                select(Tenant).where(
                    func.lower(Tenant.name) == tenant_name.lower(),
                    Tenant.status == "active",
                )
            )
        )
        if len(tenants) != 1:
            raise RuntimeError(
                f"Expected one active tenant named {tenant_name!r}, found {len(tenants)}"
            )
        tenant = tenants[0]
        if apply:
            unassigned_connections = list(
                await session.scalars(
                    select(TelegramConnection).where(
                        TelegramConnection.tenant_id == tenant.id,
                        TelegramConnection.deleted_at.is_(None),
                        TelegramConnection.assigned_employee_id.is_(None),
                        TelegramConnection.telegram_user_id.is_not(None),
                    )
                )
            )
            for connection in unassigned_connections:
                identity_filters = [
                    Employee.telegram_user_id == connection.telegram_user_id,
                ]
                if connection.username:
                    identity_filters.append(
                        func.lower(Employee.telegram_username)
                        == connection.username.lstrip("@").lower()
                    )
                employee = await session.scalar(
                    select(Employee).where(
                        Employee.tenant_id == tenant.id,
                        or_(*identity_filters),
                    )
                )
                if employee is None:
                    employee = Employee(
                        tenant_id=tenant.id,
                        display_name=connection.display_name
                        or connection.username
                        or "Сотрудник",
                        telegram_user_id=connection.telegram_user_id,
                        telegram_username=connection.username,
                        role="employee",
                        status="active",
                        notifications_enabled=True,
                        criticality_threshold=85,
                    )
                    session.add(employee)
                    await session.flush()
                connection.assigned_employee_id = employee.id
                await sync_employee_membership(session, employee)
            await session.commit()
        problems = list(
            await session.scalars(
                select(OperationalProblem).where(
                    OperationalProblem.tenant_id == tenant.id,
                    OperationalProblem.status.not_in(TERMINAL_PROBLEM_STATUSES),
                )
            )
        )
    print(f"tenant={tenant.name} active_problems={len(problems)} apply={apply}")
    if not apply:
        return 0
    lifecycle = ProblemLifecycleService(session_factory)
    resolved = 0
    failed: list[tuple[str, str]] = []
    for problem in problems:
        try:
            await lifecycle.resolve_by_human(
                tenant.id,
                problem.id,
                actor_id=f"tenant-maintenance:{tenant.id}",
                reason="Тестовый проект очищен перед новым циклом проверки.",
            )
            resolved += 1
        except (LookupError, ValueError) as exc:
            failed.append((problem.id, str(exc)))
    print(f"resolved={resolved} failed={len(failed)}")
    for problem_id, error in failed:
        print(f"failed_problem={problem_id} error={error}")
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tenant_name")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.tenant_name, apply=args.apply)))


if __name__ == "__main__":
    main()
