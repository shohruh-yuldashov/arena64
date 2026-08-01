"""The move log and `ReplayEngine` — A64-014.8.

The property under test is not "the board comes back". It is that a replay
reproduces **why** a game ended: the position occurrence counts and the
no-progress counter are recomputed by applying the log through the same
`Match.play` a live game uses, never restored from the record.

That is why the interesting cases here are a repetition draw and a corrupt
log, not a quiet move.
"""

import pytest

from app.modules.engine import (
    CURRENT_ENGINE_VERSION,
    Board,
    BoardCoordinate,
    BoardVariant,
    EngineVersion,
    Move,
    MoveApplier,
    MoveGenerator,
    MoveValidator,
    Piece,
    PieceRank,
    PlayerSide,
    Position,
    TerminalStateEvaluator,
)
from app.modules.game.domain import (
    CorruptMoveLog,
    DrawRuleSet,
    MalformedMoveLog,
    Match,
    MatchOutcome,
    MatchStatus,
    MoveRecord,
    PositionHashMismatch,
    ReplayData,
    ReplayEngine,
    ReplayError,
    ReplayResultMismatch,
    TerminationReason,
    UnsupportedEngineVersion,
)

RUSSIAN = BoardVariant.RUSSIAN_8X8

LIGHT_MAN = Piece(side=PlayerSide.LIGHT, rank=PieceRank.MAN)
DARK_MAN = Piece(side=PlayerSide.DARK, rank=PieceRank.MAN)
LIGHT_KING = Piece(side=PlayerSide.LIGHT, rank=PieceRank.KING)
DARK_KING = Piece(side=PlayerSide.DARK, rank=PieceRank.KING)

generator = MoveGenerator()
applier = MoveApplier(MoveValidator(generator))
evaluator = TerminalStateEvaluator(generator)
draw_rules = DrawRuleSet()
replay_engine = ReplayEngine(applier, evaluator, draw_rules)


def square(name: str) -> BoardCoordinate:
    return BoardCoordinate.parse(name)


def move(*path: str, captured: tuple[str, ...] = (), promotes_to: PieceRank | None = None) -> Move:
    return Move(
        path=tuple(square(name) for name in path),
        captured=tuple(square(name) for name in captured),
        promotes_to=promotes_to,
    )


def opening(placement: dict[str, Piece], side: PlayerSide = PlayerSide.LIGHT) -> Position:
    return Position(
        board=Board(RUSSIAN, {square(name): piece for name, piece in placement.items()}),
        side_to_move=side,
    )


def played(start: Position, moves: tuple[Move, ...]) -> Match:
    """A match played out live — the source of every record below."""
    match = Match(variant=RUSSIAN, engine_version=CURRENT_ENGINE_VERSION, position=start)
    match.start()
    for one in moves:
        match.play(one, applier, evaluator, draw_rules)
    return match


def recording(start: Position, moves: tuple[Move, ...]) -> ReplayData:
    """The replay payload a live game would have produced."""
    return ReplayData(
        engine_version=CURRENT_ENGINE_VERSION,
        variant=RUSSIAN,
        opening_position=start,
        records=played(start, moves).move_log,
    )


KINGS = opening({"a1": LIGHT_KING, "h2": DARK_KING})
SHUFFLE = (move("a1", "b2"), move("h2", "g1"), move("b2", "a1"), move("g1", "h2"))
CAPTURE_START = opening({"c3": LIGHT_MAN, "d4": DARK_MAN})


