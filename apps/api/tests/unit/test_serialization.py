"""Primitive projections — A64-014.8.

Two things are being held here. The first is **round-trip**: every value
survives a journey to primitives and back unchanged, which is what a
stored game depends on.

The second is that there is **one encoding**. These are the same shapes
the conformance corpus is written in, and `tests/corpus.py` reads through
these functions — so a case in the corpus and a record in a store cannot
drift into two definitions of what a move is.
"""

import json

import pytest

from app.modules.engine import (
    CURRENT_ENGINE_VERSION,
    Board,
    BoardCoordinate,
    BoardVariant,
    EngineVersion,
    InvalidBoardState,
    Move,
    Piece,
    PieceRank,
    PlayerSide,
    Position,
    initial_board,
)
from app.modules.engine.serialization import (
    board_from_primitive,
    board_to_primitive,
    coordinate_from_primitive,
    coordinate_to_primitive,
    engine_version_from_primitive,
    engine_version_to_primitive,
    move_from_primitive,
    move_to_primitive,
    piece_from_primitive,
    piece_to_primitive,
    position_from_primitive,
    position_to_primitive,
)
from app.modules.game.domain import MatchOutcome, MatchResult, MoveRecord, TerminationReason
from app.modules.game.domain.replay import ReplayData
from app.modules.game.domain.serialization import (
    match_result_from_primitive,
    match_result_to_primitive,
    move_record_from_primitive,
    move_record_to_primitive,
    replay_from_primitive,
    replay_to_primitive,
)

RUSSIAN = BoardVariant.RUSSIAN_8X8

LIGHT_MAN = Piece(side=PlayerSide.LIGHT, rank=PieceRank.MAN)
DARK_KING = Piece(side=PlayerSide.DARK, rank=PieceRank.KING)


def square(name: str) -> BoardCoordinate:
    return BoardCoordinate.parse(name)


OPENING = Position(board=initial_board(RUSSIAN), side_to_move=PlayerSide.LIGHT)
A_CAPTURE = Move(
    path=(square("c3"), square("e5"), square("g7")),
    captured=(square("d4"), square("f6")),
)


class TestEngineVersion:
    def test_it_round_trips(self) -> None:
        assert engine_version_from_primitive(engine_version_to_primitive(EngineVersion(7))) == (
            EngineVersion(7)
        )

    def test_it_serialises_to_a_plain_integer(self) -> None:
        assert engine_version_to_primitive(CURRENT_ENGINE_VERSION) == 2

    def test_it_is_never_inferred_from_an_absent_value(self) -> None:
        """Not from a timestamp, a schema version, a creation date, or the
        version this build happens to be. A replay whose version was
        guessed runs under rules the game was not played under — AD-15's
        whole concern."""
        with pytest.raises(InvalidBoardState):
            engine_version_from_primitive(None)

    def test_a_non_integer_version_is_refused(self) -> None:
        with pytest.raises(InvalidBoardState):
            engine_version_from_primitive("2")

    def test_a_boolean_is_not_an_integer_version(self) -> None:
        """`True == 1` in Python, so a boolean would silently become
        version 1 — the one value most likely to appear from a careless
        writer."""
        with pytest.raises(InvalidBoardState):
            engine_version_from_primitive(True)


class TestCoordinatesAndPieces:
    def test_a_coordinate_round_trips(self) -> None:
        assert coordinate_from_primitive(coordinate_to_primitive(square("j10"))) == square("j10")

    def test_a_coordinate_is_algebraic_notation(self) -> None:
        assert coordinate_to_primitive(square("c3")) == "c3"

    def test_a_piece_round_trips_with_its_square(self) -> None:
        entry = piece_to_primitive(square("c3"), LIGHT_MAN)

        assert piece_from_primitive(entry) == (square("c3"), LIGHT_MAN)

    def test_a_piece_states_its_side_and_rank_explicitly(self) -> None:
        assert piece_to_primitive(square("h8"), DARK_KING) == {
            "square": "h8",
            "side": "dark",
            "rank": "king",
        }


class TestBoardsAndPositions:
    def test_a_board_round_trips(self) -> None:
        board = initial_board(RUSSIAN)

        assert board_from_primitive(board_to_primitive(board)) == board

    def test_the_squares_are_sorted(self) -> None:
        """Two identical boards built by different move orders must
        serialize to identical text, or a reader diffing two stored games
        sees differences that are not there."""
        one = Board(RUSSIAN, {square("c3"): LIGHT_MAN, square("a1"): LIGHT_MAN})
        other = Board(RUSSIAN, {square("a1"): LIGHT_MAN, square("c3"): LIGHT_MAN})

        assert board_to_primitive(one) == board_to_primitive(other)

    def test_a_position_round_trips(self) -> None:
        assert position_from_primitive(position_to_primitive(OPENING)) == OPENING

    def test_a_position_carries_the_side_to_move(self) -> None:
        """Without it the board is not a position, and the repetition key
        would conflate two states the rules consider different."""
        assert position_to_primitive(OPENING)["side_to_move"] == "light"

    def test_a_position_is_structured_rather_than_its_fingerprint(self) -> None:
        """The fingerprint encodes the same facts and is what gets hashed,
        but parsing it back would be a second parser for one encoding."""
        assert "pieces" in position_to_primitive(OPENING)


