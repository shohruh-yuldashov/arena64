"""`BoardVariant` and `BoardGeometry` — the board a variant is played on,
and the rule axes that govern movement across it.

Framework-free and dependency-free (AD-13), and **configuration only**:
nothing here knows *how* to generate a move. It states the axes a generator
reads — domain-model.md §10.1's `Variant`: "board size, king mobility
(flying vs short), capture obligation (any vs maximum), promotion-ends-ply,
draw rules, first mover" — and `MoveGenerator` asks it questions.

## Why the axes live here rather than in the generator — A64-014.2

Because the alternative is `if variant is BoardVariant.RUSSIAN_8X8` inside a
rules algorithm, and that is how a second variant becomes unshippable: the
branch is invisible from the variant table, so nobody adding
`BRAZILIAN_8X8` knows to look for it, and the bug it produces is a legal
move that should not have been offered.

The rule the generator follows is therefore absolute: **it reads the
geometry and never the variant.** A `BoardVariant` value reaches
`geometry_of` and goes no further.

Two axes that could have been fields are methods instead — `forward_step`
and `promotion_row`. `BoardCoordinate` already fixes the orientation (row 0
is LIGHT's back rank, GE-1), so storing "LIGHT moves toward +1" and "LIGHT
promotes on the last row" would let a variant declare a direction that
contradicts the coordinate system, and nothing would catch it. Derived,
they cannot disagree.

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

from app.modules.engine.coordinate import (
    DIAGONAL_DIRECTIONS,
    MAX_BOARD_DIMENSION,
    BoardCoordinate,
    Direction,
)
from app.modules.engine.exceptions import InvalidBoardState
from app.modules.engine.piece import PlayerSide


class BoardVariant(StrEnum):
    """A named rule set (domain-model.md §2.1). This task configures the
    board each one is played on and the axes that govern how men move."""

    RUSSIAN_8X8 = "russian_8x8"
    INTERNATIONAL_10X10 = "international_10x10"


class CaptureObligation(StrEnum):
    """*Which* capture must be played when a player is obliged to capture —
    domain-model.md §10.1's "capture obligation (any vs maximum)".

    Separate from `capture_is_mandatory`, which answers *whether*. Two
    questions, because they have different answers in different rule sets
    and because only the second one has a bearing on this task: a variant
    that obliges a capture leaves this enum deciding between sequences,
    and there are no sequences to decide between until A64-014.4.
    """

    ANY = "any"
    """The player chooses which capture to play — Russian draughts."""

    MAXIMUM = "maximum"
    """The capture played must take the most pieces available —
    international draughts. Selecting it is A64-014.4's; this task records
    the obligation without acting on it, because a single jump is the only
    sequence it generates and every single jump takes one piece."""


@dataclass(frozen=True, slots=True)
class BoardGeometry:
    """The shape of a board, and the rule axes that govern movement on it.

    Frozen and shared — one instance per variant, held in the table below
    and handed out by `geometry_of`. Nothing about it is per-game, so there
    is no reason for two boards of one variant to hold two copies.

    Every axis is **required**. A default on "is capture mandatory" would
    be a variant silently inheriting a rule it never declared, which is the
    one failure mode this record exists to prevent.
    """

    rows: int
    columns: int
    setup_rows_per_side: int
    """How many ranks nearest each player are filled with men at setup —
    three in Russian draughts, four in international."""

    capture_is_mandatory: bool
    """Whether an available capture must be played.

    True in both configured variants and in every mainstream draughts rule
    set. It is a field rather than a constant because it is the switch
    `MoveGenerator.legal_moves` reads to decide whether captures replace
    quiet moves or merely join them — so a house rule that lets a capture
    be declined is a table entry here, not a rewrite of the generator.

    Being true everywhere today, the `False` path is configured by no
    variant and therefore exercised by no test. That is recorded rather
    than hidden: it is the one behaviour in this file taken on trust.
    """

    capture_obligation: CaptureObligation
    men_may_capture_backward: bool
    """Whether a man may jump an opponent standing behind it.

    True in both configured variants and false in English draughts, which
    is the variant this axis exists for. It governs capture only — a man
    never *moves* backward in any variant here, which is why there is no
    matching axis for quiet moves.
    """

    kings_fly: bool
    """Whether a king travels any distance along a diagonal (Russian,
    international) or exactly one square (English).

    Recorded, unread: kings do not move until A64-014.5. It is here because
    domain-model.md §10.1 names it as an axis of `Variant`, and because a
    generator that had to be re-parameterised later is a generator whose
    tests all change at once.
    """

    promotion_ends_ply: bool
    """Whether crowning stops a capture sequence that could otherwise
    continue — domain-model.md §2.1: "in most variants this ends the ply
    even if further jumps exist".

    False for Russian draughts, where a man that reaches the crownhead
    mid-sequence becomes a king and continues jumping as one.

    **Unread by this task, and provisional for international.** It only has
    observable meaning once sequences continue past a square, which is
    A64-014.4; the international value is recorded to the best reading of
    its rules and must be confirmed there against the corpus rather than
    trusted from here.
    """

    @property
    def maximum_capture_is_mandatory(self) -> bool:
        """Whether the capture played must be the one taking the most
        pieces. Recorded for A64-014.4; nothing selects on it yet, and this
        task generates single jumps only, so there is nothing to compare."""
        return self.capture_obligation is CaptureObligation.MAXIMUM

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

    def forward_step(self, side: PlayerSide) -> int:
        """Which way `side` advances — `+1` for LIGHT, `-1` for DARK.

        Derived from the coordinate system rather than configured; see the
        module docstring on why a stored direction could contradict GE-1.
        """
        return 1 if side is PlayerSide.LIGHT else -1

    def promotion_row(self, side: PlayerSide) -> int:
        """The crownhead for `side` — the rank on which a man is crowned.

        The far rank from where that side started, which on this
        orientation is the last row for LIGHT and row 0 for DARK.
        """
        return self.rows - 1 if side is PlayerSide.LIGHT else 0

    def is_promotion_square(self, side: PlayerSide, coordinate: BoardCoordinate) -> bool:
        """Whether a man of `side` arriving here is crowned."""
        return coordinate.row == self.promotion_row(side)

    def forward_directions(self, side: PlayerSide) -> tuple[Direction, ...]:
        """The two diagonals `side` advances along, in ascending order."""
        forward = self.forward_step(side)
        return tuple(
            direction for direction in DIAGONAL_DIRECTIONS if direction.row_step == forward
        )

    def man_capture_directions(self, side: PlayerSide) -> tuple[Direction, ...]:
        """The diagonals a man of `side` may jump along.

        All four where the variant lets men capture backward, the two
        forward ones where it does not. Ascending order in both cases, so
        the generated move list does not depend on which branch ran.
        """
        if self.men_may_capture_backward:
            return DIAGONAL_DIRECTIONS
        return self.forward_directions(side)

    def step(
        self, origin: BoardCoordinate, direction: Direction, distance: int = 1
    ) -> BoardCoordinate | None:
        """The square `distance` steps from `origin` along `direction`, or
        `None` if that square is off the board or unplayable.

        `None` rather than an exception, because walking off the edge is
        the ordinary outcome of asking about a piece on the rim — every
        generator does it twice per corner piece, and a `try` block per
        direction would bury the rule in error handling (CLAUDE.md §9.8).

        Bounds are checked before the coordinate is built, so an edge piece
        never produces the `InvalidCoordinate` that a negative rank would.
        """
        row = origin.row + direction.row_step * distance
        column = origin.column + direction.column_step * distance
        if not 0 <= row < self.rows or not 0 <= column < self.columns:
            return None
        target = BoardCoordinate(row=row, column=column)
        return target if self.is_playable(target) else None


_GEOMETRIES: Mapping[BoardVariant, BoardGeometry] = MappingProxyType(
    {
        BoardVariant.RUSSIAN_8X8: BoardGeometry(
            rows=8,
            columns=8,
            setup_rows_per_side=3,
            capture_is_mandatory=True,
            capture_obligation=CaptureObligation.ANY,
            men_may_capture_backward=True,
            kings_fly=True,
            promotion_ends_ply=False,
        ),
        BoardVariant.INTERNATIONAL_10X10: BoardGeometry(
            rows=10,
            columns=10,
            setup_rows_per_side=4,
            capture_is_mandatory=True,
            capture_obligation=CaptureObligation.MAXIMUM,
            men_may_capture_backward=True,
            kings_fly=True,
            promotion_ends_ply=True,
        ),
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


__all__ = ["BoardGeometry", "BoardVariant", "CaptureObligation", "geometry_of"]
