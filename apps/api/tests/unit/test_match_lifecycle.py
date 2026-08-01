"""`Match` — the lifecycle, the history, and the counters — A64-014.6.

What is asserted here is everything the aggregate adds *around* the rules:
which transitions are defined, that a move goes through the engine's own
validator and applier rather than around them, and the two things a
position cannot remember about itself — how often it has occurred, and how
long since anything irreversible happened.

The rules themselves are tested in the engine's suites. A test here that
re-asserted a legal move would be testing the wrong module.
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
    TerminalReason,
    TerminalStateEvaluator,
    initial_board,
)
from app.modules.game.domain import (
    InvalidMatchTransition,
    Match,
    MatchOutcome,
    MatchStatus,
    TerminationReason,
)
from app.modules.game.domain.match import _TERMINATION_FOR

RUSSIAN = BoardVariant.RUSSIAN_8X8

LIGHT_MAN = Piece(side=PlayerSide.LIGHT, rank=PieceRank.MAN)
DARK_MAN = Piece(side=PlayerSide.DARK, rank=PieceRank.MAN)
LIGHT_KING = Piece(side=PlayerSide.LIGHT, rank=PieceRank.KING)
DARK_KING = Piece(side=PlayerSide.DARK, rank=PieceRank.KING)

generator = MoveGenerator()
applier = MoveApplier(MoveValidator(generator))
evaluator = TerminalStateEvaluator(generator)


def square(name: str) -> BoardCoordinate:
    return BoardCoordinate.parse(name)


def move(*path: str, captured: tuple[str, ...] = (), promotes_to: PieceRank | None = None) -> Move:
    return Move(
        path=tuple(square(name) for name in path),
        captured=tuple(square(name) for name in captured),
        promotes_to=promotes_to,
    )


def match_at(placement: dict[str, Piece], side: PlayerSide = PlayerSide.LIGHT) -> Match:
    """An **active** match at a hand-written position.

    Constructed directly rather than through `create`, because a five-piece
    position states what a test is about and the opening does not.
    """
    started = Match(
        variant=RUSSIAN,
        engine_version=CURRENT_ENGINE_VERSION,
        position=Position(
            board=Board(
                RUSSIAN,
                {square(name): piece for name, piece in placement.items()},
            ),
            side_to_move=side,
        ),
    )
    started.start()
    return started


def play(match: Match, played: Move) -> None:
    match.play(played, applier, evaluator)


class TestCreation:
    def test_a_new_match_is_created_and_not_started(self) -> None:
        assert Match.create(RUSSIAN).status is MatchStatus.CREATED

    def test_a_new_match_stands_at_the_opening_position(self) -> None:
        assert Match.create(RUSSIAN).position.board == initial_board(RUSSIAN)

    def test_a_new_match_has_played_no_plies(self) -> None:
        created = Match.create(RUSSIAN)

        assert (created.ply_number, created.last_move) == (0, None)

    def test_a_new_match_has_no_result(self) -> None:
        """Absent rather than pending — DM-08. A sentinel invites the code
        that computes ratings to forget to check."""
        created = Match.create(RUSSIAN)

        assert (created.result, created.termination_reason) == (None, None)

    def test_a_new_match_records_the_engine_version_it_will_be_played_under(self) -> None:
        assert Match.create(RUSSIAN).engine_version == CURRENT_ENGINE_VERSION

    def test_the_engine_version_may_be_stated_explicitly(self) -> None:
        """What a repository does when it rehydrates a game played under an
        older build — AD-15's whole point."""
        old = Match.create(RUSSIAN, engine_version=EngineVersion(number=1))

        assert old.engine_version == EngineVersion(number=1)

    def test_a_match_refuses_a_position_from_another_variant(self) -> None:
        with pytest.raises(InvalidMatchTransition):
            Match(
                variant=BoardVariant.INTERNATIONAL_10X10,
                engine_version=CURRENT_ENGINE_VERSION,
                position=Position(board=initial_board(RUSSIAN), side_to_move=PlayerSide.LIGHT),
            )

    def test_two_matches_have_different_identities(self) -> None:
        assert Match.create(RUSSIAN).id != Match.create(RUSSIAN).id


