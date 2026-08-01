"""`Position` — value equality, the side to move, and the fingerprint.

domain-model.md §10.1 states why every one of these matters in a single
sentence: "**repetition detection requires value equality.** An entity
would compare by identity and the three-fold rule would never fire." Each
test below is one way that could quietly stop being true.
"""

from app.modules.engine import (
    Board,
    BoardCoordinate,
    BoardVariant,
    Piece,
    PieceRank,
    PlayerSide,
    Position,
    initial_board,
)

RUSSIAN = BoardVariant.RUSSIAN_8X8

A1 = BoardCoordinate(row=0, column=0)
C3 = BoardCoordinate(row=2, column=2)
LIGHT_MAN = Piece(side=PlayerSide.LIGHT, rank=PieceRank.MAN)
DARK_MAN = Piece(side=PlayerSide.DARK, rank=PieceRank.MAN)


def _position(squares: dict[BoardCoordinate, Piece], side: PlayerSide) -> Position:
    return Position(board=Board(RUSSIAN, squares), side_to_move=side)


class TestValueEquality:
    def test_two_positions_with_the_same_board_and_side_are_equal(self) -> None:
        assert _position({A1: LIGHT_MAN}, PlayerSide.LIGHT) == _position(
            {A1: LIGHT_MAN}, PlayerSide.LIGHT
        )

    def test_the_side_to_move_is_part_of_the_position(self) -> None:
        """The reason a `Board` alone is not a repetition key: the same
        placement with the other player to move is a different position,
        and a rule that conflated them would fire the three-fold draw on
        two positions that are not repeats."""
        assert _position({A1: LIGHT_MAN}, PlayerSide.LIGHT) != _position(
            {A1: LIGHT_MAN}, PlayerSide.DARK
        )

    def test_a_different_placement_is_a_different_position(self) -> None:
        assert _position({A1: LIGHT_MAN}, PlayerSide.LIGHT) != _position(
            {C3: LIGHT_MAN}, PlayerSide.LIGHT
        )

    def test_equal_positions_are_one_dictionary_key(self) -> None:
        """What a repetition counter needs, and what `Board` on its own
        cannot provide because it is deliberately unhashable."""
        seen = {_position({A1: LIGHT_MAN}, PlayerSide.LIGHT): 1}
        seen[_position({A1: LIGHT_MAN}, PlayerSide.LIGHT)] = 2

        assert seen == {_position({A1: LIGHT_MAN}, PlayerSide.LIGHT): 2}

    def test_positions_differing_only_by_side_are_two_keys(self) -> None:
        seen = {
            _position({A1: LIGHT_MAN}, PlayerSide.LIGHT): "light",
            _position({A1: LIGHT_MAN}, PlayerSide.DARK): "dark",
        }

        assert len(seen) == 2


class TestFingerprint:
    def test_it_names_the_variant_the_side_and_the_placement(self) -> None:
        position = _position({C3: LIGHT_MAN, A1: DARK_MAN}, PlayerSide.LIGHT)

        assert position.fingerprint == "russian_8x8/light/a1=dark:man,c3=light:man"

    def test_it_does_not_depend_on_the_order_the_board_was_built_in(self) -> None:
        """Dictionary order is an artefact of construction; two identical
        positions reached by different move orders must reduce to identical
        text, or a corpus written on one machine fails on another."""
        one = _position({A1: DARK_MAN, C3: LIGHT_MAN}, PlayerSide.LIGHT)
        other = _position({C3: LIGHT_MAN, A1: DARK_MAN}, PlayerSide.LIGHT)

        assert one.fingerprint == other.fingerprint

    def test_an_empty_board_still_names_the_variant_and_the_side(self) -> None:
        assert _position({}, PlayerSide.DARK).fingerprint == "russian_8x8/dark/"

    def test_equal_positions_share_a_fingerprint(self) -> None:
        assert (
            Position(board=initial_board(RUSSIAN), side_to_move=PlayerSide.LIGHT).fingerprint
            == Position(board=initial_board(RUSSIAN), side_to_move=PlayerSide.LIGHT).fingerprint
        )

    def test_the_side_to_move_changes_the_fingerprint(self) -> None:
        assert (
            _position({A1: LIGHT_MAN}, PlayerSide.LIGHT).fingerprint
            != _position({A1: LIGHT_MAN}, PlayerSide.DARK).fingerprint
        )
