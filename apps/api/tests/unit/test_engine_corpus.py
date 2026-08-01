"""The conformance corpus, executed against the Python engine.

AD-14: "The Python engine and the TypeScript client engine are two
implementations governed by one versioned corpus of positions, legal move
sets, and expected outcomes, executed by both in CI… **The corpus is the
contract.**" This module is the Python half of that sentence. There is no
TypeScript engine yet; the corpus is written so that adding one is a
reader, not a renegotiation.

Two kinds of test live here, and the distinction matters:

- **conformance** — every case's expected move list is what the generator
  produces. A failure means the engine and the contract disagree, and
  which of them is wrong is a decision, not a fix.
- **corpus integrity** — the file itself is well-formed: unique ids, moves
  written in the order the contract specifies, the cases the task requires.
  A failure means the corpus is wrong, and the engine is not implicated.
"""

import pytest

from app.modules.engine import (
    BoardVariant,
    IllegalMove,
    InvalidMove,
    MoveGenerator,
    MoveValidator,
    PlayerSide,
    Position,
    UnsupportedPieceMovement,
    initial_board,
)
from tests.corpus import CorpusCase, RejectionCase, RejectionCategory, load_cases, load_rejections

CASES = load_cases()
REJECTIONS = load_rejections()

REFUSAL_FOR = {
    RejectionCategory.ILLEGAL_MOVE: IllegalMove,
    RejectionCategory.MALFORMED_MOVE: InvalidMove,
    RejectionCategory.UNSUPPORTED_PIECE: UnsupportedPieceMovement,
}
"""Each category's exception. Exhaustive over `RejectionCategory` by a test
below, so a category added to the corpus cannot be silently unhandled."""

REQUIRED_REJECTIONS = {
    "quiet-move-refused-while-a-capture-is-available",
    "the-side-not-to-move-may-not-move",
    "a-move-from-an-empty-square-is-refused",
    "a-move-onto-an-occupied-square-is-refused",
    "a-path-that-steps-nowhere-is-malformed",
    "a-king-of-the-side-to-move-cannot-be-evaluated",
    "promotion-claimed-away-from-the-crownhead-is-refused",
}
"""The rejection cases A64-014.3 names."""

REQUIRED_CASES = {
    "russian-initial-position-light-to-move",
    "light-man-quiet-moves-both-diagonals",
    "blocked-man-has-no-moves",
    "single-forward-capture",
    "capture-suppresses-every-other-piece-quiet-moves",
    "man-captures-backward-when-the-variant-allows-it",
    "quiet-move-onto-the-crownhead-promotes",
    "forced-two-jump-sequence",
    "incomplete-prefix-is-not-offered",
    "two-alternative-complete-sequences",
    "maximum-capture-keeps-only-the-longest",
    "a-taken-piece-blocks-and-is-never-taken-again",
    "russian-man-crowns-mid-sequence-and-continues",
    "international-man-passes-through-the-crownhead",
    "international-man-crowned-when-the-sequence-ends-there",
}
"""The cases A64-014.2 and A64-014.4 name. Asserted by id rather than by
count, so that adding a case never quietly satisfies a requirement a
deleted one covered."""

generator = MoveGenerator()
validator = MoveValidator(generator)


@pytest.mark.parametrize("case", REJECTIONS, ids=[case.id for case in REJECTIONS])
def test_the_engine_refuses_what_the_corpus_says_it_must(case: RejectionCase) -> None:
    """A64-014.3's half of the contract: the moves that must not be played,
    and *where* each is stopped.

    The category is asserted, not just "something raised". `InvalidMove`
    and `IllegalMove` are refused at different moments by different code
    for different reasons — one is a caller that built a move wrong, the
    other is a player being told no — and a case that swapped them would
    satisfy a test that only checked for a failure.
    """
    expected = REFUSAL_FOR[case.rejection]

    if case.rejection is RejectionCategory.MALFORMED_MOVE:
        # Refused at construction, so there is no move to validate — which
        # is the distinction the case exists to record.
        with pytest.raises(expected):
            case.build_move()
        return

    with pytest.raises(expected):
        validator.validate(case.position, case.build_move())


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_the_engine_produces_the_moves_the_corpus_expects(case: CorpusCase) -> None:
    """The whole point of the corpus, once per case.

    The comparison is on the ordered tuple and not on a set: the order is
    part of the contract (see the corpus README), and an engine that found
    the right moves in the wrong order would diverge from a TypeScript
    implementation the first time either replayed by index.
    """
    assert generator.legal_moves(case.position) == case.expected_moves, case.description


class TestCorpusIntegrity:
    def test_the_corpus_is_not_empty(self) -> None:
        """A loader that silently found no files would make every
        conformance test above vacuously pass."""
        assert CASES

    def test_the_rejection_corpus_is_not_empty(self) -> None:
        assert REJECTIONS

    def test_every_required_case_is_present(self) -> None:
        assert {case.id for case in CASES} >= REQUIRED_CASES

    def test_every_required_rejection_is_present(self) -> None:
        assert {case.id for case in REJECTIONS} >= REQUIRED_REJECTIONS

    def test_case_ids_are_unique(self) -> None:
        identifiers = [case.id for case in CASES] + [case.id for case in REJECTIONS]

        assert len(set(identifiers)) == len(identifiers)

    def test_every_rejection_category_has_an_expected_exception(self) -> None:
        """Exhaustiveness over the enum, so a category added to the corpus
        format cannot reach the parametrised test above as a `KeyError`
        that reads like a corpus typo."""
        assert set(REFUSAL_FOR) == set(RejectionCategory)

    def test_every_rejection_category_is_exercised(self) -> None:
        """A category nothing uses is a claim the corpus is not making."""
        assert {case.rejection for case in REJECTIONS} == set(RejectionCategory)

    def test_expected_moves_are_written_in_the_contracted_order(self) -> None:
        """The README states ascending `(path, captured)`. A case written
        out of order would pass conformance only by accident of what the
        generator happens to do today."""
        for case in CASES:
            expected = list(case.expected_moves)
            assert expected == sorted(expected, key=lambda move: move.sort_key), case.source

    def test_no_case_gives_the_side_to_move_a_king(self) -> None:
        """Kings do not move until A64-014.5, so a case with one would
        claim a complete move list it cannot have — see the README."""
        for case in CASES:
            movable = [
                piece
                for piece in case.position.board.occupied_squares.values()
                if piece.side is case.position.side_to_move
            ]
            assert all(piece.rank.value == "man" for piece in movable), case.source


class TestOpeningPosition:
    def test_the_corpus_opening_matches_the_engine_opening(self) -> None:
        """The 24 squares in the corpus file are written out by hand; this
        is what stops a typo in one of them being invisible."""
        opening = next(
            case for case in CASES if case.id == "russian-initial-position-light-to-move"
        )

        assert opening.position.board == initial_board(BoardVariant.RUSSIAN_8X8)

    def test_the_opening_offers_seven_moves(self) -> None:
        position = Position(
            board=initial_board(BoardVariant.RUSSIAN_8X8), side_to_move=PlayerSide.LIGHT
        )

        assert len(generator.legal_moves(position)) == 7
