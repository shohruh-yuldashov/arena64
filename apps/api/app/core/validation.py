"""Reusable validation primitives — `Annotated` types and the functions
behind them, applied at the boundary (services.md §6 Tier 1) so a bad value
is rejected before any I/O rather than discovered three layers down.

Each one exists because a plain built-in type accepts something the domain
never should, not because validation is inherently good to add — CLAUDE.md
§9 rule 1 is "validate input at the edge", not "validate everything
twice." `domain-model.md` DB-16/DM-14 is why the datetime rule below exists
specifically: Arena64 is a clocked game, and a naive or non-UTC timestamp
compared against another is a silent correctness bug, not a type error,
until this validator exists.
"""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, StringConstraints

# A `str` field alone accepts `""` and `"   "` — neither is ever a
# meaningful value for a name, a label, or an identifier. `strip_whitespace`
# runs before the length check, so `"   "` is caught too, not laundered
# into a value that merely looks non-empty.
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def ensure_utc(value: datetime) -> datetime:
    """Rejects a naive datetime outright rather than guessing its zone, and
    converts any other zone to UTC — `domain-model.md` DM-14: "all time in
    the domain is an instant or a duration, never a local date-time."
    Guessing here is exactly the mistake DM-14 exists to prevent; comparing
    two guesses is how a flag gets adjudicated against the wrong deadline.
    """
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware (UTC)")
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]
