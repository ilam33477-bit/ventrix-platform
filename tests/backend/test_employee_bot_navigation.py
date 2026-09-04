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


@pytest.mark.parametrize("role,employee_id,responsible,tenant,allowed", [
    ("employee", "a", "a", "project", True),
    ("employee", "a", "b", "project", False),
    ("employee", None, None, "project", False),
    ("employee", "a", "a", "other-project", False),
    ("owner", None, "b", "project", True),
    ("owner", None, "b", "other-project", False),
    ("observer", None, None, "project", False),
])
def test_bot_problem_access(role, employee_id, responsible, tenant, allowed):
    context = SimpleNamespace(role=role, employee_id=employee_id, tenant_id="project")
    problem = SimpleNamespace(tenant_id=tenant, responsible_employee_id=responsible)
    assert can_access_bot_problem(context, problem) is allowed


def test_every_employee_menu_callback_is_allowed_but_admin_actions_are_not():
    for row in client_main_menu(role="employee").inline_keyboard:
        for button in row:
            assert callback_allowed_for_role("employee", button.callback_data)
    for callback in ("client:settings", "client:tg:disconnect", "np:false:123",
                     "np:restore:123", "client:report:request:week"):
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
        bot_instance_id="bot", tenant_id="tenant", telegram_user_id=123,
        role="employee", employee_id="employee-a", tenant=SimpleNamespace(name="Проект"),
    )
    for name, data, expected in (
        ("menu", "client:menu", "Выберите действие"),
        ("employee_guide", "client:employee-guide:1", "Добро пожаловать"),
        ("employee_guide", "client:employee-guide:2", "Уведомления без лишнего шума"),
        ("employee_guide", "client:employee-guide:3", "Ваши действия и отчёты"),
        ("employee_guide", "client:employee-guide:finish", "Всё готово"),
    ):
        query = SimpleNamespace(
            data=data, message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock(),
        )
        await handlers[name](query, context)
        query.message.edit_text.assert_awaited_once()
        assert expected in query.message.edit_text.await_args.args[0]
        query.answer.assert_awaited_once()
