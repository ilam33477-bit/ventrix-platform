from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx


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
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_seconds) as client:
            response = await client.post(
                "/chat/completions", headers=self._headers(), json=request
            )
            response.raise_for_status()
            body: dict[str, Any] = response.json()
        content = str(body["choices"][0]["message"].get("content") or "")
        usage = body.get("usage") or {}
        return content, {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        }
