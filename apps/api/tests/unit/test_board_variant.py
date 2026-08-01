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
    DIAGONAL_DIRECTIONS,
    MAX_BOARD_DIMENSION,
    BoardCoordinate,
    BoardGeometry,
    BoardVariant,
    CaptureObligation,
    InvalidBoardState,
    PlayerSide,
    geometry_of,
)

RUSSIAN = geometry_of(BoardVariant.RUSSIAN_8X8)
INTERNATIONAL = geometry_of(BoardVariant.INTERNATIONAL_10X10)


def _geometry(*, rows: int = 8, columns: int = 8, setup_rows_per_side: int = 3) -> BoardGeometry:
    """A geometry differing from Russian draughts only in its shape.

    The rule axes are held fixed so that a refusal test below is visibly
    about the dimension it varies and nothing else.
    """
    return BoardGeometry(
        rows=rows,
        columns=columns,
        setup_rows_per_side=setup_rows_per_side,
        capture_is_mandatory=True,
        capture_obligation=CaptureObligation.ANY,
        men_may_capture_backward=True,
        kings_fly=True,
        promotion_ends_ply=False,
    )


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


class TestRuleAxes:
    """A64-014.2. The axes exist so that `MoveGenerator` asks the geometry
    a question instead of asking which variant it is — see that module on
    why a variant check inside a rules algorithm is the defect that makes a
    second variant unshippable."""

    def test_both_variants_oblige_a_capture(self) -> None:
        for variant in BoardVariant:
            assert geometry_of(variant).capture_is_mandatory

    def test_russian_draughts_lets_the_player_choose_which_capture(self) -> None:
        assert RUSSIAN.capture_obligation is CaptureObligation.ANY
        assert not RUSSIAN.maximum_capture_is_mandatory

    def test_international_draughts_obliges_the_largest_capture(self) -> None:
        """Recorded, not enforced: selecting it needs sequences to compare,
        which arrive with A64-014.4."""
        assert INTERNATIONAL.maximum_capture_is_mandatory

    def test_both_variants_let_men_capture_backward(self) -> None:
        for variant in BoardVariant:
            assert geometry_of(variant).men_may_capture_backward

    def test_light_advances_up_the_board_and_dark_down_it(self) -> None:
        assert RUSSIAN.forward_step(PlayerSide.LIGHT) == 1
        assert RUSSIAN.forward_step(PlayerSide.DARK) == -1

    def test_each_side_is_crowned_on_the_far_rank(self) -> None:
        assert RUSSIAN.promotion_row(PlayerSide.LIGHT) == 7
        assert RUSSIAN.promotion_row(PlayerSide.DARK) == 0

    def test_promotion_rows_follow_the_board_size(self) -> None:
        """Derived rather than configured, so a 10x10 variant cannot
        declare a crownhead that contradicts its own last rank."""
        assert INTERNATIONAL.promotion_row(PlayerSide.LIGHT) == 9

    def test_a_square_on_the_far_rank_crowns_the_side_advancing_to_it(self) -> None:
        assert RUSSIAN.is_promotion_square(PlayerSide.LIGHT, BoardCoordinate(row=7, column=7))

    def test_a_square_on_the_far_rank_does_not_crown_the_other_side(self) -> None:
        assert not RUSSIAN.is_promotion_square(PlayerSide.DARK, BoardCoordinate(row=7, column=7))

    def test_each_side_advances_along_two_diagonals(self) -> None:
        forward = RUSSIAN.forward_directions(PlayerSide.LIGHT)

        assert [direction.row_step for direction in forward] == [1, 1]

    def test_men_jump_along_all_four_diagonals_when_backward_capture_is_allowed(self) -> None:
        assert RUSSIAN.man_capture_directions(PlayerSide.LIGHT) == DIAGONAL_DIRECTIONS


class TestStep:
    def test_a_step_lands_on_the_adjacent_diagonal_square(self) -> None:
        origin = BoardCoordinate(row=2, column=2)
        forward_right = RUSSIAN.forward_directions(PlayerSide.LIGHT)[1]

        assert RUSSIAN.step(origin, forward_right) == BoardCoordinate(row=3, column=3)

    def test_a_distance_of_two_lands_on_the_jump_square(self) -> None:
        origin = BoardCoordinate(row=2, column=2)
        forward_right = RUSSIAN.forward_directions(PlayerSide.LIGHT)[1]

        assert RUSSIAN.step(origin, forward_right, distance=2) == BoardCoordinate(row=4, column=4)

    def test_stepping_off_the_edge_answers_nothing(self) -> None:
        """`None` and not an exception: every generator walks off the rim
        twice per corner piece, and a negative rank would otherwise raise
        `InvalidCoordinate` from inside a loop."""
        corner = BoardCoordinate(row=0, column=0)
        backward_left = DIAGONAL_DIRECTIONS[0]

        assert RUSSIAN.step(corner, backward_left) is None

    def test_stepping_past_the_far_edge_answers_nothing(self) -> None:
        corner = BoardCoordinate(row=7, column=7)
        forward_right = RUSSIAN.forward_directions(PlayerSide.LIGHT)[1]

        assert RUSSIAN.step(corner, forward_right) is None


class TestGeometryRefusals:
    def test_an_odd_file_count_is_refused(self) -> None:
        """It would give the two sides different numbers of men on a board
        that looks symmetric."""
        with pytest.raises(InvalidBoardState):
            _geometry(columns=7)

    def test_a_board_wider_than_the_addressable_bound_is_refused(self) -> None:
        with pytest.raises(InvalidBoardState):
            _geometry(columns=MAX_BOARD_DIMENSION + 2)

    def test_a_board_with_no_ranks_to_fill_is_refused(self) -> None:
        with pytest.raises(InvalidBoardState):
            _geometry(setup_rows_per_side=0)

    def test_starting_ranks_that_meet_are_refused(self) -> None:
        """Four ranks a side on an 8x8 board leaves no gap, so the first
        move would have to be a capture."""
        with pytest.raises(InvalidBoardState):
            _geometry(setup_rows_per_side=4)
