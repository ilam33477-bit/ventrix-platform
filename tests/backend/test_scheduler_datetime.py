from datetime import UTC, datetime, timedelta, timezone

from services.backend.scheduler.service import _as_utc


def test_as_utc_normalizes_sqlite_naive_datetime() -> None:
    value = datetime.fromisoformat("2026-08-15T12:30:00")

    assert _as_utc(value) == datetime(2026, 8, 15, 12, 30, tzinfo=UTC)


def test_as_utc_converts_aware_datetime() -> None:
    value = datetime(2026, 8, 15, 15, 30, tzinfo=timezone(timedelta(hours=3)))

    assert _as_utc(value) == datetime(2026, 8, 15, 12, 30, tzinfo=UTC)
