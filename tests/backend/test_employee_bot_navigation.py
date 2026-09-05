from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.backend.api.client_router import can_manage_problem, can_read_problem
from services.backend.bot.keyboards import client_main_menu
from services.backend.client_bots.handlers import (
    ClientContext,
    build_client_router,
    callback_allowed_for_role,
    can_access_bot_problem,
)
from services.backend.models import Employee, TenantMembership
from services.backend.services.employee_access import (
    ConnectionEmployeeConflict,
    employee_membership_is_valid,
)
from services.backend.services.report_access import own_report_rows
from services.backend.telegram_sessions.service import TelegramConnectionError


@pytest.mark.parametrize("handler_name", ["receive_code", "receive_password"])
@pytest.mark.parametrize("failure", [None, "identity", "expired"])
async def test_bot_login_uses_resolved_account_id_and_explains_identity_conflict(
    session_factory, handler_name, failure
):
    connection = SimpleNamespace(
        id="existing-account", status="connected", display_name="Рабочий аккаунт"
    )
    service = SimpleNamespace(
        session_factory=session_factory,
        complete_login=AsyncMock(
            return_value=connection,
            side_effect={
                "identity": ConnectionEmployeeConflict("mismatch"),
                "expired": TelegramConnectionError("challenge missing"),
            }.get(failure),
        ),
        refresh_catalog=AsyncMock(),
        activate_default_scope=AsyncMock(return_value=connection),
        start_initial_sync=AsyncMock(return_value=SimpleNamespace(id="run")),
    )
    events = SimpleNamespace(session_factory=session_factory, record=AsyncMock())
    router = build_client_router(events, mini_app_url=None, connection_service=service)
    handler = next(
        item.callback for item in router.message.handlers if item.callback.__name__ == handler_name
    )
    saved = {"connection_id": "pending-account", "screen_chat_id": 10, "screen_message_id": 20}

    async def update_data(**values):
        saved.update(values)

    state = SimpleNamespace(
        get_data=AsyncMock(side_effect=lambda: dict(saved)),
        update_data=AsyncMock(side_effect=update_data),
        clear=AsyncMock(),
    )
    message = SimpleNamespace(
        text="temporary-secret",
        delete=AsyncMock(),
        bot=SimpleNamespace(edit_message_text=AsyncMock()),
    )
    context = SimpleNamespace(tenant_id="tenant", bot_instance_id="bot", telegram_user_id=1)
    await handler(message, state, context)
    message.delete.assert_awaited_once()
    state.clear.assert_awaited_once()
    text = message.bot.edit_message_text.await_args.kwargs["text"]
    assert "temporary-secret" not in text
    if failure:
        service.refresh_catalog.assert_not_awaited()
        assert (
            "не соответствует выбранному сотруднику"
            if failure == "identity"
            else "Начните подключение заново"
        ) in text
    else:
        service.refresh_catalog.assert_awaited_once_with("tenant", "existing-account")
        assert service.start_initial_sync.await_args.kwargs["connection_id"] == "existing-account"
        assert "Аккаунт подключён" in text


def test_legacy_report_rows_are_filtered_by_employee():
    data = {"rows": [{"employee_id": "mine"}, {"employee_id": "other"}, "invalid"]}
    assert own_report_rows(data, "mine") == [{"employee_id": "mine"}]
    assert own_report_rows(data, None) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["employee", "observer"])
async def test_direct_pdf_callback_denies_non_managers(session_factory, role):
    events = SimpleNamespace(session_factory=session_factory, record=AsyncMock())
    router = build_client_router(events, mini_app_url="https://example.test")
    handler = next(
        item.callback
        for item in router.callback_query.handlers
        if item.callback.__name__ == "report_pdf"
    )
    query = SimpleNamespace(
        data="client:report:pdf:foreign",
        answer=AsyncMock(),
        message=SimpleNamespace(answer_document=AsyncMock()),
    )
    await handler(query, SimpleNamespace(role=role))
    query.answer.assert_awaited_once()
    query.message.answer_document.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "missing", "different_user", "inactive"])
