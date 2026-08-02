"""The ports `game`'s use cases declare — AD-06 puts them in the layer that
*needs* them, so a service depends on a contract and never on
`SqlAlchemyMatchRecordRepository`.

One protocol, because there is one aggregate with durable storage. The
split that matters here is not between repositories but between this and
the three published in `game.public`: those state what *other modules* may
ask, this states what this module's own services need from a table. A
consumer holding `PairingReconciliationReader` can learn whether a ticket
produced a match; only something holding this can write one.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.game.domain.match_record import MatchRecord


class MatchRecordRepository(Protocol):
    """Storage for `MatchRecord` — one repository per aggregate root.

    Every method is designed for **more than one worker**, because the
    deployment AD-02 describes is several processes against one table.
    `create` and `claim_overdue` are the two where that is not free; the
    rest are keyed on a primary key or a partial index and are safe by
    construction.
    """

    async def create(self, record: MatchRecord) -> tuple[MatchRecord, bool]:
        """Writes a new match, or returns the one that already exists for
        its `pairing_id`.

        Returns the stored record and whether **this call** created it —
        the observable half of the idempotency contract
        `game.public.MatchCreationUseCase` states.

        **Idempotency is the unique index, not a check.** A64-015.4 §3
        forbids check-then-insert: two workers retrying one pairing would
        both read no row and both insert. This inserts, lets
        `uq_match__pairing_id` refuse the loser, and re-reads by
        `pairing_id` — so the race resolves in the database, deterministically,
        and both callers come away with the same `match_id`.

        Flushes, never commits (repositories.md §5.1): the caller's unit of
        work spans the match and the `match.created` outbox row, because an
        event for a match that rolled back is a notification about a game
        nobody has.
        """
        ...

    async def by_pairing(self, pairing_id: UUID) -> MatchRecord | None:
        """The match created for a pairing, or `None`.

        Served by `uq_match__pairing_id`, which is why the constraint that
        enforces idempotency is also the index that answers the question
        idempotency is about.
        """
        ...

    async def lock(self, match_id: UUID) -> MatchRecord | None:
        """The match, with its row locked for the caller's transaction.

        `SELECT ... FOR UPDATE` — **not** `SKIP LOCKED**, and the
        difference from every other claim on this platform is the point. A
        sweeper skipping a locked row moves on to another one; two players
        accepting the same match at the same instant have nowhere else to
        go, so the second must *wait* and then see what the first wrote.
        Skipping would report "no such match" to a player who has one.

        `None` for a match that does not exist. The lock is released with
        the transaction, as every lock on this platform is.
        """
        ...

    async def pending_for(self, player_id: UUID) -> MatchRecord | None:
        """The match this player has been offered and not yet answered.

        At most one — see `MatchAcceptanceUseCase.pending_match` on why
        that needs no rule of its own. Served by the two partial indexes on
        `(player_id) WHERE status = 'pending_acceptance'`, so the read is
        bounded by concurrency rather than by history.
        """
        ...

    async def settle(self, record: MatchRecord) -> bool:
        """Writes an accepted, declined or expired match, **only if the row
        still says what it said when it was read**.

        Returns whether it applied. A compare-and-set on the fields that
        may have moved — `status` and the two `accepted_at` instants — for
        the reason every write on `queue_ticket` carries one: a blind
        `UPDATE` would let a decline overwrite an activation, or a
        reconciler's expiry overwrite an acceptance that arrived a
        millisecond earlier.

        Redundant while the caller holds `lock`'s row lock, and checked
        anyway: the day the two are separated, a silent last-write-wins is
        a match two people accepted being recorded as expired.
        """
        ...

    async def claim_overdue(self, *, now: datetime, limit: int) -> Sequence[MatchRecord]:
        """Takes up to `limit` pending matches whose window has closed,
        oldest deadline first, for this worker.

        **Safe under concurrency**, by the outbox's mechanism rather than a
        new one: `SELECT ... FOR UPDATE SKIP LOCKED`. Two reconcilers
        calling this simultaneously receive disjoint sets.

        The rows stay pending — claiming is not a transition. `settle`
        above is what resolves them, in the caller's transaction, so a
        worker that dies between the two leaves matches the next tick
        simply claims again.
        """
        ...

    async def settlements_for(self, ticket_ids: Sequence[UUID]) -> Sequence[MatchRecord]:
        """Every match created from one of these queue tickets.

        One statement for the batch. Serves
        `game.public.PairingReconciliationReader`, whose contract explains
        why the key is the ticket rather than the pairing.
        """
        ...

    async def latest_opponent_among(self, player_ids: Sequence[UUID]) -> Mapping[UUID, UUID]:
        """For each of these players that has one, the opponent of their
        **most recent settled match**.

        A projection rather than a sequence of records, and the shape is
        load-bearing: "whose latest match is this" is a question a list of
        matches cannot answer without re-deriving it, and re-deriving it
        gets the tie wrong — a match that is player A's latest is not
        necessarily player B's.

        One statement, `DISTINCT ON` — see
        `SqlAlchemyMatchRecordRepository.latest_opponent_among`. Serves
        `game.public.RecentOpponentReader`, whose contract records what
        "recent" means today and what it will mean once a match can
        complete.
        """
        ...


__all__ = ["MatchRecordRepository"]
