"""The `Tournament` aggregate — SPEC-TOURNAMENT §3, §5.

Owns the **lifecycle** and nothing else. Not a match, not a rating, not a
bracket's contents: those belong to `game`, `rating`, and this module's own
`BracketNode` respectively, and an aggregate that reached into any of them
would be the coupling R-3 forbids.

## The state machine, and why it is a table rather than a chain of `if`s

    DRAFT ──open──> REGISTRATION_OPEN ──close──> REGISTRATION_CLOSED
                                                        │
                                                      start
                                                        v
                          COMPLETED <──complete── IN_PROGRESS

    any of the four above ──cancel──> CANCELLED

`_ALLOWED` is the whole rule. A transition added to the enum without an
entry fails at the call site rather than falling through to a permissive
`else`, and a reader sees every legal move in one place instead of
reconstructing it from scattered guards.

**`COMPLETED` and `CANCELLED` are terminal.** A completed tournament is a
permanent competitive record (A-4's class), and reopening one would make
the standings somebody read stop being the standings.

## Configuration is typed and frozen

SPEC-TOURNAMENT §4 asks for it, and the reason is the one DM-09 gives about
notation: a free-form dictionary is a contract nothing validates, and the
first field somebody misspells is discovered when a bracket is built rather
than when the tournament is created.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from app.modules.game.public import ProductVariant
from app.modules.rating.public import SpeedClass
from app.modules.tournament.domain.exceptions import (
    InvalidCapacity,
    InvalidTournamentTransition,
    UnsupportedTournamentFormat,
)

#: The smallest field that is a tournament rather than a single match.
MIN_CAPACITY: Final = 2

#: The largest field v0.x commits to — T-2. A product cap rather than an
#: arithmetic one: a bracket is a power of two and 128 is where this release
#: stops promising the operational behaviour holds.
MAX_CAPACITY: Final = 128


class TournamentFormat(StrEnum):
    """How a tournament pairs and advances.

    Every member exists from day one for the reason `MatchOrigin` and
    `TerminationReason` do (R-19): a format added after tournaments have
    been recorded makes every historical query about format wrong. What
    differs is which are *runnable* — see `SUPPORTED_FORMATS`.
    """

    SINGLE_ELIMINATION = "single_elimination"
    DOUBLE_ELIMINATION = "double_elimination"
    SWISS = "swiss"
    ROUND_ROBIN = "round_robin"
    ARENA = "arena"


#: What v0.x actually runs — SPEC-TOURNAMENT §2, T-1.
#:
#: A set rather than a comparison, so the day a second format ships it is
#: one line here beside the sentence explaining why it was not there.
SUPPORTED_FORMATS: Final = frozenset({TournamentFormat.SINGLE_ELIMINATION})


class TournamentStatus(StrEnum):
    """Where a tournament is in its life."""

    DRAFT = "draft"
    """Created, not yet advertised. Nothing may register."""

    REGISTRATION_OPEN = "registration_open"
    REGISTRATION_CLOSED = "registration_closed"
    """The field is fixed. The bracket is built from exactly these players."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (TournamentStatus.COMPLETED, TournamentStatus.CANCELLED)

    @property
    def is_published(self) -> bool:
        """Whether a viewer with no account may see a tournament in this
        state — A64-026.4 §43.2.

        Every state except `DRAFT`, and the enum member's own docstring is
        the reason: *"Created, not yet advertised. Nothing may register."*
        A tournament that has not been advertised is one whose operator has
        not decided it exists yet, and publishing it to the open web is a
        decision made on their behalf.

        Deliberately **not** the inverse of `is_terminal`. A completed
        tournament is the most useful thing on this platform to show
        somebody without an account — a finished bracket is a record of
        something that happened, and hiding it would answer "what happens
        here?" with silence, which is the argument the lobby already makes
        for showing finished tournaments to players.

        This narrows nothing for an authenticated player: it is applied on
        the anonymous path only, so the lobby a signed-in player has seen
        since A64-020.0B is unchanged, drafts included.
        """
        return self is not TournamentStatus.DRAFT