async def test_employee_membership_requires_active_matching_identity(
    session_factory, make_service, tenant_payload, change
):
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        employee = Employee(tenant_id=tenant.id, display_name="TEST", telegram_user_id=123)
        session.add(employee)
        await session.flush()
        member = TenantMembership(
            tenant_id=tenant.id,
            telegram_user_id=123,
            employee_id=employee.id,
            role="employee",
            status="active",
        )
        if change == "missing":
            member.employee_id = None
        elif change == "different_user":
            member.telegram_user_id = 456
        elif change == "inactive":
            employee.status = "inactive"
        await session.flush()
        assert await employee_membership_is_valid(session, member) is (change == "none")


@pytest.mark.parametrize(
    "role,employee_id,responsible,tenant,allowed",
    [
        ("employee", "a", "a", "project", True),
        ("employee", "a", "b", "project", False),
        ("employee", None, None, "project", False),
        ("employee", "a", "a", "other-project", False),
        ("owner", None, "b", "project", True),
        ("owner", None, "b", "other-project", False),
        ("observer", None, None, "project", False),
    ],
)
def test_bot_problem_access(role, employee_id, responsible, tenant, allowed):
    context = SimpleNamespace(role=role, employee_id=employee_id, tenant_id="project")
    problem = SimpleNamespace(tenant_id=tenant, responsible_employee_id=responsible)
    assert can_access_bot_problem(context, problem) is allowed


def test_every_employee_menu_callback_is_allowed_but_admin_actions_are_not():
    for row in client_main_menu(role="employee").inline_keyboard:
        for button in row:
            assert callback_allowed_for_role("employee", button.callback_data)
    for callback in (
        "client:settings",
        "client:tg:disconnect",
        "np:false:123",
        "np:restore:123",
        "client:report:request:week",
    ):
        assert not callback_allowed_for_role("employee", callback)
    assert not callback_allowed_for_role("observer", "np:open:123")
    assert not callback_allowed_for_role("unknown", "np:open:123")


@pytest.mark.parametrize("permission", ["problems.read_own", "problems.manage_own"])
def test_employee_problem_ownership_is_required(permission):
    context = SimpleNamespace(
        membership=SimpleNamespace(role="employee", employee_id="employee-a"),
        allows=lambda value: value == permission,
    )
    check = can_read_problem if permission == "problems.read_own" else can_manage_problem
    assert check(context, SimpleNamespace(responsible_employee_id="employee-a"))
    assert not check(context, SimpleNamespace(responsible_employee_id="employee-b"))
    assert not check(context, SimpleNamespace(responsible_employee_id=None))


@pytest.mark.asyncio
async def test_employee_menu_and_guide_render_once(session_factory):
    events = SimpleNamespace(session_factory=session_factory, record=AsyncMock())
    router = build_client_router(events, mini_app_url="https://example.test")
    handlers = {item.callback.__name__: item.callback for item in router.callback_query.handlers}
    context = ClientContext(
        bot_instance_id="bot",
        tenant_id="tenant",
        telegram_user_id=123,
        role="employee",
        employee_id="employee-a",
        tenant=SimpleNamespace(name="Проект"),
    )
    for name, data, expected in (
        ("menu", "client:menu", "Выберите действие"),
        ("employee_guide", "client:employee-guide:1", "Добро пожаловать"),
        ("employee_guide", "client:employee-guide:2", "Уведомления без лишнего шума"),
        ("employee_guide", "client:employee-guide:3", "Ваши действия и отчёты"),
        ("employee_guide", "client:employee-guide:finish", "Всё готово"),
    ):
        query = SimpleNamespace(
            data=data,
            message=SimpleNamespace(edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        await handlers[name](query, context)
        query.message.edit_text.assert_awaited_once()
        assert expected in query.message.edit_text.await_args.args[0]
        query.answer.assert_awaited_once()
