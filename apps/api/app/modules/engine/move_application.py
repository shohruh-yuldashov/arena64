"""`MoveApplier` — the position a legal move produces.

Framework-free and dependency-free (AD-13). Pure: the same position and
move produce the same result every time, and neither argument is touched.

## The order, and why it is the order

    1. validate                    — through `MoveValidator`, never inline
    2. remove every captured square, in the order the move records
    3. relocate the moving piece from origin to destination
    4. crown it, if the move says it is crowned
    5. hand the turn to the opponent

**Victims come off before the attacker moves**, and that is not a stylistic
preference. `Board` knows nothing whatever about capture — deliberately,
since A64-014.1 — and refuses to place a piece on an occupied square, so
the board must already be in the state the relocation is legal in. Doing it
the other way round would mean either teaching `Board` about capture, which
would put a rule inside the placement primitive, or relaxing the
destination check, which would remove the protection every other caller
relies on.

It also matches the rule as players state it: you take the piece, then you
stand beyond it. A64-014.4's longer sequences remove more victims in step
two, and changed one thing in step three: see `_relocated`.

## Validation is not optional, and not a parameter

`apply` validates every time. There is no `apply_unchecked`, no
`validate=False`, and there should not be one: architecture.md AD-13
records that "a move generator bug does not produce a crash — it produces a
*plausible but illegal* game that is rated, ranked, and permanently
recorded", and an unchecked application path is the shortest route to
exactly that. A caller that has already validated pays one extra generation
per ply, which is nothing next to what the escape hatch would cost the
first time somebody used it in a hurry.

## No undo

Deliberately absent. Positions are immutable values, so "undo" is holding
the previous one — a caller that wants a stack keeps a list. An `undo` that
recomputed a prior position from a move would be a second implementation of
this one, and A64-013's apply/undo property test (AD-13) belongs with a
search that actually needs the memory saving, with a measurement behind it.
"""

from app.modules.engine.board import Board
from app.modules.engine.coordinate import BoardCoordinate
from app.modules.engine.exceptions import PieceNotFound
from app.modules.engine.move import Move
from app.modules.engine.move_validation import MoveValidator
from app.modules.engine.position import Position


class MoveApplier:
    """Turns a position and a legal move into the position that follows."""

    def __init__(self, validator: MoveValidator) -> None:
        self._validator = validator

    def apply(self, position: Position, move: Move) -> Position:
        """The position reached by playing `move`.

        Raises `IllegalMove` if the move is not playable here, and
        `UnsupportedPieceMovement` if the engine cannot answer for the
        position at all (a king of the side to move — A64-014.5).

        Neither `position` nor its board is modified. Every intermediate
        board below is a new value, which means a failure part-way through
        leaves the caller's position exactly as it was — the immutability
        doing the work CLAUDE.md §9.12 would otherwise need a transaction
        for.
        """
        self._validator.validate(position, move)

        board = position.board
        for captured in move.captured:
            board = board.remove(captured)
        board = _relocated(board, move.origin, move.destination)
        if move.promotes_to is not None:
            board = _crowned(board, move.destination)

        return Position(board=board, side_to_move=position.side_to_move.opponent())


def _relocated(board: Board, origin: BoardCoordinate, destination: BoardCoordinate) -> Board:
    """`board` with the piece on `origin` standing on `destination`.

    Lift, then place — rather than `Board.move`, which refuses to relocate a
    piece onto the square it is already on. That refusal is correct where it
    lives: A64-014.1 made it so because a bare relocation onto itself is a
    caller with a bug, and weakening it would remove the protection for
    every other caller.

    It is wrong *here*, though, and A64-014.4 is what showed it. A capture
    sequence may circle a ring of victims and come back to the square it
    started from, which makes `origin == destination` an ordinary, legal
    ply. Lifting first says what actually happens on a board — the piece is
    picked up, the victims come off, the piece is put down — and keeps
    every guarantee: `remove` refuses an empty origin, `place` refuses an
    occupied or unplayable destination.
    """
    piece = board.piece_at(origin)
    if piece is None:
        raise PieceNotFound(f"Square {origin} holds no piece to move.")
    return board.remove(origin).place(destination, piece)


def _crowned(board: Board, square: BoardCoordinate) -> Board:
    """`board` with the piece on `square` crowned.

    Takes no rank. `Move.promotes_to` carries one, and the only value it
    can carry is `KING` — the generator produces no other, and a move
    claiming any other is not in the generated set and never reaches here
    (see `MoveValidator` on why equality includes the field). So the field
    is read as *whether* the move crowns, and `Piece.promote` stays the one
    implementation of *what crowning means*. The day a variant has a second
    promoted rank, this takes the rank and `promote` gains an argument —
    together, rather than one of them quietly disagreeing with the other.

    A new `Piece` rather than a mutation, which costs nothing: a piece is a
    value object with no identity (domain-model.md §16.1), so "the same
    piece, promoted" and "a different piece" are the same statement.

    The empty-square guard is this helper's contract rather than a
    reachable branch — `apply` has validated, so the piece it just
    relocated is standing there. It is written out because
    `Board.piece_at` answers `Piece | None`, and silently assuming
    otherwise is how a `None` reaches a board three calls later.
    """
    piece = board.piece_at(square)
    if piece is None:
        raise PieceNotFound(f"Nothing on {square} to crown.")
    return board.remove(square).place(square, piece.promote())


__all__ = ["MoveApplier"]
