"""Day mapping between the scenario's dates and the database's timestamps (docs/temporal-model.md).

An event dated D is stored at D 00:00 UTC. A question "as of date D" uses the *end* of D, so it
includes every event of that day, as the scenario oracle does (it compares whole dates, inclusive).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

MICRO = timedelta(microseconds=1)


def day_start(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time(), UTC)


def day_end(d: date) -> datetime:
    """The last instant of ``d`` (UTC): the query timestamp for "as of d"."""
    return day_start(d) + timedelta(days=1) - MICRO


def as_of_instant(value: date | datetime | None) -> datetime:
    """``None`` means now; a date means the end of that day; a datetime is used as given."""
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return day_end(value)
