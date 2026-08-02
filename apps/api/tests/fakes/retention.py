"""In-memory stand-ins for the two retention stores — A64-015.5 §8.

What is faked is **storage**, never the thing under test.
`QueueRetentionService` runs for real against these, so the horizon
arithmetic, the batching, the drain loop and the two alarms are genuinely
exercised.

## The predicate is modelled, and it is the reason these exist

Both stores exclude the rows a retention sweep must never reach — a live
queue ticket, an active or pending match — by the *same predicate* the real
adapters use. That is not a convenience: it is the safety property §8 asks
for, and a fake that filtered only by age would let a service pass a test
the database would fail.

What is **not** modelled is `SKIP LOCKED`. That belongs to PostgreSQL and is
asserted where it can be, in `tests/contract/test_queue_retention.py` with
two real sessions — the same line `tests/fakes/queue_repository.py` draws.
"""

from datetime import datetime
from uuid import UUID

from app.modules.matchmaking.domain.queue_ticket import QueueTicket


class InMemoryQueueRetentionStore:
    """The bounded deletes over `queue_ticket`, as a dict.

    `fails` makes every call raise, which is how the service's "a
    maintenance job must never escalate" path is exercised rather than
    asserted.
    """

    def __init__(self) -> None:
        self.tickets: dict[UUID, QueueTicket] = {}
        self.fails = False

    async def prune_resolved(self, *, before: datetime, batch_size: int) -> int:
        if self.fails:
            raise RuntimeError("the queue relation is unreachable")

        # `resolved_at IS NOT NULL` — which the CHECK makes equivalent to
        # "terminal", so a live ticket is unreachable from here however
        # `before` is chosen.
        stale = sorted(
            (
                ticket
                for ticket in self.tickets.values()
                if ticket.resolved_at is not None and ticket.resolved_at < before
            ),
            key=lambda ticket: ticket.resolved_at or before,
        )[:batch_size]
        for ticket in stale:
            del self.tickets[ticket.id]
        return len(stale)

    async def live_before(self, instant: datetime) -> int:
        if self.fails:
            raise RuntimeError("the queue relation is unreachable")
        return sum(
            1
            for ticket in self.tickets.values()
            if ticket.resolved_at is None and ticket.entered_at < instant
        )


class InMemoryAbandonedMatches:
    """`game.public.AbandonedMatchRetention`, as three counters.

    Counters rather than records, because the service under test never
    inspects a match — it asks `game` to delete some and to count the
    pending ones, and both answers are numbers. Modelling whole
    `MatchRecord`s here would be modelling a relation this module is not
    allowed to read.
    """

    def __init__(self) -> None:
        self.abandoned: list[datetime] = []
        self.pending: list[datetime] = []
        self.active = 0
        self.fails = False

    def abandon(self, *, settled_at: datetime) -> None:
        """A match that was cancelled or expired at `settled_at`."""
        self.abandoned.append(settled_at)

    def leave_pending(self, *, created_at: datetime) -> None:
        """A match still awaiting an answer since `created_at`."""
        self.pending.append(created_at)

    def activate(self, *, created_at: datetime) -> None:
        """A match that was played. Never deletable by any horizon."""
        self.active += 1

    async def prune_abandoned(self, *, before: datetime, batch_size: int) -> int:
        if self.fails:
            raise RuntimeError("game is unreachable")
        stale = sorted(instant for instant in self.abandoned if instant < before)[:batch_size]
        for instant in stale:
            self.abandoned.remove(instant)
        return len(stale)

    async def unsettled_before(self, instant: datetime) -> int:
        if self.fails:
            raise RuntimeError("game is unreachable")
        return sum(1 for created_at in self.pending if created_at < instant)


__all__ = ["InMemoryAbandonedMatches", "InMemoryQueueRetentionStore"]
