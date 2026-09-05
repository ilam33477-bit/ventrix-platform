from __future__ import annotations

import pytest
from sqlalchemy import func, select

from services.backend.intelligence.connection_settings import effective_connection_settings
from services.backend.models import Employee, TelegramConnection, TenantMembership, TenantSettings
from services.backend.services.employee_access import bind_connection_employee


@pytest.mark.asyncio
async def test_connection_analysis_settings_override_tenant_defaults(
    session_factory, make_service, tenant_payload
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        settings = await session.scalar(
            select(TenantSettings).where(TenantSettings.tenant_id == tenant.id)
        )
        connection = TelegramConnection(
            tenant_id=tenant.id,
            status="ready",
            history_days=14,
            response_sla_minutes_override=30,
            signal_problem_threshold_override=78,
        )
        session.add(connection)
        await session.commit()

        effective = await effective_connection_settings(
            session,
            tenant_id=tenant.id,
            connection_id=connection.id,
            tenant_settings=settings,
        )
        defaults = await effective_connection_settings(
            session,
            tenant_id=tenant.id,
            connection_id=None,
            tenant_settings=settings,
        )

    assert effective.response_sla_minutes == 30
    assert effective.signal_problem_threshold == 78
    assert defaults.response_sla_minutes == tenant_payload.response_sla_minutes
    assert defaults.signal_problem_threshold == settings.signal_problem_threshold


@pytest.mark.asyncio
async def test_connection_employee_binding_is_durable_and_idempotent(
    session_factory, make_service, tenant_payload
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        connection = TelegramConnection(
            tenant_id=tenant.id,
            status="connected",
            telegram_user_id=777_555_333,
            username="new_employee",
            display_name="Новый сотрудник",
        )
        session.add(connection)
        await session.flush()
        employee = await bind_connection_employee(session, connection)
        await session.commit()
        tenant_id = tenant.id
        connection_id = connection.id
        employee_id = employee.id

    # The shared service now binds before returning a detached connection;
    # the API no longer needs a second transaction to make the employee visible.
    async with session_factory() as session:
        stored = await session.get(TelegramConnection, connection_id)
        employee = await session.get(Employee, employee_id)
        membership = await session.scalar(
            select(TenantMembership).where(
                TenantMembership.tenant_id == tenant_id,
                TenantMembership.employee_id == employee_id,
            )
        )
        assert stored is not None
        assert stored.assigned_employee_id == employee_id
        assert employee is not None
        assert employee.telegram_username == "new_employee"
        assert membership is not None
        assert membership.status == "active"

        await bind_connection_employee(session, stored)
        await session.commit()
        employee_count = await session.scalar(
            select(func.count(Employee.id)).where(
                Employee.tenant_id == tenant_id,
                Employee.telegram_user_id == 777_555_333,
            )
        )
        assert employee_count == 1
