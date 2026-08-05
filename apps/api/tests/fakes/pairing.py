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
    ProductVariant,
)
from app.modules.rating.public import RatingSnapshot, SpeedClass


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
        #: A64-015.5. Makes the read raise, so a consumer's "an unreadable
        #: block graph must not stop delivery" path is exercised rather
        #: than asserted.
        self.fails = False

    def block(self, one: UUID, other: UUID) -> None:
        """Records a block. Stored one-directionally, exactly as the table
        does, so a service that only checked one direction would still
        pair the pair."""
        self.blocked.setdefault(one, set()).add(other)

    async def blocked_pairs_among(
        self, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, frozenset[UUID]]:
        self.calls.append(tuple(player_ids))
        if self.fails:
            raise RuntimeError("the social graph is unreachable")
        batch = set(player_ids)
        return {
            player_id: frozenset(others & batch)
            for player_id, others in self.blocked.items()
            if player_id in batch and others & batch
        }


class StubRecentOpponents:
    """A `RecentOpponentProvider` a test dictates the answers of.

    The port `GameRecentOpponents` satisfies in production since A64-015.4.
    A stub here rather than the real reader for the same reason `game`'s
    match creation is stubbed: what this file's suite is about is what
    `PairingService` does with an answer, not how `game` arrives at one —
    which is `tests/unit/test_recent_opponents.py`'s.
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

    A `game` that declines — a variant withdrawn mid-flight, a player
    already in a live match. What is under test is A64-015.3 §10's
    compensation, not which refusal fired, so the name says the behaviour
    rather than the cause.
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


class StubRatings:
    """A `RatingSnapshotProvider` that answers with one triple.

    The seat snapshot's *contents* are `test_rating_persistence.py`'s and
    the arithmetic is `test_glicko2.py`'s. What the pairing tests need is
    only that a snapshot reaches the creation request, so this returns a
    fixed one rather than modelling a rating store.
    """

    def __init__(self, snapshot: RatingSnapshot | None = None) -> None:
        self.snapshot = snapshot if snapshot is not None else RatingSnapshot.unrated()
        self.asked: list[UUID] = []
        #: The `(variant, speed class)` each read was keyed by — A64-020.5A-pre
        #: §15. Recorded rather than ignored because "the seat was rated in
        #: the ladder the players chose" is a property nothing else can see.
        self.keys: list[tuple[ProductVariant, SpeedClass]] = []

    async def rating_for(
        self, player_id: UUID, *, variant: ProductVariant, speed_class: SpeedClass
    ) -> RatingSnapshot:
        self.asked.append(player_id)
        self.keys.append((variant, speed_class))
        return self.snapshot
