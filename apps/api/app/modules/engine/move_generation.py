"""`MoveGenerator` — what may be played in a position.

Framework-free and dependency-free (AD-13): no I/O, no clock, no
randomness, no configuration read from anywhere but the position's own
geometry. The same `Position` produces the same tuple of moves on every
machine, in every process, forever — which is the property the conformance
corpus tests and the property a rated result rests on.

## Scope

Present: quiet moves for men, **complete capture sequences** of any length,
mandatory-capture priority, maximum-capture filtering where the variant
obliges it, promotion on arrival and mid-sequence, one deterministic order.

Absent: **standalone king movement**. A king belonging to the side to move
is refused with `UnsupportedPieceMovement`; a king that appears *during* a
sequence, because a man crowned mid-jump, keeps jumping under the king
rules below. A64-014.5 removes the refusal, and what it has left to build
is king *quiet* moves — flying steps along a diagonal — plus starting a
walk from a king that was already on the board. The jump scan here already
handles a king correctly; nothing calls it with one on move one.

An opponent's king raises nothing. It is a piece a man may jump, which this
build handles, and refusing it would reject positions the engine answers
for.

## Complete sequences, and why prefixes never escape

`_captures` returns only **terminal** sequences: if a jump can be continued,
the shorter move is not offered. That is the rule in both configured
variants — a player who has jumped once and can jump again must — and it is
also what makes `MoveApplier` correct without knowing anything about
sequences, since every move it is handed is a whole ply.

The search is a depth-first walk over the jumps available from the piece's
current square. It terminates because every step consumes one victim, a
victim is never taken twice, and there are finitely many opponent pieces.

## The board the walk sees

Two adjustments, both made once at the start of a piece's walk:

- **The moving piece is lifted off.** Otherwise a sequence that comes back
  round to a square it has already stood on would find itself in the way.
  A64-014.4's corpus has exactly that case.
- **Victims are left standing.** A captured piece stays on the board until
  the ply ends — the "Turkish strike" rule — so it blocks a later leg of
  the same sequence rather than opening a hole in it. `captured` records
  which of the standing pieces have already been taken, and they can be
  neither jumped again nor passed through.

Both fall out of one immutable board (`board.remove(origin)`) plus a tuple
of captured squares carried down the recursion. Nothing is mutated, so a
branch that fails leaves nothing behind for the next branch to trip over.

## Why captures are generated first rather than filtered afterwards

Mandatory capture is the rule the whole variant hangs off (architecture.md
A-1 names it as the reason the engine is complex at all). Generating quiet
moves and discarding them when a capture exists gets the same answer today
and the wrong shape tomorrow: under the maximum-capture obligation the
survivors must be compared against each other by length, and a pipeline
that has already mixed quiet moves into the pool has to re-identify which
were captures to do it.

So the flow is: ask for every complete capture sequence; narrow it to the
longest where the variant obliges the maximum; and if anything survives and
captures are mandatory, that *is* the answer. Quiet moves are generated
only when they can be played.

The maximum-capture rule is a **filter over finished sequences**, never a
pruning rule inside the search. A branch that starts by taking one piece
can end up the longest sequence on the board, so a walker that preferred
the wider first jump would return the wrong move — and would do it only in
positions rare enough to reach production.

## Why a class rather than a module function

It holds no state and could be a function. It is a class because it is the
collaborator `game` will be given — architecture.md R-2 lets exactly three
modules import the engine, and each of them wants a named, injectable
thing at its boundary rather than a bare import — and because AD-14's
TypeScript engine mirrors this surface. One instance is safe to share.
"""

from collections.abc import Iterator

from app.modules.engine.board import Board
from app.modules.engine.coordinate import DIAGONAL_DIRECTIONS, BoardCoordinate, Direction
from app.modules.engine.exceptions import UnsupportedPieceMovement
from app.modules.engine.move import Move
from app.modules.engine.piece import Piece, PieceRank, PlayerSide
from app.modules.engine.position import Position
from app.modules.engine.variant import BoardGeometry, MidSequencePromotion


