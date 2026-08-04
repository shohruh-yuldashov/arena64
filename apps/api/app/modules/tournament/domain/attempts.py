"""Match attempts and the advancement they decide — SPEC-TOURNAMENT §6c.

Single elimination needs one winner per pairing, and this platform's games
can draw: threefold repetition is live on the only variant
(`engine/variant.py` uses `THREEFOLD_REPETITION_ONLY`). The v0.x policy is a
**bounded rematch**.

    attempt 1 decisive   the winner advances
    attempt 1 drawn      one rematch, sides swapped
    attempt 2 decisive   the winner advances
    attempt 2 drawn      the higher seed advances, by adjudication

Bounded at two, and the bound is the point: an unbounded rematch chain is a
tournament that can never finish, and nothing would force one — every match
on this platform is untimed today (`specs/rating.md` §8).

## Why the higher seed rather than a third game or a coin

A third game repeats the question that twice failed to answer it. A random
winner is a permanent competitive record decided by chance. Manual
adjudication needs an `admin` module that does not exist, and until it did
the tournament would be frozen.

The higher seed is the one answer already earned: it is the rating the field
was seeded on, recorded before anyone played. Stated as a **v0.x** policy
because a dedicated tie-break — a faster rematch under a real time control —
is the better answer once `reference.time_control` exists.

## The adjudicated advancement is not a game

It creates no third match and therefore no rating adjustment. Both *drawn*
games were ordinary rated draws and moved ratings normally; the bracket
decision on top of them is a tournament fact, not a competitive result. That
is why `specs/rating.md`'s termination allowlist is untouched.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from app.modules.tournament.domain.exceptions import InvalidBracketPosition

#: Attempts are numbered from 1, like rounds and plies.
FIRST_ATTEMPT: Final = 1

#: The bound. A third attempt is refused rather than discouraged.
MAX_ATTEMPTS: Final = 2


class AttemptStatus(StrEnum):
    CREATED = "created"
    """A `game` match exists; nothing has come back yet."""

    COMPLETED = "completed"


class AttemptOutcome(StrEnum):
    """What an attempt settled, in this module's vocabulary.

    Deliberately **not** `game.public.MatchOutcome`: a tournament cares only
    whether the node was decided, and translating at the boundary keeps a
    `game` enum out of the bracket's own record.
    """

    DECISIVE = "decisive"
    DRAW = "draw"


class AdvancementReason(StrEnum):
    PLAYED = "played"
    """They won a game."""

    BYE = "bye"
    """No opponent — A64-019.4 §7."""

    ADJUDICATION = "adjudication"
    """Two draws; the higher seed advances. **No rating effect.**"""


@dataclass(frozen=True, slots=True)
class PairingAttempt:
    """One `game` match played for one pairing.

    A relation rather than a list in a column: two attempts are two rows, so
    `unique (pairing_id, attempt_number)` and `unique match_id` do the work
    application code would otherwise have to remember.
    """

    id: UUID
    pairing_id: UUID
    attempt_number: int

    match_id: UUID
    light_player_id: UUID
    dark_player_id: UUID

    status: AttemptStatus = AttemptStatus.CREATED
    outcome: AttemptOutcome | None = None
    winner_id: UUID | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not FIRST_ATTEMPT <= self.attempt_number <= MAX_ATTEMPTS:
            raise InvalidBracketPosition(
                f"attempt numbers run from {FIRST_ATTEMPT} to {MAX_ATTEMPTS}, "
                f"got {self.attempt_number}"
            )

    @property
    def is_final_attempt(self) -> bool:
        return self.attempt_number >= MAX_ATTEMPTS


@dataclass(frozen=True, slots=True)
class Advancement:
    """What one completed attempt decided for its node.

    Returned rather than applied, so the decision is a value a test can
    inspect and a caller cannot half-perform: either a player advances, or a
    rematch is due, or neither.
    """

    winner_id: UUID | None
    reason: AdvancementReason | None
    rematch_due: bool

    @property
    def decided(self) -> bool:
        return self.winner_id is not None


def decide(
    attempt: PairingAttempt,
    *,
    outcome: AttemptOutcome,
    winner_id: UUID | None,
    higher_seed_player_id: UUID,
) -> Advancement:
    """What this attempt's result means for the bracket.

    Pure, and the whole policy in one function: a caller cannot reach a
    different conclusion by taking branches in a different order.

    `higher_seed_player_id` is **passed**, not looked up, because seeding is
    persisted (A64-019.3 §4) — an adjudication must use the seed the
    tournament was built on, never a rating read at the moment of the
    decision.
    """
    if outcome is AttemptOutcome.DECISIVE:
        if winner_id is None:
            raise InvalidBracketPosition("a decisive attempt must name a winner")
        return Advancement(winner_id=winner_id, reason=AdvancementReason.PLAYED, rematch_due=False)

    if not attempt.is_final_attempt:
        # One rematch, sides swapped. Nobody advances yet.
        return Advancement(winner_id=None, reason=None, rematch_due=True)

    # Two draws: the higher seed advances. No third match, so no rating
    # adjustment — see this module's docstring.
    return Advancement(
        winner_id=higher_seed_player_id,
        reason=AdvancementReason.ADJUDICATION,
        rematch_due=False,
    )


def rematch_seats(attempt: PairingAttempt) -> tuple[UUID, UUID]:
    """The rematch's `(light, dark)` — the first attempt's, swapped.

    Swapped rather than repeated: the first attempt's sides came from the
    bracket's alternating rule, and repeating them would give one player the
    first move in both games of a tie.
    """
    return (attempt.dark_player_id, attempt.light_player_id)


__all__ = [
    "FIRST_ATTEMPT",
    "MAX_ATTEMPTS",
    "Advancement",
    "AdvancementReason",
    "AttemptOutcome",
    "AttemptStatus",
    "PairingAttempt",
    "decide",
    "rematch_seats",
]
