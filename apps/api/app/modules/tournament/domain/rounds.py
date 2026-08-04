"""`TournamentRound` — SPEC-TOURNAMENT §5, §6.

One layer of the bracket, and the rule that a published one does not change.

## Why publication is a separate state from starting

    PENDING ──publish──> PUBLISHED ──start──> IN_PROGRESS ──complete──> COMPLETED

Publishing is when players can *read* their pairing; starting is when the
matches exist. Collapsing them would mean either that a pairing appears only
once its match has been created — so nobody can prepare — or that a pairing
somebody read can still change, which §6 forbids.

The gap between the two is also where an operator looks at a bracket before
committing to it. That is the one moment a correction is legitimate, and
after publication it is not.

## Completed rounds do not reopen

v0.x has no bracket correction (SPEC-TOURNAMENT OQ-1 leaves moderation to
the Administration epic). A round that reopened would invalidate the
advancement already computed from it, and a tournament is a permanent
record.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from app.modules.tournament.domain.exceptions import (
    InvalidRoundNumber,
    InvalidTournamentTransition,
    PublishedRoundIsImmutable,
)

#: Rounds are numbered from one. Zero would make "the first round" ambiguous
#: between the value and the ordinal — the same reason ply numbers start at
#: one (MT-5).
FIRST_ROUND: Final = 1


class RoundStatus(StrEnum):
    PENDING = "pending"
    """Pairings exist and are still changeable."""

    PUBLISHED = "published"
    """Players can read them. **Immutable from here on** — §6."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


_ALLOWED: Final[dict[RoundStatus, frozenset[RoundStatus]]] = {
    RoundStatus.PENDING: frozenset({RoundStatus.PUBLISHED}),
    RoundStatus.PUBLISHED: frozenset({RoundStatus.IN_PROGRESS}),
    RoundStatus.IN_PROGRESS: frozenset({RoundStatus.COMPLETED}),
    RoundStatus.COMPLETED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class TournamentRound:
    """One round of one tournament."""

    tournament_id: UUID
    round_number: int
    status: RoundStatus = RoundStatus.PENDING

    published_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.round_number < FIRST_ROUND:
            raise InvalidRoundNumber(
                f"round numbers start at {FIRST_ROUND}, got {self.round_number}"
            )

    @property
    def is_mutable(self) -> bool:
        """Whether this round's pairings may still change — §6.

        A property rather than a status comparison at every call site, so
        the rule has one spelling and a fifth status cannot leave two places
        disagreeing about it.
        """
        return self.status is RoundStatus.PENDING

    @property
    def is_active(self) -> bool:
        """Whether this is the round currently being played.

        The invariant "only one active round" is the *tournament's* to
        enforce across its rounds, not this entity's — a round cannot see
        its siblings. This is the predicate that check will use.
        """
        return self.status in (RoundStatus.PUBLISHED, RoundStatus.IN_PROGRESS)

    def published(self, at: datetime) -> "TournamentRound":
        """Fixes the pairings and makes them readable."""
        self._require_move_to(RoundStatus.PUBLISHED)
        return replace(self, status=RoundStatus.PUBLISHED, published_at=at)

    def started(self, at: datetime) -> "TournamentRound":
        self._require_move_to(RoundStatus.IN_PROGRESS)
        return replace(self, status=RoundStatus.IN_PROGRESS, started_at=at)

    def completed(self, at: datetime) -> "TournamentRound":
        self._require_move_to(RoundStatus.COMPLETED)
        return replace(self, status=RoundStatus.COMPLETED, completed_at=at)

    def require_mutable(self) -> None:
        """Raises unless the pairings may still change — §6.

        Called by whatever writes pairings, which is A64-019.3's. Here
        rather than there because the rule belongs to the round: a second
        writer added later inherits it instead of having to remember it.
        """
        if not self.is_mutable:
            raise PublishedRoundIsImmutable(
                f"round {self.round_number} is {self.status.value} and its "
                "pairings can no longer change"
            )

    def _require_move_to(self, status: RoundStatus) -> None:
        if status not in _ALLOWED[self.status]:
            raise InvalidTournamentTransition(
                f"a round cannot move from {self.status.value} to {status.value}"
            )


__all__ = ["FIRST_ROUND", "RoundStatus", "TournamentRound"]