class MoveGenerator:
    """Every move the side to move may play, in one deterministic order."""

    def legal_moves(self, position: Position) -> tuple[Move, ...]:
        """The moves available in `position`, ordered and immutable.

        Empty means exactly one thing: **the side to move has nothing to
        play**, which under the full rules is a loss for that side. It no
        longer also means "the engine could not tell" — a king of the side
        to move raises `UnsupportedPieceMovement` instead (A64-014.3).

        Drawing the losing conclusion is still not this method's; terminal
        state is a later task. What this guarantees is that the conclusion
        will be safe to draw.
        """
        _reject_unsupported_pieces(position)

        geometry = position.board.geometry
        captures = _obliged(self._captures(position, geometry), geometry)
        if captures and geometry.capture_is_mandatory:
            return _ordered(captures)

        quiet = self._quiet_moves(position, geometry)
        return _ordered(captures + quiet)

    def _captures(self, position: Position, geometry: BoardGeometry) -> tuple[Move, ...]:
        """Every **complete** capture sequence available to the side to move.

        Complete meaning terminal: a sequence appears here only if the
        piece cannot jump again from where it ends. Prefixes are not
        offered, because a player who can continue must.

        Each piece walks against a board with itself lifted off and every
        victim still standing — see the module docstring on why those two
        adjustments are what make revisits and the Turkish strike fall out
        rather than needing rules of their own.
        """
        sequences: list[Move] = []
        for square, man in _men_to_move(position):
            sequences.extend(
                _sequences(position.board.remove(square), geometry, man, (square,), ())
            )
        return tuple(sequences)

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


def _sequences(
    board: Board,
    geometry: BoardGeometry,
    mover: Piece,
    path: tuple[BoardCoordinate, ...],
    captured: tuple[BoardCoordinate, ...],
) -> list[Move]:
    """Every complete sequence that carries on from `path[-1]`.

    `mover` is the piece as it stands *now* — a man, or the king it became
    when it crossed the crownhead — and `board` has it lifted off with
    every victim, taken or not, still in place.

    Empty when nothing can be jumped from here, which is how a caller one
    level up learns that the sequence it built is terminal and should be
    emitted. At the top level it means the piece has no capture at all.
    """
    complete: list[Move] = []
    for victim, landing in _jumps_from(board, geometry, mover, path[-1], captured):
        arriving = mover.promote() if _crowns_on_arrival(geometry, mover, landing) else mover
        extended = (*path, landing)
        taken = (*captured, victim)

        continuations = _sequences(board, geometry, arriving, extended, taken)
        if continuations:
            complete.extend(continuations)
        else:
            complete.append(
                Move(
                    path=extended,
                    captured=taken,
                    promotes_to=_sequence_promotion(geometry, arriving, landing),
                )
            )
    return complete


def _jumps_from(
    board: Board,
    geometry: BoardGeometry,
    mover: Piece,
    origin: BoardCoordinate,
    captured: tuple[BoardCoordinate, ...],
) -> Iterator[tuple[BoardCoordinate, BoardCoordinate]]:
    """Every `(victim, landing)` pair `mover` may take from `origin`.

    Ordered by direction and then by landing distance, so the walk itself
    is reproducible before `_ordered` ever sees the result.

    A man reaches exactly one square; a king reaches `king_reach`, which is
    the far side of the board where kings fly and one square where they do
    not. That single number is why there is no separate short-king branch.
    """
    reach = 1 if mover.rank is PieceRank.MAN else geometry.king_reach
    for direction in _capture_directions(geometry, mover):
        obstruction = _first_obstruction(board, geometry, origin, direction, reach)
        if obstruction is None:
            continue
        square, blocker, distance = obstruction
        if blocker.side is mover.side or square in captured:
            # An own piece was never takeable; a victim already taken this
            # ply is still standing and may be neither jumped again nor
            # passed through. Either way this diagonal is closed.
            continue
        for landing in _landings(board, geometry, origin, direction, distance, reach):
            yield square, landing


def _first_obstruction(
    board: Board,
    geometry: BoardGeometry,
    origin: BoardCoordinate,
    direction: Direction,
    reach: int,
) -> tuple[BoardCoordinate, Piece, int] | None:
    """The nearest occupied square within `reach`, and how far away it is."""
    for distance in range(1, reach + 1):
        square = geometry.step(origin, direction, distance)
        if square is None:
            return None
        piece = board.piece_at(square)
        if piece is not None:
            return square, piece, distance
    return None


def _landings(
    board: Board,
    geometry: BoardGeometry,
    origin: BoardCoordinate,
    direction: Direction,
    victim_distance: int,
    reach: int,
) -> Iterator[BoardCoordinate]:
    """The empty squares a jumper may come down on, nearest first.

    One square for a man. For a flying king, every empty square beyond the
    victim until the board ends or something else is in the way — each of
    which is a distinct move, because where a king stops decides what it
    can take next.
    """
    for extra in range(1, reach + 1):
        square = geometry.step(origin, direction, victim_distance + extra)
        if square is None or board.piece_at(square) is not None:
            return
        yield square


