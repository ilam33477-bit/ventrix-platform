from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from httpx import ASGITransport, AsyncClient

from services.backend.api.app import create_app
from services.backend.api.client_router import (
    get_client_connection_service,
    validate_webapp_init_data,
)
from services.backend.api.dependencies import get_foundation_service
from services.backend.config import get_settings
from services.backend.database import get_session


def signed_init_data(token: str, user_id: int, auth_date: int) -> str:
    values = {
        "auth_date": str(auth_date),
        "query_id": "query-1",
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_webapp_init_data_signature_and_age_are_checked() -> None:
    token = "123456:telegram-bot-token"
    payload = signed_init_data(token, 777, 1_700_000_000)
    assert validate_webapp_init_data(payload, token, now=1_700_000_100)["user_id"] == 777
    assert validate_webapp_init_data(payload, "wrong-token", now=1_700_000_100) is None
    assert validate_webapp_init_data(payload, token, now=1_700_200_000) is None


@pytest.mark.asyncio
async def test_owner_api_endpoints(
    session_factory, settings, make_service, tenant_payload, monkeypatch
) -> None:
    app_module = importlib.import_module("services.backend.api.app")
    monkeypatch.setattr(app_module, "get_session_factory", lambda: session_factory)
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    app = create_app()

    async def session_override():
        async with session_factory() as session:
            yield session

    async def service_override():
        async with session_factory() as session:
            yield make_service(session, source="api-test")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_foundation_service] = service_override

    class FakeConnectionService:
        async def begin_login(self, tenant_id, phone, assigned_employee_id=None):
            return SimpleNamespace(
                id="connection-1", status="awaiting_code", phone_masked="+7••••••0011"
            )

        async def complete_login(self, tenant_id, *, connection_id, code=None, password=None):
            if code:
                return SimpleNamespace(
                    id=connection_id,
                    status="awaiting_2fa",
                    display_name=None,
                    phone_masked="+7••••••0011",
                    username=None,
                )
            return SimpleNamespace(
                id=connection_id,
                status="connected",
                display_name="Рабочий аккаунт",
                phone_masked="+7••••••0011",
                username="work_account",
            )

        async def refresh_catalog(self, tenant_id, connection_id):
            return SimpleNamespace(id=connection_id)

        async def list_folders(self, tenant_id, connection_id):
            return [SimpleNamespace(telegram_folder_id=10, title="Работа", chat_count=12)]

        async def select_scope(self, tenant_id, folder_ids, **kwargs):
            return SimpleNamespace(
                id=kwargs["connection_id"],
                status="connected",
                selected_folder_title="Работа",
                history_days=kwargs["history_days"],
            )

        async def start_initial_sync(self, tenant_id, **kwargs):
            return SimpleNamespace(id="run-1", status="pending")

        async def cancel_login(self, tenant_id, connection_id):
            return None

    app.dependency_overrides[get_client_connection_service] = FakeConnectionService
    transport = ASGITransport(app=app)
    headers = {"X-Owner-Token": settings.owner_api_token.get_secret_value()}

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/health")).json() == {"status": "ok"}
        ready = await client.get("/ready")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready"}
        unauthorized = await client.get("/api/v1/owner/tenants")
        assert unauthorized.status_code == 401

        created = await client.post(
            "/api/v1/owner/tenants",
            headers=headers,
            json=tenant_payload.model_dump(mode="json"),
        )
        assert created.status_code == 201, created.text
        tenant_id = created.json()["id"]

        listed = await client.get("/api/v1/owner/tenants", headers=headers)
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [tenant_id]

        fetched = await client.get(f"/api/v1/owner/tenants/{tenant_id}", headers=headers)
        assert fetched.json()["settings"]["response_sla_minutes"] == 60

        patched = await client.patch(
            f"/api/v1/owner/tenants/{tenant_id}", headers=headers, json={"plan": "pro"}
        )
        assert patched.status_code == 200
        assert patched.json()["plan"] == "pro"

        profile = await client.patch(
            f"/api/v1/owner/tenants/{tenant_id}/ai-profile",
            headers=headers,
            json={"typical_processes": ["sales", "support"], "significant_amounts": [900000]},
        )
        assert profile.status_code == 200
        assert profile.json()["version"] == 2

        bot = await client.post(
            f"/api/v1/owner/tenants/{tenant_id}/bots",
            headers=headers,
            json={"token": "mock-telegram-token-must-remain-secret"},
        )
        assert bot.status_code == 201, bot.text
        assert bot.json()["telegram_url"] == "https://t.me/axiom_ops_bot"
        assert "token" not in bot.json()

        second_payload = tenant_payload.model_copy(
            update={
                "name": "Foreign Tenant",
                "owner_telegram_username": "foreign_owner",
                "owner_telegram_user_id": 999_000_111,
            }
        )
        second_tenant = await client.post(
            "/api/v1/owner/tenants",
            headers=headers,
            json=second_payload.model_dump(mode="json"),
        )
        assert second_tenant.status_code == 201
        second_tenant_id = second_tenant.json()["id"]

        init_data = signed_init_data(
            "mock-telegram-token-must-remain-secret",
            tenant_payload.owner_telegram_user_id,
            int(time.time()),
        )
        mini_app_auth = await client.post(
            f"/api/v1/client/mini-app/auth?tenant_id={second_tenant_id}",
            headers={"Authorization": f"tma {init_data}"},
        )
        assert mini_app_auth.status_code == 200, mini_app_auth.text
        assert mini_app_auth.json()["tenant_id"] == tenant_id
        assert mini_app_auth.json()["tenant_id"] != second_tenant_id
        assert mini_app_auth.json()["user"]["telegram_user_id"] == (
            tenant_payload.owner_telegram_user_id
        )
        assert mini_app_auth.json()["permissions"] == ["*"]
        assert "dashboard_summary" in mini_app_auth.json()
        assert mini_app_auth.json()["project_context"]["onboarding"] == {
            "step": "welcome",
            "completed": False,
            "completed_at": None,
            "steps": [
                "welcome",
                "telegram_connection",
                "scope_selection",
                "employees_review",
                "completed",
            ],
        }
        for onboarding_step in (
            "telegram_connection",
            "scope_selection",
            "employees_review",
            "completed",
        ):
            onboarding = await client.patch(
                "/api/v1/client/onboarding",
                headers={"Authorization": f"tma {init_data}"},
                json={"step": onboarding_step},
            )
            assert onboarding.status_code == 200, onboarding.text
            assert onboarding.json()["step"] == onboarding_step
        assert onboarding.json()["completed"] is True

        client_menu = await client.get(
            "/api/v1/client/menu", headers={"Authorization": f"tma {init_data}"}
        )
        assert client_menu.status_code == 200, client_menu.text
        assert client_menu.json()["tenant"]["id"] == tenant_id
        assert client_menu.json()["permissions"] == ["*"]
        client_bootstrap = await client.get(
            "/api/v1/client/bootstrap", headers={"Authorization": f"tma {init_data}"}
        )
        assert client_bootstrap.status_code == 200
        assert client_bootstrap.json()["onboarding"]["completed"] is True
        assert client_bootstrap.json()["onboarding"]["step"] == "completed"

        employee = await client.post(
            "/api/v1/client/employees",
            headers={"Authorization": f"tma {init_data}"},
            json={
                "display_name": "Мария",
                "telegram_user_id": 700001,
                "telegram_username": "maria_sales",
            },
        )
        assert employee.status_code == 201, employee.text
        employees = await client.get(
            "/api/v1/client/employees", headers={"Authorization": f"tma {init_data}"}
        )
        assert employees.json()[0]["telegram_user_id"] == 700001

        group = await client.post(
            "/api/v1/client/group-integrations",
            headers={"Authorization": f"tma {init_data}"},
            json={"telegram_chat_id": -100700001, "title": "Продажи"},
        )
        assert group.status_code == 201, group.text
        assert (
            await client.get(
                "/api/v1/client/group-integrations",
                headers={"Authorization": f"tma {init_data}"},
            )
        ).json()[0]["title"] == "Продажи"
        assert (
            await client.get(
                "/api/v1/client/ai-usage", headers={"Authorization": f"tma {init_data}"}
            )
        ).json()["total_tokens"] == 0

        started_login = await client.post(
            "/api/v1/client/connections/login/start",
            headers={"Authorization": f"tma {init_data}"},
            json={"phone": "+7 999 000-00-11"},
        )
        assert started_login.status_code == 201
        assert started_login.json()["phone_masked"].endswith("0011")
        code_result = await client.post(
            "/api/v1/client/connections/connection-1/login/complete",
            headers={"Authorization": f"tma {init_data}"},
            json={"code": "12345"},
        )
        assert code_result.json()["requires_2fa"] is True
        password_result = await client.post(
            "/api/v1/client/connections/connection-1/login/complete",
            headers={"Authorization": f"tma {init_data}"},
            json={"password": "temporary-test-password"},
        )
        assert password_result.json()["status"] == "connected"
        catalog = await client.post(
            "/api/v1/client/connections/connection-1/catalog",
            headers={"Authorization": f"tma {init_data}"},
        )
        assert catalog.json()["folders"] == [{"id": 10, "title": "Работа", "chat_count": 12}]
        scope = await client.post(
            "/api/v1/client/connections/connection-1/scope",
            headers={"Authorization": f"tma {init_data}"},
            json={"folder_ids": [10], "history_days": 7, "personal_dialogs_consent": False},
        )
        assert scope.json()["analysis_run_id"] == "run-1"

        foreign_init_data = signed_init_data(
            "mock-telegram-token-must-remain-secret",
            999_999_999,
            int(time.time()),
        )
        forbidden = await client.get(
            "/api/v1/client/menu",
            headers={"Authorization": f"tma {foreign_init_data}"},
        )
        assert forbidden.status_code == 401

        bots = await client.get(f"/api/v1/owner/tenants/{tenant_id}/bots", headers=headers)
        assert bots.status_code == 200
        assert len(bots.json()) == 1
