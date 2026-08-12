from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Employee, Permission, TenantMembership

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "manager": frozenset(
        {
            "problems.read_all",
            "problems.manage",
            "employees.read",
            "employees.manage",
            "groups.manage",
            "reports.read",
            "commitments.read_all",
            "settings.read",
            "settings.manage",
        }
    ),
    "employee": frozenset(
        {
            "problems.read_own",
            "problems.manage_own",
            "commitments.read_own",
            "commitments.manage_own",
            "reports.read_own",
        }
    ),
    "observer": frozenset({"reports.read"}),
}


async def sync_employee_membership(
    session: AsyncSession, employee: Employee, *, previous_telegram_user_id: int | None = None
) -> TenantMembership | None:
    if previous_telegram_user_id and previous_telegram_user_id != employee.telegram_user_id:
        previous = await session.scalar(
            select(TenantMembership).where(
                TenantMembership.tenant_id == employee.tenant_id,
                TenantMembership.employee_id == employee.id,
                TenantMembership.telegram_user_id == previous_telegram_user_id,
            )
        )
        if previous is not None:
            previous.status = "inactive"
    if employee.telegram_user_id is None:
        linked = await session.scalar(
            select(TenantMembership).where(
                TenantMembership.tenant_id == employee.tenant_id,
                TenantMembership.employee_id == employee.id,
            )
        )
        if linked is not None:
            linked.status = "inactive"
        return None
    membership = await session.scalar(
        select(TenantMembership).where(
            TenantMembership.tenant_id == employee.tenant_id,
            TenantMembership.telegram_user_id == employee.telegram_user_id,
        )
    )
    if membership is not None and membership.employee_id not in {None, employee.id}:
        raise ValueError("Telegram user already belongs to another employee in tenant")
    if membership is None:
        membership = TenantMembership(
            tenant_id=employee.tenant_id,
            telegram_user_id=employee.telegram_user_id,
            employee_id=employee.id,
            role=employee.role,
            status=employee.status,
        )
        session.add(membership)
        await session.flush()
    else:
        membership.employee_id = employee.id
        membership.role = employee.role
        membership.status = employee.status
    await session.execute(delete(Permission).where(Permission.membership_id == membership.id))
    session.add_all(
        Permission(tenant_id=employee.tenant_id, membership_id=membership.id, permission=permission)
        for permission in ROLE_PERMISSIONS[employee.role]
    )
    return membership


async def claim_employee_by_username(
    session: AsyncSession, *, tenant_id: str, telegram_user_id: int, telegram_username: str | None
) -> TenantMembership | None:
    """Bind one pre-approved username once; subsequent auth relies on Telegram user id."""
    username = (telegram_username or "").lstrip("@").strip().lower()
    if not username:
        return None
    candidates = list(
        await session.scalars(
            select(Employee).where(
                Employee.tenant_id == tenant_id,
                Employee.status == "active",
                Employee.telegram_user_id.is_(None),
                func.lower(Employee.telegram_username) == username,
            )
        )
    )
    if len(candidates) != 1:
        return None
    employee = candidates[0]
    employee.telegram_user_id = telegram_user_id
    return await sync_employee_membership(session, employee)