class TestTransitions:
    def test_a_created_match_starts(self) -> None:
        created = Match.create(RUSSIAN)

        created.start()

        assert created.status is MatchStatus.ACTIVE

    def test_an_active_match_cannot_be_started_again(self) -> None:
        started = Match.create(RUSSIAN)
        started.start()

        with pytest.raises(InvalidMatchTransition):
            started.start()

    def test_a_move_cannot_be_played_before_the_match_starts(self) -> None:
        created = Match.create(RUSSIAN)

        with pytest.raises(InvalidMatchTransition):
            play(created, move("c3", "d4"))

    def test_a_move_cannot_be_played_after_the_match_ends(self) -> None:
        finished = match_at({"c3": LIGHT_MAN, "f6": DARK_MAN})
        finished.resign(PlayerSide.LIGHT)

        with pytest.raises(InvalidMatchTransition):
            play(finished, move("c3", "d4"))

    def test_a_completed_match_cannot_be_resigned(self) -> None:
        finished = match_at({"c3": LIGHT_MAN, "f6": DARK_MAN})
        finished.resign(PlayerSide.LIGHT)

        with pytest.raises(InvalidMatchTransition):
            finished.resign(PlayerSide.DARK)

    def test_a_created_match_cannot_be_resigned(self) -> None:
        """There is nothing to give up yet, and system-design.md §3 resolves
        a match nobody joined to an abort rather than to a loss."""
        with pytest.raises(InvalidMatchTransition):
            Match.create(RUSSIAN).resign(PlayerSide.LIGHT)

    def test_a_completed_match_cannot_be_aborted(self) -> None:
        """MT-10: a completed match is a permanent record."""
        finished = match_at({"c3": LIGHT_MAN, "f6": DARK_MAN})
        finished.resign(PlayerSide.LIGHT)

        with pytest.raises(InvalidMatchTransition):
            finished.abort()

    def test_an_aborted_match_cannot_be_aborted_again(self) -> None:
        aborted = Match.create(RUSSIAN)
        aborted.abort()

        with pytest.raises(InvalidMatchTransition):
            aborted.abort()

    def test_the_refusal_names_the_status_it_found(self) -> None:
        """What an operator reading a log at 3am needs, and it identifies no
        player."""
        with pytest.raises(InvalidMatchTransition, match="created"):
            play(Match.create(RUSSIAN), move("c3", "d4"))


class TestPlayingAMove:
    def test_the_position_advances(self) -> None:
        playing = match_at({"c3": LIGHT_MAN})

        play(playing, move("c3", "d4"))

        assert playing.position.board.piece_at(square("d4")) == LIGHT_MAN

    def test_the_turn_passes(self) -> None:
        playing = match_at({"c3": LIGHT_MAN})

        play(playing, move("c3", "d4"))

        assert playing.side_to_move is PlayerSide.DARK

    def test_the_ply_number_increments(self) -> None:
        """MT-5 numbers plies contiguously from 1, so the first move played
        is ply 1."""
        playing = match_at({"c3": LIGHT_MAN, "f6": DARK_MAN})

        play(playing, move("c3", "d4"))
        play(playing, move("f6", "e5"))

        assert playing.ply_number == 2

    def test_the_last_move_is_recorded(self) -> None:
        playing = match_at({"c3": LIGHT_MAN})
        played = move("c3", "d4")

        play(playing, played)

        assert playing.last_move == played

    def test_an_illegal_move_is_refused_by_the_engine(self) -> None:
        """`Match` re-derives nothing. The refusal comes from the validator
        the applier already holds."""
        from app.modules.engine import IllegalMove

        playing = match_at({"c3": LIGHT_MAN, "d4": DARK_MAN})

        with pytest.raises(IllegalMove):
            play(playing, move("c3", "b4"))

    def test_a_refused_move_leaves_the_match_untouched(self) -> None:
        from app.modules.engine import IllegalMove

        playing = match_at({"c3": LIGHT_MAN, "d4": DARK_MAN})
        before = playing.position

        with pytest.raises(IllegalMove):
            play(playing, move("c3", "b4"))

        assert (playing.position, playing.ply_number, playing.last_move) == (before, 0, None)


