"""`TerminalStateEvaluator` and `EngineVersion` — A64-014.6's engine half.

The evaluator holds no rules. It counts material and asks
`MoveGenerator.legal_moves`, so what is tested here is that it *delegates*
and that it reports the more specific of two overlapping reasons. The rules
of movement are tested once, in the generator's own suites; asserting them
again through this would be the duplication the delegation exists to avoid.
"""

import pytest

from app.modules.engine import (
    CURRENT_ENGINE_VERSION,
    Board,
    BoardCoordinate,
    BoardVariant,
    EngineVersion,
    MoveGenerator,
    Piece,
    PieceRank,
    PlayerSide,
    Position,
    TerminalReason,
    TerminalStateEvaluator,
    initial_board,
)

RUSSIAN = BoardVariant.RUSSIAN_8X8
ENGLISH = BoardVariant.ENGLISH_8X8

LIGHT_MAN = Piece(side=PlayerSide.LIGHT, rank=PieceRank.MAN)
DARK_MAN = Piece(side=PlayerSide.DARK, rank=PieceRank.MAN)
LIGHT_KING = Piece(side=PlayerSide.LIGHT, rank=PieceRank.KING)

evaluator = TerminalStateEvaluator(MoveGenerator())


def position(
    placement: dict[str, Piece],
    side: PlayerSide = PlayerSide.LIGHT,
    variant: BoardVariant = RUSSIAN,
) -> Position:
    return Position(
        board=Board(
            variant,
            {BoardCoordinate.parse(name): piece for name, piece in placement.items()},
        ),
        side_to_move=side,
    )


class TestLosingByMaterial:
    def test_a_side_with_no_pieces_has_lost(self) -> None:
        verdict = evaluator.evaluate(position({"c3": DARK_MAN}))

        assert verdict is not None
        assert (verdict.winner, verdict.reason) == (
            PlayerSide.DARK,
            TerminalReason.ALL_PIECES_CAPTURED,
        )

    def test_the_opponent_wins_whichever_side_ran_out(self) -> None:
        verdict = evaluator.evaluate(position({"c3": LIGHT_MAN}, side=PlayerSide.DARK))

        assert verdict is not None
        assert verdict.winner is PlayerSide.LIGHT

    def test_an_empty_board_ends_the_game_for_the_side_to_move(self) -> None:
        """Unreachable in play — the side that took the last piece still has
        its own — but the evaluator must not have an opinion that depends on
        reachability."""
        verdict = evaluator.evaluate(position({}))

        assert verdict is not None
        assert verdict.winner is PlayerSide.DARK

    def test_running_out_of_pieces_is_reported_ahead_of_having_no_moves(self) -> None:
        """Both are true of the same position and either would give the same
        winner. The reason is what a player reads afterwards, and the
        specific one is the useful one."""
        verdict = evaluator.evaluate(position({"c3": DARK_MAN}))

        assert verdict is not None
        assert verdict.reason is TerminalReason.ALL_PIECES_CAPTURED


class TestLosingByMobility:
    def test_a_side_with_pieces_and_no_moves_has_lost(self) -> None:
        """English rules, so the cornered king reaches one square: b2 is not
        jumpable with c3 behind it."""
        cornered = position({"a1": LIGHT_KING, "b2": DARK_MAN, "c3": DARK_MAN}, variant=ENGLISH)

        verdict = evaluator.evaluate(cornered)

        assert verdict is not None
        assert (verdict.winner, verdict.reason) == (
            PlayerSide.DARK,
            TerminalReason.NO_LEGAL_MOVES,
        )

    def test_being_blocked_in_is_a_loss_and_not_a_draw(self) -> None:
        """The chess intuition is wrong here: draughts has no stalemate."""
        cornered = position({"a1": LIGHT_KING, "b2": DARK_MAN, "c3": DARK_MAN}, variant=ENGLISH)

        verdict = evaluator.evaluate(cornered)

        assert verdict is not None
        assert verdict.winner is PlayerSide.DARK


class TestPositionsThatContinue:
    def test_the_opening_position_is_not_terminal(self) -> None:
        opening = Position(board=initial_board(RUSSIAN), side_to_move=PlayerSide.LIGHT)

        assert evaluator.evaluate(opening) is None

    def test_an_ordinary_position_is_not_terminal(self) -> None:
        assert evaluator.evaluate(position({"c3": LIGHT_MAN, "f6": DARK_MAN})) is None

    def test_a_king_with_moves_is_not_terminal(self) -> None:
        """The case terminal detection had to wait for kings to be able to
        answer: before A64-014.5 an empty move list could also have meant
        "this build cannot evaluate a king"."""
        assert evaluator.evaluate(position({"c3": LIGHT_KING, "h8": DARK_MAN})) is None

    def test_a_side_that_can_only_capture_is_not_terminal(self) -> None:
        assert evaluator.evaluate(position({"c3": LIGHT_MAN, "d4": DARK_MAN})) is None


class TestTheEvaluatorStaysDrawFree:
    """A64-014.7 added draw rules and changed nothing here, deliberately.
    Widening this evaluator would mean giving the kernel a memory — the one
    thing AD-13 does not allow it — and would make "is this position
    terminal" a question with a different answer depending on how the game
    got there."""

    def test_a_repeated_position_is_not_terminal_to_the_evaluator(self) -> None:
        """It has no idea the position has occurred before, and must not."""
        repeated = position({"a1": LIGHT_KING, "h2": DARK_MAN})

        assert evaluator.evaluate(repeated) is None

    def test_it_takes_a_position_and_nothing_else(self) -> None:
        """The signature is the contract. A history argument would be the
        first crack in it."""
        import inspect

        parameters = list(inspect.signature(evaluator.evaluate).parameters)

        assert parameters == ["position"]


class TestEveryVerdictNamesAWinner:
    """Not an accident of these examples — a guarantee. Every draw in
    draughts is a property of the game's history, which `Match` owns and
    this evaluator cannot see, so it has no way to produce a verdict with
    nobody winning and a caller never has to handle one."""

    def test_no_verdict_this_evaluator_produces_lacks_a_winner(self) -> None:
        for source in (
            position({"c3": DARK_MAN}),
            position({"a1": LIGHT_KING, "b2": DARK_MAN, "c3": DARK_MAN}, variant=ENGLISH),
        ):
            verdict = evaluator.evaluate(source)

            assert verdict is not None
            assert verdict.winner is not None


class TestEngineVersion:
    def test_the_current_version_is_stable(self) -> None:
        """A constant, not something read from package metadata or git — see
        `app.modules.engine.version`. Two runs of the same source must stamp
        the same value or AD-15's enumeration is a guess."""
        assert EngineVersion(number=2) == CURRENT_ENGINE_VERSION

    def test_it_serialises_to_a_primitive(self) -> None:
        assert CURRENT_ENGINE_VERSION.as_primitive() == 2

    def test_versions_compare(self) -> None:
        """ "Played under a version older than the fix" is the query AD-15
        exists to make answerable, and it should be a comparison."""
        assert EngineVersion(number=1) < EngineVersion(number=2)

    def test_two_versions_with_one_number_are_equal(self) -> None:
        assert EngineVersion(number=3) == EngineVersion(number=3)

    def test_a_version_reads_legibly(self) -> None:
        assert str(EngineVersion(number=7)) == "engine-v7"

    def test_a_version_below_one_is_refused(self) -> None:
        with pytest.raises(ValueError):
            EngineVersion(number=0)