#: Every legal move, as data. See this module's docstring on why.
_ALLOWED: Final[dict[TournamentStatus, frozenset[TournamentStatus]]] = {
    TournamentStatus.DRAFT: frozenset(
        {TournamentStatus.REGISTRATION_OPEN, TournamentStatus.CANCELLED}
    ),
    TournamentStatus.REGISTRATION_OPEN: frozenset(
        {TournamentStatus.REGISTRATION_CLOSED, TournamentStatus.CANCELLED}
    ),
    TournamentStatus.REGISTRATION_CLOSED: frozenset(
        {TournamentStatus.IN_PROGRESS, TournamentStatus.CANCELLED}
    ),
    TournamentStatus.IN_PROGRESS: frozenset(
        {TournamentStatus.COMPLETED, TournamentStatus.CANCELLED}
    ),
    TournamentStatus.COMPLETED: frozenset(),
    TournamentStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class Tournament:
    """One tournament's identity, configuration and lifecycle.

    Frozen: every transition returns a new instance, so "what it was before"
    is a value the caller already holds rather than something to copy. The
    same argument `PlayerRating` makes.
    """

    id: UUID
    name: str
    format: TournamentFormat
    variant: ProductVariant
    speed_class: SpeedClass
    rated: bool
    capacity: int

    created_by: UUID | None
    """The administrator who created it, or `None` for a system tournament.

    Nullable rather than a sentinel id, because "the platform created this"
    is genuinely the absence of a person — and T-3 makes those the only two
    cases in v0.x. A player-created tournament waits for the Administration
    epic, at which point this stops being nullable in practice without a
    schema change.
    """

    created_at: datetime
    registration_deadline: datetime | None = None
    status: TournamentStatus = TournamentStatus.DRAFT

    started_at: datetime | None = None
    completed_at: datetime | None = None
    """When play began and when the result was recorded — A64-019.6 §9.

    Stored rather than derived from the rounds, because both are facts a
    public detail page renders and neither is answerable from a round once
    the bracket is pruned. `None` until the transition that sets it, so the
    pair reads as the lifecycle rather than as two nullable decorations.
    """

    def __post_init__(self) -> None:
        if self.format not in SUPPORTED_FORMATS:
            raise UnsupportedTournamentFormat(
                f"{self.format.value} is not run in this release; "
                f"supported: {sorted(f.value for f in SUPPORTED_FORMATS)}"
            )
        if not MIN_CAPACITY <= self.capacity <= MAX_CAPACITY:
            raise InvalidCapacity(
                f"capacity must be between {MIN_CAPACITY} and {MAX_CAPACITY}, got {self.capacity}"
            )

    @property
    def is_open_for_registration(self) -> bool:
        return self.status is TournamentStatus.REGISTRATION_OPEN

    def transitioned_to(
        self, status: TournamentStatus, *, at: datetime | None = None
    ) -> "Tournament":
        """This tournament in `status`, or a refusal.

        The single mutator. A caller cannot reach a state by assembling one
        by hand, which is what keeps `_ALLOWED` the whole rule rather than
        the rule most callers happen to follow.

        `at` stamps the instant the transition *names* — `started_at` on the
        move into play, `completed_at` on the move out of it — and is
        ignored for the transitions that name none. Set here rather than by
        the caller so the status and its instant are one write, and a
        tournament cannot say it finished without saying when.
        """
        if status not in _ALLOWED[self.status]:
            raise InvalidTournamentTransition(
                f"a tournament cannot move from {self.status.value} to {status.value}"
            )
        if at is None:
            return replace(self, status=status)
        if status is TournamentStatus.IN_PROGRESS:
            return replace(self, status=status, started_at=at)
        if status is TournamentStatus.COMPLETED:
            return replace(self, status=status, completed_at=at)
        return replace(self, status=status)


__all__ = [
    "MAX_CAPACITY",
    "MIN_CAPACITY",
    "SUPPORTED_FORMATS",
    "Tournament",
    "TournamentFormat",
    "TournamentStatus",
]
