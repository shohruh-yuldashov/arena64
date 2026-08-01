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
from app.modules.engine.draw_rules import THREEFOLD_REPETITION_ONLY, DrawRules
from app.modules.engine.exceptions import InvalidBoardState
from app.modules.engine.piece import PlayerSide


class BoardVariant(StrEnum):
    """A named rule set (domain-model.md §2.1). This task configures the
    board each one is played on and the axes that govern how men move."""

    RUSSIAN_8X8 = "russian_8x8"
    INTERNATIONAL_10X10 = "international_10x10"
    ENGLISH_8X8 = "english_8x8"
    """Added by A64-014.5, and **configuration only** — one row in the table
    below, no algorithm anywhere reads its name.

    It is here because it is the variant that gives three axes a second
    value. `kings_fly`, `men_may_capture_backward` and
    `mid_sequence_promotion` were each single-valued across the two
    variants that existed, which made them settings nothing could tell
    apart from constants. English draughts is the rule set they were
    written for, and adding it costs six fields and no branches — which is
    itself the strongest evidence the configuration-driven design works.
    """


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
    international draughts. Applied by `MoveGenerator` as a filter over
    complete sequences (A64-014.4), never as a pruning rule inside the
    search: a branch that looks short can end up the longest."""


class MidSequencePromotion(StrEnum):
    """What happens when a man reaches the crownhead *during* a capture.

    domain-model.md §2.1 describes one of these — "a man reaching the
    crownhead becomes a king; in most variants this ends the ply even if
    further jumps exist" — and A64-014.2 encoded it as a boolean
    `promotion_ends_ply`, flagged provisional because it had no observable
    meaning until sequences existed. A64-014.4 gives it one, and the
    boolean turns out to be the wrong shape: the two variants configured
    here take **neither** of its two values.

    | Variant | Rule |
    | --- | --- |
    | Russian 8x8 | Crowns on arrival and carries on jumping, now as a king |
    | International 10x10 | Passes over without crowning and carries on as a man |
    | English *(not configured)* | Crowns on arrival and the ply ends there |

    Three distinct rules, so the axis is an enum. A64-014.4 left the third
    member out because no variant then configured had it; A64-014.5 adds
    English draughts and with it `CROWNS_AND_ENDS_PLY`, so all three are
    now reachable, configured and covered by a corpus case.
    """

    CROWNS_AND_CONTINUES = "crowns_and_continues"
    """Russian draughts. The crowned man continues the same sequence under
    king capture rules, which is why king jumps existed before kings could
    start a ply."""

    PASSES_THROUGH = "passes_through"
    """International draughts. Crossing the crownhead mid-sequence is not
    a promotion; the piece is crowned only if the sequence *ends* there."""

    CROWNS_AND_ENDS_PLY = "crowns_and_ends_ply"
    """English draughts, and the rule domain-model.md §2.1 describes: "a
    man reaching the crownhead becomes a king; in most variants this ends
    the ply even if further jumps exist". The sequence stops on the
    crownhead whether or not another jump was available."""


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

    Read through `king_reach`, which is how A64-014.4 continues a sequence
    after a man crowns mid-jump. Standalone king movement is still
    A64-014.5's.
    """

    mid_sequence_promotion: MidSequencePromotion
    """What crowning mid-capture does — see that enum.

    Replaces A64-014.2's provisional `promotion_ends_ply` boolean, which
    could not express what either configured variant actually does.
    """

    draw_rules: DrawRules
    """The thresholds that end a game in a draw — see `DrawRules`.

    Configuration only. Nothing in `engine` reads it: a draw is a property
    of the game's history (MT-12) and the kernel has none, so `game`'s
    `DrawRuleSet` is what evaluates these against a match.

    All three variants currently carry the same value, because three-fold
    repetition is the only threshold this repository documents. That is a
    recorded gap, not a claim that the variants agree — see `DrawRules`.
    """

    @property
    def maximum_capture_is_mandatory(self) -> bool:
        """Whether the capture played must be the one taking the most
        pieces available. Applied to complete sequences, after the search
        (A64-014.4)."""
        return self.capture_obligation is CaptureObligation.MAXIMUM

    @property
    def king_reach(self) -> int:
        """How far a king travels in one leg of a jump — to the far side of
        the board where kings fly, one square where they do not.

        Expressing "short king" as a reach of one rather than as a separate
        code path is what keeps the jump scan single: a short king is a
        flying king that cannot see past its neighbour, and the same loop
        is correct for both without a branch that only one variant ever
        takes.
        """
        return max(self.rows, self.columns) if self.kings_fly else 1

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
            mid_sequence_promotion=MidSequencePromotion.CROWNS_AND_CONTINUES,
            draw_rules=THREEFOLD_REPETITION_ONLY,
        ),
        BoardVariant.INTERNATIONAL_10X10: BoardGeometry(
            rows=10,
            columns=10,
            setup_rows_per_side=4,
            capture_is_mandatory=True,
            capture_obligation=CaptureObligation.MAXIMUM,
            men_may_capture_backward=True,
            kings_fly=True,
            mid_sequence_promotion=MidSequencePromotion.PASSES_THROUGH,
            draw_rules=THREEFOLD_REPETITION_ONLY,
        ),
        BoardVariant.ENGLISH_8X8: BoardGeometry(
            rows=8,
            columns=8,
            setup_rows_per_side=3,
            capture_is_mandatory=True,
            capture_obligation=CaptureObligation.ANY,
            men_may_capture_backward=False,
            kings_fly=False,
            mid_sequence_promotion=MidSequencePromotion.CROWNS_AND_ENDS_PLY,
            draw_rules=THREEFOLD_REPETITION_ONLY,
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


__all__ = [
    "BoardGeometry",
    "BoardVariant",
    "CaptureObligation",
    "MidSequencePromotion",
    "geometry_of",
]
