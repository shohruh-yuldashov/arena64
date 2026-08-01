"""`Board` — creation, lookup, placement, removal, relocation and counting.

A64-014.1 asks for board creation, piece counting, invalid coordinates and
invalid placement. Two further properties are asserted because the rest of
the kernel is built on them and neither fails loudly:

- **Nothing mutates.** Every operation returns a new board and leaves its
  receiver alone. `fairplay` will explore positions by applying and
  discarding, and a board that changed under a caller would corrupt a
  search rather than crash it (architecture.md AD-13).
- **The storage does not escape.** `occupied_squares` is a read-only view,
  so no caller can reach past the board's own refusals.

`move` is asserted as *relocation mechanics* only — an empty origin, an
occupied destination, an unusable destination. Whether a relocation is a
legal draughts move is move generation's question, which this task does not
implement and which nothing here pretends to answer.
"""

import pytest

from app.modules.engine import (
    Board,
    BoardCoordinate,
    BoardVariant,
    DestinationOccupied,
    InvalidBoardState,
    InvalidCoordinate,
    Piece,
    PieceNotFound,
    PieceRank,
    PlayerSide,
)

RUSSIAN = BoardVariant.RUSSIAN_8X8

A1 = BoardCoordinate(row=0, column=0)
C1 = BoardCoordinate(row=0, column=2)
B2 = BoardCoordinate(row=1, column=1)
B1 = BoardCoordinate(row=0, column=1)
"""A light square: `(0 + 1)` is odd, so no draughts piece ever stands here."""

OFF_BOARD = BoardCoordinate(row=8, column=0)
"""Addressable — an international board has a ninth rank — but off an 8x8
board. The case that separates the coordinate's bound from the board's."""

LIGHT_MAN = Piece(side=PlayerSide.LIGHT, rank=PieceRank.MAN)
DARK_MAN = Piece(side=PlayerSide.DARK, rank=PieceRank.MAN)
DARK_KING = Piece(side=PlayerSide.DARK, rank=PieceRank.KING)


class TestCreation:
    def test_an_empty_board_holds_nothing(self) -> None:
        assert Board.empty(RUSSIAN).piece_count() == 0

    def test_an_empty_board_keeps_its_variant(self) -> None:
        assert Board.empty(RUSSIAN).variant is RUSSIAN

    def test_a_board_can_be_built_from_a_placement(self) -> None:
        board = Board(RUSSIAN, {A1: LIGHT_MAN})

        assert board.piece_at(A1) == LIGHT_MAN

    def test_the_placement_is_copied_rather_than_held(self) -> None:
        """A caller keeping its dictionary must not be able to edit a board
        that has already been built from it."""
        squares = {A1: LIGHT_MAN}
        board = Board(RUSSIAN, squares)

        squares[C1] = DARK_MAN

        assert board.piece_count() == 1

    def test_a_piece_on_a_light_square_is_an_invalid_state(self) -> None:
        with pytest.raises(InvalidBoardState):
            Board(RUSSIAN, {B1: LIGHT_MAN})

    def test_a_piece_off_the_board_is_an_invalid_state(self) -> None:
        with pytest.raises(InvalidBoardState):
            Board(RUSSIAN, {OFF_BOARD: LIGHT_MAN})

    def test_a_square_off_an_8x8_board_is_on_a_10x10_one(self) -> None:
        """The same coordinate, refused by one variant and accepted by the
        other — which is what makes the board, not the coordinate, the
        authority on its own geometry."""
        board = Board(BoardVariant.INTERNATIONAL_10X10, {OFF_BOARD: LIGHT_MAN})

        assert board.piece_at(OFF_BOARD) == LIGHT_MAN


class TestValueEquality:
    """domain-model.md §16.1 rejects an entity `Board` because "the
    three-fold repetition draw rule requires positions to compare by
    value"."""

    def test_two_boards_with_the_same_pieces_are_equal(self) -> None:
        assert Board(RUSSIAN, {A1: LIGHT_MAN}) == Board(RUSSIAN, {A1: LIGHT_MAN})

    def test_a_different_placement_is_a_different_board(self) -> None:
        assert Board(RUSSIAN, {A1: LIGHT_MAN}) != Board(RUSSIAN, {C1: LIGHT_MAN})

    def test_a_different_piece_on_the_same_square_is_a_different_board(self) -> None:
        assert Board(RUSSIAN, {A1: DARK_MAN}) != Board(RUSSIAN, {A1: DARK_KING})

    def test_the_same_placement_under_another_variant_is_a_different_board(self) -> None:
        assert Board(RUSSIAN, {A1: LIGHT_MAN}) != Board(
            BoardVariant.INTERNATIONAL_10X10, {A1: LIGHT_MAN}
        )

    def test_a_board_is_not_equal_to_something_that_is_not_a_board(self) -> None:
        assert Board.empty(RUSSIAN) != RUSSIAN


class TestLookup:
    def test_an_empty_square_holds_nothing(self) -> None:
        """`None`, not an exception: most of the board is empty."""
        assert Board.empty(RUSSIAN).piece_at(A1) is None

    def test_a_square_off_the_board_holds_nothing(self) -> None:
        assert Board.empty(RUSSIAN).piece_at(OFF_BOARD) is None

    def test_occupied_squares_reports_every_piece(self) -> None:
        board = Board(RUSSIAN, {A1: LIGHT_MAN, C1: DARK_MAN})

        assert dict(board.occupied_squares) == {A1: LIGHT_MAN, C1: DARK_MAN}

    def test_occupied_squares_cannot_be_written_through(self) -> None:
        """The board's storage never escapes in a form a caller could edit,
        which is what makes its refusals unbypassable."""
        board = Board(RUSSIAN, {A1: LIGHT_MAN})

        with pytest.raises(TypeError):
            board.occupied_squares[C1] = DARK_MAN  # type: ignore[index]


