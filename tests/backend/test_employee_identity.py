from sqlalchemy import select

from services.backend.models import Employee, Permission, TenantMembership
from services.backend.services.employee_access import (
    claim_employee_by_username,
    sync_employee_membership,
)


async def test_connecting_owner_account_preserves_owner_role_and_permissions(
    session_factory, make_service, tenant_payload
):
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        owner = await session.scalar(
            select(TenantMembership).where(
                TenantMembership.tenant_id == tenant.id, TenantMembership.role == "owner"
            )
        )
        permission_ids = list(
            await session.scalars(select(Permission.id).where(Permission.membership_id == owner.id))
        )
        employee = Employee(
            tenant_id=tenant.id,
            telegram_user_id=owner.telegram_user_id,
            display_name="Рабочий аккаунт владельца",
            role="employee",
        )
        session.add(employee)
        await session.flush()
        result = await sync_employee_membership(session, employee)
        await session.commit()
        assert result.id == owner.id
        assert result.role == "owner"
        assert result.status == "active"
        assert (
            list(
                await session.scalars(
                    select(Permission.id).where(Permission.membership_id == owner.id)
                )
            )
            == permission_ids
        )


async def test_username_claim_is_one_time_and_does_not_reactivate_revoked_user(
    session_factory, make_service, tenant_payload
):
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        employee = Employee(tenant_id=tenant.id, display_name="TEST", telegram_username="sales")
        session.add(employee)
        await session.commit()
        member = await claim_employee_by_username(
            session, tenant_id=tenant.id, telegram_user_id=123, telegram_username="@Sales"
        )
        assert member is not None
        await session.commit()
        assert (
            await claim_employee_by_username(
                session, tenant_id=tenant.id, telegram_user_id=456, telegram_username="sales"
            )
            is None
        )
        assert employee.telegram_user_id == 123
        member.status = "inactive"
        another = Employee(tenant_id=tenant.id, display_name="Other", telegram_username="newname")
        session.add(another)
        await session.commit()
        assert (
            await claim_employee_by_username(
                session, tenant_id=tenant.id, telegram_user_id=123, telegram_username="newname"
            )
            is None
        )
        assert another.telegram_user_id is None
        assert member.status == "inactive"
