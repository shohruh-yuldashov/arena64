"""The clock port — architecture.md AD-07: `domain/` may not read the
clock, and an application service may not either (services.md §3.3 lists
"reading the wall clock" among a service's prohibited responsibilities).
Time arrives as an injected dependency instead.

**Why this is worth a port on this platform specifically.** AD-07's own
argument: Arena64 is a clocked game, and half the match domain's rules are
time-dependent — flag falls, increment application, abandonment
thresholds, correspondence deadlines. Code that calls `datetime.now()`
directly can only be tested by sleeping, and tests that sleep are tests
that get deleted. With an injected clock, a three-day correspondence
timeout is a unit test that runs in a microsecond.

`users` needs far less than that — `created_at` and `updated_at` — but it
is the first module to need *any* of it, so the port lands here in `core/`
where `game` will find it rather than as a local helper that would have to
be moved and re-plumbed later.

Every value produced is timezone-aware UTC, per domain-model.md DM-14.
"""

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Reads the current instant. The only sanctioned source of "now"."""

    def now(self) -> datetime:
        """A timezone-aware UTC instant — never naive, never local."""
        ...


class SystemClock:
    """The production implementation: the real wall clock, in UTC.

    Bound at the composition root. Deliberately the only place in the
    application package that calls `datetime.now`, so a grep for it has
    exactly one legitimate hit.
    """

    def now(self) -> datetime:
        return datetime.now(UTC)
