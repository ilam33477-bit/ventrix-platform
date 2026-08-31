from __future__ import annotations

import re
from urllib.parse import urlencode

_START_PARAMETER_RE = re.compile(r"[^A-Za-z0-9_-]+")


def private_bot_link(bot_username: str, start_parameter: str | None = None) -> str:
    """Open the tenant bot in private chat with an optional safe start payload.

    A ``?startapp`` link only works when BotFather has configured a Main Mini
    App, while a named Direct Mini App additionally requires its short name.
    Tenant bots are configured through Bot API menu buttons, so group cards use
    the universal private-bot deep link and the bot then offers a ``web_app``
    button with the requested context.
    """
    username = bot_username.strip().lstrip("@")
    if not username:
        raise ValueError("bot username is required")
    query: dict[str, str] = {}
    if start_parameter:
        normalized = _START_PARAMETER_RE.sub("_", start_parameter).strip("_")[:64]
        query["start"] = normalized
    suffix = f"?{urlencode(query)}" if query else ""
    return f"https://t.me/{username}{suffix}"
