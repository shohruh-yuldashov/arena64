"""`initial_board` — the position every game starts from.

A64-014.1 names the requirements exactly: 12 light men, 12 dark men, no
kings, and only playable squares occupied. Each is asserted here as its own
test, because a setup that is wrong in one of the four is a game that is
unfair from the first move and that nobody reports as a bug.

The 10x10 assertions are the ones that would catch an 8 hard-coded
somewhere below the variant table.
"""

from app.modules.engine import (
    BoardVariant,
    PieceRank,
    PlayerSide,
    geometry_of,
    initial_board,
)

RUSSIAN = BoardVariant.RUSSIAN_8X8


class TestRussianOpeningPosition:
    def test_light_starts_with_twelve_men(self) -> None:
        assert initial_board(RUSSIAN).piece_count_for(PlayerSide.LIGHT) == 12

    def test_dark_starts_with_twelve_men(self) -> None:
        assert initial_board(RUSSIAN).piece_count_for(PlayerSide.DARK) == 12

    def test_twenty_four_pieces_stand_on_the_board(self) -> None:
        assert initial_board(RUSSIAN).piece_count() == 24

    def test_nobody_starts_with_a_king(self) -> None:
        """The only thing in the platform that produces a king is a man
        reaching the far rank."""
        board = initial_board(RUSSIAN)

        assert all(piece.rank is PieceRank.MAN for piece in board.occupied_squares.values())

    def test_only_playable_squares_are_occupied(self) -> None:
        board = initial_board(RUSSIAN)
        geometry = geometry_of(RUSSIAN)

        assert all(geometry.is_playable(square) for square in board.occupied_squares)

    def test_light_holds_the_three_ranks_nearest_it(self) -> None:
        """Row 0 is LIGHT's back rank — the orientation `BoardCoordinate`
        fixes, and the one the direction of a man's move will depend on."""
        board = initial_board(RUSSIAN)

        light_rows = {
            square.row
            for square, piece in board.occupied_squares.items()
            if piece.side is PlayerSide.LIGHT
        }

        assert light_rows == {0, 1, 2}

    def test_dark_holds_the_three_ranks_nearest_it(self) -> None:
        board = initial_board(RUSSIAN)

        dark_rows = {
            square.row
            for square, piece in board.occupied_squares.items()
            if piece.side is PlayerSide.DARK
        }

        assert dark_rows == {5, 6, 7}

    def test_the_two_middle_ranks_are_empty(self) -> None:
        """Without them the first move would have to be a capture."""
        board = initial_board(RUSSIAN)

        assert all(square.row not in {3, 4} for square in board.occupied_squares)

    def test_every_playable_square_outside_the_gap_is_filled(self) -> None:
        board = initial_board(RUSSIAN)
        geometry = geometry_of(RUSSIAN)

        unfilled = [
            square
            for square in geometry.playable_squares()
            if square.row not in {3, 4} and board.piece_at(square) is None
        ]

        assert unfilled == []


class TestInternationalOpeningPosition:
    """Configuration only — international draughts has capture and king
    rules of its own, and none of them exists. What is asserted is that the
    board below the variant table is not 8x8 with the eight renamed."""

    def test_each_side_starts_with_twenty_men(self) -> None:
        board = initial_board(BoardVariant.INTERNATIONAL_10X10)

        assert board.piece_count_for(PlayerSide.LIGHT) == 20
        assert board.piece_count_for(PlayerSide.DARK) == 20

    def test_forty_pieces_stand_on_the_board(self) -> None:
        assert initial_board(BoardVariant.INTERNATIONAL_10X10).piece_count() == 40

    def test_each_side_holds_the_four_ranks_nearest_it(self) -> None:
        board = initial_board(BoardVariant.INTERNATIONAL_10X10)

        rows_by_side: dict[PlayerSide, set[int]] = {PlayerSide.LIGHT: set(), PlayerSide.DARK: set()}
        for square, piece in board.occupied_squares.items():
            rows_by_side[piece.side].add(square.row)

        assert rows_by_side == {
            PlayerSide.LIGHT: {0, 1, 2, 3},
            PlayerSide.DARK: {6, 7, 8, 9},
        }


class TestFreshBoards:
    def test_two_openings_are_equal(self) -> None:
        assert initial_board(RUSSIAN) == initial_board(RUSSIAN)

    def test_two_openings_are_separate_boards(self) -> None:
        """A shared constant would be an invitation the day `Board` grows an
        internal cache; the boards are equal without being the same object."""
        assert initial_board(RUSSIAN) is not initial_board(RUSSIAN)
