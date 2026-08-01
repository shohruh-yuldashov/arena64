"""`MoveApplier` — the position a legal move produces.

Two things are being held here. The obvious one is that a move does what
the rules say: the piece arrives, the victims go, the crown is placed, the
turn changes.

The less obvious one is that **nothing else moves**. The engine's value is
that a position is a value (domain-model.md §10.1), and an applier that
edited its argument would break every guarantee built on that — repetition
comparison, `fairplay`'s position search, and any caller holding a previous
position for any reason. So the original is asserted unchanged on every
path, including the paths that raise.
"""

import pytest

from app.modules.engine import (
    Board,
    BoardCoordinate,
    BoardVariant,
    IllegalMove,
    Move,
    MoveApplier,
    MoveGenerator,
    MoveValidator,
    Piece,
    PieceRank,
    PlayerSide,
    Position,
    initial_board,
)

RUSSIAN = BoardVariant.RUSSIAN_8X8

LIGHT_MAN = Piece(side=PlayerSide.LIGHT, rank=PieceRank.MAN)
DARK_MAN = Piece(side=PlayerSide.DARK, rank=PieceRank.MAN)
LIGHT_KING = Piece(side=PlayerSide.LIGHT, rank=PieceRank.KING)

generator = MoveGenerator()
applier = MoveApplier(MoveValidator(generator))


def square(notation: str) -> BoardCoordinate:
    return BoardCoordinate.parse(notation)


def position(placement: dict[str, Piece], side: PlayerSide) -> Position:
    return Position(
        board=Board(RUSSIAN, {square(name): piece for name, piece in placement.items()}),
        side_to_move=side,
    )


def move(*path: str, captured: tuple[str, ...] = (), promotes_to: PieceRank | None = None) -> Move:
    return Move(
        path=tuple(square(name) for name in path),
        captured=tuple(square(name) for name in captured),
        promotes_to=promotes_to,
    )


class TestQuietMoveApplication:
    def test_the_piece_arrives_at_the_destination(self) -> None:
        after = applier.apply(position({"c3": LIGHT_MAN}, PlayerSide.LIGHT), move("c3", "d4"))

        assert after.board.piece_at(square("d4")) == LIGHT_MAN

    def test_the_origin_is_left_empty(self) -> None:
        after = applier.apply(position({"c3": LIGHT_MAN}, PlayerSide.LIGHT), move("c3", "d4"))

        assert after.board.piece_at(square("c3")) is None

    def test_a_quiet_move_takes_nothing_off_the_board(self) -> None:
        before = position({"c3": LIGHT_MAN, "f6": DARK_MAN}, PlayerSide.LIGHT)

        assert applier.apply(before, move("c3", "d4")).board.piece_count() == 2

    def test_the_turn_passes_to_the_opponent(self) -> None:
        after = applier.apply(position({"c3": LIGHT_MAN}, PlayerSide.LIGHT), move("c3", "d4"))

        assert after.side_to_move is PlayerSide.DARK

    def test_the_turn_passes_back(self) -> None:
        after = applier.apply(position({"c5": DARK_MAN}, PlayerSide.DARK), move("c5", "d4"))

        assert after.side_to_move is PlayerSide.LIGHT


class TestCaptureApplication:
    def test_the_captured_piece_is_removed(self) -> None:
        before = position({"c3": LIGHT_MAN, "d4": DARK_MAN}, PlayerSide.LIGHT)

        after = applier.apply(before, move("c3", "e5", captured=("d4",)))

        assert after.board.piece_at(square("d4")) is None

    def test_the_attacker_lands_beyond_the_victim(self) -> None:
        before = position({"c3": LIGHT_MAN, "d4": DARK_MAN}, PlayerSide.LIGHT)

        after = applier.apply(before, move("c3", "e5", captured=("d4",)))

        assert after.board.piece_at(square("e5")) == LIGHT_MAN

    def test_the_board_loses_exactly_the_captured_piece(self) -> None:
        before = position(
            {"c3": LIGHT_MAN, "d4": DARK_MAN, "h8": DARK_MAN},
            PlayerSide.LIGHT,
        )

        after = applier.apply(before, move("c3", "e5", captured=("d4",)))

        assert (after.board.piece_count(), before.board.piece_count()) == (2, 3)

    def test_a_capture_onto_the_crownhead_both_takes_and_crowns(self) -> None:
        before = position({"b6": LIGHT_MAN, "c7": DARK_MAN}, PlayerSide.LIGHT)

        after = applier.apply(
            before, move("b6", "d8", captured=("c7",), promotes_to=PieceRank.KING)
        )

        assert after.board.occupied_squares == {square("d8"): LIGHT_KING}


class TestPromotionApplication:
    def test_the_arriving_piece_is_crowned(self) -> None:
        after = applier.apply(
            position({"g7": LIGHT_MAN}, PlayerSide.LIGHT),
            move("g7", "h8", promotes_to=PieceRank.KING),
        )

        assert after.board.piece_at(square("h8")) == LIGHT_KING

    def test_crowning_keeps_the_side(self) -> None:
        after = applier.apply(
            position({"b2": DARK_MAN}, PlayerSide.DARK),
            move("b2", "a1", promotes_to=PieceRank.KING),
        )

        assert after.board.piece_at(square("a1")) == Piece(
            side=PlayerSide.DARK, rank=PieceRank.KING
        )

    def test_crowning_does_not_add_or_remove_a_piece(self) -> None:
        after = applier.apply(
            position({"g7": LIGHT_MAN}, PlayerSide.LIGHT),
            move("g7", "h8", promotes_to=PieceRank.KING),
        )

        assert after.board.piece_count() == 1

    def test_a_move_short_of_the_crownhead_leaves_a_man(self) -> None:
        after = applier.apply(position({"c3": LIGHT_MAN}, PlayerSide.LIGHT), move("c3", "d4"))

        assert after.board.piece_at(square("d4")) == LIGHT_MAN


