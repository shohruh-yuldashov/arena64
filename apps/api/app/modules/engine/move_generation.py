"""`MoveGenerator` — what may be played in a position.

Framework-free and dependency-free (AD-13): no I/O, no clock, no
randomness, no configuration read from anywhere but the position's own
geometry. The same `Position` produces the same tuple of moves on every
machine, in every process, forever — which is the property the conformance
corpus tests and the property a rated result rests on.

## Scope

The rules of movement are complete as of A64-014.5: men and kings, quiet
moves and complete capture sequences of any length, mandatory capture,
maximum capture where the variant obliges it, and every configured answer
to crowning mid-jump. What the engine still lacks is everything *around* a
move — terminal states, draws, repetition — none of which is generation.

A64-014.3's `UnsupportedPieceMovement` guard is gone with this task, along
with the exception itself. It existed to stop an empty move set meaning two
things at once while kings were unimplemented; kings are implemented, so
the guard is not a safety net any more, it is a lie about what the engine
can do.

## Men and kings share one pipeline

A king is not a special case with its own generator. It differs from a man
in exactly three answers, each of which the piece's rank selects:

| Question | Man | King |
| --- | --- | --- |
| How far does it travel in one leg? | one square | `geometry.king_reach` |
| Which diagonals may it move quietly along? | forward only | all four |
| Which diagonals may it jump along? | what the variant allows | all four |

Everything else — the capture walk, the taken-once rule, mandatory
capture, the maximum filter, the ordering — is written once and does not
know which it is looking at. That is why `kings_fly` is read as a *reach*
rather than as an `if`: a short king is a flying king that cannot see past
its neighbour, and one loop is correct for both.

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
from app.modules.engine.move import Move
from app.modules.engine.piece import Piece, PieceRank
from app.modules.engine.position import Position
from app.modules.engine.variant import BoardGeometry, MidSequencePromotion


class MoveGenerator:
    """Every move the side to move may play, in one deterministic order."""

    def legal_moves(self, position: Position) -> tuple[Move, ...]:
        """The moves available in `position`, ordered and immutable.

        Empty means exactly one thing: **the side to move has nothing to
        play**, which under the full rules is a loss for that side. Since
        A64-014.5 that is unconditional — there is no piece the generator
        declines to answer for.

        Drawing the losing conclusion is still not this method's; terminal
        state is A64-014.6's. What this guarantees is that the conclusion
        is safe to draw.
        """
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

        Men and kings both. The piece that starts the ply is carried
        through the recursion unchanged as `started_as`, because whether a
        move *promotes* depends on what it began as, not on what it is by
        the end.
        """
        sequences: list[Move] = []
        for square, piece in _pieces_to_move(position):
            sequences.extend(
                _sequences(position.board.remove(square), geometry, piece, piece, (square,), ())
            )
        return tuple(sequences)

    def _quiet_moves(self, position: Position, geometry: BoardGeometry) -> tuple[Move, ...]:
        """Every non-capturing move available to the side to move.

        A man steps one square forward. A king slides along any of the four
        diagonals, and **every empty square it passes is a move of its
        own** — where a king stops is a real choice, and it decides what it
        can do next ply. It stops at the first piece of either colour: a
        quiet move jumps nothing.
        """
        moves: list[Move] = []
        for square, piece in _pieces_to_move(position):
            for direction in _quiet_directions(geometry, piece):
                for target in _open_squares(
                    position.board, geometry, square, direction, _reach(geometry, piece)
                ):
                    moves.append(
                        Move(
                            path=(square, target),
                            promotes_to=_promotion(geometry, piece, target),
                        )
                    )
        return tuple(moves)


