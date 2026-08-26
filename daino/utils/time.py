"""Time utilities."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(UTC)
