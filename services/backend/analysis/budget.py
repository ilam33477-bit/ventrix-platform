from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelInputBudget:
    context_window: int = 64_000
    max_output_tokens: int = 4_000
    safety_margin_tokens: int = 8_000
    max_dialogs_per_request: int = 12
    overlap_tokens: int = 800

    def usable_input_tokens(self, system_prompt_tokens: int) -> int:
        return max(
            1_000,
            self.context_window
            - self.max_output_tokens
            - self.safety_margin_tokens
            - system_prompt_tokens,
        )


class ConservativeTokenEstimator:
    """Provider-neutral upper estimate suitable for Cyrillic Telegram text.

    DeepSeek does not expose its production tokenizer through the API. UTF-8
    bytes / 2 is intentionally conservative for mixed Russian/English JSON.
    """

    @staticmethod
    def text(value: str) -> int:
        return max(1, math.ceil(len(value.encode("utf-8")) / 2))

    def payload(self, value: dict[str, Any] | list[Any]) -> int:
        return self.text(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def prompt_bytes(system_prompt: str, payload: dict[str, Any]) -> int:
    return len(system_prompt.encode("utf-8")) + len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
