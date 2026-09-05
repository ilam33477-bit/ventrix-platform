from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx


class DeepSeekAPIError(RuntimeError):
    """Sanitized provider failure with optional retry guidance for the job queue."""

    def __init__(self, status_code: int, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(f"DeepSeek API request failed with status {status_code}")
        self.status_code = status_code
        self.error_code = f"deepseek_http_{status_code}"
        self.retry_after_seconds = retry_after_seconds
        self.retryable = status_code == 429 or status_code >= 500


def _provider_user_id(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "-", value)[:512]
    if not normalized:
        raise ValueError("user_id must contain a letter, number, dash or underscore")
    return normalized


@dataclass(frozen=True, slots=True)
class DeepSeekProvider:
    base_url: str = "https://api.deepseek.com"
    timeout_seconds: float = 30.0
    api_key_value: str | None = None

    @property
    def api_key(self) -> str:
        value = self.api_key_value or os.getenv("DEEPSEEK_API_KEY")
        if not value:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        return value

    async def list_models(self) -> set[str]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_seconds) as client:
            response = await client.get("/models", headers=self._headers())
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
            return {item["id"] for item in payload.get("data", []) if isinstance(item.get("id"), str)}

    async def assert_models_available(self, required: set[str]) -> None:
        available = await self.list_models()
        missing = required - available
        if missing:
            raise RuntimeError(f"Configured DeepSeek models are unavailable: {sorted(missing)}")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def generate_json(
        self,
        *,
        model: str,
        system_prompt: str,
        payload: dict[str, Any],
        thinking: bool = False,
        reasoning_effort: str | None = None,
        max_tokens: int = 4000,
        user_id: str | None = None,
    ) -> tuple[str, dict[str, int]]:
        request: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens,
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }
        if reasoning_effort:
            request["reasoning_effort"] = reasoning_effort
        if user_id:
            request["user_id"] = _provider_user_id(user_id)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_seconds) as client:
            response = await client.post(
                "/chat/completions", headers=self._headers(), json=request
            )
            if response.is_error:
                retry_after = None
                if response.status_code == 429:
                    try:
                        retry_after = max(1, min(86_400, int(response.headers.get("Retry-After", "30"))))
                    except ValueError:
                        retry_after = 30
                raise DeepSeekAPIError(
                    response.status_code, retry_after_seconds=retry_after
                )
            body: dict[str, Any] = response.json()
        content = str(body["choices"][0]["message"].get("content") or "")
        usage = body.get("usage") or {}
        return content, {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        }
