"""`BoardCoordinate` — addressability, value semantics, and the refusals.

A64-014.1 asks for coordinate validation and invalid coordinates. What is
asserted beyond that is the property the rest of the kernel is built on and
that would fail *silently* rather than loudly: two coordinates naming one
square must be one dictionary key, because that is how `Board` stores a
position and how repetition detection will eventually compare two of them
(domain-model.md §16.1).
"""

import pytest

from app.modules.engine import MAX_BOARD_DIMENSION, BoardCoordinate, InvalidCoordinate


class TestValueSemantics:
    def test_two_coordinates_naming_one_square_are_equal(self) -> None:
        assert BoardCoordinate(row=2, column=3) == BoardCoordinate(row=2, column=3)

    def test_row_and_column_are_not_interchangeable(self) -> None:
        """The one confusion an `(int, int)` pair invites."""
        assert BoardCoordinate(row=2, column=3) != BoardCoordinate(row=3, column=2)

    def test_equal_coordinates_are_one_dictionary_key(self) -> None:
        squares = {BoardCoordinate(row=0, column=0): "first"}
        squares[BoardCoordinate(row=0, column=0)] = "second"

        assert squares == {BoardCoordinate(row=0, column=0): "second"}

    def test_coordinates_sort_row_major(self) -> None:
        """Deterministic iteration order — see the class docstring on why a
        position's hash must not depend on insertion order."""
        unordered = [
            BoardCoordinate(row=1, column=0),
            BoardCoordinate(row=0, column=2),
            BoardCoordinate(row=0, column=0),
        ]

        assert sorted(unordered) == [
            BoardCoordinate(row=0, column=0),
            BoardCoordinate(row=0, column=2),
            BoardCoordinate(row=1, column=0),
        ]

    def test_a_coordinate_cannot_be_reassigned(self) -> None:
        coordinate = BoardCoordinate(row=0, column=0)

        with pytest.raises(AttributeError):
            coordinate.row = 4  # type: ignore[misc]


class TestValidation:
    """`0 .. MAX_BOARD_DIMENSION - 1` on both axes — the squares the
    platform can address at all, on no board in particular."""

    def test_the_near_left_corner_is_addressable(self) -> None:
        assert BoardCoordinate(row=0, column=0).row == 0

    def test_the_far_corner_of_the_largest_board_is_addressable(self) -> None:
        last = MAX_BOARD_DIMENSION - 1

        assert BoardCoordinate(row=last, column=last).column == last

    def test_a_negative_row_is_refused(self) -> None:
        """Not merely wrong — Python would index a sequence backwards from
        it, which is the bug this bound exists to make impossible."""
        with pytest.raises(InvalidCoordinate):
            BoardCoordinate(row=-1, column=0)

    def test_a_negative_column_is_refused(self) -> None:
        with pytest.raises(InvalidCoordinate):
            BoardCoordinate(row=0, column=-1)

    def test_a_row_past_the_largest_board_is_refused(self) -> None:
        with pytest.raises(InvalidCoordinate):
            BoardCoordinate(row=MAX_BOARD_DIMENSION, column=0)

    def test_a_column_past_the_largest_board_is_refused(self) -> None:
        with pytest.raises(InvalidCoordinate):
            BoardCoordinate(row=0, column=MAX_BOARD_DIMENSION)

    def test_a_square_off_an_8x8_board_is_still_addressable(self) -> None:
        """The split the module docstring argues for: this type owns
        "addressable at all", the board owns "on *this* board"."""
        assert BoardCoordinate(row=9, column=9).row == 9


class TestReadableRepresentation:
    def test_the_near_left_corner_reads_as_a1(self) -> None:
        assert str(BoardCoordinate(row=0, column=0)) == "a1"

    def test_rows_are_one_based_and_columns_lettered(self) -> None:
        assert str(BoardCoordinate(row=7, column=7)) == "h8"

    def test_every_addressable_square_has_a_representation(self) -> None:
        """A logger must never raise (CLAUDE.md §8.10), so the letters have
        to cover the bound rather than one variant's width."""
        for row in range(MAX_BOARD_DIMENSION):
            for column in range(MAX_BOARD_DIMENSION):
                assert str(BoardCoordinate(row=row, column=column))

    def test_the_repr_names_both_axes(self) -> None:
        assert repr(BoardCoordinate(row=2, column=3)) == "BoardCoordinate(row=2, column=3)"