class TestTheMoveLog:
    def test_a_new_match_has_an_empty_log(self) -> None:
        assert played(KINGS, ()).move_log == ()

    def test_one_record_is_appended_per_move(self) -> None:
        assert len(played(KINGS, SHUFFLE).move_log) == 4

    def test_ply_numbers_start_at_one(self) -> None:
        assert played(KINGS, SHUFFLE).move_log[0].ply_number == 1

    def test_ply_numbers_are_contiguous(self) -> None:
        """MT-5. A gap makes the game unreplayable, which invalidates the
        result, the analysis and the fair-play record at once."""
        log = played(KINGS, SHUFFLE).move_log

        assert [record.ply_number for record in log] == [1, 2, 3, 4]

    def test_a_rejected_move_appends_nothing(self) -> None:
        """A refused move must be indistinguishable from one nobody sent —
        a phantom ply is exactly the gap MT-5 forbids."""
        from app.modules.engine import IllegalMove

        match = played(CAPTURE_START, ())

        with pytest.raises(IllegalMove):
            match.play(move("c3", "b4"), applier, evaluator, draw_rules)

        assert match.move_log == ()

    def test_every_record_carries_the_position_it_produced(self) -> None:
        match = played(KINGS, SHUFFLE)

        assert match.move_log[-1].resulting_position_hash == match.position.fingerprint

    def test_the_log_is_handed_out_as_a_tuple(self) -> None:
        """A caller holding the list could edit a recorded ply while the
        sequence looked untouched."""
        assert isinstance(played(KINGS, SHUFFLE).move_log, tuple)

    def test_editing_the_returned_log_does_not_reach_the_match(self) -> None:
        match = played(KINGS, SHUFFLE)

        log = list(match.move_log)
        log.clear()

        assert len(match.move_log) == 4

    def test_the_last_move_is_read_off_the_log(self) -> None:
        """One history, not two — A64-014.6 stored `last_move` beside the
        log, and two histories of one game are two answers to what was
        played."""
        match = played(KINGS, SHUFFLE)

        assert match.last_move == match.move_log[-1].move


class TestReplayReconstruction:
    def test_a_replay_reaches_the_same_position(self) -> None:
        live = played(KINGS, SHUFFLE)

        assert replay_engine.replay(recording(KINGS, SHUFFLE)).position == live.position

    def test_a_replay_reproduces_the_ply_count(self) -> None:
        assert replay_engine.replay(recording(KINGS, SHUFFLE)).ply_number == 4

    def test_a_replay_rebuilds_the_position_occurrence_counts(self) -> None:
        """Rebuilt by applying the log, never restored: `ReplayData` carries
        no counts at all."""
        replayed = replay_engine.replay(recording(KINGS, SHUFFLE))

        assert replayed.current_position_occurrences == 2

    def test_a_replay_rebuilds_the_no_progress_counter(self) -> None:
        assert replay_engine.replay(recording(KINGS, SHUFFLE)).plies_since_progress == 4

    def test_a_replay_reproduces_a_repetition_draw(self) -> None:
        """The case that fails if a replay reconstructs only the board: the
        final position is unremarkable, and the game ended because of the
        sequence that reached it twice before."""
        replayed = replay_engine.replay(recording(KINGS, SHUFFLE * 2))

        assert replayed.status is MatchStatus.COMPLETED
        assert replayed.termination_reason is TerminationReason.REPETITION

    def test_a_replayed_draw_has_no_winner(self) -> None:
        replayed = replay_engine.replay(recording(KINGS, SHUFFLE * 2))

        assert replayed.result is not None
        assert (replayed.result.outcome, replayed.result.winner) == (MatchOutcome.DRAW, None)

    def test_a_replay_reproduces_a_decisive_win(self) -> None:
        capture = (move("c3", "e5", captured=("d4",)),)

        replayed = replay_engine.replay(recording(CAPTURE_START, capture))

        assert replayed.result is not None
        assert replayed.result.winner is PlayerSide.LIGHT

    def test_a_replay_keeps_the_recorded_engine_version(self) -> None:
        """Not the current one — the version the game was played under is
        the thing being reproduced."""
        assert replay_engine.replay(recording(KINGS, SHUFFLE)).engine_version == (
            CURRENT_ENGINE_VERSION
        )

    def test_a_replay_rebuilds_the_move_log(self) -> None:
        replayed = replay_engine.replay(recording(KINGS, SHUFFLE))

        assert replayed.move_log == played(KINGS, SHUFFLE).move_log

    def test_a_replay_is_deterministic(self) -> None:
        payload = recording(KINGS, SHUFFLE)

        assert replay_engine.replay(payload).position == replay_engine.replay(payload).position

    def test_a_replay_leaves_its_payload_unchanged(self) -> None:
        payload = recording(KINGS, SHUFFLE)

        replay_engine.replay(payload)

        assert payload.records == recording(KINGS, SHUFFLE).records

    def test_an_expected_result_that_matches_is_accepted(self) -> None:
        live = played(KINGS, SHUFFLE * 2)
        payload = ReplayData(
            engine_version=CURRENT_ENGINE_VERSION,
            variant=RUSSIAN,
            opening_position=KINGS,
            records=live.move_log,
            expected_result=live.result,
        )

        assert replay_engine.replay(payload).result == live.result


