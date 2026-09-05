from __future__ import annotations

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Employee, Permission, TelegramConnection, TenantMembership


class ConnectionEmployeeConflict(ValueError):
    """The verified Telegram account cannot be bound to the selected employee."""


async def bind_connection_employee(
    session: AsyncSession,
    connection: TelegramConnection,
    *,
    requested_employee_id: str | None = None,
) -> Employee:
    """Bind a verified account in the same transaction as the session credentials.

    Usernames are display data here, never identity. Reconnecting an account does
    not reactivate revoked access or transfer it to another employee.
    """
    user_id = connection.telegram_user_id
    if not user_id or user_id <= 0:
        raise ConnectionEmployeeConflict("Telegram did not return a verified user id")
    employee = None
    for employee_id in {connection.assigned_employee_id, requested_employee_id} - {None}:
        candidate = await session.scalar(
            select(Employee).where(
                Employee.id == employee_id, Employee.tenant_id == connection.tenant_id
            )
        )
        if (
            candidate is None
            or candidate.telegram_user_id not in {None, user_id}
            or (employee is not None and candidate.id != employee.id)
        ):
            raise ConnectionEmployeeConflict("Telegram account belongs to another employee")
        employee = candidate
    identified = await session.scalar(
        select(Employee).where(
            Employee.tenant_id == connection.tenant_id, Employee.telegram_user_id == user_id
        )
    )
    if employee is not None and identified is not None and employee.id != identified.id:
        raise ConnectionEmployeeConflict("Telegram account already has an employee")
    employee = employee or identified
    membership = await session.scalar(
        select(TenantMembership).where(
            TenantMembership.tenant_id == connection.tenant_id,
            TenantMembership.telegram_user_id == user_id,
        )
    )
    previous_status = membership.status if membership is not None else "active"
    revoked = previous_status != "active"
    if employee is None:
        employee = Employee(
            tenant_id=connection.tenant_id,
            display_name=connection.display_name or connection.username or "Сотрудник",
            telegram_user_id=user_id,
            role=(
                membership.role
                if membership and membership.role in ROLE_PERMISSIONS
                else "employee"
            ),
            status=previous_status,
            notifications_enabled=True,
            criticality_threshold=85,
        )
        session.add(employee)
        await session.flush()
    employee.telegram_user_id = user_id
    employee.telegram_username = connection.username
    connection.assigned_employee_id = employee.id
    try:
        linked = await sync_employee_membership(session, employee)
    except ValueError as exc:
        raise ConnectionEmployeeConflict("Telegram membership belongs to another employee") from exc
    if revoked and linked is not None:
        linked.status = previous_status
    return employee


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


async def employee_membership_is_valid(session: AsyncSession, membership: TenantMembership) -> bool:
    if membership.role != "employee":
        return True
    if not membership.employee_id:
        return False
    return bool(
        await session.scalar(
            select(Employee.id).where(
                Employee.id == membership.employee_id,
                Employee.tenant_id == membership.tenant_id,
                Employee.telegram_user_id == membership.telegram_user_id,
                Employee.status == "active",
            )
        )
    )


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
        if previous is not None and previous.role != "owner":
            previous.status = "inactive"
    if employee.telegram_user_id is None:
        linked = await session.scalar(
            select(TenantMembership).where(
                TenantMembership.tenant_id == employee.tenant_id,
                TenantMembership.employee_id == employee.id,
            )
        )
        if linked is not None and linked.role != "owner":
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
    if membership is not None and membership.role == "owner":
        # Connecting the owner's working account must never downgrade project ownership.
        return membership
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
    permissions = set(ROLE_PERMISSIONS[employee.role])
    if employee.reports_access_all:
        permissions.add("reports.read")
    session.add_all(
        Permission(tenant_id=employee.tenant_id, membership_id=membership.id, permission=permission)
        for permission in permissions
    )
    return membership


async def claim_employee_by_username(
    session: AsyncSession, *, tenant_id: str, telegram_user_id: int, telegram_username: str | None
) -> TenantMembership | None:
    """Bind one pre-approved username once; subsequent auth relies on Telegram user id."""
    username = (telegram_username or "").lstrip("@").strip().lower()
    if not username:
        return None
    existing = await session.scalar(
        select(TenantMembership.id).where(
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.telegram_user_id == telegram_user_id,
        )
    )
    if existing:
        # An inactive membership is a revocation, not a new username invitation.
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
    claimed = await session.execute(
        update(Employee)
        .where(
            Employee.id == employee.id,
            Employee.tenant_id == tenant_id,
            Employee.telegram_user_id.is_(None),
            Employee.status == "active",
        )
        .values(telegram_user_id=telegram_user_id)
    )
    if claimed.rowcount != 1:
        return None
    await session.refresh(employee)
    return await sync_employee_membership(session, employee)
