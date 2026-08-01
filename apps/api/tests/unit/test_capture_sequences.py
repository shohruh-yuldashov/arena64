"""Multi-jump capture sequences — A64-014.4.

Single jumps are tested in `test_move_generation.py`; what is here is
everything that only exists once a sequence can continue: terminal-only
generation, the taken-once rule, path revisits, the maximum-capture filter,
and the two configured answers to crowning mid-jump.

The last two tests are the ones that matter most for the epic. A capture
sequence is worth nothing if `MoveValidator` will not accept it or
`MoveApplier` cannot play it, and those two were written before sequences
existed — so they are exercised here against the longest moves the engine
can now produce.
"""

from app.modules.engine import (
    Board,
    BoardCoordinate,
    BoardVariant,
    MoveApplier,
    MoveGenerator,
    MoveValidator,
    Piece,
    PieceRank,
    PlayerSide,
    Position,
)

RUSSIAN = BoardVariant.RUSSIAN_8X8
INTERNATIONAL = BoardVariant.INTERNATIONAL_10X10

LIGHT_MAN = Piece(side=PlayerSide.LIGHT, rank=PieceRank.MAN)
DARK_MAN = Piece(side=PlayerSide.DARK, rank=PieceRank.MAN)
LIGHT_KING = Piece(side=PlayerSide.LIGHT, rank=PieceRank.KING)

generator = MoveGenerator()
validator = MoveValidator(generator)
applier = MoveApplier(validator)


def square(notation: str) -> BoardCoordinate:
    return BoardCoordinate.parse(notation)


def position(
    placement: dict[str, Piece],
    side: PlayerSide = PlayerSide.LIGHT,
    variant: BoardVariant = RUSSIAN,
) -> Position:
    return Position(
        board=Board(variant, {square(name): piece for name, piece in placement.items()}),
        side_to_move=side,
    )


def notation(position: Position) -> list[str]:
    return [str(move) for move in generator.legal_moves(position)]


TWO_JUMPS = position({"c3": LIGHT_MAN, "d4": DARK_MAN, "f6": DARK_MAN})
"""Landing on e5 puts the man beside a second victim, so the ply continues."""

THREE_JUMPS = position(
    {"c3": LIGHT_MAN, "d4": DARK_MAN, "d6": DARK_MAN, "b6": DARK_MAN},
)

ALTERNATIVES = {"c3": LIGHT_MAN, "b4": DARK_MAN, "d4": DARK_MAN, "f6": DARK_MAN}
"""One short sequence and one long one from the same square. Russian rules
offer both; international keeps only the longer."""

RING = position(
    {
        "c3": LIGHT_MAN,
        "b4": DARK_MAN,
        "d4": DARK_MAN,
        "b6": DARK_MAN,
        "d6": DARK_MAN,
    }
)
"""Four victims in a ring. The man goes round either way and comes back to
the square it started from."""


class TestCompleteSequences:
    def test_a_two_jump_sequence_is_generated_whole(self) -> None:
        assert notation(TWO_JUMPS) == ["c3xe5xg7"]

    def test_the_prefix_of_a_longer_sequence_is_not_offered(self) -> None:
        """`c3xe5` is a legal-looking single jump and must not appear: a
        player who has jumped once and can jump again has to."""
        assert "c3xe5" not in notation(TWO_JUMPS)

    def test_neither_prefix_of_a_three_jump_sequence_is_offered(self) -> None:
        assert notation(THREE_JUMPS) == ["c3xe5xc7xa5"]

    def test_the_path_records_every_landing_in_order(self) -> None:
        sequence = generator.legal_moves(TWO_JUMPS)[0]

        assert sequence.path == (square("c3"), square("e5"), square("g7"))

    def test_the_captured_squares_are_recorded_in_the_order_they_were_jumped(self) -> None:
        """Order is part of the record — a replay steps a sequence square by
        square, and two paths through the same pieces differ only in it."""
        sequence = generator.legal_moves(TWO_JUMPS)[0]

        assert sequence.captured == (square("d4"), square("f6"))

    def test_more_than_one_complete_sequence_can_be_offered(self) -> None:
        assert notation(position(ALTERNATIVES)) == ["c3xa5", "c3xe5xg7"]


