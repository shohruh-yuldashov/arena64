"""`Board` — which pieces stand on which squares, and nothing more.

Framework-free and dependency-free (AD-13). No clock, no identity, no
persistence.

## Immutable, and why that is the whole point

Every operation returns a new `Board`; none mutates the receiver, and the
storage is never handed out in a form a caller could write to.

domain-model.md §16.1 rejects an entity `Board` and gives the reason:
"an entity `Board` would compare by identity, and the **three-fold
repetition draw rule requires positions to compare by value**. Modelling
the board as an entity does not merely add a table; it makes a rule of
checkers unimplementable." Two boards here are equal when they hold the
same pieces on the same squares of the same variant, which is what that
rule needs.

The second reason is search. `fairplay` replays whole games and explores
positions (architecture.md AD-13.3); a mutable board makes every explored
line a place where an undo can be forgotten, and an undo bug in a rules
kernel produces a plausible illegal game rather than a crash.

## What this class deliberately does not do

It does not know a legal move from an illegal one. `move` relocates a
piece and refuses only what is mechanically impossible — no piece to move,
nowhere to put it. Direction, distance, mandatory capture, multi-jump,
promotion on arrival and whose turn it is are movement rules, and they
belong to move generation and move application, which this task does not
implement. A caller that treats `move` as validation has skipped the rules.

## Not hashable — yet

Defining `__eq__` leaves `__hash__` unset, so a `Board` cannot be a
dictionary key. That is correct for now: repetition detection needs a
position *including the side to move*, and hashing a board without it
would compare two positions that the rules consider different. Position
hashing arrives with `Position`, where the side to move lives.
"""

from collections.abc import Mapping
from types import MappingProxyType

from app.modules.engine.coordinate import BoardCoordinate
from app.modules.engine.exceptions import (
    DestinationOccupied,
    InvalidBoardState,
    InvalidCoordinate,
    PieceNotFound,
)
from app.modules.engine.piece import Piece, PlayerSide
from app.modules.engine.variant import BoardGeometry, BoardVariant, geometry_of


