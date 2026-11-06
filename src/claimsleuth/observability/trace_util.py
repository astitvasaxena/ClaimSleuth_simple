"""Time helpers for the trace: every timestamp timezone-aware (UTC when a record carried none)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def aware(value: Any) -> datetime | None:
    """A timezone-aware datetime from a datetime, an ISO string or a Unix time; None when there is none."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def ms_between(start: Any, end: Any) -> float | None:
    a, b = aware(start), aware(end)
    if a is None or b is None:
        return None
    return round(max(0.0, (b - a).total_seconds() * 1000), 3)


def overlap_ms(first: tuple[Any, Any], second: tuple[Any, Any]) -> float:
    """How long two [start, end] intervals ran at the same time (0 when they did not overlap)."""
    start = max(aware(first[0]), aware(second[0]))
    end = min(aware(first[1]), aware(second[1]))
    return round(max(0.0, (end - start).total_seconds() * 1000), 3)
