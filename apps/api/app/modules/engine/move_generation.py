"""`MoveGenerator` — what may be played in a position.

Framework-free and dependency-free (AD-13): no I/O, no clock, no
randomness, no configuration read from anywhere but the position's own
geometry. The same `Position` produces the same tuple of moves on every
machine, in every process, forever — which is the property the conformance
corpus tests and the property a rated result rests on.

## Scope — A64-014.2 generates men's moves only

Present: quiet moves for men, single jumps for men, mandatory-capture
priority, promotion detection on arrival.

Absent: **kings**, and **capture sequences longer than one jump**. A king
belonging to the side to move contributes nothing to the returned tuple —
it is skipped, not refused — and a position where the only mobile piece is
a king therefore reports no moves at all. That is a scope boundary, not a
rules claim: until A64-014.5 this generator's answer is complete only for
positions without kings, and nothing should present it to a player before
then.

`_captures` returning single jumps is the other half. A64-014.4 replaces
the body of one private method with a recursive walk; the mandatory-capture
structure below, the ordering, and every signature stay as they are, which
is why the split is drawn there.

## Why captures are generated first rather than filtered afterwards

Mandatory capture is the rule the whole variant hangs off (architecture.md
A-1 names it as the reason the engine is complex at all). Generating quiet
moves and discarding them when a capture exists gets the same answer today
and the wrong shape tomorrow: under the maximum-capture obligation the
survivors must be compared against each other by length, and a pipeline
that has already mixed quiet moves into the pool has to re-identify which
were captures to do it.

So the flow is: ask for captures; if there are any and the variant obliges
them, that *is* the answer. Quiet moves are generated only when they can be
played.

## Why a class rather than a module function

It holds no state and could be a function. It is a class because it is the
collaborator `game` will be given — architecture.md R-2 lets exactly three
modules import the engine, and each of them wants a named, injectable
thing at its boundary rather than a bare import — and because AD-14's
TypeScript engine mirrors this surface. One instance is safe to share.
"""

from app.modules.engine.board import Board
from app.modules.engine.coordinate import BoardCoordinate, Direction
from app.modules.engine.move import Move
from app.modules.engine.piece import Piece, PieceRank, PlayerSide
from app.modules.engine.position import Position
from app.modules.engine.variant import BoardGeometry


class MoveGenerator:
    """Every move the side to move may play, in one deterministic order."""

    def legal_moves(self, position: Position) -> tuple[Move, ...]:
        """The moves available in `position`, ordered and immutable.

        Empty when the side to move has nothing to play, which under the
        full rules is a loss for that side — a conclusion this task does
        not draw, because it is also what an unimplemented king looks like
        from here. Terminal-state detection is A64-014.3's, and it needs a
        generator whose emptiness means what it says.
        """
        geometry = position.board.geometry
        captures = self._captures(position, geometry)
        if captures and geometry.capture_is_mandatory:
            return _ordered(captures)

        quiet = self._quiet_moves(position, geometry)
        return _ordered(captures + quiet)

    def _captures(self, position: Position, geometry: BoardGeometry) -> tuple[Move, ...]:
        """Every single jump available to the side to move.

        One jump, never a sequence: a man that lands beside a second
        opponent is obliged to continue in both configured variants, and
        this generator does not yet look. A64-014.4 turns this into the
        recursive walk, and the corpus is what will catch the difference.
        """
        moves: list[Move] = []
        for square, man in _men_to_move(position):
            for direction in geometry.man_capture_directions(position.side_to_move):
                jump = self._jump(position.board, geometry, square, man, direction)
                if jump is not None:
                    moves.append(jump)
        return tuple(moves)

    def _quiet_moves(self, position: Position, geometry: BoardGeometry) -> tuple[Move, ...]:
        """Every non-capturing move available to the side to move.

        Forward only, one square. A man never steps backward in any variant
        configured here, which is why `forward_directions` is asked
        unconditionally while captures consult `men_may_capture_backward`.
        """
        moves: list[Move] = []
        for square, man in _men_to_move(position):
            for direction in geometry.forward_directions(position.side_to_move):
                target = geometry.step(square, direction)
                if target is None or position.board.piece_at(target) is not None:
                    continue
                moves.append(
                    Move(
                        path=(square, target),
                        promotes_to=_promotion(geometry, man.side, target),
                    )
                )
        return tuple(moves)

    def _jump(
        self,
        board: Board,
        geometry: BoardGeometry,
        origin: BoardCoordinate,
        man: Piece,
        direction: Direction,
    ) -> Move | None:
        """The jump from `origin` along `direction`, or `None` if there is
        no opponent to take or nowhere to land."""
        over = geometry.step(origin, direction)
        if over is None:
            return None
        victim = board.piece_at(over)
        if victim is None or victim.side is man.side:
            return None

        landing = geometry.step(origin, direction, distance=2)
        if landing is None or board.piece_at(landing) is not None:
            return None

        return Move(
            path=(origin, landing),
            captured=(over,),
            promotes_to=_promotion(geometry, man.side, landing),
        )


def _men_to_move(position: Position) -> list[tuple[BoardCoordinate, Piece]]:
    """The side to move's men, in ascending square order.

    Sorted here as well as in `_ordered`, so that the walk itself is
    reproducible: a bug that made two moves compare equal would otherwise
    surface as an ordering that depended on how the board's mapping was
    built, which is the class of defect that reproduces on one machine
    only.

    Kings are excluded — see the module docstring.
    """
    return sorted(
        (
            (square, piece)
            for square, piece in position.board.occupied_squares.items()
            if piece.side is position.side_to_move and piece.rank is PieceRank.MAN
        ),
        key=lambda entry: entry[0],
    )


def _promotion(
    geometry: BoardGeometry, side: PlayerSide, destination: BoardCoordinate
) -> PieceRank | None:
    """`KING` when arriving at `destination` crowns a man of `side`.

    Correct for every move this task generates because each one is a
    complete move: a man that finishes on the crownhead is crowned. It stops
    being unconditional in A64-014.4, where a sequence may pass through the
    crownhead and continue, and `promotion_ends_ply` is the axis that
    decides what happens then. This function is where that decision lands.
    """
    if not geometry.is_promotion_square(side, destination):
        return None
    return PieceRank.KING


def _ordered(moves: tuple[Move, ...]) -> tuple[Move, ...]:
    """One total order over a move set — ascending `Move.sort_key`.

    Required, not a nicety. The corpus states expected moves as a sequence
    (AD-14), a replay reproduces a game by index, and a future search
    depends on identical siblings being visited in identical order; each of
    those breaks if the answer depends on how a dictionary happened to be
    laid out.
    """
    return tuple(sorted(moves, key=lambda move: move.sort_key))


__all__ = ["MoveGenerator"]
