from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LEGACY_TIMEZONE_ALIASES = {
    "moscow": "Europe/Moscow",
    "msk": "Europe/Moscow",
    "москва": "Europe/Moscow",
}


def normalize_timezone(value: str) -> str:
    """Return a validated IANA timezone key and normalize supported legacy names."""
    candidate = value.strip()
    if not candidate:
        raise ValueError("timezone is required")
    candidate = LEGACY_TIMEZONE_ALIASES.get(candidate.casefold(), candidate)
    try:
        zone = ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ModuleNotFoundError) as exc:
        raise ValueError(f"invalid IANA timezone: {value}") from exc
    return zone.key


def timezone_info(value: str) -> ZoneInfo:
    return ZoneInfo(normalize_timezone(value))
