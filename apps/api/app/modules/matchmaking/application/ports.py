"""The ports `matchmaking`'s use cases declare — AD-06 puts them in the
layer that *needs* them, so a service depends on a contract and never on
`SqlAlchemyQueueRepository`.

Two protocols, and the split between them is by capability rather than by
storage — the argument every port pair on this platform makes:

    QueueRepository        everything about a ticket in PostgreSQL
    RatingSnapshotProvider what number a ticket records at entry (QT-2)

The presence port is deliberately **not** declared here.
`users.public.PresenceProvider` already exists, `matchmaking` imports it
from that published surface (R-1), and a local re-declaration would be a
second contract for one fact — which is exactly how "who is online" ends up
with two implementations and one of them wrong. A64-014.1 says so directly:
reuse `presence:v1:`, do not create another online-player index.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.matchmaking.domain.queue_ticket import (
    QueueSnapshot,
    QueueTicket,
    QueueType,
    Region,
)


class QueueRepository(Protocol):
    """Storage for `QueueTicket` — one repository per aggregate root.

    Every method is designed for **more than one worker**, because the
    deployment AD-02 describes is several processes against one table and
    A64-014.1 requires the queue to support horizontal workers from the
    first release. `claim_due` is the method where that is not free; the
    rest are safe because they are single statements keyed on a player or a
    ticket.
    """

    async def enqueue(self, ticket: QueueTicket) -> QueueTicket:
        """Writes a new waiting ticket. Flushes, never commits
        (repositories.md §5.1).

        Raises `AlreadyQueued` when the partial unique index refuses a
        second live ticket. The service checks first to produce that error
        without a write; this is the guard that holds when two joins race,
        and it is the one that matters — QT-1 exists because a player with
        two tickets can be paired into two matches.
        """
        ...

    async def cancel(self, ticket: QueueTicket) -> bool:
        """Records a resolved ticket, **only if the row is still waiting**.

        Returns whether it applied. A compare-and-set rather than a blind
        `UPDATE`, and the race it closes is ordinary rather than exotic: a
        player with the queue open on two devices, or one who taps cancel as
        the expiry sweep commits. A blind write would let a cancellation
        overwrite an expiry — or, worse once A64-014.2 exists, overwrite a
        `matched` ticket whose match has already been created.

        `False` is not a failure. It means somebody else resolved the
        ticket first, which is what `QueueService.leave` reports as "you
        were not queued" — the honest answer, and the idempotent one.
        """
        ...

    async def active_ticket(self, player_id: UUID, *, now: datetime) -> QueueTicket | None:
        """The player's live ticket, or `None`.

        **Live means `waiting` and not yet due.** A ticket past its
        `expires_at` that the sweeper has not reached is reported as
        absent, deliberately: `expires_at` is the rule and the sweep is
        only the bookkeeping, so a player must never be told they are
        queued because a worker is a few seconds behind — nor blocked from
        re-queueing by one.

        `None` rather than raising: a player who is not queued is the
        ordinary case, and every caller branches on it.
        """
        ...

    async def queue_snapshot(
        self,
        *,
        queue_type: QueueType,
        region: Region,
        now: datetime,
        limit: int,
    ) -> QueueSnapshot:
        """One pool as it stands: its depth, and its oldest live tickets.

        The read A64-014.2's pairing scan runs, declared now so the shape
        the scan needs is settled before anything depends on a different
        one. `limit` bounds the tickets; the depth is a count over the same
        predicate, so a bounded read never reports a wrong number (CLAUDE.md
        §10.5, and `QueueSnapshot` on why the two are separate).

        **Not a lock and not a reservation.** Two scans over one pool see
        the same tickets, which is why QT-4's atomic claim exists and why
        `claim_due` below is a different method rather than a flag here.
        """
        ...

    async def claim_due(
        self, *, now: datetime, limit: int, claimed_by: str
    ) -> Sequence[QueueTicket]:
        """Takes up to `limit` tickets whose window has closed, oldest
        first, for this worker.

        **The one method that must be safe under concurrency**, and the
        contract is the outbox's: two workers calling this simultaneously
        receive disjoint sets. A64-014.1 requires the proven mechanism
        rather than a new one, so the implementation is
        `SELECT ... FOR UPDATE SKIP LOCKED` — see
        `SqlAlchemyQueueRepository.claim_due` for what the alternatives cost.

        `claimed_by` is diagnostic. Correctness comes from the row lock, not
        from a column: a lease recorded in a column has no way to detect a
        dead holder, which is why this table has no `claimed_by` at all and
        the identifier reaches the log line instead.

        The rows stay `waiting` — claiming is not a transition. `expire`
        below is what resolves them, in the caller's transaction, so a
        worker that dies between the two leaves tickets that the next sweep
        simply claims again.
        """
        ...

    async def expire(self, ticket_ids: Sequence[UUID], *, at: datetime) -> int:
        """Marks claimed tickets expired. **One statement per sweep**,
        whatever the batch size.

        Returns the number of rows actually updated, which is the count the
        log line reports — not `len(ticket_ids)`, which would claim a
        transition for a ticket somebody cancelled in between. The predicate
        carries `status = 'waiting'` as well as the id list, for that reason.
        """
        ...


class RatingSnapshotProvider(Protocol):
    """What rating a ticket records when the player joins — QT-2.

    A port with one implementation that returns a constant, and it is worth
    saying plainly why that is not a stub.

    QT-2 is a *correctness* rule: "pairing must be deterministic within a
    tick; a rating changing mid-scan would make the same scan pair
    inconsistently." The rule is about the ticket carrying a snapshot rather
    than a reference, and it is fully implemented — the column exists, the
    aggregate holds it, the event carries it. What does not exist is a
    rating *system* (domain-model.md Q-3 is open, and `rating` is an unbuilt
    module), so the only honest snapshot today is the provisional starting
    value.

    Declaring it as a port now means the day `rating` ships, this is
    satisfied by `rating.public` and no use case, no aggregate and no test
    changes. Declaring it later would mean `QueueService` reaching for a
    constant, and a constant is not something another module can replace.
    """

    async def rating_for(self, player_id: UUID, *, queue_type: QueueType) -> int:
        """The player's rating in the pool they are joining.

        Per pool rather than per player, because that is what a rating is:
        `RatingCategory` already splits classic, rapid and blitz, and a
        single number would have to pick one. `QueueType` is the axis this
        task has; when time controls arrive the argument widens and callers
        do not.

        Never raises and never returns `None`. A player with no measured
        rating has a provisional one (PR-6), which is a value rather than an
        absence — and a join that failed because a rating could not be read
        would take matchmaking down for a number that has a safe default.
        """
        ...