class TestMoves:
    def test_a_move_round_trips(self) -> None:
        assert move_from_primitive(move_to_primitive(A_CAPTURE)) == A_CAPTURE

    def test_a_move_serialises_its_complete_path(self) -> None:
        assert move_to_primitive(A_CAPTURE)["path"] == ["c3", "e5", "g7"]

    def test_a_move_serialises_every_captured_square_in_order(self) -> None:
        assert move_to_primitive(A_CAPTURE)["captured"] == ["d4", "f6"]

    def test_two_paths_to_one_destination_serialise_differently(self) -> None:
        """The reason a move is a path and not a from/to pair: a multi-jump
        can reach one square by different routes taking different pieces,
        and a from/to record could not tell these apart (§2.1)."""
        one_way = Move(
            path=(square("c3"), square("e5"), square("c7")),
            captured=(square("d4"), square("d6")),
        )
        other_way = Move(
            path=(square("c3"), square("a5"), square("c7")),
            captured=(square("b4"), square("b6")),
        )

        assert move_to_primitive(one_way) != move_to_primitive(other_way)

    def test_a_promotion_is_recorded(self) -> None:
        crowning = Move(path=(square("g7"), square("h8")), promotes_to=PieceRank.KING)

        assert move_to_primitive(crowning)["promotes_to"] == "king"

    def test_an_absent_promotion_is_explicit(self) -> None:
        """`None`, not a missing key. A record whose promotion quietly
        defaulted would replay into a different position."""
        assert move_to_primitive(A_CAPTURE)["promotes_to"] is None


class TestMoveRecords:
    RECORD = MoveRecord(
        ply_number=1,
        move=A_CAPTURE,
        resulting_position_hash="russian_8x8/dark/g7=light:man",
    )

    def test_a_record_round_trips(self) -> None:
        assert move_record_from_primitive(move_record_to_primitive(self.RECORD)) == self.RECORD

    def test_a_record_is_immutable(self) -> None:
        with pytest.raises(AttributeError):
            self.RECORD.ply_number = 2  # type: ignore[misc]

    def test_the_clock_fields_are_present_and_unmeasured(self) -> None:
        """`None` says "not measured"; a zero would say "measured, and it
        was instant". The fields exist now because MT-6 needs them and a
        log written without them would have a hole nothing can fill."""
        entry = move_record_to_primitive(self.RECORD)

        assert entry["think_time_ms"] is None
        assert entry["remaining_clock_ms"] is None

    def test_a_ply_number_below_one_is_refused(self) -> None:
        """MT-5: contiguous from 1."""
        with pytest.raises(ValueError):
            MoveRecord(ply_number=0, move=A_CAPTURE, resulting_position_hash="x")


class TestResults:
    def test_a_win_round_trips(self) -> None:
        win = MatchResult(
            outcome=MatchOutcome.WIN,
            reason=TerminationReason.ALL_PIECES_CAPTURED,
            winner=PlayerSide.LIGHT,
        )

        assert match_result_from_primitive(match_result_to_primitive(win)) == win

    def test_a_draw_round_trips_without_a_winner(self) -> None:
        drawn = MatchResult(outcome=MatchOutcome.DRAW, reason=TerminationReason.REPETITION)

        assert match_result_from_primitive(match_result_to_primitive(drawn)) == drawn


class TestReplayPayloads:
    REPLAY = ReplayData(
        engine_version=CURRENT_ENGINE_VERSION,
        variant=RUSSIAN,
        opening_position=OPENING,
        records=(
            MoveRecord(
                ply_number=1,
                move=Move(path=(square("c3"), square("d4"))),
                resulting_position_hash="x",
            ),
        ),
        expected_result=MatchResult(outcome=MatchOutcome.DRAW, reason=TerminationReason.REPETITION),
    )

    def test_a_replay_round_trips(self) -> None:
        assert replay_from_primitive(replay_to_primitive(self.REPLAY)) == self.REPLAY

    def test_a_replay_serialises_deterministically(self) -> None:
        assert json.dumps(replay_to_primitive(self.REPLAY)) == json.dumps(
            replay_to_primitive(self.REPLAY)
        )

    def test_a_replay_is_plain_json(self) -> None:
        """No framework objects anywhere in it — the whole point of a
        language-neutral encoding."""
        json.dumps(replay_to_primitive(self.REPLAY))

    def test_a_replay_carries_no_derived_history(self) -> None:
        """No position counts, no no-progress counter, no final board.
        Every one is rebuilt by replaying the log; storing them would be a
        second answer to a question the moves already settle."""
        entry = replay_to_primitive(self.REPLAY)

        assert set(entry) == {
            "engine_version",
            "variant",
            "opening_position",
            "records",
            "expected_result",
        }