class TestPositionHistory:
    def test_the_opening_position_is_recorded_at_creation(self) -> None:
        """A game that returns to its opening has repeated it once, not
        reached it for the first time."""
        created = Match.create(RUSSIAN)

        assert created.current_position_occurrences == 1

    def test_each_new_position_is_recorded(self) -> None:
        playing = match_at({"c3": LIGHT_MAN})

        play(playing, move("c3", "d4"))

        assert playing.current_position_occurrences == 1

    def test_a_repeated_position_is_counted_again(self) -> None:
        """Two kings shuffling back and forth. Four plies return the board
        *and* the side to move to where they started."""
        shuffling = match_at({"a1": LIGHT_KING, "h2": DARK_KING})
        opening = shuffling.position

        play(shuffling, move("a1", "b2"))
        play(shuffling, move("h2", "g1"))
        play(shuffling, move("b2", "a1"))
        play(shuffling, move("g1", "h2"))

        assert shuffling.occurrences_of(opening) == 2

    def test_the_repeated_position_is_the_current_one(self) -> None:
        shuffling = match_at({"a1": LIGHT_KING, "h2": DARK_KING})

        play(shuffling, move("a1", "b2"))
        play(shuffling, move("h2", "g1"))
        play(shuffling, move("b2", "a1"))
        play(shuffling, move("g1", "h2"))

        assert shuffling.current_position_occurrences == 2

    def test_the_same_board_with_the_other_side_to_move_is_a_different_position(self) -> None:
        """`Position` is the repetition key precisely because it includes
        the side to move — see `test_position.py`."""
        shuffling = match_at({"a1": LIGHT_KING, "h2": DARK_KING})

        play(shuffling, move("a1", "b2"))
        play(shuffling, move("h2", "g1"))
        play(shuffling, move("b2", "a1"))

        assert shuffling.current_position_occurrences == 1

    def test_a_position_never_reached_has_occurred_nothing(self) -> None:
        playing = match_at({"c3": LIGHT_MAN})

        assert playing.occurrences_of(Match.create(RUSSIAN).position) == 0

    def test_the_history_cannot_be_written_through(self) -> None:
        """The counts are the aggregate's to change; a caller that could
        edit them could desynchronise the history from the moves that
        produced it."""
        playing = match_at({"c3": LIGHT_MAN})

        with pytest.raises(TypeError):
            playing.position_history[playing.position] = 99  # type: ignore[index]


class TestHistoryCounter:
    def test_a_new_match_has_made_no_progress_yet(self) -> None:
        assert Match.create(RUSSIAN).plies_since_progress == 0

    def test_a_quiet_king_move_increments_it(self) -> None:
        """Kings shuffling is the state the move-limit draws exist to end."""
        shuffling = match_at({"a1": LIGHT_KING, "h2": DARK_KING})

        play(shuffling, move("a1", "b2"))

        assert shuffling.plies_since_progress == 1

    def test_it_keeps_incrementing(self) -> None:
        shuffling = match_at({"a1": LIGHT_KING, "h2": DARK_KING})

        play(shuffling, move("a1", "b2"))
        play(shuffling, move("h2", "g1"))
        play(shuffling, move("b2", "c3"))

        assert shuffling.plies_since_progress == 3

    def test_a_man_move_resets_it(self) -> None:
        """A man only ever advances, so a game moving one is going
        somewhere."""
        playing = match_at({"a1": LIGHT_KING, "g7": LIGHT_MAN, "h2": DARK_KING})
        play(playing, move("a1", "b2"))
        play(playing, move("h2", "g1"))
        assert playing.plies_since_progress == 2

        play(playing, move("g7", "f8", promotes_to=PieceRank.KING))

        assert playing.plies_since_progress == 0

    def test_a_capture_resets_it(self) -> None:
        """Material only ever decreases. The capture only becomes available
        on the third ply, so the counter is genuinely non-zero first."""
        playing = match_at({"a1": LIGHT_KING, "h2": DARK_KING, "c7": DARK_MAN})
        play(playing, move("a1", "b2"))
        play(playing, move("h2", "e5"))
        assert playing.plies_since_progress == 2

        play(playing, move("b2", "f6", captured=("e5",)))

        assert playing.plies_since_progress == 0

    def test_a_promoting_man_move_resets_it(self) -> None:
        """It began as a man, and the advance is what was irreversible."""
        playing = match_at({"a1": LIGHT_KING, "g7": LIGHT_MAN, "h2": DARK_KING})
        play(playing, move("a1", "b2"))
        play(playing, move("h2", "g1"))
        assert playing.plies_since_progress == 2

        play(playing, move("g7", "h8", promotes_to=PieceRank.KING))

        assert playing.plies_since_progress == 0

    def test_the_counter_lives_on_the_match_and_not_the_position(self) -> None:
        """Two games can reach the same board having done very different
        things to get there. MT-12: terminal detection consults game
        history, not just the position."""
        assert not hasattr(Match.create(RUSSIAN).position, "plies_since_progress")