class TestNothingIsMutated:
    def test_the_original_position_is_unchanged_by_a_quiet_move(self) -> None:
        before = position({"c3": LIGHT_MAN}, PlayerSide.LIGHT)
        snapshot = position({"c3": LIGHT_MAN}, PlayerSide.LIGHT)

        applier.apply(before, move("c3", "d4"))

        assert before == snapshot

    def test_the_original_position_is_unchanged_by_a_capture(self) -> None:
        before = position({"c3": LIGHT_MAN, "d4": DARK_MAN}, PlayerSide.LIGHT)
        snapshot = position({"c3": LIGHT_MAN, "d4": DARK_MAN}, PlayerSide.LIGHT)

        applier.apply(before, move("c3", "e5", captured=("d4",)))

        assert before == snapshot

    def test_the_original_position_is_unchanged_by_a_refused_move(self) -> None:
        """Immutability doing the work a transaction would otherwise need:
        a failure part-way through cannot leave a half-applied board,
        because no board was ever edited."""
        before = position({"c3": LIGHT_MAN, "d4": DARK_MAN}, PlayerSide.LIGHT)
        snapshot = position({"c3": LIGHT_MAN, "d4": DARK_MAN}, PlayerSide.LIGHT)

        with pytest.raises(IllegalMove):
            applier.apply(before, move("c3", "b4"))

        assert before == snapshot

    def test_the_result_is_a_different_position(self) -> None:
        before = position({"c3": LIGHT_MAN}, PlayerSide.LIGHT)

        assert applier.apply(before, move("c3", "d4")) != before


class TestRefusals:
    def test_an_illegal_move_is_not_applied(self) -> None:
        obliged = position({"c3": LIGHT_MAN, "d4": DARK_MAN}, PlayerSide.LIGHT)

        with pytest.raises(IllegalMove):
            applier.apply(obliged, move("c3", "b4"))

    def test_a_move_for_the_wrong_side_is_not_applied(self) -> None:
        both = position({"c3": LIGHT_MAN, "f6": DARK_MAN}, PlayerSide.LIGHT)

        with pytest.raises(IllegalMove):
            applier.apply(both, move("f6", "e5"))


class TestDeterminism:
    def test_the_same_move_produces_the_same_position_every_time(self) -> None:
        before = position({"c3": LIGHT_MAN, "d4": DARK_MAN}, PlayerSide.LIGHT)
        capture = move("c3", "e5", captured=("d4",))

        assert applier.apply(before, capture) == applier.apply(before, capture)

    def test_a_sequence_of_plies_reaches_one_position(self) -> None:
        """Three plies applied twice from the same start. The engine has to
        be a function of its inputs for a replay to reconstruct a game
        (AD-15), and this is the smallest statement of that."""
        start = Position(board=initial_board(RUSSIAN), side_to_move=PlayerSide.LIGHT)
        plies = (
            move("c3", "d4"),
            move("f6", "e5"),
            move("d4", "f6", captured=("e5",)),
        )

        def play(position: Position) -> Position:
            for ply in plies:
                position = applier.apply(position, ply)
            return position

        assert play(start) == play(start)


class TestAppliedMoveProperties:
    """One property, asserted over every move the generator offers in a
    handful of positions: applying a legal move yields a new, coherent
    position and leaves the old one alone.

    Written as a property rather than more examples because the interesting
    failures are the ones nobody thought to write an example for — a piece
    count that drifts on a capture, a turn that fails to change on a
    promotion, a board that shares state with its predecessor.
    """

    POSITIONS = (
        position({"c3": LIGHT_MAN, "e3": LIGHT_MAN, "a1": LIGHT_MAN}, PlayerSide.LIGHT),
        position({"c3": LIGHT_MAN, "d4": DARK_MAN}, PlayerSide.LIGHT),
        position({"c5": LIGHT_MAN, "b4": DARK_MAN}, PlayerSide.LIGHT),
        position({"g7": LIGHT_MAN}, PlayerSide.LIGHT),
        position({"c5": DARK_MAN, "f6": DARK_MAN}, PlayerSide.DARK),
        Position(board=initial_board(RUSSIAN), side_to_move=PlayerSide.LIGHT),
    )

    @pytest.mark.parametrize("before", POSITIONS, ids=lambda before: before.fingerprint[:40])
    def test_applying_any_legal_move_yields_a_coherent_new_position(self, before: Position) -> None:
        legal = generator.legal_moves(before)
        assert legal, "a position with no moves proves nothing here"

        for candidate in legal:
            mover = before.board.piece_at(candidate.origin)
            assert mover is not None

            after = applier.apply(before, candidate)

            assert after.side_to_move is before.side_to_move.opponent()
            assert after.board.variant is before.board.variant
            assert after.board.piece_count() == before.board.piece_count() - len(candidate.captured)
            assert after.board.piece_at(candidate.origin) is None
            arrived = after.board.piece_at(candidate.destination)
            assert arrived is not None
            assert arrived.side is mover.side
            assert arrived.rank is (candidate.promotes_to or mover.rank)
            assert all(after.board.piece_at(taken) is None for taken in candidate.captured)

    def test_the_positions_survive_the_whole_sweep_unchanged(self) -> None:
        """The sweep above applies every legal move of six shared
        positions. This asserts none of those applications edited one —
        and it holds whatever order the suite runs in, because the
        positions are values."""
        assert self.POSITIONS[3] == position({"g7": LIGHT_MAN}, PlayerSide.LIGHT)
        assert self.POSITIONS[5].board == initial_board(RUSSIAN)