class TestPlacement:
    def test_placing_a_piece_returns_a_board_holding_it(self) -> None:
        assert Board.empty(RUSSIAN).place(A1, LIGHT_MAN).piece_at(A1) == LIGHT_MAN

    def test_placing_a_piece_leaves_the_original_board_empty(self) -> None:
        board = Board.empty(RUSSIAN)

        board.place(A1, LIGHT_MAN)

        assert board.piece_count() == 0

    def test_a_light_square_cannot_hold_a_piece(self) -> None:
        with pytest.raises(InvalidCoordinate):
            Board.empty(RUSSIAN).place(B1, LIGHT_MAN)

    def test_a_square_off_the_board_cannot_hold_a_piece(self) -> None:
        with pytest.raises(InvalidCoordinate):
            Board.empty(RUSSIAN).place(OFF_BOARD, LIGHT_MAN)

    def test_an_occupied_square_refuses_a_second_piece(self) -> None:
        board = Board(RUSSIAN, {A1: LIGHT_MAN})

        with pytest.raises(DestinationOccupied):
            board.place(A1, DARK_MAN)


class TestRemoval:
    def test_removing_a_piece_empties_the_square(self) -> None:
        board = Board(RUSSIAN, {A1: LIGHT_MAN})

        assert board.remove(A1).piece_at(A1) is None

    def test_removing_a_piece_leaves_the_original_board_intact(self) -> None:
        board = Board(RUSSIAN, {A1: LIGHT_MAN})

        board.remove(A1)

        assert board.piece_at(A1) == LIGHT_MAN

    def test_removing_from_an_empty_square_is_refused(self) -> None:
        with pytest.raises(PieceNotFound):
            Board.empty(RUSSIAN).remove(A1)


class TestRelocation:
    """Mechanics only — see the module docstring. Nothing here asserts that
    a relocation is a legal draughts move, because `Board` does not know."""

    def test_the_piece_arrives_on_the_destination(self) -> None:
        board = Board(RUSSIAN, {A1: LIGHT_MAN})

        assert board.move(A1, B2).piece_at(B2) == LIGHT_MAN

    def test_the_origin_is_left_empty(self) -> None:
        board = Board(RUSSIAN, {A1: LIGHT_MAN})

        assert board.move(A1, B2).piece_at(A1) is None

    def test_relocation_does_not_change_the_piece_count(self) -> None:
        board = Board(RUSSIAN, {A1: LIGHT_MAN, C1: DARK_MAN})

        assert board.move(A1, B2).piece_count() == 2

    def test_a_king_stays_a_king(self) -> None:
        """Promotion is a rule about arriving at the far rank, not
        something relocation does or undoes."""
        board = Board(RUSSIAN, {A1: DARK_KING})

        assert board.move(A1, B2).piece_at(B2) == DARK_KING

    def test_an_empty_origin_is_refused(self) -> None:
        with pytest.raises(PieceNotFound):
            Board.empty(RUSSIAN).move(A1, B2)

    def test_an_occupied_destination_is_refused(self) -> None:
        """Not a capture: capture removes the taken piece first, and that
        is move application's job."""
        board = Board(RUSSIAN, {A1: LIGHT_MAN, C1: DARK_MAN})

        with pytest.raises(DestinationOccupied):
            board.move(A1, C1)

    def test_relocating_a_piece_onto_itself_is_refused(self) -> None:
        board = Board(RUSSIAN, {A1: LIGHT_MAN})

        with pytest.raises(DestinationOccupied):
            board.move(A1, A1)

    def test_a_light_square_is_refused_as_a_destination(self) -> None:
        board = Board(RUSSIAN, {A1: LIGHT_MAN})

        with pytest.raises(InvalidCoordinate):
            board.move(A1, B1)

    def test_a_destination_off_the_board_is_refused(self) -> None:
        board = Board(RUSSIAN, {A1: LIGHT_MAN})

        with pytest.raises(InvalidCoordinate):
            board.move(A1, OFF_BOARD)


class TestPieceCounting:
    def test_an_empty_board_counts_nothing(self) -> None:
        assert Board.empty(RUSSIAN).piece_count() == 0

    def test_the_total_counts_both_sides(self) -> None:
        board = Board(RUSSIAN, {A1: LIGHT_MAN, C1: DARK_MAN})

        assert board.piece_count() == 2

    def test_a_side_counts_only_its_own_pieces(self) -> None:
        board = Board(RUSSIAN, {A1: LIGHT_MAN, C1: DARK_MAN, B2: DARK_KING})

        assert board.piece_count_for(PlayerSide.DARK) == 2

    def test_a_side_with_nothing_left_counts_zero(self) -> None:
        """The condition that ends a game by capture, once `game` reads it."""
        board = Board(RUSSIAN, {A1: LIGHT_MAN})

        assert board.piece_count_for(PlayerSide.DARK) == 0

    def test_kings_and_men_count_alike(self) -> None:
        board = Board(RUSSIAN, {C1: DARK_MAN, B2: DARK_KING})

        assert board.piece_count_for(PlayerSide.DARK) == 2
