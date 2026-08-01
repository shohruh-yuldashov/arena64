"""Regression — the defects this epic actually produced. A64-014.9.

## Why this file is short

Broad feature coverage already exists: quiet moves, captures, sequences,
kings, draws, replay and serialization each have a suite of their own, and
copying assertions across into a file labelled "regression" would be
duplication with a better name (CLAUDE.md §2.7). A regression test earns
its place by pinning a **specific failure that happened**, with the story
of how it happened, so that the next person who breaks it the same way
learns what they broke.

So this holds one entry per real defect or near-miss found while building
A64-014.1 through A64-014.8, and nothing else.

## The two that were real

Both were caught by a test rather than in review, and both would have
produced a *plausible* wrong game rather than a crash — which is the
failure mode AD-13 says the engine's whole design is defending against.

## The three that were near-misses

Design decisions that would have been defects had they gone the other way,
each identified when the task after them made the consequence visible.
They are pinned because the reasoning that avoided them is not obvious
from the code that resulted.
"""

import pytest

from app.modules.engine import (
    CURRENT_ENGINE_VERSION,
    Board,
    BoardCoordinate,
    BoardVariant,
    DestinationOccupied,
    Move,
    MoveApplier,
    MoveGenerator,
    MoveValidator,
    Piece,
    PieceRank,
    PlayerSide,
    Position,
    TerminalStateEvaluator,
    geometry_of,
)
from app.modules.game.domain import DrawRuleSet, Match

RUSSIAN = BoardVariant.RUSSIAN_8X8

LIGHT_MAN = Piece(side=PlayerSide.LIGHT, rank=PieceRank.MAN)
DARK_MAN = Piece(side=PlayerSide.DARK, rank=PieceRank.MAN)
LIGHT_KING = Piece(side=PlayerSide.LIGHT, rank=PieceRank.KING)
DARK_KING = Piece(side=PlayerSide.DARK, rank=PieceRank.KING)

generator = MoveGenerator()
applier = MoveApplier(MoveValidator(generator))
evaluator = TerminalStateEvaluator(generator)
draw_rules = DrawRuleSet()


def square(name: str) -> BoardCoordinate:
    return BoardCoordinate.parse(name)


def position(placement: dict[str, Piece], side: PlayerSide = PlayerSide.LIGHT) -> Position:
    return Position(
        board=Board(RUSSIAN, {square(name): piece for name, piece in placement.items()}),
        side_to_move=side,
    )


class TestApplyingASequenceThatReturnsToItsOrigin:
    """**A64-014.4, real defect.** `MoveApplier` could not play a capture
    sequence that circled a ring of victims and came back to the square it
    started from: it relocated through `Board.move`, which refuses
    `origin == destination`.

    That refusal is correct where it lives — a bare self-relocation is a
    caller with a bug — so the fix was in the applier, which now lifts the
    piece and places it. The rule is genuine draughts and the generator had
    been producing such sequences since the same task.
    """

    RING = position(
        {
            "c3": LIGHT_MAN,
            "b4": DARK_MAN,
            "d4": DARK_MAN,
            "b6": DARK_MAN,
            "d6": DARK_MAN,
        }
    )

    def test_the_generator_offers_the_looping_sequence(self) -> None:
        assert [str(move) for move in generator.legal_moves(self.RING)] == [
            "c3xa5xc7xe5xc3",
            "c3xe5xc7xa5xc3",
        ]

    def test_the_applier_can_play_it(self) -> None:
        looping = generator.legal_moves(self.RING)[0]

        after = applier.apply(self.RING, looping)

        assert after.board.occupied_squares == {square("c3"): LIGHT_MAN}

    def test_board_move_still_refuses_a_bare_self_relocation(self) -> None:
        """The protection that was *not* weakened to fix the above. Every
        other caller still relies on it."""
        with pytest.raises(DestinationOccupied):
            self.RING.board.move(square("c3"), square("c3"))


class TestAKingThatStartedThePlyIsNotPromoted:
    """**A64-014.5, real defect, caught in design and pinned here.**
    `_sequence_promotion` read the mover's *current* rank: "the mover is a
    king" was sufficient evidence it had been crowned, because before
    kings could start a ply it always was.

    The moment kings could move, every king move would have claimed a
    promotion — including a king merely sliding across its own crownhead.
    A replay of such a game would have crowned an already-crowned piece and
    produced a board nobody played.
    """

    def test_a_king_capture_ending_on_the_crownhead_reports_nothing(self) -> None:
        ending = generator.legal_moves(position({"e5": LIGHT_KING, "f6": DARK_MAN}))

        assert [(str(move), move.promotes_to) for move in ending] == [
            ("e5xg7", None),
            ("e5xh8", None),
        ]

    def test_a_king_sliding_across_its_crownhead_reports_nothing(self) -> None:
        crossing = generator.legal_moves(position({"g5": LIGHT_KING}))

        assert all(move.promotes_to is None for move in crossing)

    def test_a_man_reaching_the_crownhead_still_reports_a_promotion(self) -> None:
        """The behaviour the fix had to preserve."""
        crowning = generator.legal_moves(position({"g7": LIGHT_MAN}))

        assert all(move.promotes_to is PieceRank.KING for move in crowning)


