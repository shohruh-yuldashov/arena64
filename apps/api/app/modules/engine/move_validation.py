"""`MoveValidator` — is this move legal in this position?

Framework-free and dependency-free (AD-13).

## One rule, one implementation

The validator holds **no rules**. It asks `MoveGenerator` what may be
played and checks membership:

    move in move_generator.legal_moves(position)

That is the whole of it, and the restraint is the design. The obvious
alternative — a validator that checks the piece belongs to the side to
move, that the step is diagonal and forward, that a capture was available —
is a second implementation of the rules of draughts, and two
implementations of one rule set disagree. Not in principle: in the specific
way that matters here, where the generator offers a move the validator then
refuses, or worse, the validator accepts a move the generator never offered
and `game` applies it to the board.

architecture.md AD-13 explains why that would be expensive to discover: "a
move generator bug does not produce a crash — it produces a *plausible but
illegal* game that is rated, ranked, and permanently recorded."

So mandatory capture, promotion geometry, forward direction, whose turn it
is, and every rule added later are enforced here by construction, without
this module knowing any of them.

## What it costs

Generating the full move set to check one move is more work than checking
that one move directly, and for a validator called once per ply against a
board of at most 50 squares it is not work anybody will notice. If a
profile ever says otherwise (CLAUDE.md §10.1), the lever is a generator
that can answer for a single origin square — not a second copy of the rules
in here.

## Equality is exact, including promotion

`Move` compares by path, captured squares *and* `promotes_to`, so a move
that omits the promotion the rules require, or claims one they do not, is
simply not in the set and is refused. That makes the promotion field part
of a move's identity rather than advisory metadata, and it means `game`
should echo the generated move back rather than reconstruct one from a
client's from/to.
"""

from app.modules.engine.exceptions import IllegalMove
from app.modules.engine.move import Move
from app.modules.engine.move_generation import MoveGenerator
from app.modules.engine.position import Position


class MoveValidator:
    """Answers whether a move may be played, by asking the generator."""

    def __init__(self, move_generator: MoveGenerator) -> None:
        self._move_generator = move_generator

    def is_legal(self, position: Position, move: Move) -> bool:
        """Whether `move` is among the moves available in `position`.

        Total since A64-014.5: there is no position the generator declines
        to answer for, so this is always a `True` or a `False` about the
        rules rather than about the engine.
        """
        return move in self._move_generator.legal_moves(position)

    def validate(self, position: Position, move: Move) -> None:
        """Accept `move`, or raise `IllegalMove`.

        The counterpart to `is_legal` for callers whose next line assumes
        the move is playable — `MoveApplier` is the first. Two methods
        rather than one, because a caller offering a player a choice wants
        a boolean and a caller about to mutate wants a guarantee, and
        making the second one `if not is_legal(): raise` at every call site
        is how one of them eventually forgets.
        """
        if not self.is_legal(position, move):
            raise IllegalMove(f"{move} is not legal in this position.")


__all__ = ["MoveValidator"]
