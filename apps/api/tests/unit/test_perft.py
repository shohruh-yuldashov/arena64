"""Perft — move generation verified by node count, A64-014.9.

architecture.md AD-13 lists this first among what makes a rules kernel
trustworthy: "move-generation node counts verified against known reference
values at increasing depth". It is the **only** check in this suite with an
external oracle. Everything else asks the engine whether it agrees with
itself; this asks whether it agrees with published numbers computed by
other people's programs decades ago.

## English draughts is the oracle

The perft series for English/American checkers from the opening position
is long-published and widely reproduced:

    depth   0      1      2      3       4       5        6
    nodes   1      7     49    302   1,469   7,361   36,768

`BoardVariant.ENGLISH_8X8` was added in A64-014.5 as configuration only —
men that capture forward, kings that move one square, crowning that ends
the ply — and this engine reproduces the series exactly. That is
independent evidence for move generation, mandatory capture, the capture
walk, promotion-ends-ply and the short-king reach all at once, and it is
worth more than the rest of the suite combined.

## Russian draughts has no oracle here

**No published Russian 8x8 perft table was available to this task**, so
the Russian numbers below are *not* verified against an external source.
Inventing values to assert against would be worse than having none — the
test would pass, prove nothing, and read as though it proved something.

What is asserted for Russian instead is two things that carry real weight:

1. **It must agree with English through depth 4, and it does.** The three
   rules that differ — backward capture for men, flying kings, promotion
   continuing mid-sequence — cannot apply any earlier. A king needs a man
   to reach the crownhead, which cannot happen before ply 5; a backward
   capture needs an enemy piece *behind* a man, which needs two moves from
   each side, so also ply 5. Agreement through 4 and divergence at 5 is
   therefore a prediction the rules make, and the engine meets it.
2. **The deeper numbers are pinned as a characterization baseline** — a
   record of what this build does, so a later change that moves them is
   visible. That is a regression detector, not a correctness proof, and
   the test says so.

Closing this gap needs a published table or a second implementation, and
is recorded as an outstanding audit item in `specs/game-engine.md`.

## Depth, and why the deep runs are opt-in

Perft is exponential and this one is deliberately naive — no caching, no
bulk counting, and every move re-validated through `MoveApplier` because
that is the path a real move takes. Depth 6 costs about four seconds per
variant, which is more than a suite people run on every save should pay.

Depths up to 5 run always. Depth 6 runs when `ENGINE_PERFT_DEEP` is set,
and is skipped with a reason rather than silently omitted — a check nobody
knows exists is a check nobody runs.
"""

import os
from functools import cache

import pytest

from app.modules.engine import (
    BoardVariant,
    MoveApplier,
    MoveGenerator,
    MoveValidator,
    PlayerSide,
    Position,
    initial_board,
)
from tests.perft import perft, perft_divide

generator = MoveGenerator()
applier = MoveApplier(MoveValidator(generator))

ENGLISH_REFERENCE = {0: 1, 1: 7, 2: 49, 3: 302, 4: 1_469, 5: 7_361, 6: 36_768}
"""Published English/American checkers perft from the opening position.

An **external** oracle: these are not this engine's output written down,
they are the numbers this engine had to reproduce."""

RUSSIAN_BASELINE = {0: 1, 1: 7, 2: 49, 3: 302, 4: 1_469, 5: 7_482, 6: 37_986}
"""This build's Russian 8x8 perft.

**Not externally verified** — see the module docstring. Depths 0 to 4 are
independently justified by having to equal English; 5 and 6 are a
characterization baseline that detects change rather than proving
correctness.
"""

DEEP = pytest.mark.skipif(
    not os.environ.get("ENGINE_PERFT_DEEP"),
    reason="depth 6 costs ~4s per variant; set ENGINE_PERFT_DEEP to run it",
)


def opening(variant: BoardVariant) -> Position:
    return Position(board=initial_board(variant), side_to_move=PlayerSide.LIGHT)


@cache
def nodes(variant: BoardVariant, depth: int) -> int:
    """Perft, memoised **per test run**.

    The cache is on this helper and emphatically not inside `perft`: a
    perft that cached would share a transposition bug with the generator
    it is checking and agree with it beautifully. Several assertions below
    want the same number, and computing depth 5 four times costs three
    seconds for nothing.
    """
    return perft(opening(variant), depth, generator, applier)


class TestEnglishAgainstPublishedValues:
    """The one external oracle in the suite."""

    @pytest.mark.parametrize("depth", [0, 1, 2, 3, 4, 5])
    def test_the_node_count_matches_the_published_series(self, depth: int) -> None:
        assert nodes(BoardVariant.ENGLISH_8X8, depth) == ENGLISH_REFERENCE[depth]

    @DEEP
    def test_depth_six_matches_too(self) -> None:
        assert nodes(BoardVariant.ENGLISH_8X8, 6) == ENGLISH_REFERENCE[6]


