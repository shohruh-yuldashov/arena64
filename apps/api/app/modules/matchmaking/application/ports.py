"""The ports `matchmaking`'s use cases declare — AD-06 puts them in the
layer that *needs* them, so a service depends on a contract and never on
`SqlAlchemyQueueRepository`.

Three protocols, and the split between them is by capability rather than
by storage — the argument every port pair on this platform makes:

    QueueRepository        everything about a ticket in PostgreSQL
    RatingSnapshotProvider what number a ticket records at entry (QT-2)
    RecentOpponentProvider who these players have just played (A64-015.3)

The **pairwise block** port is deliberately not here either:
`friends.public.PairingExclusions` already answers it, and BL-2 is
`friends`' rule to enforce. Two ports for "who may not be paired" would be
two places the block graph is interpreted, and one of them would eventually
check a single direction.

The presence port is deliberately **not** declared here.
`users.public.PresenceProvider` already exists, `matchmaking` imports it
from that published surface (R-1), and a local re-declaration would be a
second contract for one fact — which is exactly how "who is online" ends up
with two implementations and one of them wrong. A64-014.1 says so directly:
reuse `presence:v1:`, do not create another online-player index.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.matchmaking.domain.queue_pool import QueuePool, QueueType
from app.modules.matchmaking.domain.queue_ticket import QueueSnapshot, QueueTicket


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

    async def queue_snapshot(self, *, pool: QueuePool, now: datetime, limit: int) -> QueueSnapshot:
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
        carries the two live statuses as well as the id list, for that
        reason.
        """
        ...

    async def claim_pair(
        self, ticket_ids: Sequence[UUID], *, now: datetime
    ) -> Sequence[QueueTicket]:
        """Locks exactly the two tickets a scan selected, or takes neither
        — A64-015.3's atomic claim.

        **The second method that must be safe under concurrency**, and it
        reuses `claim_due`'s mechanism rather than inventing one:
        `SELECT ... FOR UPDATE SKIP LOCKED` over the two ids. A64-015.3 §7
        requires exactly this and forbids a distributed lock.

        Returns both tickets or **an empty sequence** — never one. A single
        locked ticket is not a claim on a pair, and returning it would hand
        the caller half a pairing to reason about. Whatever the loser
        skipped is still `waiting` and will be reconsidered by the next
        scan.

        `SKIP LOCKED` is what makes the loser *skip* rather than *wait*: two
        workers that both selected the same pair would otherwise serialise,
        and the second would then be holding a lock on tickets the first is
        about to reserve.

        The predicate is narrower than `claim_due`'s and every clause
        excludes a real row:

            id IN (...)          the two the engine chose
            status = 'waiting'   not already reserved by another worker,
                                 not cancelled, not expired, not matched
            expires_at > now     the window has not closed since the
                                 snapshot was taken

        **Claiming is not a transition**, exactly as it is not for the
        expiry sweep: the rows come back `waiting`, the lock lasts as long
        as the caller's transaction, and `reserve` below is what changes
        anything.
        """
        ...

    async def reserve(self, tickets: Sequence[QueueTicket]) -> bool:
        """Moves claimed tickets from `waiting` to `reserved`. All or
        nothing.

        Returns whether **every** ticket transitioned, and writes nothing
        when the answer is no. `False` means at least one row was no longer
        `waiting`, and the caller treats the whole pairing as lost — a
        half-reserved pair would leave one player invisible to every future
        scan with no match coming.

        All-or-nothing is the *statement's* property, not the caller's
        rollback — see `SqlAlchemyQueueRepository._transition` on the guard
        subquery that makes it so.

        Compare-and-set on `status = 'waiting'`, like `cancel`, and for the
        same reason: the row lock from `claim_pair` makes this safe within
        one transaction, and the predicate is what makes it safe if the two
        are ever separated.
        """
        ...

    async def release(self, tickets: Sequence[QueueTicket]) -> bool:
        """Returns reserved tickets to `waiting` — A64-015.3's compensation.

        Called when `game` refused the match after both tickets were
        reserved. Returns whether every one applied; `False` means somebody
        else resolved a reserved ticket, which is a state this system should
        not be able to reach and is therefore logged as an error rather than
        retried.

        **Writes `status` and `resolved_at` only.** `entered_at` is not in
        the statement at all, so a released player keeps the place in line
        they held — losing it to a failure that was the platform's would be
        a second penalty for the same fault.

        Compare-and-set on `status = 'reserved'`: a ticket the expiry sweep
        took because its window closed mid-pairing must not be resurrected
        into `waiting` past its own deadline.
        """
        ...

    async def complete(self, tickets: Sequence[QueueTicket], *, at: datetime) -> bool:
        """Moves reserved tickets to `matched`. All or nothing.

        Called **after** `game` has accepted the match request, never
        before — A64-015.3 §8. `at` is the instant the match was created.

        Returns whether every one applied. `False` after a successful match
        creation is the one genuinely bad outcome this module has: a match
        exists whose tickets do not say so. It is logged with the match id
        at `ERROR`, because the reconciliation is manual until A64-015.4
        gives a match a durable link back to its tickets.

        Compare-and-set on `status = 'reserved'`, so a ticket that was never
        reserved cannot be marked matched by a stray call.
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


class RecentOpponentProvider(Protocol):
    """Who each of these players has just finished a game against —
    A64-015.3 §6.

    The rematch guard. Being handed the same opponent twice in a row is the
    most common complaint about a thin pool, and the exclusion is cheap to
    apply and awkward to retrofit into a scan that was not designed for it
    — which is why the port exists now and the implementation does not.

    ## Why this is declared here and not imported from `game.public`

    AD-06: a port is declared by the layer that needs it. When `game` gains
    durable match history this is satisfied by `game.public` and no use
    case, no engine and no test changes — exactly the path
    `RatingSnapshotProvider` is already on. Declaring it there first would
    mean `game` publishing a read for a consumer that had not asked.

    **Deferred, and stated rather than hidden.** `game` has a `Match`
    aggregate and no repository, no table and no migration for one (see
    `game.public.UnavailableMatchCreation`), so there is no match history
    to read. `NoRecentOpponents` is the implementation until there is, and
    it excludes nothing — which is the safe direction: the failure mode is
    an occasional rematch, not a player who cannot be paired at all.
    """

    async def recent_opponents_among(
        self, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, frozenset[UUID]]:
        """For each of `player_ids`, which **others in the same batch** they
        have just played.

        The same shape as `friends.public.PairingExclusions`, deliberately:
        both are "pairs to veto", `PairExclusions.merged` unions them, and
        one shape means the engine asks one question rather than two.

        Batch and symmetric, for the same reasons — a per-candidate form
        would be an N+1 inside a scan that runs continuously, and "they
        played me" and "I played them" are the same game.

        Never raises. An unreadable history must degrade to "no exclusions"
        rather than stop pairing: a rematch is a disappointment, and an
        empty pool is an outage.
        """
        ...
