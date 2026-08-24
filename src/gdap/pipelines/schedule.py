"""Schedule arithmetic (§21).

Schedules are tenant-owned rows, not OS cron entries: they are portable, auditable and visible in
the API. Two forms are supported — a full cron expression, or a friendly ``every`` interval — and
both resolve to the same thing: *the next UTC instant this pipeline should run*.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from gdap.core.contracts import ScheduleSpec
from gdap.core.errors import ValidationFailedError
from gdap.observability.logging import get_logger

log = get_logger(__name__)

_INTERVAL = re.compile(
    r"^(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hour|hours|d|day|days|w|week|weeks)$"
)

_NAMED = {
    "minutely": timedelta(minutes=1),
    "hourly": timedelta(hours=1),
    "daily": timedelta(days=1),
    "nightly": timedelta(days=1),
    "weekly": timedelta(weeks=1),
    "monthly": timedelta(days=30),
}

_UNITS = {
    "s": "seconds",
    "sec": "seconds",
    "secs": "seconds",
    "second": "seconds",
    "seconds": "seconds",
    "m": "minutes",
    "min": "minutes",
    "mins": "minutes",
    "minute": "minutes",
    "minutes": "minutes",
    "h": "hours",
    "hour": "hours",
    "hours": "hours",
    "d": "days",
    "day": "days",
    "days": "days",
    "w": "weeks",
    "week": "weeks",
    "weeks": "weeks",
}


def parse_interval(expression: str) -> timedelta:
    """``"5m"``, ``"2 hours"``, ``"daily"`` → :class:`timedelta`."""
    text = str(expression).strip().lower()
    if text in _NAMED:
        return _NAMED[text]
    match = _INTERVAL.match(text)
    if not match:
        raise ValidationFailedError(
            f"cannot parse interval '{expression}'",
            details={"examples": ["5m", "30 minutes", "2h", "daily", "weekly"]},
        )
    amount, unit = int(match.group(1)), _UNITS[match.group(2)]
    if amount <= 0:
        raise ValidationFailedError("interval must be positive")
    return timedelta(**{unit: amount})


def next_run(schedule: ScheduleSpec, *, after: datetime | None = None) -> datetime | None:
    """Next execution instant in UTC, or ``None`` when the schedule is disabled."""
    if not schedule.enabled:
        return None
    moment = (after or datetime.now(UTC)).astimezone(UTC)

    if schedule.cron:
        try:
            zone = ZoneInfo(schedule.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValidationFailedError(
                f"unknown timezone '{schedule.timezone}'",
                details={"hint": "use an IANA name, e.g. America/Sao_Paulo"},
            ) from exc
        local = moment.astimezone(zone)
        try:
            iterator = croniter(schedule.cron, local)
        except (ValueError, KeyError) as exc:
            raise ValidationFailedError(
                f"invalid cron expression '{schedule.cron}'",
                details={"format": "minute hour day month weekday"},
            ) from exc
        return iterator.get_next(datetime).astimezone(UTC)

    return moment + parse_interval(str(schedule.every))


def describe(schedule: ScheduleSpec) -> str:
    if not schedule.enabled:
        return "disabled"
    if schedule.cron:
        return f"cron '{schedule.cron}' ({schedule.timezone})"
    return f"every {schedule.every} ({schedule.timezone})"


def validate(schedule: ScheduleSpec) -> datetime | None:
    """Validate a schedule by computing its next run — fails fast on a bad expression."""
    return next_run(schedule)
