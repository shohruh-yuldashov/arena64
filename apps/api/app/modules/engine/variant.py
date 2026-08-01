"""`BoardVariant` and `BoardGeometry` — the board a variant is played on.

Framework-free and dependency-free (AD-13), and **configuration only**:
nothing here knows how a piece moves, what a capture obliges, whether kings
fly, or when a game is drawn. Those are the rest of the variant's rule set
(domain-model.md §10.1 lists all six axes) and they arrive with move
generation.

## Why the geometry is a table and not a subclass per variant

architecture.md's module map gives `engine` no aggregate roots — "pure
functions and value objects" — and a `RussianBoard` / `InternationalBoard`
hierarchy would put behaviour behind a polymorphic call at exactly the
place where the two variants do not differ. They differ in movement, not in
what a square is. One record per variant, looked up by key, keeps the
difference visible as data and keeps `Board` variant-agnostic.

## `INTERNATIONAL_10X10`

Configured, not stubbed. domain-model.md DM-10 argues the general form of
this at length for ratings — "keyed by `(variant, speed class)` from day
one, even if only one variant ships", because retrofitting the dimension
later means migrating everything that ever recorded it. The same reasoning
applies to geometry, and here it is cheaper still: a second row in a table.

What that buys is not a second playable game — international draughts has
its own capture and king rules, and none of them exist — but proof that
nothing below is 8x8 with the eight spelled differently. A board built from
this entry is a correct empty 10x10 board with 20 men a side at setup.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from app.modules.engine.coordinate import MAX_BOARD_DIMENSION, BoardCoordinate
from app.modules.engine.exceptions import InvalidBoardState


class BoardVariant(StrEnum):
    """A named rule set (domain-model.md §2.1). This task configures only
    the board each one is played on."""

    RUSSIAN_8X8 = "russian_8x8"
    INTERNATIONAL_10X10 = "international_10x10"


@dataclass(frozen=True, slots=True)
class BoardGeometry:
    """The shape of a board: how big it is, and how it starts out filled.

    Frozen and shared — one instance per variant, held in the table below
    and handed out by `geometry_of`. Nothing about it is per-game, so there
    is no reason for two boards of one variant to hold two copies.
    """

    rows: int
    columns: int
    setup_rows_per_side: int
    """How many ranks nearest each player are filled with men at setup —
    three in Russian draughts, four in international."""

    def __post_init__(self) -> None:
        if not 2 <= self.rows <= MAX_BOARD_DIMENSION:
            raise InvalidBoardState(f"A board of {self.rows} ranks cannot be addressed.")
        if not 2 <= self.columns <= MAX_BOARD_DIMENSION:
            raise InvalidBoardState(f"A board of {self.columns} files cannot be addressed.")
        if self.columns % 2 != 0:
            # An odd file count makes the playable squares per rank
            # alternate, so the two sides would start with different numbers
            # of men on a board that looks symmetric. No draughts variant
            # has one; refusing it here is what keeps `men_per_side` exact
            # rather than approximately right.
            raise InvalidBoardState("A draughts board has an even number of files.")
        if self.setup_rows_per_side < 1:
            raise InvalidBoardState("Each side starts with at least one filled rank.")
        if self.setup_rows_per_side * 2 >= self.rows:
            # Without at least one empty rank between them the two sides
            # start in contact, which is not a position any variant opens
            # from and would make the first move a capture.
            raise InvalidBoardState("The two sides' starting ranks must not meet.")

    @property
    def men_per_side(self) -> int:
        """Men each side has at setup — 12 on 8x8, 20 on 10x10.

        Exact rather than nominal: `__post_init__` guarantees an even file
        count, so every rank holds exactly half its files as playable
        squares.
        """
        return self.setup_rows_per_side * (self.columns // 2)

    def contains(self, coordinate: BoardCoordinate) -> bool:
        """Whether the square is on this board at all."""
        return coordinate.row < self.rows and coordinate.column < self.columns

    def is_playable(self, coordinate: BoardCoordinate) -> bool:
        """Whether the square is one of the dark squares the game uses.

        Dark squares are those whose row and column sum to an even number,
        which puts one at `a1` — the near-left corner — matching how both
        variants are set up in every rule book and every board sold.
        """
        return self.contains(coordinate) and (coordinate.row + coordinate.column) % 2 == 0

    def playable_squares(self) -> Iterator[BoardCoordinate]:
        """Every playable square, row-major from `a1`.

        A generator rather than a tuple: the only callers walk it once, and
        materialising 32 or 50 coordinates per board is exactly the kind of
        allocation a move generator repeats.
        """
        for row in range(self.rows):
            for column in range(row % 2, self.columns, 2):
                yield BoardCoordinate(row=row, column=column)


_GEOMETRIES: Mapping[BoardVariant, BoardGeometry] = MappingProxyType(
    {
        BoardVariant.RUSSIAN_8X8: BoardGeometry(rows=8, columns=8, setup_rows_per_side=3),
        BoardVariant.INTERNATIONAL_10X10: BoardGeometry(rows=10, columns=10, setup_rows_per_side=4),
    }
)
"""Every variant's board, exhaustively.

A read-only mapping, so a caller that gets hold of it cannot reconfigure a
variant at runtime — the rules a game was played under are part of its
record (AD-15), and a mutable global holding them would be the one place
that could rewrite history.

Exhaustiveness is covered by a test rather than a runtime guard: a member
added without an entry fails `test_every_variant_has_a_geometry`, which is
where a missing configuration should be caught, not on the first request
that names the new variant.
"""


def geometry_of(variant: BoardVariant) -> BoardGeometry:
    """The board `variant` is played on."""
    return _GEOMETRIES[variant]


__all__ = ["BoardGeometry", "BoardVariant", "geometry_of"]