def _capture_directions(geometry: BoardGeometry, mover: Piece) -> tuple[Direction, ...]:
    """The diagonals `mover` may jump along.

    A man asks the variant, which may or may not let it take backward. A
    king takes along all four in every rule set there is.
    """
    if mover.rank is PieceRank.MAN:
        return geometry.man_capture_directions(mover.side)
    return DIAGONAL_DIRECTIONS


def _crowns_on_arrival(geometry: BoardGeometry, mover: Piece, landing: BoardCoordinate) -> bool:
    """Whether landing here crowns the mover *mid-sequence*.

    Only under `CROWNS_AND_CONTINUES`. Where the variant passes a man
    through the crownhead instead, it stays a man for the rest of the ply
    and is crowned — or not — by where the sequence ends.
    """
    return (
        geometry.mid_sequence_promotion is MidSequencePromotion.CROWNS_AND_CONTINUES
        and mover.rank is PieceRank.MAN
        and geometry.is_promotion_square(mover.side, landing)
    )


def _sequence_promotion(
    geometry: BoardGeometry, mover: Piece, destination: BoardCoordinate
) -> PieceRank | None:
    """The rank a finished sequence leaves its piece with, or `None`.

    Every sequence starts with a man — a king of the side to move is
    refused before any of this runs — so a mover that is a king by the end
    was crowned along the way, and a mover that is still a man is crowned
    only if it stopped on the crownhead. When A64-014.5 lets a king start a
    ply, this takes the starting rank so it can tell the two apart.
    """
    if mover.rank is PieceRank.KING:
        return PieceRank.KING
    if geometry.is_promotion_square(mover.side, destination):
        return PieceRank.KING
    return None


def _obliged(captures: tuple[Move, ...], geometry: BoardGeometry) -> tuple[Move, ...]:
    """The sequences the variant's capture obligation actually permits.

    Under `MAXIMUM` only the longest survive; under `ANY` the player picks,
    so all of them do. Applied here, to finished sequences, and never
    inside the search — a branch that opens with a single jump can end up
    the longest on the board, so a walker that chose greedily would be
    wrong exactly where nobody would notice.
    """
    if not captures or not geometry.maximum_capture_is_mandatory:
        return captures
    longest = max(len(capture.captured) for capture in captures)
    return tuple(capture for capture in captures if len(capture.captured) == longest)


def _reject_unsupported_pieces(position: Position) -> None:
    """Refuse a position this build cannot answer for — see
    `UnsupportedPieceMovement`.

    The check is on the side to move only, and it runs before any
    generation so that no half-built answer can escape.
    """
    for square, piece in position.board.occupied_squares.items():
        if piece.side is position.side_to_move and piece.rank is PieceRank.KING:
            raise UnsupportedPieceMovement(
                f"King movement is not implemented; the {piece.side.value} king on "
                f"{square} has moves this engine cannot generate."
            )


def _men_to_move(position: Position) -> list[tuple[BoardCoordinate, Piece]]:
    """The side to move's pieces, in ascending square order.

    Every one of them is a man: `_reject_unsupported_pieces` has already
    run, so a king of this side would have raised rather than reached here.
    Re-filtering on rank would be a second statement of that invariant,
    which is the kind of duplication that stays behind after the first one
    is deleted in A64-014.5.

    Sorted here as well as in `_ordered`, so that the walk itself is
    reproducible: a bug that made two moves compare equal would otherwise
    surface as an ordering that depended on how the board's mapping was
    built, which is the class of defect that reproduces on one machine
    only.
    """
    return sorted(
        (
            (square, piece)
            for square, piece in position.board.occupied_squares.items()
            if piece.side is position.side_to_move
        ),
        key=lambda entry: entry[0],
    )


def _promotion(
    geometry: BoardGeometry, side: PlayerSide, destination: BoardCoordinate
) -> PieceRank | None:
    """`KING` when a **quiet** move to `destination` crowns a man of `side`.

    Unconditional, because a quiet move is one square and is always the
    whole ply: a man that steps onto the crownhead has nowhere left to go.
    Captures are the case with a choice in it, and `_sequence_promotion`
    is where the variant's answer lands.
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
