from __future__ import annotations

import json

import httpx
import pytest

from services.api.deepseek import DeepSeekAPIError, DeepSeekProvider


async def test_deepseek_request_scopes_provider_scheduling_by_tenant(monkeypatch) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2},
            },
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "services.api.deepseek.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs),
    )

    content, usage = await DeepSeekProvider(api_key_value="sk-test-secret").generate_json(
        model="deepseek-test",
        system_prompt="Return JSON",
        payload={"tenant_id": "tenant:a"},
        user_id="tenant:a",
    )

    assert content == "{}"
    assert usage == {"input_tokens": 7, "output_tokens": 2}
    assert json.loads(requests[0].content)["user_id"] == "tenant-a"


async def test_deepseek_rate_limit_exposes_bounded_retry_without_secret(monkeypatch) -> None:
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "services.api.deepseek.httpx.AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    429, headers={"Retry-After": "45"}, json={"error": "busy"}
                )
            ),
            **kwargs,
        ),
    )

    with pytest.raises(DeepSeekAPIError) as caught:
        await DeepSeekProvider(api_key_value="sk-never-log-this").generate_json(
            model="deepseek-test", system_prompt="Return JSON", payload={}
        )

    assert caught.value.retry_after_seconds == 45
    assert caught.value.status_code == 429
    assert caught.value.error_code == "deepseek_http_429"
    assert caught.value.retryable is True
    assert "sk-never-log-this" not in str(caught.value)


async def test_deepseek_auth_error_is_permanent_and_sanitized(monkeypatch) -> None:
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "services.api.deepseek.httpx.AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(401, text="secret-provider-body")
            ),
            **kwargs,
        ),
    )

    with pytest.raises(DeepSeekAPIError) as caught:
        await DeepSeekProvider(api_key_value="sk-never-log-this").generate_json(
            model="deepseek-test", system_prompt="Return JSON", payload={}
        )

    assert caught.value.error_code == "deepseek_http_401"
    assert caught.value.retryable is False
    assert "sk-never-log-this" not in str(caught.value)
    assert "secret-provider-body" not in str(caught.value)
