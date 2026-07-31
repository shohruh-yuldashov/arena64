"""Custom SQLAlchemy column types — the ORM-boundary counterpart to
`app.core.validation`'s Pydantic types. Both exist for the same reason and
enforce the same rule; they simply guard different boundaries (a request
body versus a database row).
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import INET
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


class IpAddress(TypeDecorator[str]):
    """PostgreSQL `inet` that hands Python a plain `str`, both ways.

    `inet` is the right storage type — it validates the value, stores IPv6
    without a 45-character column, and makes the subnet queries SE-2's
    anomaly detection will want. What it also does is hand asyncpg's
    `ipaddress.IPv4Address` / `IPv6Address` back on read, and that object
    would travel straight into the domain entity.

    Caught by `tests/contract/test_session_repository.py` running the same
    contract against the fake and the real adapter: the fake stored the
    `"203.0.113.7"` it was given, PostgreSQL returned
    `IPv4Address('203.0.113.7')`, and the two compared unequal. Exactly the
    divergence RP-05 exists to surface — and left alone it would have meant
    a domain object whose field type depended on which adapter loaded it.

    Normalising here rather than in the repository's mapper keeps it true
    for every future table with an address column, and keeps the model's
    `Mapped[str | None]` annotation honest rather than aspirational.
    """

    impl = INET
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        # Passed through unchanged: PostgreSQL parses and validates the
        # text form, so a malformed address is rejected by the database
        # rather than by a second, driftable check here.
        return value

    def process_result_value(self, value: object, dialect: Dialect) -> str | None:
        if value is None:
            return None
        # `IPv4Address`/`IPv6Address` and `str` both render correctly.
        return str(value)
