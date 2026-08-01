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
    initial_board,
)
from tests.corpus import (
    CorpusCase,
    RejectionCase,
    RejectionCategory,
    load_cases,
    load_rejections,
    superseded_ids,
)

CASES = load_cases()
REJECTIONS = load_rejections()
SUPERSEDED = superseded_ids()

REFUSAL_FOR = {
    RejectionCategory.ILLEGAL_MOVE: IllegalMove,
    RejectionCategory.MALFORMED_MOVE: InvalidMove,
}
"""Each **active** category's exception.

`UNSUPPORTED_PIECE` is deliberately absent: v2 superseded the only case
that used it and A64-014.5 deleted the exception it mapped to. The
category itself survives in the corpus format because v1's files are kept
verbatim — a test below asserts no case in force still carries it."""

REQUIRED_REJECTIONS = {
    "quiet-move-refused-while-a-capture-is-available",
    "the-side-not-to-move-may-not-move",
    "a-move-from-an-empty-square-is-refused",
    "a-move-onto-an-occupied-square-is-refused",
    "a-path-that-steps-nowhere-is-malformed",
    "promotion-claimed-away-from-the-crownhead-is-refused",
}
"""The rejection cases A64-014.3 names that are still in force.

`a-king-of-the-side-to-move-cannot-be-evaluated` was one of them and is
not any more: A64-014.5 implements kings, and v2 supersedes it. It is
asserted below as *superseded* rather than dropped silently."""

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
    "king-quiet-moves-along-open-diagonals",
    "an-opponent-stops-a-king-slide",
    "a-friendly-piece-stops-a-king-slide",
    "a-flying-king-capture-offers-every-square-beyond-the-victim",
    "a-king-capture-changes-direction",
    "a-king-cannot-take-the-same-piece-twice",
    "a-king-capture-suppresses-every-quiet-move",
    "maximum-capture-filters-king-sequences",
    "a-promoted-man-carries-on-as-a-flying-king",
    "promotion-ends-the-ply-when-the-variant-says-so",
    "a-man-and-a-king-share-one-move-list",
}
"""The cases A64-014.2, A64-014.4 and A64-014.5 name. Asserted by id rather
than by count, so that adding a case never quietly satisfies a requirement
a deleted one covered."""

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

    def test_every_category_in_force_has_an_expected_exception(self) -> None:
        """So a category added to the corpus format cannot reach the
        parametrised test above as a `KeyError` that reads like a corpus
        typo."""
        assert {case.rejection for case in REJECTIONS} <= set(REFUSAL_FOR)

    def test_every_expected_exception_is_exercised(self) -> None:
        """A mapping entry nothing uses is a claim the corpus is not
        making."""
        assert {case.rejection for case in REJECTIONS} == set(REFUSAL_FOR)

    def test_expected_moves_are_written_in_the_contracted_order(self) -> None:
        """The README states ascending `(path, captured)`. A case written
        out of order would pass conformance only by accident of what the
        generator happens to do today."""
        for case in CASES:
            expected = list(case.expected_moves)
            assert expected == sorted(expected, key=lambda move: move.sort_key), case.source


class TestSupersession:
    """v2 retires v1's king-rejection case. The mechanism is data — a
    `supersedes` array in the newer version — so v1's files stay
    byte-for-byte what they were and a TypeScript reader derives the same
    active set without reading any prose."""

    def test_the_king_rejection_case_is_superseded(self) -> None:
        assert "a-king-of-the-side-to-move-cannot-be-evaluated" in SUPERSEDED

    def test_a_superseded_case_is_not_in_force(self) -> None:
        assert not {case.id for case in REJECTIONS} & SUPERSEDED

    def test_the_historical_category_is_used_by_nothing_in_force(self) -> None:
        """`unsupported_piece` still parses, because v1 is kept and
        unreadable history is no history. Nothing active carries it."""
        assert RejectionCategory.UNSUPPORTED_PIECE not in {case.rejection for case in REJECTIONS}

    def test_the_superseded_case_still_exists_on_disk(self) -> None:
        """Superseded, not deleted. Loading v1 alone must still find it —
        that is what makes the change explainable a year from now."""
        assert "a-king-of-the-side-to-move-cannot-be-evaluated" in {
            case.id for case in load_rejections(through=1)
        }


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
