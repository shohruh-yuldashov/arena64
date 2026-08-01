"""`Match` — the platform's central aggregate root (domain-model.md §10.4).

Framework-free (architecture.md §8): no ORM, no HTTP, no Redis. It imports
`engine`, which architecture.md R-2 permits for this module and R-3 makes
it the only module allowed to *mutate* through.

## What it owns, and what it deliberately does not

It owns the authoritative position, whose turn it is, the status, the
result, the ply number, and **the history the position cannot carry** —
how often each position has occurred, and how long since anything
irreversible happened. domain-model.md MT-12: "terminal detection consults
game **history**, not just the position." `TerminalStateEvaluator` is the
half that reads only the position; this is the half that remembers.

It does not own clocks, seats, the move log, offers, or the sequence
number. Those are the rest of §10.4 and belong to the tasks that build
transport and persistence — see the module docstring.

## Why the history lives here and not on `Position`

Because putting it there would destroy the thing it is for. A `Position`
carrying a ply number or a timestamp would be unique every time, so two
identical boards would never compare equal and the repetition rule would
never fire — the exact failure domain-model.md §10.1 gives as the reason
`Position` is a value object at all. The counter has the same problem in
reverse: it is a property of the path taken to a position, not of the
position, and two games can reach the same board with different counters.

## Mutable, unlike everything in `engine`

`Match` is an **entity**: it has identity and a lifecycle, and it is
loaded, changed and saved. That is the shape every other aggregate on this
platform has — see `friends.Friendship`, which also mutates in place and
returns `None` from its transitions — and it is what a repository expects.

The values it holds are all immutable, so nothing it exposes can be edited
behind its back: `Position` and `Move` are frozen, and `position_history`
hands out a read-only view.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from app.core.identifiers import generate_uuid7
from app.modules.engine import (
    CURRENT_ENGINE_VERSION,
    BoardVariant,
    EngineVersion,
    Move,
    MoveApplier,
    PieceRank,
    PlayerSide,
    Position,
    TerminalReason,
    TerminalState,
    TerminalStateEvaluator,
    initial_board,
)
from app.modules.game.domain.exceptions import InvalidMatchTransition
from app.modules.game.domain.result import MatchOutcome, MatchResult, TerminationReason


class MatchStatus(StrEnum):
    """Where a match is in its life — system-design.md §3, narrowed.

    That state machine has eleven states, because it also models pairing,
    infrastructure incidents, flag falls, abandonment and rating. These
    four are the ones a match reaches by the rules alone, and they are
    named as it names them.

    Two of the missing ones are worth knowing about now: `PAUSED` is what
    turns an infrastructure incident into a delay rather than a loss (T-2),
    and `FLAGGED` sits between `ACTIVE` and `COMPLETED` because a flag fall
    in draughts is not automatically a loss. Both need a clock.
    """

    CREATED = "created"
    """The match exists and no move has been played.

    Distinct from `ACTIVE` on purpose: "a match exists before either clock
    starts. Without this state, a player whose client crashes between
    pairing and load either loses time they never had a chance to use, or
    the opponent waits indefinitely."
    """

    ACTIVE = "active"
    """Under way. The only status in which a move may be played."""

    COMPLETED = "completed"
    """Finished with a result. Immutable from here, except by an `admin`
    adjudication that is itself recorded (MT-10)."""

    ABORTED = "aborted"
    """Ended with **no** result and no rating effect — MT-11. This is what
    the task brief calls cancellation; system-design.md §3 reserves
    `Cancelled` for a challenge or a queue ticket that never became a
    match at all, so a match that ends this way is aborted.
    """

    @property
    def is_final(self) -> bool:
        """Whether nothing further can happen to a match in this status."""
        return self in {MatchStatus.COMPLETED, MatchStatus.ABORTED}


@dataclass(slots=True)
class Match:
    """One complete contest, as far as the rules are concerned."""

    variant: BoardVariant
    engine_version: EngineVersion
    """The rules build this match is played under — AD-15, and immutable
    after creation by MT-3. Recorded so a 2027 replay of a 2025 game runs
    under the semantics it was actually played under."""

    position: Position
    """The authoritative position, including whose turn it is."""

    status: MatchStatus = MatchStatus.CREATED
    ply_number: int = 0
    """Plies played. MT-5 numbers them contiguously from 1, so this is the
    number of the ply just played and `0` means none has been."""

    last_move: Move | None = None
    result: MatchResult | None = None
    """Absent until the match ends — DM-08. A sentinel "pending" result
    invites the code that computes ratings to forget to check."""

    plies_since_progress: int = 0
    """Plies since anything irreversible happened — see `_is_progress`.

    The input to the move-limit draws (A64-014.7), counted here rather than
    derived later because it cannot be recovered from a position: two games
    reach the same board having done very different things to get there.
    """

    id: UUID = field(default_factory=generate_uuid7)
    """UUIDv7, application-generated (DB-07). Last so every other field can
    be passed positionally by a future repository's rehydration."""

    _position_counts: dict[Position, int] = field(default_factory=dict, repr=False)
    """How often each position has occurred, this match.

    A `dict` keyed by `Position`, which is exactly what that type was made
    hashable for. Private, and exposed read-only through
    `position_history`, so nothing outside can desynchronise it from the
    moves that produced it.
    """

    def __post_init__(self) -> None:
        if self.position.board.variant is not self.variant:
            # The two would silently disagree about which rules apply, and
            # the disagreement would surface as a legal move being refused.
            raise InvalidMatchTransition(
                f"A {self.variant.value} match cannot hold a "
                f"{self.position.board.variant.value} position."
            )
        if not self._position_counts:
            self._position_counts[self.position] = 1

    @classmethod
    def create(
        cls,
        variant: BoardVariant,
        *,
        engine_version: EngineVersion = CURRENT_ENGINE_VERSION,
        first_to_move: PlayerSide = PlayerSide.LIGHT,
    ) -> "Match":
        """A new match at the opening position, not yet started.

        The starting position is recorded in the history immediately: a
        repetition rule counts *occurrences*, and a game that returns to
        its opening has repeated it once, not reached it for the first
        time.
        """
        return cls(
            variant=variant,
            engine_version=engine_version,
            position=Position(board=initial_board(variant), side_to_move=first_to_move),
        )

    @property
    def side_to_move(self) -> PlayerSide:
        return self.position.side_to_move

    @property
    def termination_reason(self) -> TerminationReason | None:
        """Why the match ended, or `None` while it is running.

        Read through the result rather than stored beside it, because
        DM-08 makes the two "inseparable" — a stored copy is a second
        place for them to disagree about a game somebody is disputing.
        """
        return None if self.result is None else self.result.reason

    @property
    def position_history(self) -> Mapping[Position, int]:
        """Every position this match has held, and how often.

        A read-only view over the aggregate's own record, not a copy: the
        counts are the aggregate's to change, and a caller holding a
        snapshot that silently went stale is worse than one that cannot
        write.
        """
        return MappingProxyType(self._position_counts)

    def occurrences_of(self, position: Position) -> int:
        """How often `position` has occurred in this match."""
        return self._position_counts.get(position, 0)

    @property
    def current_position_occurrences(self) -> int:
        """How often the position now on the board has occurred.

        The number a repetition rule compares against a threshold — which
        this task deliberately does not set. A64-014.7 decides what counts
        as enough and what it means.
        """
        return self.occurrences_of(self.position)

    def start(self) -> None:
        """Move from `CREATED` to `ACTIVE`."""
        self._require(MatchStatus.CREATED, "start")
        self.status = MatchStatus.ACTIVE

    def play(self, move: Move, applier: MoveApplier, evaluator: TerminalStateEvaluator) -> None:
        """Play `move`, and finish the match if the position it reaches has
        ended.

        The two engine services arrive as arguments rather than as fields.
        They are stateless and shared, so holding them would put a
        collaborator inside an entity a repository has to rehydrate —
        every load would have to know how to rebuild them, and a match read
        from storage without them would be a half-object. Passing them
        keeps `Match` a pure record of a game.

        Nothing here re-derives a rule. `MoveApplier` validates and
        applies; `TerminalStateEvaluator` decides whether the result has
        ended; this method sequences them and remembers what happened.

        Raises `InvalidMatchTransition` if the match is not active, and
        whatever the engine raises for a move that is not legal — in which
        case nothing below has run and the match is untouched.
        """
        self._require(MatchStatus.ACTIVE, "play a move")

        moved = self._moving_rank(move)
        self.position = applier.apply(self.position, move)
        self.ply_number += 1
        self.last_move = move
        self._record(self.position)
        self.plies_since_progress = (
            0 if _is_progress(move, moved) else self.plies_since_progress + 1
        )

        terminal = evaluator.evaluate(self.position)
        if terminal is not None:
            self._complete(terminal)

    def resign(self, side: PlayerSide) -> None:
        """`side` gives up; the opponent wins.

        No board changes. A resignation is a statement about the players,
        not about the position, and a resigned game must still replay to
        the position it was abandoned in.

        The side is given explicitly. Who is allowed to resign for whom is
        an authorization question, and this layer has no notion of a
        caller.
        """
        self._require(MatchStatus.ACTIVE, "resign")
        self.status = MatchStatus.COMPLETED
        self.result = MatchResult(
            outcome=MatchOutcome.WIN,
            reason=TerminationReason.RESIGNATION,
            winner=side.opponent(),
        )

    def abort(self) -> None:
        """End the match with no result — MT-11.

        Permitted from `CREATED` and from `ACTIVE`; refused once the match
        has ended, because a completed match is a permanent record (MT-10)
        and an aborted one is already over.

        **Not a draw.** A draw is an outcome two players played to and it
        counts everywhere; an abort is a match that did not happen, and
        MT-11 keeps it out of every rating and statistic.
        """
        if self.status.is_final:
            raise InvalidMatchTransition(f"A {self.status.value} match cannot be aborted.")
        self.status = MatchStatus.ABORTED
        self.result = MatchResult(outcome=MatchOutcome.NONE, reason=TerminationReason.ABORT)

    def _complete(self, terminal: TerminalState) -> None:
        self.status = MatchStatus.COMPLETED
        self.result = MatchResult(
            outcome=MatchOutcome.WIN,
            reason=_TERMINATION_FOR[terminal.reason],
            winner=terminal.winner,
        )

    def _record(self, position: Position) -> None:
        self._position_counts[position] = self._position_counts.get(position, 0) + 1

    def _moving_rank(self, move: Move) -> PieceRank | None:
        """The rank of the piece standing on the move's origin, before it
        moves. `None` for an origin that is empty, which the applier is
        about to refuse."""
        piece = self.position.board.piece_at(move.origin)
        return None if piece is None else piece.rank

    def _require(self, status: MatchStatus, transition: str) -> None:
        if self.status is not status:
            raise InvalidMatchTransition(
                f"Cannot {transition}: the match is {self.status.value}, not {status.value}."
            )


def _is_progress(move: Move, moved: PieceRank | None) -> bool:
    """Whether `move` resets the counter behind the move-limit draws.

    Progress is a capture or a man's move — the two things that cannot be
    undone. Material only ever decreases, and a man only ever advances, so
    a game doing either is going somewhere; a game shuffling kings around
    an empty board is not, which is the whole reason the counter exists.

    A man's move that crowns still counts as a man's move: it began as one,
    and it is the advance that was irreversible.
    """
    return move.is_capture or moved is PieceRank.MAN


_TERMINATION_FOR: Mapping[TerminalReason, TerminationReason] = MappingProxyType(
    {
        TerminalReason.ALL_PIECES_CAPTURED: TerminationReason.ALL_PIECES_CAPTURED,
        TerminalReason.NO_LEGAL_MOVES: TerminationReason.NO_LEGAL_MOVES,
    }
)
"""The engine's reasons, in this module's vocabulary.

Two enums rather than one because they answer different questions: the
engine says why a *position* has ended, and can only ever know the two
things a board can tell it; `TerminationReason` says why a *match* ended,
and nine of its members are about clocks, connections and moderators. The
values coincide, so a test asserts the mapping is total rather than
trusting it.
"""


__all__ = ["Match", "MatchStatus"]