class TestTakenPiecesStayStanding:
    """The Turkish-strike rule: a captured piece is removed when the ply
    ends, not when it is jumped. Until then it blocks, and it can never be
    taken a second time."""

    def test_a_sequence_that_could_recross_a_victim_terminates(self) -> None:
        """From g7 the man faces f6, which it has already taken and whose far
        side — e5 — it has already vacated. Without the taken-once rule the
        walk would jump back and forth forever."""
        assert notation(TWO_JUMPS) == ["c3xe5xg7"]

    def test_no_sequence_captures_one_piece_twice(self) -> None:
        for sequence in generator.legal_moves(RING):
            assert len(set(sequence.captured)) == len(sequence.captured)

    def test_a_sequence_may_return_to_a_square_it_has_left(self) -> None:
        """The moving piece is lifted off its origin for the walk, so a ring
        of victims can be circled back to the start. Rejecting this would
        reject legal draughts moves."""
        assert notation(RING) == ["c3xa5xc7xe5xc3", "c3xe5xc7xa5xc3"]

    def test_a_revisiting_sequence_takes_every_victim_once(self) -> None:
        clockwise = generator.legal_moves(RING)[0]

        assert clockwise.captured == (
            square("b4"),
            square("b6"),
            square("d6"),
            square("d4"),
        )


class TestMaximumCapture:
    def test_the_longest_sequences_are_kept_where_the_variant_obliges_it(self) -> None:
        international = position(ALTERNATIVES, variant=INTERNATIONAL)

        assert notation(international) == ["c3xe5xg7"]

    def test_every_sequence_is_kept_where_it_does_not(self) -> None:
        """Russian draughts obliges *a* capture, not the biggest one, so the
        one-piece sequence stays on offer."""
        assert notation(position(ALTERNATIVES)) == ["c3xa5", "c3xe5xg7"]

    def test_the_filter_runs_after_the_search_and_not_inside_it(self) -> None:
        """A branch that opens with a single jump ends up the longest here.
        A walker that preferred the wider first jump would return `c3xa5`,
        which is the defect this asserts against."""
        international = position(ALTERNATIVES, variant=INTERNATIONAL)

        assert len(generator.legal_moves(international)[0].captured) == 2

    def test_a_capture_still_suppresses_every_quiet_move(self) -> None:
        crowded = position({"a1": LIGHT_MAN, "c3": LIGHT_MAN, "d4": DARK_MAN, "f6": DARK_MAN})

        assert notation(crowded) == ["c3xe5xg7"]


class TestMidSequencePromotion:
    def test_a_russian_man_crowns_on_arrival_and_carries_on_as_a_king(self) -> None:
        """The second jump — d8 to f6, over e7 — is a flying king's, not a
        man's. It fails outright if crowning waits for the ply to end."""
        crowning = position({"b6": LIGHT_MAN, "g5": LIGHT_MAN, "c7": DARK_MAN, "e7": DARK_MAN})

        assert notation(crowning) == ["b6xd8xf6"]

    def test_the_russian_sequence_reports_the_crown(self) -> None:
        crowning = position({"b6": LIGHT_MAN, "g5": LIGHT_MAN, "c7": DARK_MAN, "e7": DARK_MAN})

        assert generator.legal_moves(crowning)[0].promotes_to is PieceRank.KING

    def test_an_international_man_passes_over_the_crownhead_uncrowned(self) -> None:
        passing = position({"d8": LIGHT_MAN, "e9": DARK_MAN, "g9": DARK_MAN}, variant=INTERNATIONAL)

        assert [str(move) for move in generator.legal_moves(passing)] == ["d8xf10xh8"]

    def test_an_international_pass_through_reports_no_promotion(self) -> None:
        """The difference from Russian draughts is a geometry axis, not a
        variant check in the walker."""
        passing = position({"d8": LIGHT_MAN, "e9": DARK_MAN, "g9": DARK_MAN}, variant=INTERNATIONAL)

        assert generator.legal_moves(passing)[0].promotes_to is None

    def test_an_international_man_is_crowned_when_it_stops_on_the_crownhead(self) -> None:
        stopping = position({"h8": LIGHT_MAN, "i9": DARK_MAN}, variant=INTERNATIONAL)

        assert generator.legal_moves(stopping)[0].promotes_to is PieceRank.KING


