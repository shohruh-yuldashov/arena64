"""`TerminalStateEvaluator` — is this position the end of the game?

Framework-free and dependency-free (AD-13), and **pure in the strong
sense**: it looks at one position and nothing else. No history, no clock,
no move count.

## Two ways to lose, and only two

A side loses when it has **no pieces** or **no legal moves**. Both are
properties of the position alone, which is why they live here.

Everything else that ends a draughts game is a property of the *game*:
threefold repetition needs the position history, the move-limit draws need
a counter, a flag fall needs a clock, an abandonment needs a connection.
domain-model.md MT-12 states the split — "terminal detection consults game
**history**, not just the position" — and this is the half that does not.
`Match` is the half that does.

**So this evaluator can never report a draw.** Every draw in draughts is
historical. `TerminalState` therefore always names a winner, and that is a
guarantee rather than an omission: a caller does not have to handle a
terminal state with nobody winning, because this cannot produce one.

## No legal moves is now a sound signal

`MoveGenerator.legal_moves` returning empty means the side to move has
nothing to play — unconditionally, since A64-014.5. Between A64-014.3 and
A64-014.5 it could also have meant "this build cannot evaluate a king", and
an evaluator built on it then would have declared a loss for a player who
had moves the engine could not see. That is why terminal detection waited
for kings.

The rule that follows is absolute: **this module does not decide whether a
side can move.** It asks the generator. Any second implementation of "can
this side move" would eventually disagree with the first, and the
disagreement would take the form of a game ending that should not have.

## Why the material check comes first

A side with no pieces also has no moves, so both checks fire and either
would give the right winner. The reason is asked first because
`ALL_PIECES_CAPTURED` is the more specific answer, and the reason is what a
player reads afterwards: "you ran out of pieces" and "you were blocked in"
are different games.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.modules.engine.move_generation import MoveGenerator
from app.modules.engine.piece import PlayerSide
from app.modules.engine.position import Position


class TerminalReason(StrEnum):
    """Why a *position* is the end of the game.

    Deliberately smaller than `game`'s `TerminationReason`, which also
    covers flags, abandonment, adjudication and abort. Those are facts
    about a match; these two are facts about a board, and the engine can
    only know the second kind. The values match their counterparts there
    exactly, so mapping one to the other is not a translation table with
    somewhere to go wrong.
    """

    ALL_PIECES_CAPTURED = "all_pieces_captured"
    """The side to move has nothing left on the board."""

    NO_LEGAL_MOVES = "no_legal_moves"
    """It has pieces and cannot move any of them — blocked in, which in
    draughts is a loss rather than the stalemate draw of chess."""


@dataclass(frozen=True, slots=True)
class TerminalState:
    """A finished position: who won, and why.

    There is no `is_terminal` flag and no "nobody won" case. A
    `TerminalState` exists only for a position that has ended, and
    `TerminalStateEvaluator.evaluate` answers `None` for one that has not
    — a legitimately absent thing modelled in the return type rather than
    as a sentinel somebody forgets to check (CLAUDE.md §9.8, and DM-08
    makes the same call about `MatchResult`).
    """

    winner: PlayerSide
    """The side that did **not** run out of pieces or moves."""

    reason: TerminalReason


class TerminalStateEvaluator:
    """Decides whether a position has ended, by asking the generator."""

    def __init__(self, move_generator: MoveGenerator) -> None:
        self._move_generator = move_generator

    def evaluate(self, position: Position) -> TerminalState | None:
        """The outcome of `position`, or `None` if the game continues.

        Draws are never reported — see the module docstring. A caller that
        needs them is asking a question about the game's history, which is
        `Match`'s to answer (A64-014.7).
        """
        mover = position.side_to_move
        if position.board.piece_count_for(mover) == 0:
            return TerminalState(winner=mover.opponent(), reason=TerminalReason.ALL_PIECES_CAPTURED)
        if not self._move_generator.legal_moves(position):
            return TerminalState(winner=mover.opponent(), reason=TerminalReason.NO_LEGAL_MOVES)
        return None


__all__ = ["TerminalReason", "TerminalState", "TerminalStateEvaluator"]