def _sequences(
    board: Board,
    geometry: BoardGeometry,
    started_as: Piece,
    mover: Piece,
    path: tuple[BoardCoordinate, ...],
    captured: tuple[BoardCoordinate, ...],
) -> list[Move]:
    """Every complete sequence that carries on from `path[-1]`.

    `started_as` is the piece that began the ply and never changes; `mover`
    is the piece as it stands *now*, which differs from it only when a man
    has crowned along the way. `board` has the mover lifted off its origin,
    with every victim — taken or not — still in place.

    Empty when nothing can be jumped from here, which is how a caller one
    level up learns that the sequence it built is terminal and should be
    emitted. At the top level it means the piece has no capture at all.
    """
    complete: list[Move] = []
    for victim, landing in _jumps_from(board, geometry, mover, path[-1], captured):
        crowns = _crowns_on_arrival(geometry, mover, landing)
        arriving = mover.promote() if crowns else mover
        extended = (*path, landing)
        taken = (*captured, victim)

        continuations = (
            []
            if crowns and geometry.mid_sequence_promotion is _ENDS_PLY
            else _sequences(board, geometry, started_as, arriving, extended, taken)
        )
        if continuations:
            complete.extend(continuations)
        else:
            complete.append(
                Move(
                    path=extended,
                    captured=taken,
                    promotes_to=_sequence_promotion(geometry, started_as, arriving, landing),
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

    A flying king may come down on **any** empty square beyond its victim,
    and each is a separate move: where it stops decides what it can take
    next, so two landings after one capture are two different plies, not
    two spellings of one.
    """
    reach = _reach(geometry, mover)
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


def _open_squares(
    board: Board,
    geometry: BoardGeometry,
    origin: BoardCoordinate,
    direction: Direction,
    reach: int,
) -> Iterator[BoardCoordinate]:
    """The empty squares `origin` can slide to along `direction`, nearest
    first, stopping at the first piece of either colour.

    A quiet move passes over nothing, so the *first* occupant ends the
    scan whoever owns it — a friendly piece and an enemy piece block a
    king's slide identically, and the difference between them only matters
    to `_jumps_from`.
    """
    for distance in range(1, reach + 1):
        square = geometry.step(origin, direction, distance)
        if square is None or board.piece_at(square) is not None:
            return
        yield square


def _reach(geometry: BoardGeometry, mover: Piece) -> int:
    """How far `mover` travels in one leg — of a slide or of a jump.

    One square for a man. For a king, whatever the variant says: the far
    side of the board where kings fly, one square where they do not. The
    same number governs both kinds of movement, which is what lets a short
    king and a flying king share every loop in this module.
    """
    if mover.rank is PieceRank.MAN:
        return 1
    return geometry.king_reach


def _quiet_directions(geometry: BoardGeometry, mover: Piece) -> tuple[Direction, ...]:
    """The diagonals `mover` may slide along without capturing.

    A man advances only — no variant configured here lets one step
    backward, which is why this asks the geometry for its forward pair
    rather than for an axis. A king slides all four ways.
    """
    if mover.rank is PieceRank.MAN:
        return geometry.forward_directions(mover.side)
    return DIAGONAL_DIRECTIONS


def _capture_directions(geometry: BoardGeometry, mover: Piece) -> tuple[Direction, ...]:
    """The diagonals `mover` may jump along.

    A man asks the variant, which may or may not let it take backward — it
    does in Russian and international draughts, it does not in English. A
    king takes along all four in every rule set there is.
    """
    if mover.rank is PieceRank.MAN:
        return geometry.man_capture_directions(mover.side)
    return DIAGONAL_DIRECTIONS


_CROWNS_IMMEDIATELY = frozenset(
    {
        MidSequencePromotion.CROWNS_AND_CONTINUES,
        MidSequencePromotion.CROWNS_AND_ENDS_PLY,
    }
)
"""The two rules under which reaching the crownhead crowns a man *there*,
mid-sequence. They differ in what happens next, not in whether it happens —
see `_sequences`, where one of them stops the ply."""

_ENDS_PLY = MidSequencePromotion.CROWNS_AND_ENDS_PLY


def _crowns_on_arrival(geometry: BoardGeometry, mover: Piece, landing: BoardCoordinate) -> bool:
    """Whether landing here crowns the mover *mid-sequence*.

    Not under `PASSES_THROUGH`, where a man crosses its crownhead unchanged
    and is crowned — or not — by where the sequence finally ends.
    """
    return (
        geometry.mid_sequence_promotion in _CROWNS_IMMEDIATELY
        and mover.rank is PieceRank.MAN
        and geometry.is_promotion_square(mover.side, landing)
    )


def _sequence_promotion(
    geometry: BoardGeometry,
    started_as: Piece,
    mover: Piece,
    destination: BoardCoordinate,
) -> PieceRank | None:
    """The rank a finished sequence leaves its piece with, or `None`.

    Three questions in order, and the first is the one A64-014.5 had to
    add: **a king that began the ply as a king is not promoted by
    anything.** Before kings could start a move, "the mover is a king" was
    sufficient evidence that it had been crowned along the way; it is not
    any more, and reading it that way would have every king move claim a
    promotion.
    """
    if started_as.rank is PieceRank.KING:
        return None
    if mover.rank is PieceRank.KING:
        return PieceRank.KING
    if geometry.is_promotion_square(started_as.side, destination):
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


def _pieces_to_move(position: Position) -> list[tuple[BoardCoordinate, Piece]]:
    """The side to move's pieces, of either rank, in ascending square order.

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
    geometry: BoardGeometry, mover: Piece, destination: BoardCoordinate
) -> PieceRank | None:
    """`KING` when a **quiet** move to `destination` crowns `mover`.

    Unconditional for a man, because a quiet move is always the whole ply:
    one that steps onto the crownhead has nowhere left to go, so there is
    no variant to consult. A king sliding across its own crownhead is not
    promoted by anything, which is the case A64-014.5 had to add.

    Captures are where the choice lives, and `_sequence_promotion` is where
    the variant answers it.
    """
    if mover.rank is PieceRank.KING:
        return None
    if not geometry.is_promotion_square(mover.side, destination):
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
