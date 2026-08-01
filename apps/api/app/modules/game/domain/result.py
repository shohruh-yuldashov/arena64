"""`MatchResult` and the vocabulary a finished match is described with.

Framework-free (architecture.md §8). No clock, no I/O.

domain-model.md DM-08 defines the type and both of its non-obvious
properties:

> `MatchResult` = outcome (win-A / win-B / draw / none) + termination
> reason + the seat that won.
>
> **Why the two parts are inseparable:** "Black won" and "Black won
> because White's flag fell with insufficient material to convert" are
> different facts to a player disputing a game, and the second is the one
> that ends the dispute.
>
> **Why absence rather than a "pending" value:** a sentinel result invites
> code that forgets to check for it, and the first place that forgets is
> whatever computes ratings.

So there is no `MatchOutcome.PENDING`, and `Match.result` is `None` until
the match ends.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.modules.engine import PlayerSide


class TerminationReason(StrEnum):
    """Why a match ended — domain-model.md §15, populated in full.

    **Every member is here on purpose, including the eight nothing can
    produce yet.** That is R-19, and it is one of the few places this
    codebase deliberately writes code ahead of its callers: "Make
    `TerminationReason` a closed enumeration and populate it fully from
    §15.3 now — adding 'abandonment' later, after months of games were
    recorded as 'resignation', makes every historical statistic wrong and
    unfixable."

    The cost of the extra members is a docstring each. The cost of adding
    them later is every match already recorded under the wrong one.

    A64-014.6 can produce four: the two the engine derives from a position
    plus resignation and abort.
    """

    NO_LEGAL_MOVES = "no_legal_moves"
    """The side to move was blocked in. A loss in draughts, not a draw."""

    ALL_PIECES_CAPTURED = "all_pieces_captured"
    """The side to move had nothing left."""

    RESIGNATION = "resignation"
    """A player gave up. Distinct from abandonment, and the reason R-19
    argues for the full enumeration: conflating the two makes "resigned"
    a statistic nobody can trust."""

    ABORT = "abort"
    """The match ended with no result and no rating effect — MT-11. Not a
    draw: a draw is an outcome two players played to, an abort is a match
    that did not happen."""

    AGREED_DRAW = "agreed_draw"
    """Both players accepted a draw offer. Needs `Offer`, deferred."""

    REPETITION = "repetition"
    """The same position occurred often enough to end the game. Needs the
    history `Match` already records — A64-014.7."""

    MOVE_LIMIT = "move_limit"
    """A move-count rule expired. Needs `plies_since_progress`, which
    `Match` already counts — A64-014.7."""

    FLAG = "flag"
    """A clock ran out. Needs clocks."""

    FLAG_INSUFFICIENT_MATERIAL = "flag_insufficient_material"
    """A clock ran out against material that could not have forced a win,
    which scores as a draw (system-design.md §3). Needs clocks and a
    material adjudication."""

    ABANDONMENT = "abandonment"
    """Both players became unreachable. Needs presence."""

    ADJUDICATION = "adjudication"
    """A moderator decided it — MT-10's sole exception to the permanence
    of a completed match, and itself recorded."""


class MatchOutcome(StrEnum):
    """What the result *was*, independent of why — DM-08's first component."""

    WIN = "win"
    """One side won; `MatchResult.winner` names it."""

    DRAW = "draw"
    """Both sides scored equally. Nothing produces one yet — every draw in
    draughts is historical, and A64-014.7 owns the rules that find them."""

    NONE = "none"
    """No result at all. An aborted match, which MT-11 keeps out of every
    rating and statistic — distinct from a draw, which counts."""


@dataclass(frozen=True, slots=True)
class MatchResult:
    """How a match ended: the outcome, the winner if there is one, and why.

    Frozen. A result is a fact about a finished game, and MT-10 makes a
    completed match immutable except by an adjudication that replaces the
    whole thing.

    The `__post_init__` check is what stops the contradictions DM-08's two
    inseparable parts invite: a win with nobody winning, or a draw that
    also names a winner. Both are unrepresentable rather than merely
    discouraged (CLAUDE.md §2.4).
    """

    outcome: MatchOutcome
    reason: TerminationReason
    winner: PlayerSide | None = None

    def __post_init__(self) -> None:
        if self.outcome is MatchOutcome.WIN and self.winner is None:
            raise ValueError("A win names the side that won.")
        if self.outcome is not MatchOutcome.WIN and self.winner is not None:
            raise ValueError(f"A {self.outcome.value} result has no winner.")

    def __str__(self) -> str:
        if self.winner is None:
            return f"{self.outcome.value} ({self.reason.value})"
        return f"{self.winner.value} wins ({self.reason.value})"


__all__ = ["MatchOutcome", "MatchResult", "TerminationReason"]
