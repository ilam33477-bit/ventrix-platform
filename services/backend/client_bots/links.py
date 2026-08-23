from __future__ import annotations

import re
from urllib.parse import urlencode

_START_PARAMETER_RE = re.compile(r"[^A-Za-z0-9_-]+")


def direct_mini_app_link(bot_username: str, start_parameter: str | None = None) -> str:
    """Build a Telegram Direct Mini App link usable from group messages.

    Bot API ``web_app`` inline buttons are private-chat only. A t.me ``startapp``
    URL is the supported group equivalent and still supplies signed initData.
    """
    username = bot_username.strip().lstrip("@")
    if not username:
        raise ValueError("bot username is required")
    query: dict[str, str] = {"startapp": ""}
    if start_parameter:
        normalized = _START_PARAMETER_RE.sub("_", start_parameter).strip("_")[:512]
        query["startapp"] = normalized
    return f"https://t.me/{username}?{urlencode(query)}"