class TestDeterminism:
    def test_equal_length_sequences_have_a_pinned_order(self) -> None:
        """Both four captures long, so length cannot separate them. The order
        is `(path, captured)`, which a TypeScript engine reproduces from the
        same rule (AD-14)."""
        assert notation(RING) == ["c3xa5xc7xe5xc3", "c3xe5xc7xa5xc3"]

    def test_the_same_position_produces_the_same_sequences_every_time(self) -> None:
        assert generator.legal_moves(RING) == generator.legal_moves(RING)

    def test_the_order_does_not_depend_on_how_the_board_was_built(self) -> None:
        reordered = position(
            {
                "d6": DARK_MAN,
                "c3": LIGHT_MAN,
                "d4": DARK_MAN,
                "b6": DARK_MAN,
                "b4": DARK_MAN,
            }
        )

        assert generator.legal_moves(reordered) == generator.legal_moves(RING)


class TestNothingIsMutated:
    def test_generation_leaves_the_position_alone(self) -> None:
        """The walk lifts the moving piece off a *copy* — the position it was
        given has to still hold every piece afterwards."""
        snapshot = position(
            {
                "c3": LIGHT_MAN,
                "b4": DARK_MAN,
                "d4": DARK_MAN,
                "b6": DARK_MAN,
                "d6": DARK_MAN,
            }
        )

        generator.legal_moves(RING)

        assert snapshot == RING

    def test_the_board_still_holds_every_victim_after_generation(self) -> None:
        generator.legal_moves(TWO_JUMPS)

        assert TWO_JUMPS.board.piece_count() == 3


class TestSequencesWorkWithTheRestOfTheEngine:
    """A64-014.3's validator and applier were written before sequences
    existed. Neither was changed by this task, so these hold them to the
    longest moves the engine can now produce."""

    def test_every_generated_sequence_validates(self) -> None:
        for source in (TWO_JUMPS, THREE_JUMPS, RING, position(ALTERNATIVES)):
            for sequence in generator.legal_moves(source):
                validator.validate(source, sequence)

    def test_a_sequence_applies_and_removes_every_victim(self) -> None:
        after = applier.apply(THREE_JUMPS, generator.legal_moves(THREE_JUMPS)[0])

        assert after.board.occupied_squares == {square("a5"): LIGHT_MAN}

    def test_a_revisiting_sequence_applies_back_onto_its_own_origin(self) -> None:
        """The applier removes all four victims before relocating, so the
        origin is free for the piece to land on again."""
        after = applier.apply(RING, generator.legal_moves(RING)[0])

        assert after.board.occupied_squares == {square("c3"): LIGHT_MAN}

    def test_a_crowning_sequence_applies_as_a_king(self) -> None:
        crowning = position({"b6": LIGHT_MAN, "g5": LIGHT_MAN, "c7": DARK_MAN, "e7": DARK_MAN})

        after = applier.apply(crowning, generator.legal_moves(crowning)[0])

        assert after.board.piece_at(square("f6")) == LIGHT_KING

    def test_applying_a_sequence_hands_over_the_turn(self) -> None:
        after = applier.apply(TWO_JUMPS, generator.legal_moves(TWO_JUMPS)[0])

        assert after.side_to_move is PlayerSide.DARK