class Board:
    """A position's placement of pieces, for one variant.

    Written as a plain slotted class rather than the frozen dataclass this
    codebase reaches for by default, because the storage has to be copied
    and wrapped on the way in: a dataclass field holding a caller's `dict`
    would be a mutable back door through an object whose immutability is
    load-bearing, and coercing it in `__post_init__` means
    `object.__setattr__` against the frozen guarantee it was supposed to
    provide. An explicit constructor says what happens.
    """

    __slots__ = ("_geometry", "_occupied", "_squares", "_variant")

    def __init__(self, variant: BoardVariant, squares: Mapping[BoardCoordinate, Piece]) -> None:
        """Build a board holding exactly `squares`.

        The mapping is copied, so later changes to the caller's dictionary
        do not reach the board.

        Raises `InvalidBoardState` if any square is off this board or is a
        light square. Blaming the *state* rather than the coordinate is
        deliberate — see `InvalidBoardState`: this constructor receives a
        whole position, and the fault is that the position could not have
        arisen, not that one argument was mistyped. It is also the guard
        that a future repository rehydrating a stored position hits when
        the row is corrupt, which is the case that matters (BE-06).
        """
        geometry = geometry_of(variant)
        for coordinate in squares:
            if not geometry.is_playable(coordinate):
                raise InvalidBoardState(
                    f"Square {coordinate} cannot hold a piece on a {variant.value} board."
                )
        self._variant = variant
        self._geometry = geometry
        self._squares: dict[BoardCoordinate, Piece] = dict(squares)
        self._occupied = MappingProxyType(self._squares)

    @classmethod
    def empty(cls, variant: BoardVariant) -> "Board":
        """A board of the right shape with nothing on it."""
        return cls(variant, {})

    @property
    def variant(self) -> BoardVariant:
        return self._variant

    @property
    def geometry(self) -> BoardGeometry:
        """This board's shape. Frozen and shared across every board of the
        variant, so exposing it hands out no mutable state."""
        return self._geometry

    @property
    def occupied_squares(self) -> Mapping[BoardCoordinate, Piece]:
        """Every occupied square and what stands on it.

        A read-only view over the board's own storage, not a copy: the
        board never changes, so the view can never show a caller something
        that has since moved, and a copy per call would be an allocation on
        the path move generation walks most.
        """
        return self._occupied

    def piece_at(self, coordinate: BoardCoordinate) -> Piece | None:
        """What stands on `coordinate`, or `None` for an empty square.

        `None` rather than an exception: an empty square is the normal
        state of most of the board, and a lookup that raised would make
        "is this square free" a `try` block (CLAUDE.md §9.8).

        A square that is off this board, or a light square, is empty by
        this method's reckoning. It answers what is *there*, and nothing is
        there; the commands below are where naming an impossible square is
        an error.
        """
        return self._squares.get(coordinate)

    def place(self, coordinate: BoardCoordinate, piece: Piece) -> "Board":
        """This board with `piece` added on `coordinate`."""
        self._require_playable(coordinate)
        if coordinate in self._squares:
            raise DestinationOccupied(f"Square {coordinate} is already occupied.")
        return Board(self._variant, {**self._squares, coordinate: piece})

    def remove(self, coordinate: BoardCoordinate) -> "Board":
        """This board with whatever stands on `coordinate` taken off."""
        if coordinate not in self._squares:
            raise PieceNotFound(f"Square {coordinate} holds no piece.")
        remaining = dict(self._squares)
        del remaining[coordinate]
        return Board(self._variant, remaining)

    def move(self, origin: BoardCoordinate, destination: BoardCoordinate) -> "Board":
        """This board with the piece on `origin` standing on `destination`.

        **A relocation, not a move.** Nothing here consults the rules of
        draughts: it refuses an empty origin, an unusable destination and
        an occupied destination, and permits every geometrically absurd
        relocation in between. Legality is move generation's answer.

        Relocating a piece onto its own square raises `DestinationOccupied`
        — the destination does hold a piece — rather than succeeding as a
        no-op. A caller asking for that has a bug, and a silent success
        would hide it.
        """
        piece = self._squares.get(origin)
        if piece is None:
            raise PieceNotFound(f"Square {origin} holds no piece to move.")
        self._require_playable(destination)
        if destination in self._squares:
            raise DestinationOccupied(f"Square {destination} is already occupied.")
        relocated = dict(self._squares)
        del relocated[origin]
        relocated[destination] = piece
        return Board(self._variant, relocated)

    def piece_count(self) -> int:
        """How many pieces stand on the board, both sides together."""
        return len(self._squares)

    def piece_count_for(self, side: PlayerSide) -> int:
        """How many pieces `side` has left.

        A second method rather than an optional argument on `piece_count`:
        an argument that switches what a function counts is the flag
        CLAUDE.md §2.3 refuses, and "how many pieces are on the board" and
        "how many has this player got" are different questions — the second
        is the one that decides a game (all pieces captured).
        """
        return sum(1 for piece in self._squares.values() if piece.side is side)

    def _require_playable(self, coordinate: BoardCoordinate) -> None:
        if not self._geometry.is_playable(coordinate):
            raise InvalidCoordinate(
                f"Square {coordinate} is not a playable square on a {self._variant.value} board."
            )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Board):
            return NotImplemented
        return self._variant is other._variant and self._squares == other._squares

    def __repr__(self) -> str:
        placement = ", ".join(
            f"{coordinate}={self._squares[coordinate].side.value}"
            f" {self._squares[coordinate].rank.value}"
            for coordinate in sorted(self._squares)
        )
        return f"Board({self._variant.value}: {placement or 'empty'})"


__all__ = ["Board"]
