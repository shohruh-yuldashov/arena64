"""`MoveValidator` — legality, and the king boundary.

The validator holds no rules, so there is nothing here that re-checks
draughts. What is tested is that it *delegates*: a move the generator
offers is accepted, one it does not is refused, and a position the
generator cannot evaluate is refused differently.

The rules themselves are tested once, in `test_move_generation.py`. Testing
them again through the validator would be the same duplication in the test
suite that the validator exists to avoid in the code — and it would pass
while the delegation was broken.
"""

import pytest

from app.core.exceptions import RuleViolationError
from app.modules.engine import (
    Board,
    BoardCoordinate,
    BoardVariant,
    GameDomainError,
    IllegalMove,
    InvalidMove,
    Move,
    MoveGenerator,
    MoveValidator,
    Piece,
    PieceRank,
    PlayerSide,
    Position,
    UnsupportedPieceMovement,
)

RUSSIAN = BoardVariant.RUSSIAN_8X8

LIGHT_MAN = Piece(side=PlayerSide.LIGHT, rank=PieceRank.MAN)
DARK_MAN = Piece(side=PlayerSide.DARK, rank=PieceRank.MAN)
LIGHT_KING = Piece(side=PlayerSide.LIGHT, rank=PieceRank.KING)
DARK_KING = Piece(side=PlayerSide.DARK, rank=PieceRank.KING)

generator = MoveGenerator()
validator = MoveValidator(generator)


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


class TestLegalMoves:
    def test_a_quiet_move_the_generator_offers_is_legal(self) -> None:
        assert validator.is_legal(position({"c3": LIGHT_MAN}, PlayerSide.LIGHT), move("c3", "d4"))

    def test_validating_a_legal_quiet_move_raises_nothing(self) -> None:
        validator.validate(position({"c3": LIGHT_MAN}, PlayerSide.LIGHT), move("c3", "d4"))

    def test_a_capture_the_generator_offers_is_legal(self) -> None:
        capture = move("c3", "e5", captured=("d4",))

        assert validator.is_legal(
            position({"c3": LIGHT_MAN, "d4": DARK_MAN}, PlayerSide.LIGHT), capture
        )

    def test_a_promoting_move_is_legal_with_its_promotion_stated(self) -> None:
        crowning = move("g7", "h8", promotes_to=PieceRank.KING)

        assert validator.is_legal(position({"g7": LIGHT_MAN}, PlayerSide.LIGHT), crowning)

    def test_every_generated_move_validates(self) -> None:
        """The delegation stated as a property: whatever the generator
        offers, the validator accepts. Anything else means the two have
        drifted, which is the whole failure this design exists to make
        impossible."""
        opening = position(
            {"a1": LIGHT_MAN, "c3": LIGHT_MAN, "g7": LIGHT_MAN, "f6": DARK_MAN},
            PlayerSide.LIGHT,
        )

        for generated in generator.legal_moves(opening):
            validator.validate(opening, generated)


class TestIllegalMoves:
    def test_a_quiet_move_is_refused_while_a_capture_is_available(self) -> None:
        obliged = position({"c3": LIGHT_MAN, "d4": DARK_MAN}, PlayerSide.LIGHT)

        with pytest.raises(IllegalMove):
            validator.validate(obliged, move("c3", "b4"))

    def test_the_side_not_to_move_may_not_move(self) -> None:
        both = position({"c3": LIGHT_MAN, "f6": DARK_MAN}, PlayerSide.LIGHT)

        with pytest.raises(IllegalMove):
            validator.validate(both, move("f6", "e5"))

    def test_a_move_from_an_empty_square_is_refused(self) -> None:
        with pytest.raises(IllegalMove):
            validator.validate(position({"c3": LIGHT_MAN}, PlayerSide.LIGHT), move("e3", "d4"))

    def test_a_move_onto_an_occupied_square_is_refused(self) -> None:
        crowded = position({"c3": LIGHT_MAN, "b4": LIGHT_MAN}, PlayerSide.LIGHT)

        with pytest.raises(IllegalMove):
            validator.validate(crowded, move("c3", "b4"))

    def test_a_backward_step_is_refused(self) -> None:
        with pytest.raises(IllegalMove):
            validator.validate(position({"c3": LIGHT_MAN}, PlayerSide.LIGHT), move("c3", "b2"))

    def test_is_legal_answers_false_where_validate_raises(self) -> None:
        assert not validator.is_legal(
            position({"c3": LIGHT_MAN}, PlayerSide.LIGHT), move("c3", "b2")
        )


