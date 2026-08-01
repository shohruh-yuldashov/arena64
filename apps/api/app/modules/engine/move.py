"""`Move` — an ordered path of squares and everything taken along it.

Framework-free and dependency-free (AD-13).

## The path *is* the move

domain-model.md §2.1 is unusually emphatic about this, and gives the
reason: "a multi-jump in draughts can reach the same destination square by
different capture paths, capturing different pieces." An origin/destination
pair is therefore not a move — it is an ambiguous description of several,
and the ambiguity lands exactly on the piece the two paths disagree about.
§10.1 records the shape this type must have: "ordered path of squares,
captured squares, promotion flag".

Everything downstream inherits the ambiguity if it is not resolved here.
A move log that stored from/to could not be replayed unambiguously, which
`replay` and `fairplay` both depend on, and a client that sent from/to
would be asking the server to guess which pieces the player meant to take.

So a quiet move is a two-square path with nothing captured, and it is not a
special case — it is the shortest member of the same shape. This task
generates single jumps, whose paths happen to be two squares long as well;
A64-014.4 makes them longer without changing this type.

## Immutability and ordering

Frozen, with tuples rather than lists, so a generated move set cannot be
edited by whoever received it. `sort_key` is what makes a *set* of moves
deterministic; see `MoveGenerator`.
"""

from dataclasses import dataclass

from app.modules.engine.coordinate import BoardCoordinate
from app.modules.engine.exceptions import InvalidMove
from app.modules.engine.piece import PieceRank


@dataclass(frozen=True, slots=True)
class Move:
    """One complete move: where the piece went, and what it took.

    Nothing here records *which* piece moved, or whose. The piece is
    whatever stands on `origin` in the position the move was generated for,
    and duplicating it would create a second thing to disagree with the
    board.
    """

    path: tuple[BoardCoordinate, ...]
    """The squares the moving piece occupies in order, starting where it
    stood and ending where it lands. At least two."""

    captured: tuple[BoardCoordinate, ...] = ()
    """The squares of the pieces taken, in the order they were jumped.

    Ordered rather than a set, because the order is part of the record: a
    replay steps a capture sequence square by square, and "which piece went
    first" is what makes two paths through the same pieces distinguishable.
    """

    promotes_to: PieceRank | None = None
    """The rank the moving piece ends the move with, when the move crowns
    it — otherwise `None`.

    A statement about the *result*, computed by the generator from the
    variant's promotion row. Nothing here mutates a `Piece`; applying a
    move is a later task's, and this is the input it will read.
    """

    def __post_init__(self) -> None:
        if len(self.path) < 2:
            raise InvalidMove("A move covers at least two squares.")
        for previous, following in zip(self.path, self.path[1:], strict=False):
            if previous == following:
                # A step to the square already occupied is not a shorter
                # move, it is a malformed one: it makes `len(path)` stop
                # describing how many steps were taken, which is what a
                # replay counts.
                raise InvalidMove(f"A move does not step from {previous} to itself.")
        if len(set(self.captured)) != len(self.captured):
            # The same piece taken twice would make a capture sequence
            # score higher than it should, which is the input to the
            # maximum-capture rule (A64-014.4).
            raise InvalidMove("A move captures each piece at most once.")

    @property
    def origin(self) -> BoardCoordinate:
        """Where the moving piece stood."""
        return self.path[0]

    @property
    def destination(self) -> BoardCoordinate:
        """Where it ends up."""
        return self.path[-1]

    @property
    def is_capture(self) -> bool:
        return bool(self.captured)

    @property
    def sort_key(self) -> tuple[tuple[BoardCoordinate, ...], tuple[BoardCoordinate, ...]]:
        """The total order over moves — see `MoveGenerator` on why a move
        list has to have one.

        `(path, captured)` and not the promotion rank: two moves with the
        same path taking the same pieces cannot differ in whether they
        crown, because crowning is a function of where the path ends. The
        rank is therefore never a tie-break, and including it would suggest
        it could be.

        A property rather than `order=True` on the dataclass, because
        `promotes_to` is `PieceRank | None` and dataclass ordering would
        compare `None` against a rank the first time two moves tied —
        raising `TypeError` from inside a sort, at whatever depth a search
        happened to be.
        """
        return (self.path, self.captured)

    def __str__(self) -> str:
        """`a3-b4` for a quiet move, `a3xc5` for a capture — the shape
        draughts notation has always had, for logs and test failures."""
        separator = "x" if self.is_capture else "-"
        return separator.join(str(square) for square in self.path)


__all__ = ["Move"]
