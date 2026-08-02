"""Letting go of the pairings that never became games — A64-015.5 §8.

`game.match` is the permanent competitive record, and A64-015.4 said so:
"this relation is **meant** to grow without bound. A match is the permanent
record A-4 is about." That sentence is about matches that were *played*. It
was never true of the other kind, and the same docstring said which:

> "What will need a horizon is the pending-acceptance **churn** — cancelled
> and expired rows that were never games — and that is a product decision
> (how long is 'why did my opponent decline' answerable?) rather than a
> capacity one."

This is that horizon, and the product decision behind it is recorded in
`specs/matchmaking.md` §11.6.

## Why `matchmaking` drives a sweep over `game`'s table

The same reason it drives `MatchAcceptanceExpiryUseCase`, and it is worth
restating because it looks like the wrong module holding the schedule.

`game` owns the rows. `matchmaking` owns the **question**: an abandoned
pairing is a queue event that failed, its retention horizon is the same
product judgement as the queue's own, and running two schedules that must be
configured consistently is how they stop being consistent. So the capability
is published as a narrow command, and the module that has an opinion about
the horizon supplies it.

What is *not* published is a way to delete anything else. `prune_abandoned`
cannot reach an `active` match, cannot reach a `pending_acceptance` one, and
takes no predicate — the caller supplies a cutoff and a batch size, and
nothing more.
"""

from datetime import datetime
from typing import Protocol


class AbandonedMatchRetention(Protocol):
    """Deleting matches that ended before anybody played them."""

    async def prune_abandoned(self, *, before: datetime, batch_size: int) -> int:
        """Deletes up to `batch_size` **cancelled or expired** matches
        settled before `before`. Returns how many rows went.

        **Never touches a match that was played.** `active` is excluded by
        predicate rather than by the horizon, and so is
        `pending_acceptance` — a pairing still awaiting an answer is not
        abandoned however old the row looks, and one that is old *and*
        pending is a reconciliation failure this job must not paper over by
        deleting the evidence.

        The cutoff is `settled_at`, which is non-null exactly for settled
        matches (`ck_match__settled_iff_answered`), so the predicate and the
        horizon agree by construction rather than by review.

        Bounded by `batch_size` and safe for more than one pruner, by the
        same `FOR UPDATE SKIP LOCKED` every claim on this platform uses.

        **The queue tickets it names are not deleted with it.** They are
        `matchmaking`'s rows on `matchmaking`'s own horizon, and the two
        relations are pruned independently — a match may outlive its tickets
        or the reverse. Neither carries a foreign key to the other, which is
        what makes that legal (DM-06, and `game.infrastructure.models`).
        """
        ...

    async def unsettled_before(self, instant: datetime) -> int:
        """How many matches older than `instant` are still awaiting an
        answer.

        Published alongside the delete because it is the number that says
        *why* the floor did not move, and here it is a genuine alarm rather
        than bookkeeping: a match still `pending_acceptance` past the whole
        retention horizon means two players are holding an offer whose
        deadline passed days ago, and the acceptance-expiry sweep has
        stopped.

        The caller reports it; nothing acts on it. A retention job that
        *deleted* such a row would destroy the evidence of the failure
        instead of surfacing it — see `MatchRetentionStore` on why the
        predicate excludes a pending match twice over.
        """
        ...


__all__ = ["AbandonedMatchRetention"]