class TestTheOpeningPositionCountsAsOccurrenceOne:
    """**A64-014.7, near-miss.** A repetition rule that counted *returns*
    rather than *occurrences* would fire one return late, so a threefold
    draw would arrive on the third return instead of the second.

    The fix is that `Match` records its starting position at creation. The
    off-by-one is invisible in any test that only checks the draw fires.
    """

    SHUFFLE = (
        Move(path=(square("a1"), square("b2"))),
        Move(path=(square("h2"), square("g1"))),
        Move(path=(square("b2"), square("a1"))),
        Move(path=(square("g1"), square("h2"))),
    )

    def shuffling(self) -> Match:
        match = Match(
            variant=RUSSIAN,
            engine_version=CURRENT_ENGINE_VERSION,
            position=position({"a1": LIGHT_KING, "h2": DARK_KING}),
        )
        match.start()
        return match

    def test_a_new_match_has_already_seen_its_opening_once(self) -> None:
        assert self.shuffling().current_position_occurrences == 1

    def test_the_first_return_is_the_second_occurrence_and_not_a_draw(self) -> None:
        match = self.shuffling()

        for move in self.SHUFFLE:
            match.play(move, applier, evaluator, draw_rules)

        assert (match.current_position_occurrences, match.status.value) == (2, "active")

    def test_the_second_return_is_the_third_occurrence_and_draws(self) -> None:
        match = self.shuffling()

        for move in self.SHUFFLE * 2:
            match.play(move, applier, evaluator, draw_rules)

        assert (match.current_position_occurrences, match.status.value) == (3, "completed")


class TestARejectedMoveLeavesNoTrace:
    """**A64-014.8, near-miss.** Appending the move record before the
    applier ran would have put a phantom ply in the log for every refused
    move — and MT-5's gap-free guarantee is what makes a game replayable at
    all.
    """

    def test_a_refused_move_appends_nothing(self) -> None:
        from app.modules.engine import IllegalMove

        match = Match(
            variant=RUSSIAN,
            engine_version=CURRENT_ENGINE_VERSION,
            position=position({"c3": LIGHT_MAN, "d4": DARK_MAN}),
        )
        match.start()

        with pytest.raises(IllegalMove):
            match.play(Move(path=(square("c3"), square("b4"))), applier, evaluator, draw_rules)

        assert (match.move_log, match.ply_number, match.last_move) == ((), 0, None)

    def test_a_refused_move_leaves_the_position_alone(self) -> None:
        from app.modules.engine import IllegalMove

        start = position({"c3": LIGHT_MAN, "d4": DARK_MAN})
        match = Match(variant=RUSSIAN, engine_version=CURRENT_ENGINE_VERSION, position=start)
        match.start()

        with pytest.raises(IllegalMove):
            match.play(Move(path=(square("c3"), square("b4"))), applier, evaluator, draw_rules)

        assert match.position == start


class TestVariantAxesAreNotConstants:
    """**A64-014.5, near-miss.** Three `BoardGeometry` axes were
    single-valued across the two variants that existed —
    `men_may_capture_backward`, `kings_fly`, `mid_sequence_promotion` —
    which made them settings nothing could tell apart from constants. A
    generator that ignored one would have passed every test.

    Adding English draughts as configuration gave each a second value.
    This pins that they are still read, by asserting the behaviour differs
    between variants on one position.
    """

    BEHIND = {"c5": LIGHT_MAN, "b4": DARK_MAN}
    """A man with an opponent behind it: Russian takes it, English cannot."""

    def test_backward_capture_differs_between_variants(self) -> None:
        russian = [str(move) for move in generator.legal_moves(position(self.BEHIND))]
        english = [
            str(move)
            for move in generator.legal_moves(
                Position(
                    board=Board(
                        BoardVariant.ENGLISH_8X8,
                        {square(name): piece for name, piece in self.BEHIND.items()},
                    ),
                    side_to_move=PlayerSide.LIGHT,
                )
            )
        ]

        assert russian == ["c5xa3"]
        assert english == ["c5-b6", "c5-d6"]

    def test_king_reach_differs_between_variants(self) -> None:
        assert geometry_of(RUSSIAN).king_reach == 8
        assert geometry_of(BoardVariant.ENGLISH_8X8).king_reach == 1

    def test_mid_sequence_promotion_differs_between_variants(self) -> None:
        crowning = {"b6": LIGHT_MAN, "c7": DARK_MAN, "e7": DARK_MAN}
        russian = [str(move) for move in generator.legal_moves(position(crowning))]
        english = [
            str(move)
            for move in generator.legal_moves(
                Position(
                    board=Board(
                        BoardVariant.ENGLISH_8X8,
                        {square(name): piece for name, piece in crowning.items()},
                    ),
                    side_to_move=PlayerSide.LIGHT,
                )
            )
        ]

        assert russian == ["b6xd8xh4", "b6xd8xg5", "b6xd8xf6"]
        assert english == ["b6xd8"]
