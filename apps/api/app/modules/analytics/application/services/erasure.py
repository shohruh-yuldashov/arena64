"""Analytics erasure — A64-027.2's D3, and the one operation that must be
irreversible.

## What erasure is here

One `DELETE` from `analytics.subject`.

The event rows are untouched, and that is the design rather than an
omission. They carry a `subject_key` that is a **random** value: not a hash
of the player id, not a ciphertext, not a derivation of anything. The only
function from a player to their key was the row this deletes, so after it
there is no way from the person to the rows and none from the rows back to
the person.

    before   player_id -> subject_key -> events
    after                  subject_key -> events

That satisfies the decision's actual requirement — "after deletion it must
not be possible to re-identify the user through analytics" — in a way that
keeping `PlayerId` and calling it opaque would not: `PlayerId` is the
platform's primary key everywhere else, so a raw analytics store holding one
is a store joinable to the product database by any operator with both.

## What survives, and why that is not a loophole

The events keep their dimensions and their grouping. A cohort's retention
does not shift when one of its members erases, and "matches per active
player" still counts that person's matches as one player's. What is gone is
which player.

This is the aggregate-preserving half the decision explicitly allows:
"non-PII facts needed for product measurement may be retained; aggregate
statistics may be retained."

## Not wired to an erasure workflow, because there is not one

Arena64 has **no account deletion or erasure implementation** — the audit
found the lifecycle documented in `domain-model.md` (AC-4, AC-5) and no code
that performs it. So this is a service and a public port with tests, ready
for the transaction that will eventually call it, rather than a hook into a
workflow that does not exist.

Recorded as such in `analytics.md` rather than left as a surprise: the
analytics half of erasure is complete, and the product half is not this
task's to build.
"""

import logging
from uuid import UUID

from app.modules.analytics.application.ports import SubjectEraser

logger = logging.getLogger(__name__)


class AnalyticsErasureService:
    """Destroys a player's link to their analytics history."""

    def __init__(self, *, eraser: SubjectEraser) -> None:
        self._eraser = eraser

    async def erase(self, player_id: UUID) -> bool:
        """Unlinks, irreversibly. Idempotent.

        Returns whether a link existed. Erasing an account that never
        produced an analytics event is not an error — a deletion request
        that failed because there was nothing to delete would make the
        retry of a deletion fail.
        """
        unlinked = await self._eraser.erase(player_id)
        logger.info(
            "analytics_subject_erased",
            # The player id and nothing else: no subject key, which would
            # put the very link this deleted into a log with its own
            # retention policy.
            extra={"player_id": str(player_id), "had_subject": unlinked},
        )
        return unlinked
