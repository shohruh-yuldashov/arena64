"""In-memory stand-ins for `PairingService`'s cross-module ports —
A64-015.3.

What is faked is **another module**, never the thing under test.
`PairingService` and `PairingEngine` run for real against these, so the
ordering, the widening window, the exclusion merge, the claim sequencing and
the compensation are all genuinely exercised.

`game`'s match creation is the interesting one: three implementations,
because the three outcomes `PairingService` has to distinguish are "created
it", "already had it" and "refused" — and getting the middle one wrong is
how one ticket pair becomes two matches.
"""

from collections.abc import Mapping, Sequence
from uuid import UUID

from app.core.identifiers import generate_uuid7
from app.modules.game.public import (
    CreateMatchRequest,
    CreateMatchResult,
    MatchCreationRefused,
)


class StubExclusions:
    """A `friends.public.PairingExclusions` a test dictates the answers of.

    Records every batch it was asked about, so a test can assert the read
    was **batched** — one call for a pool rather than one per candidate,
    which is the property §5 names and which no assertion on the *result*
    could catch.
    """

    def __init__(self) -> None:
        self.blocked: dict[UUID, set[UUID]] = {}
        self.calls: list[tuple[UUID, ...]] = []

    def block(self, one: UUID, other: UUID) -> None:
        """Records a block. Stored one-directionally, exactly as the table
        does, so a service that only checked one direction would still
        pair the pair."""
        self.blocked.setdefault(one, set()).add(other)

    async def blocked_pairs_among(
        self, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, frozenset[UUID]]:
        self.calls.append(tuple(player_ids))
        batch = set(player_ids)
        return {
            player_id: frozenset(others & batch)
            for player_id, others in self.blocked.items()
            if player_id in batch and others & batch
        }


class StubRecentOpponents:
    """A `RecentOpponentProvider` a test dictates the answers of.

    The port `NoRecentOpponents` leaves empty in production. A test that
    fills it is asserting the *seam* holds — that a non-empty answer really
    does veto a pairing — rather than asserting a rule nothing implements.
    """

    def __init__(self) -> None:
        self.recent: dict[UUID, set[UUID]] = {}
        self.calls: list[tuple[UUID, ...]] = []

    def played(self, one: UUID, other: UUID) -> None:
        self.recent.setdefault(one, set()).add(other)

    async def recent_opponents_among(
        self, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, frozenset[UUID]]:
        self.calls.append(tuple(player_ids))
        batch = set(player_ids)
        return {
            player_id: frozenset(others & batch)
            for player_id, others in self.recent.items()
            if player_id in batch and others & batch
        }


class RecordingMatchCreation:
    """A `MatchCreationUseCase` that creates matches and **is idempotent**.

    The only fake here that implements a rule rather than returning a
    dictated answer, and it has to: §11's contract is that a retry with the
    same `pairing_id` returns the same `match_id` with `created=False`, and
    a fake that minted a fresh id per call would let a broken service pass
    the retry test.

    Keyed on `pairing_id` alone — not on the tickets, not on the players —
    because that is the key the contract names.
    """

    def __init__(self) -> None:
        self.requests: list[CreateMatchRequest] = []
        self._by_pairing: dict[UUID, UUID] = {}

    async def create_match(self, request: CreateMatchRequest) -> CreateMatchResult:
        self.requests.append(request)

        existing = self._by_pairing.get(request.pairing_id)
        if existing is not None:
            return CreateMatchResult(
                match_id=existing, pairing_id=request.pairing_id, created=False
            )

        match_id = generate_uuid7()
        self._by_pairing[request.pairing_id] = match_id
        return CreateMatchResult(match_id=match_id, pairing_id=request.pairing_id, created=True)


class RefusingMatchCreation:
    """A `MatchCreationUseCase` that always refuses.

    `UnavailableMatchCreation`'s behaviour with a name a test reads
    correctly: what is under test is §10's compensation, not which refusal
    fired.
    """

    def __init__(self) -> None:
        self.requests: list[CreateMatchRequest] = []

    async def create_match(self, request: CreateMatchRequest) -> CreateMatchResult:
        self.requests.append(request)
        raise MatchCreationRefused("no match for you")


class ExplodingMatchCreation:
    """A `MatchCreationUseCase` that raises something that is *not* a
    refusal.

    An unreachable database, a bug in `game`. The compensation must be
    identical — two players are waiting either way — and this is what proves
    the service does not treat a fault as a reason to strand them.
    """

    async def create_match(self, request: CreateMatchRequest) -> CreateMatchResult:
        raise RuntimeError("the database is on fire")


__all__ = [
    "ExplodingMatchCreation",
    "RecordingMatchCreation",
    "RefusingMatchCreation",
    "StubExclusions",
    "StubRecentOpponents",
]
