"""`BoardVariant` and `BoardGeometry` — the configuration a board is built
from.

A64-014.1 asks for tests of board creation; this is the half of creation
that decides what a board's squares *are*. The geometry refusals are
asserted because every one of them is a way to produce a board that looks
symmetric and is not — and a board that starts unfair is not a defect
anybody notices from the code.
"""

import pytest

from app.modules.engine import (
    MAX_BOARD_DIMENSION,
    BoardCoordinate,
    BoardGeometry,
    BoardVariant,
    InvalidBoardState,
    geometry_of,
)

RUSSIAN = geometry_of(BoardVariant.RUSSIAN_8X8)
INTERNATIONAL = geometry_of(BoardVariant.INTERNATIONAL_10X10)


class TestConfiguredVariants:
    def test_every_variant_has_a_geometry(self) -> None:
        """The completeness guard for the table. A member added without an
        entry fails here rather than on the first request naming it."""
        for variant in BoardVariant:
            assert geometry_of(variant).rows > 0

    def test_russian_draughts_is_played_on_an_8x8_board(self) -> None:
        assert (RUSSIAN.rows, RUSSIAN.columns) == (8, 8)

    def test_russian_draughts_starts_with_twelve_men_a_side(self) -> None:
        assert RUSSIAN.men_per_side == 12

    def test_international_draughts_is_played_on_a_10x10_board(self) -> None:
        assert (INTERNATIONAL.rows, INTERNATIONAL.columns) == (10, 10)

    def test_international_draughts_starts_with_twenty_men_a_side(self) -> None:
        assert INTERNATIONAL.men_per_side == 20

    def test_no_variant_exceeds_the_addressable_board(self) -> None:
        """`MAX_BOARD_DIMENSION` and this table must not drift — a wider
        variant is a deliberate change to the bound, not a silent one."""
        for variant in BoardVariant:
            geometry = geometry_of(variant)
            assert geometry.rows <= MAX_BOARD_DIMENSION
            assert geometry.columns <= MAX_BOARD_DIMENSION


class TestPlayableSquares:
    def test_the_near_left_corner_is_playable(self) -> None:
        """Dark squares are the ones the game uses, and a1 is dark on every
        board either variant is played on."""
        assert RUSSIAN.is_playable(BoardCoordinate(row=0, column=0))

    def test_the_square_beside_it_is_not(self) -> None:
        assert not RUSSIAN.is_playable(BoardCoordinate(row=0, column=1))

    def test_a_square_off_the_board_is_not_playable(self) -> None:
        assert not RUSSIAN.is_playable(BoardCoordinate(row=8, column=0))

    def test_a_square_off_the_board_is_not_contained(self) -> None:
        assert not RUSSIAN.contains(BoardCoordinate(row=0, column=8))

    def test_an_8x8_board_has_thirty_two_playable_squares(self) -> None:
        """domain-model.md §2.1: "one of the 32 playable dark squares on an
        8x8 board (50 on 10x10)"."""
        assert len(list(RUSSIAN.playable_squares())) == 32

    def test_a_10x10_board_has_fifty_playable_squares(self) -> None:
        assert len(list(INTERNATIONAL.playable_squares())) == 50

    def test_every_enumerated_square_agrees_with_is_playable(self) -> None:
        """The two are separate implementations of one rule; a disagreement
        would let a piece be placed where nothing enumerates it."""
        for variant in BoardVariant:
            geometry = geometry_of(variant)
            assert all(geometry.is_playable(square) for square in geometry.playable_squares())

    def test_the_enumeration_is_row_major(self) -> None:
        squares = list(RUSSIAN.playable_squares())

        assert squares == sorted(squares)


class TestGeometryRefusals:
    def test_an_odd_file_count_is_refused(self) -> None:
        """It would give the two sides different numbers of men on a board
        that looks symmetric."""
        with pytest.raises(InvalidBoardState):
            BoardGeometry(rows=8, columns=7, setup_rows_per_side=3)

    def test_a_board_wider_than_the_addressable_bound_is_refused(self) -> None:
        with pytest.raises(InvalidBoardState):
            BoardGeometry(rows=8, columns=MAX_BOARD_DIMENSION + 2, setup_rows_per_side=3)

    def test_a_board_with_no_ranks_to_fill_is_refused(self) -> None:
        with pytest.raises(InvalidBoardState):
            BoardGeometry(rows=8, columns=8, setup_rows_per_side=0)

    def test_starting_ranks_that_meet_are_refused(self) -> None:
        """Four ranks a side on an 8x8 board leaves no gap, so the first
        move would have to be a capture."""
        with pytest.raises(InvalidBoardState):
            BoardGeometry(rows=8, columns=8, setup_rows_per_side=4)
