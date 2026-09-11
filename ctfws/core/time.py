"""Time helpers used by persistence and event records."""

from datetime import UTC, datetime


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp suitable for SQLite text columns."""

    return datetime.now(UTC).isoformat(timespec="seconds")
