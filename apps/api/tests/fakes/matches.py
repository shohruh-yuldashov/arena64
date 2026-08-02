"""In-memory stand-ins for `game`'s match storage and for the two published
reads `matchmaking` consumes — A64-015.4.

What is faked here is **storage**, never the thing under test.
`MatchAcceptanceService`, `PairingReconciliationService` and
`GameRecentOpponents` all run for real against these, so the lifecycle
guards, the transaction sequencing and the reconciler's decision table are
genuinely exercised.

## The two deliberate simplifications

`InMemoryMatchRecordRepository.lock` does not model `FOR UPDATE`, and
`claim_overdue` does not model `SKIP LOCKED`. Both properties belong to
PostgreSQL rather than to this code, and both are asserted where they can
be — `tests/contract/test_match_repository.py`, with two real sessions and
two real transactions — for the same reason `tests/fakes/queue_repository.py`
declines to reimplement the same thing.

What *is* modelled is the compare-and-set in `settle` and the uniqueness of
`pairing_id` in `create`, because those are the two storage behaviours the
services' correctness depends on: a fake that let a second match through
would leave idempotency untested on the path that actually enforces it, and
one that ignored the predicate would let a decline overwrite an activation.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID

from app.modules.game.domain.match_record import MatchRecord, MatchRecordStatus
from app.modules.game.public import PairingSettlement


class InMemoryMatchRecordRepository:
    """The `game.match` relation, as a dict.

    Records are stored as the frozen `MatchRecord` values the repository
    returns, and every transition replaces one — so a test holding a
    reference keeps seeing what it read, exactly as it would with the real
    adapter's mapped-and-detached values.
    """

    def __init__(self) -> None:
        self.matches: dict[UUID, MatchRecord] = {}

    async def create(self, record: MatchRecord) -> tuple[MatchRecord, bool]:
        """Refuses a second match for one pairing, as the unique index does.

        Returns the existing one instead of raising, which is the behaviour
        `SqlAlchemyMatchRecordRepository.create` produces by catching the
        `IntegrityError` and re-reading — the contract callers see.
        """
        existing = await self.by_pairing(record.pairing_id)
        if existing is not None:
            return existing, False

        self.matches[record.id] = record
        return record, True

    async def by_pairing(self, pairing_id: UUID) -> MatchRecord | None:
        for record in self.matches.values():
            if record.pairing_id == pairing_id:
                return record
        return None

    async def lock(self, match_id: UUID) -> MatchRecord | None:
        """The record, with no lock — see this module's docstring."""
        return self.matches.get(match_id)

    async def pending_for(self, player_id: UUID) -> MatchRecord | None:
        for record in self.matches.values():
            if record.status.is_pending and player_id in record.player_ids():
                return record
        return None

    async def settle(self, record: MatchRecord) -> bool:
        """Compare-and-set on `status = 'pending_acceptance'`, exactly as the
        real `UPDATE` carries it.

        Checked *before* anything is written, so a service that expected an
        applied write cannot observe a half-applied one here either.
        """
        stored = self.matches.get(record.id)
        if stored is None or not stored.status.is_pending:
            return False
        self.matches[record.id] = record
        return True

    async def claim_overdue(self, *, now: datetime, limit: int) -> Sequence[MatchRecord]:
        return sorted(
            (
                record
                for record in self.matches.values()
                if record.status.is_pending and record.acceptance_deadline <= now
            ),
            key=lambda record: (record.acceptance_deadline, record.id),
        )[:limit]

    async def settlements_for(self, ticket_ids: Sequence[UUID]) -> Sequence[MatchRecord]:
        wanted = set(ticket_ids)
        return [record for record in self.matches.values() if wanted & set(record.ticket_ids())]

    async def latest_opponent_among(self, player_ids: Sequence[UUID]) -> Mapping[UUID, UUID]:
        """Each player's most recent **settled** opponent.

        The `DISTINCT ON` the real adapter issues, written as a sort: a
        pending match is an offer nobody has answered, and treating it as a
        game already played would exclude a pair on the strength of a match
        that may be about to expire.
        """
        wanted = set(player_ids)
        latest: dict[UUID, tuple[datetime, UUID]] = {}
        for record in self.matches.values():
            if record.status is MatchRecordStatus.PENDING_ACCEPTANCE:
                continue
            for player_id, opponent_id in (
                (record.light.player_id, record.dark.player_id),
                (record.dark.player_id, record.light.player_id),
            ):
                if player_id not in wanted:
                    continue
                seen = latest.get(player_id)
                if seen is None or record.created_at > seen[0]:
                    latest[player_id] = (record.created_at, opponent_id)
        return {player_id: opponent_id for player_id, (_, opponent_id) in latest.items()}


class StubSettlements:
    """A `PairingReconciliationReader` a test dictates the answers of.

    Records every batch it was asked about, so a test can assert the read
    was **batched** — one call for the claim rather than one per ticket,
    which is the property §9's bounded batch is about and which no
    assertion on the result could catch.
    """

    def __init__(self) -> None:
        self.settled: dict[UUID, PairingSettlement] = {}
        self.calls: list[tuple[UUID, ...]] = []
        self.fails = False

    def record(self, ticket_id: UUID, settlement: PairingSettlement) -> None:
        self.settled[ticket_id] = settlement

    async def settlements_for(self, ticket_ids: Sequence[UUID]) -> Mapping[UUID, PairingSettlement]:
        self.calls.append(tuple(ticket_ids))
        if self.fails:
            # The one read with no safe default — see
            # `GamePairingSettlements.settlements_for` on why guessing in
            # either direction is worse than failing the tick.
            raise RuntimeError("the match table is unreachable")
        return {
            ticket_id: settlement
            for ticket_id, settlement in self.settled.items()
            if ticket_id in set(ticket_ids)
        }


class StubAcceptanceExpiry:
    """A `MatchAcceptanceExpiryUseCase` a test dictates the answers of."""

    def __init__(self, expired: Sequence[UUID] = ()) -> None:
        self.expired = list(expired)
        self.calls: list[int] = []
        self.fails = False

    async def expire_overdue(self, *, limit: int) -> Sequence[UUID]:
        self.calls.append(limit)
        if self.fails:
            raise RuntimeError("game is unreachable")
        return tuple(self.expired)


__all__ = [
    "InMemoryMatchRecordRepository",
    "StubAcceptanceExpiry",
    "StubSettlements",
]