class TestTerminalDetection:
    def test_a_capture_that_takes_the_last_piece_completes_the_match(self) -> None:
        playing = match_at({"c3": LIGHT_MAN, "d4": DARK_MAN})

        play(playing, move("c3", "e5", captured=("d4",)))

        assert playing.status is MatchStatus.COMPLETED

    def test_the_winner_and_reason_are_recorded(self) -> None:
        playing = match_at({"c3": LIGHT_MAN, "d4": DARK_MAN})

        play(playing, move("c3", "e5", captured=("d4",)))

        assert playing.result is not None
        assert (playing.result.winner, playing.result.reason) == (
            PlayerSide.LIGHT,
            TerminationReason.ALL_PIECES_CAPTURED,
        )

    def test_a_win_is_a_win_outcome(self) -> None:
        playing = match_at({"c3": LIGHT_MAN, "d4": DARK_MAN})

        play(playing, move("c3", "e5", captured=("d4",)))

        assert playing.result is not None
        assert playing.result.outcome is MatchOutcome.WIN

    def test_a_non_terminal_move_leaves_the_match_active(self) -> None:
        playing = match_at({"c3": LIGHT_MAN, "f6": DARK_MAN})

        play(playing, move("c3", "d4"))

        assert (playing.status, playing.result) == (MatchStatus.ACTIVE, None)

    def test_every_engine_reason_maps_to_a_termination_reason(self) -> None:
        """Two enums, because the engine can only know what a board shows
        and nine of `TerminationReason`'s members are about clocks,
        connections and moderators. The mapping is asserted total rather
        than trusted."""
        assert set(_TERMINATION_FOR) == set(TerminalReason)
        assert set(_TERMINATION_FOR.values()) <= set(TerminationReason)


class TestResignation:
    def test_the_opponent_wins(self) -> None:
        playing = match_at({"c3": LIGHT_MAN, "f6": DARK_MAN})

        playing.resign(PlayerSide.LIGHT)

        assert playing.result is not None
        assert playing.result.winner is PlayerSide.DARK

    def test_the_match_completes(self) -> None:
        playing = match_at({"c3": LIGHT_MAN, "f6": DARK_MAN})

        playing.resign(PlayerSide.DARK)

        assert playing.status is MatchStatus.COMPLETED

    def test_the_reason_is_resignation(self) -> None:
        playing = match_at({"c3": LIGHT_MAN, "f6": DARK_MAN})

        playing.resign(PlayerSide.DARK)

        assert playing.termination_reason is TerminationReason.RESIGNATION

    def test_either_side_may_resign_regardless_of_whose_turn_it_is(self) -> None:
        """Giving up is not a move. Who may resign for whom is an
        authorization question, and this layer has no notion of a caller."""
        playing = match_at({"c3": LIGHT_MAN, "f6": DARK_MAN}, side=PlayerSide.LIGHT)

        playing.resign(PlayerSide.DARK)

        assert playing.result is not None
        assert playing.result.winner is PlayerSide.LIGHT

    def test_the_board_is_untouched(self) -> None:
        """A resigned game must still replay to the position it was
        abandoned in."""
        playing = match_at({"c3": LIGHT_MAN, "f6": DARK_MAN})
        before = playing.position

        playing.resign(PlayerSide.LIGHT)

        assert (playing.position, playing.ply_number) == (before, 0)


class TestAbort:
    def test_a_created_match_may_be_aborted(self) -> None:
        created = Match.create(RUSSIAN)

        created.abort()

        assert created.status is MatchStatus.ABORTED

    def test_an_active_match_may_be_aborted(self) -> None:
        playing = match_at({"c3": LIGHT_MAN, "f6": DARK_MAN})

        playing.abort()

        assert playing.status is MatchStatus.ABORTED

    def test_an_aborted_match_has_no_winner(self) -> None:
        playing = match_at({"c3": LIGHT_MAN, "f6": DARK_MAN})

        playing.abort()

        assert playing.result is not None
        assert playing.result.winner is None

    def test_an_abort_is_not_a_draw(self) -> None:
        """MT-11: an aborted match produces no result and no rating effect.
        A draw is an outcome two players played to, and it counts."""
        playing = match_at({"c3": LIGHT_MAN, "f6": DARK_MAN})

        playing.abort()

        assert playing.result is not None
        assert playing.result.outcome is MatchOutcome.NONE

    def test_the_reason_is_abort(self) -> None:
        playing = match_at({"c3": LIGHT_MAN, "f6": DARK_MAN})

        playing.abort()

        assert playing.termination_reason is TerminationReason.ABORT