class TestReplayRefusals:
    def test_an_unsupported_engine_version_is_refused(self) -> None:
        """Refused, not approximated. Version 1 had no draw rules, so
        replaying it under version 2 could end a game earlier than it
        really ended — AD-15's scenario word for word."""
        payload = ReplayData(
            engine_version=EngineVersion(number=1),
            variant=RUSSIAN,
            opening_position=KINGS,
            records=(),
        )

        with pytest.raises(UnsupportedEngineVersion):
            replay_engine.replay(payload)

    def test_a_ply_gap_is_refused(self) -> None:
        live = played(KINGS, SHUFFLE)
        payload = ReplayData(
            engine_version=CURRENT_ENGINE_VERSION,
            variant=RUSSIAN,
            opening_position=KINGS,
            records=(live.move_log[0], live.move_log[2]),
        )

        with pytest.raises(MalformedMoveLog):
            replay_engine.replay(payload)

    def test_a_duplicate_ply_number_is_refused(self) -> None:
        live = played(KINGS, SHUFFLE)
        payload = ReplayData(
            engine_version=CURRENT_ENGINE_VERSION,
            variant=RUSSIAN,
            opening_position=KINGS,
            records=(live.move_log[0], live.move_log[0]),
        )

        with pytest.raises(MalformedMoveLog):
            replay_engine.replay(payload)

    def test_a_log_that_does_not_start_at_one_is_refused(self) -> None:
        live = played(KINGS, SHUFFLE)
        payload = ReplayData(
            engine_version=CURRENT_ENGINE_VERSION,
            variant=RUSSIAN,
            opening_position=KINGS,
            records=(live.move_log[1],),
        )

        with pytest.raises(MalformedMoveLog):
            replay_engine.replay(payload)

    def test_a_malformed_log_is_refused_before_anything_is_applied(self) -> None:
        """Refused whole rather than half-replayed into a position that
        never occurred."""
        live = played(KINGS, SHUFFLE)
        payload = ReplayData(
            engine_version=CURRENT_ENGINE_VERSION,
            variant=RUSSIAN,
            opening_position=KINGS,
            records=(live.move_log[0], live.move_log[2]),
        )

        with pytest.raises(MalformedMoveLog):
            replay_engine.replay(payload)

    def test_an_illegal_move_is_refused(self) -> None:
        payload = ReplayData(
            engine_version=CURRENT_ENGINE_VERSION,
            variant=RUSSIAN,
            opening_position=CAPTURE_START,
            records=(MoveRecord(ply_number=1, move=move("c3", "b4"), resulting_position_hash="x"),),
        )

        with pytest.raises(CorruptMoveLog):
            replay_engine.replay(payload)

    def test_the_refusal_that_caused_it_is_chained(self) -> None:
        """`__cause__` preserved, because *which* rule refused it is the
        diagnostic (CLAUDE.md §9.4)."""
        from app.modules.engine import IllegalMove

        payload = ReplayData(
            engine_version=CURRENT_ENGINE_VERSION,
            variant=RUSSIAN,
            opening_position=CAPTURE_START,
            records=(MoveRecord(ply_number=1, move=move("c3", "b4"), resulting_position_hash="x"),),
        )

        with pytest.raises(CorruptMoveLog) as failure:
            replay_engine.replay(payload)

        assert isinstance(failure.value.__cause__, IllegalMove)

    def test_a_move_recorded_after_the_game_ended_is_refused(self) -> None:
        won = played(CAPTURE_START, (move("c3", "e5", captured=("d4",)),))
        payload = ReplayData(
            engine_version=CURRENT_ENGINE_VERSION,
            variant=RUSSIAN,
            opening_position=CAPTURE_START,
            records=(
                *won.move_log,
                MoveRecord(ply_number=2, move=move("e5", "f6"), resulting_position_hash="x"),
            ),
        )

        with pytest.raises(CorruptMoveLog):
            replay_engine.replay(payload)

    def test_a_mismatched_position_hash_is_refused(self) -> None:
        live = played(KINGS, SHUFFLE)
        first = live.move_log[0]
        payload = ReplayData(
            engine_version=CURRENT_ENGINE_VERSION,
            variant=RUSSIAN,
            opening_position=KINGS,
            records=(
                MoveRecord(
                    ply_number=1,
                    move=first.move,
                    resulting_position_hash="russian_8x8/light/a1=light:king",
                ),
            ),
        )

        with pytest.raises(PositionHashMismatch):
            replay_engine.replay(payload)

    def test_a_hash_mismatch_names_the_ply_that_caused_it(self) -> None:
        """Caught on the move whose semantics changed, not at the end where
        it would name nothing."""
        live = played(KINGS, SHUFFLE)
        payload = ReplayData(
            engine_version=CURRENT_ENGINE_VERSION,
            variant=RUSSIAN,
            opening_position=KINGS,
            records=(
                live.move_log[0],
                MoveRecord(
                    ply_number=2,
                    move=live.move_log[1].move,
                    resulting_position_hash="wrong",
                ),
            ),
        )

        with pytest.raises(PositionHashMismatch, match="Ply 2"):
            replay_engine.replay(payload)

    def test_a_result_that_does_not_match_is_refused(self) -> None:
        from app.modules.game.domain import MatchResult

        live = played(KINGS, SHUFFLE * 2)
        payload = ReplayData(
            engine_version=CURRENT_ENGINE_VERSION,
            variant=RUSSIAN,
            opening_position=KINGS,
            records=live.move_log,
            expected_result=MatchResult(
                outcome=MatchOutcome.WIN,
                reason=TerminationReason.RESIGNATION,
                winner=PlayerSide.LIGHT,
            ),
        )

        with pytest.raises(ReplayResultMismatch):
            replay_engine.replay(payload)

    def test_every_replay_failure_is_a_replay_error(self) -> None:
        """One family for a caller to catch — and distinct from
        `InvalidMatchTransition`, because a corrupt record is data
        integrity rather than a stale view."""
        for failure in (
            UnsupportedEngineVersion,
            MalformedMoveLog,
            CorruptMoveLog,
            PositionHashMismatch,
            ReplayResultMismatch,
        ):
            assert issubclass(failure, ReplayError)


class TestVersionResolution:
    def test_only_the_current_version_is_supported_today(self) -> None:
        from app.modules.game.domain import SUPPORTED_ENGINE_VERSIONS

        assert frozenset({CURRENT_ENGINE_VERSION}) == SUPPORTED_ENGINE_VERSIONS

    def test_the_supported_set_is_injectable(self) -> None:
        """The seam is a set rather than an `if`, so supporting an older
        build means adding a rules profile beside the current one rather
        than editing the replay engine."""
        permissive = ReplayEngine(
            applier,
            evaluator,
            draw_rules,
            supported_versions=frozenset({EngineVersion(number=1), CURRENT_ENGINE_VERSION}),
        )
        payload = ReplayData(
            engine_version=EngineVersion(number=1),
            variant=RUSSIAN,
            opening_position=KINGS,
            records=(),
        )

        assert permissive.replay(payload).engine_version == EngineVersion(number=1)
