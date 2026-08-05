"""What the statistics projection counts about itself — A64-020.5F §12.

Two counters and two bounded label sets. Every label is a member of a
closed enum, so no series can be created by data: §12 forbids labelling by
player, match, username or event id, and the way to make that structural
rather than remembered is for the label type to have no room for one.
"""

from enum import StrEnum
from typing import Final

#: Completed matches this projection saw, by what it did with each.
#:
#: One counter with a `result` label rather than four names, because every
#: question asked of these is comparative: what share of deliveries were
#: duplicates, and is `rejected` rising. A rising `rejected` rate is the
#: alert — it means events are arriving that cannot be attributed to two
#: players, which is a contract problem rather than a load problem.
STATISTICS_PROJECTIONS: Final = "statistics.projections_total"

#: Matches a backfill run scanned, by outcome. Separate from the live
#: counter so an operator replaying history cannot make the live rate look
#: like an incident.
STATISTICS_BACKFILL: Final = "statistics.backfill_matches_total"


class ProjectionResult(StrEnum):
    """What happened to one completed match."""

    APPLIED = "applied"
    ALREADY_PROCESSED = "already_processed"
    """A duplicate delivery, a relay retry, or a backfill passing over
    history the live consumer already counted. **Not a failure** — it is
    the exactly-once mechanism working, and its rate is how visible that
    is."""

    IGNORED = "ignored"
    """A match that was not played to a result. An abort, which MT-11 keeps
    out of every statistic."""

    REJECTED = "rejected"
    """The facts needed to count it were not in the payload. Never
    guessed."""

    FAILED = "failed"
    """The projection raised. Retried by the relay."""


__all__ = [
    "STATISTICS_BACKFILL",
    "STATISTICS_PROJECTIONS",
    "ProjectionResult",
]
