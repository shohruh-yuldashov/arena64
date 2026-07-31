"""Custom SQLAlchemy column types — the ORM-boundary counterpart to
`app.core.validation`'s Pydantic types. Both exist for the same reason and
enforce the same rule; they simply guard different boundaries (a request
body versus a database row).
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UtcDateTime(TypeDecorator[datetime]):
    """A `timestamptz` that refuses to store or return a naive datetime.

    `domain-model.md` DM-14: "all time in the domain is an instant or a
    duration, never a local date-time." PostgreSQL's `timestamptz` already
    stores everything in UTC internally, but SQLAlchemy will happily bind a
    *naive* Python `datetime` to it — silently assuming that value is
    already UTC, which is exactly the guess DM-14 forbids. This type makes
    that guess an error instead: naive in, exception; naive out (which
    should never happen against a `timestamptz` column, but is checked
    anyway) is normalised to UTC rather than trusted.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "UtcDateTime received a naive datetime; DM-14 requires a "
                "timezone-aware value. Use app.core.validation.ensure_utc "
                "or construct with tzinfo=UTC."
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # Should not happen against a `timestamptz` column — defensive
            # normalisation only, for whatever dialect or misconfiguration
            # would otherwise hand back a naive value silently.
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