class TestRussianAgainstTheRulesItShares:
    """Russian has no published table here, so what is asserted is a
    prediction the rules make rather than a number from a book."""

    @pytest.mark.parametrize("depth", [0, 1, 2, 3, 4])
    def test_it_agrees_with_english_while_the_rules_coincide(self, depth: int) -> None:
        """The three rules that differ cannot apply before ply 5: a king
        needs a man on the crownhead, and a backward capture needs an enemy
        piece behind a man. Both take at least two moves from each side."""
        assert nodes(BoardVariant.RUSSIAN_8X8, depth) == ENGLISH_REFERENCE[depth]

    def test_it_diverges_from_english_at_depth_five(self) -> None:
        """Where backward captures for men first become reachable. A
        Russian count that still matched English here would mean the
        variant configuration was doing nothing."""
        assert nodes(BoardVariant.RUSSIAN_8X8, 5) != ENGLISH_REFERENCE[5]

    def test_it_offers_more_at_depth_five_and_not_fewer(self) -> None:
        """Russian's extra rules only ever *add* moves at this depth —
        backward captures — so a smaller count would mean something was
        being lost rather than gained."""
        assert nodes(BoardVariant.RUSSIAN_8X8, 5) > ENGLISH_REFERENCE[5]

    @pytest.mark.parametrize("depth", [0, 1, 2, 3, 4, 5])
    def test_the_characterization_baseline_holds(self, depth: int) -> None:
        """A regression detector, not a proof. If this fails, a rules
        change moved the numbers — decide whether it should have, and
        update the baseline deliberately."""
        assert nodes(BoardVariant.RUSSIAN_8X8, depth) == RUSSIAN_BASELINE[depth]

    @DEEP
    def test_the_deep_baseline_holds(self) -> None:
        assert nodes(BoardVariant.RUSSIAN_8X8, 6) == RUSSIAN_BASELINE[6]


class TestInternationalIsNotPinned:
    """10x10 has a published table too, and this task did not have it. The
    shallow depths are asserted from first principles instead, so the
    variant is not left entirely unchecked."""

    def test_the_opening_offers_nine_moves(self) -> None:
        """Four rows of twenty men: only the fourth rank can move, and its
        five men have nine forward steps between them — the man on the j
        file has one, because its outer diagonal leaves the board, and the
        other four have two each."""
        assert nodes(BoardVariant.INTERNATIONAL_10X10, 1) == 9

    def test_the_second_ply_is_nine_replies_to_each(self) -> None:
        """No capture is reachable at ply 2, so the count is the square."""
        assert nodes(BoardVariant.INTERNATIONAL_10X10, 2) == 81


class TestPerftItself:
    def test_depth_zero_counts_the_position(self) -> None:
        """The convention every published table uses. Without it the
        recursion has no base case and every number is off by a ply."""
        assert nodes(BoardVariant.RUSSIAN_8X8, 0) == 1

    def test_a_negative_depth_is_refused(self) -> None:
        with pytest.raises(ValueError):
            nodes(BoardVariant.RUSSIAN_8X8, -1)

    def test_it_is_deterministic(self) -> None:
        assert nodes(BoardVariant.ENGLISH_8X8, 4) == nodes(BoardVariant.ENGLISH_8X8, 4)

    def test_a_terminal_position_contributes_nothing_deeper(self) -> None:
        """A finished game has no continuations to count, so depth 2 from a
        position with one move that ends the game is zero, not one."""
        from app.modules.engine import Board, BoardCoordinate, Piece, PieceRank

        won = Position(
            board=Board(
                BoardVariant.RUSSIAN_8X8,
                {
                    BoardCoordinate.parse("c3"): Piece(side=PlayerSide.LIGHT, rank=PieceRank.MAN),
                    BoardCoordinate.parse("d4"): Piece(side=PlayerSide.DARK, rank=PieceRank.MAN),
                },
            ),
            side_to_move=PlayerSide.LIGHT,
        )

        assert perft(won, 1, generator, applier) == 1
        assert perft(won, 2, generator, applier) == 0

    def test_divide_sums_to_the_total(self) -> None:
        """The tool for finding *where* a count went wrong: a total that
        disagrees with a reference says the generator is wrong somewhere, a
        divide says which opening move's subtree carries it."""
        start = opening(BoardVariant.ENGLISH_8X8)

        divided = perft_divide(start, 4, generator, applier)

        assert sum(divided.values()) == ENGLISH_REFERENCE[4]

    def test_divide_names_every_legal_first_move(self) -> None:
        start = opening(BoardVariant.ENGLISH_8X8)

        assert set(perft_divide(start, 2, generator, applier)) == set(generator.legal_moves(start))