class TestPromotionMetadata:
    """Promotion is part of a move's identity, because `Move` compares on
    it. A caller must echo the move the engine offered rather than rebuild
    one from an origin and a destination."""

    def test_promotion_claimed_away_from_the_crownhead_is_refused(self) -> None:
        with pytest.raises(IllegalMove):
            validator.validate(
                position({"c3": LIGHT_MAN}, PlayerSide.LIGHT),
                move("c3", "d4", promotes_to=PieceRank.KING),
            )

    def test_promotion_omitted_on_the_crownhead_is_refused(self) -> None:
        with pytest.raises(IllegalMove):
            validator.validate(position({"g7": LIGHT_MAN}, PlayerSide.LIGHT), move("g7", "h8"))


class TestMalformedIsNotIllegal:
    """The two refusals must not collapse into one another.

    They have different audiences and `game` will handle them differently:
    an illegal move is a message back to one client, a malformed one is a
    caller bug that should never occur in play. The hierarchy encodes that
    — `IllegalMove` is a `RuleViolationError`, everything else the kernel
    raises is a `GameDomainError` — and these tests are what stop a later
    edit quietly merging them.
    """

    def test_a_malformed_move_is_refused_before_a_position_is_involved(self) -> None:
        with pytest.raises(InvalidMove):
            move("c3", "c3")

    def test_an_illegal_move_is_not_a_malformed_one(self) -> None:
        with pytest.raises(IllegalMove):
            validator.validate(position({"c3": LIGHT_MAN}, PlayerSide.LIGHT), move("c3", "b2"))

        assert not issubclass(IllegalMove, InvalidMove)

    def test_a_malformed_move_is_not_an_illegal_one(self) -> None:
        assert not issubclass(InvalidMove, IllegalMove)

    def test_an_illegal_move_is_a_rule_violation_and_not_a_kernel_bug(self) -> None:
        """The category split `game` branches on: expected gameplay traffic
        on one side, "the kernel was used wrongly" on the other."""
        assert issubclass(IllegalMove, RuleViolationError)
        assert not issubclass(IllegalMove, GameDomainError)

    def test_every_other_engine_failure_is_a_kernel_bug(self) -> None:
        for failure in (InvalidMove, UnsupportedPieceMovement):
            assert issubclass(failure, GameDomainError)


class TestKingBoundary:
    """A64-014.3's explicit boundary, deleted by A64-014.5."""

    def test_a_king_of_the_side_to_move_is_refused_by_the_generator(self) -> None:
        with pytest.raises(UnsupportedPieceMovement):
            generator.legal_moves(position({"c3": LIGHT_KING}, PlayerSide.LIGHT))

    def test_validating_against_such_a_position_raises_the_same_thing(self) -> None:
        """Not `IllegalMove`, and not `False`: "this engine cannot tell" is
        a different answer from "no", and collapsing them would let a
        caller record a refusal the rules never made."""
        with pytest.raises(UnsupportedPieceMovement):
            validator.validate(position({"c3": LIGHT_KING}, PlayerSide.LIGHT), move("c3", "b4"))

    def test_is_legal_raises_rather_than_answering_false(self) -> None:
        with pytest.raises(UnsupportedPieceMovement):
            validator.is_legal(position({"c3": LIGHT_KING}, PlayerSide.LIGHT), move("c3", "b4"))

    def test_a_king_belonging_to_the_opponent_is_evaluated_normally(self) -> None:
        """It is a piece a man may jump, which this build handles — so
        refusing it would reject positions the engine answers for."""
        moves = generator.legal_moves(
            position({"c3": LIGHT_MAN, "d4": DARK_KING}, PlayerSide.LIGHT)
        )

        assert [str(generated) for generated in moves] == ["c3xe5"]

    def test_a_king_of_the_other_side_does_not_block_that_side_later(self) -> None:
        """The same board, DARK to move, is what raises — the guard is on
        the side to move, not on the board."""
        with pytest.raises(UnsupportedPieceMovement):
            generator.legal_moves(position({"c3": LIGHT_MAN, "d4": DARK_KING}, PlayerSide.DARK))
