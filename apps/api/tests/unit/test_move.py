"""`Move` — the path invariants and the ordering key.

domain-model.md §2.1: a move is "an ordered **path of squares**, plus every
piece captured along it", because "a multi-jump in draughts can reach the
same destination square by different capture paths, capturing different
pieces". These tests hold the shape that makes that statement true.
"""

import pytest

from app.modules.engine import BoardCoordinate, InvalidMove, Move, PieceRank

A1 = BoardCoordinate(row=0, column=0)
B2 = BoardCoordinate(row=1, column=1)
C3 = BoardCoordinate(row=2, column=2)
D4 = BoardCoordinate(row=3, column=3)
E5 = BoardCoordinate(row=4, column=4)


class TestPathInvariants:
    def test_a_quiet_move_is_a_two_square_path(self) -> None:
        move = Move(path=(A1, B2))

        assert (move.origin, move.destination) == (A1, B2)

    def test_the_destination_is_the_end_of_a_longer_path(self) -> None:
        """The shape A64-014.4 fills in; nothing here generates one yet."""
        move = Move(path=(A1, C3, E5), captured=(B2, D4))

        assert move.destination == E5

    def test_a_single_square_is_not_a_move(self) -> None:
        with pytest.raises(InvalidMove):
            Move(path=(A1,))

    def test_an_empty_path_is_not_a_move(self) -> None:
        with pytest.raises(InvalidMove):
            Move(path=())

    def test_a_step_to_the_same_square_is_refused(self) -> None:
        """It would make `len(path)` stop counting steps, which is what a
        replay walks."""
        with pytest.raises(InvalidMove):
            Move(path=(A1, A1))

    def test_a_repeated_square_inside_a_path_is_refused(self) -> None:
        with pytest.raises(InvalidMove):
            Move(path=(A1, C3, C3, E5))

    def test_a_path_may_revisit_a_square_it_did_not_just_leave(self) -> None:
        """Only *adjacent* duplicates are malformed. A capture sequence can
        legitimately cross its own track, and forbidding that here would
        reject legal draughts moves in A64-014.4."""
        move = Move(path=(A1, C3, A1), captured=(B2,))

        assert move.destination == A1

    def test_capturing_one_piece_twice_is_refused(self) -> None:
        """It would inflate a sequence's length, which is the input to the
        maximum-capture rule."""
        with pytest.raises(InvalidMove):
            Move(path=(A1, C3, E5), captured=(B2, B2))


class TestMoveShape:
    def test_a_move_with_nothing_captured_is_quiet(self) -> None:
        assert not Move(path=(A1, B2)).is_capture

    def test_a_move_with_something_captured_is_a_capture(self) -> None:
        assert Move(path=(A1, C3), captured=(B2,)).is_capture

    def test_a_move_is_immutable(self) -> None:
        move = Move(path=(A1, B2))

        with pytest.raises(AttributeError):
            move.path = (A1, C3)  # type: ignore[misc]

    def test_promotion_is_absent_unless_stated(self) -> None:
        assert Move(path=(A1, B2)).promotes_to is None

    def test_promotion_records_the_resulting_rank(self) -> None:
        assert Move(path=(A1, B2), promotes_to=PieceRank.KING).promotes_to is PieceRank.KING


class TestOrdering:
    def test_moves_order_by_origin_then_destination(self) -> None:
        earlier = Move(path=(A1, B2))
        later = Move(path=(C3, D4))

        assert sorted([later, earlier], key=lambda move: move.sort_key) == [earlier, later]

    def test_two_moves_from_one_square_order_by_destination(self) -> None:
        to_b2 = Move(path=(C3, B2))
        to_d4 = Move(path=(C3, D4))

        assert sorted([to_d4, to_b2], key=lambda move: move.sort_key) == [to_b2, to_d4]

    def test_the_promotion_rank_is_not_part_of_the_key(self) -> None:
        """Crowning is a function of where the path ends, so it can never
        be a tie-break — and a key that included it would compare `None`
        against a rank and raise from inside a sort."""
        crowned = Move(path=(A1, B2), promotes_to=PieceRank.KING)
        plain = Move(path=(A1, B2))

        assert crowned.sort_key == plain.sort_key


class TestNotation:
    def test_a_quiet_move_reads_with_a_dash(self) -> None:
        assert str(Move(path=(A1, B2))) == "a1-b2"

    def test_a_capture_reads_with_a_cross(self) -> None:
        assert str(Move(path=(A1, C3), captured=(B2,))) == "a1xc3"
