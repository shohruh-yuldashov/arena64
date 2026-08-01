"""King movement — A64-014.5.

Men are tested in `test_move_generation.py` and capture sequences in
`test_capture_sequences.py`; this file is what only kings can do. The three
things that actually distinguish them — reach, direction, and the fact that
a slide has several stopping points — plus the two questions A64-014.5 had
to answer that nobody could ask before: what a king that *starts* the ply
promotes to, and what a crowned man does next under each variant's rule.

There is no `TestKingBoundary` any more. `UnsupportedPieceMovement` is
gone; a test asserting its absence lives in `TestTheTemporaryBoundaryIsGone`
below, because "we removed it" is easier to break than it looks.
"""

import pytest

from app.modules.engine import (
    Board,
    BoardCoordinate,
    BoardVariant,
    Move,
    MoveApplier,
    MoveGenerator,
    MoveValidator,
    Piece,
    PieceRank,
    PlayerSide,
    Position,
    geometry_of,
)

RUSSIAN = BoardVariant.RUSSIAN_8X8
INTERNATIONAL = BoardVariant.INTERNATIONAL_10X10
ENGLISH = BoardVariant.ENGLISH_8X8

LIGHT_MAN = Piece(side=PlayerSide.LIGHT, rank=PieceRank.MAN)
DARK_MAN = Piece(side=PlayerSide.DARK, rank=PieceRank.MAN)
LIGHT_KING = Piece(side=PlayerSide.LIGHT, rank=PieceRank.KING)
DARK_KING = Piece(side=PlayerSide.DARK, rank=PieceRank.KING)

generator = MoveGenerator()
validator = MoveValidator(generator)
applier = MoveApplier(validator)


def square(name: str) -> BoardCoordinate:
    return BoardCoordinate.parse(name)


def position(
    placement: dict[str, Piece],
    side: PlayerSide = PlayerSide.LIGHT,
    variant: BoardVariant = RUSSIAN,
) -> Position:
    return Position(
        board=Board(variant, {square(name): piece for name, piece in placement.items()}),
        side_to_move=side,
    )


def notation(source: Position) -> list[str]:
    return [str(move) for move in generator.legal_moves(source)]


class TestQuietSlides:
    def test_a_flying_king_reaches_every_empty_square_on_every_diagonal(self) -> None:
        """Eleven from c3 on an empty board — each stopping point is a move
        of its own, because where a king stops decides its next ply."""
        assert notation(position({"c3": LIGHT_KING})) == [
            "c3-a1",
            "c3-e1",
            "c3-b2",
            "c3-d2",
            "c3-b4",
            "c3-d4",
            "c3-a5",
            "c3-e5",
            "c3-f6",
            "c3-g7",
            "c3-h8",
        ]

    def test_an_opponent_stops_the_slide(self) -> None:
        """g7 cannot be jumped — h8 behind it is occupied — so the diagonal
        simply ends at f6."""
        blocked = position({"c3": LIGHT_KING, "g7": DARK_MAN, "h8": DARK_MAN})

        assert [move for move in notation(blocked) if move > "c3-e5"] == ["c3-f6"]

    def test_a_friendly_piece_stops_the_slide_the_same_way(self) -> None:
        """A quiet move passes over nothing, so the first occupant ends the
        scan whoever owns it."""
        blocked = position({"c3": LIGHT_KING, "e5": LIGHT_MAN})

        assert "c3-d4" in notation(blocked)
        assert "c3-e5" not in notation(blocked)
        assert "c3-f6" not in notation(blocked)

    def test_a_king_slides_backward_as_freely_as_forward(self) -> None:
        moves = notation(position({"c3": LIGHT_KING}))

        assert {"c3-b2", "c3-a1", "c3-d2", "c3-e1"} <= set(moves)

    def test_a_short_king_reaches_one_square(self) -> None:
        """English draughts. The same scan with a reach of one — no separate
        code path, which is why `kings_fly` is read as a distance."""
        assert notation(position({"c3": LIGHT_KING}, variant=ENGLISH)) == [
            "c3-b2",
            "c3-d2",
            "c3-b4",
            "c3-d4",
        ]

    def test_a_king_sliding_across_its_crownhead_is_not_promoted(self) -> None:
        """It is already a king. Before kings could move, "the mover is a
        king" was sufficient evidence that it had been crowned this ply."""
        crossing = generator.legal_moves(position({"g5": LIGHT_KING}))

        assert all(move.promotes_to is None for move in crossing)


class TestKingCaptures:
    def test_a_king_may_begin_a_capture(self) -> None:
        taking = position({"c3": LIGHT_KING, "d4": DARK_MAN})

        assert notation(taking) == ["c3xe5", "c3xf6", "c3xg7", "c3xh8"]

    def test_a_flying_capture_offers_every_square_beyond_the_victim(self) -> None:
        """Two landings, two moves — not two spellings of one."""
        taking = position({"c3": LIGHT_KING, "f6": DARK_MAN})

        assert notation(taking) == ["c3xg7", "c3xh8"]

    def test_a_king_captures_from_a_distance(self) -> None:
        taking = generator.legal_moves(position({"c3": LIGHT_KING, "f6": DARK_MAN}))

        assert taking[0].captured == (square("f6"),)

    def test_a_king_may_not_jump_two_pieces_in_one_step(self) -> None:
        """f6 and g7 stand together; nothing on that diagonal is takeable,
        and the king's only moves come from elsewhere."""
        walled = position({"c3": LIGHT_KING, "f6": DARK_MAN, "g7": DARK_MAN})

        assert not any(move.startswith("c3x") and "g7" in move for move in notation(walled))

    def test_a_king_may_not_jump_a_friendly_piece(self) -> None:
        blocked = position({"c3": LIGHT_KING, "f6": LIGHT_MAN})

        assert not any("x" in move for move in notation(blocked))

    def test_a_landing_square_must_be_empty(self) -> None:
        """The victim is takeable in principle and there is nowhere to come
        down, so the diagonal yields nothing."""
        blocked = position({"c3": LIGHT_KING, "f6": DARK_MAN, "g7": DARK_KING, "h8": DARK_MAN})

        assert not any("x" in move for move in notation(blocked))

    def test_a_king_capture_may_change_direction(self) -> None:
        turning = position({"a1": LIGHT_KING, "e5": LIGHT_MAN, "c3": DARK_MAN, "c5": DARK_MAN})

        assert notation(turning) == ["a1xd4xb6", "a1xd4xa7"]

    def test_a_king_cannot_take_the_same_piece_twice(self) -> None:
        """Both victims sit behind the king on the diagonal it arrived
        along, still standing and already taken. Without the taken-once
        rule the walk would not terminate."""
        looping = position({"a1": LIGHT_KING, "c3": DARK_MAN, "e5": DARK_MAN})

        assert notation(looping) == ["a1xd4xf6", "a1xd4xg7", "a1xd4xh8"]

    def test_every_king_sequence_is_complete(self) -> None:
        looping = generator.legal_moves(
            position({"a1": LIGHT_KING, "c3": DARK_MAN, "e5": DARK_MAN})
        )

        assert all(len(move.captured) == 2 for move in looping)

    def test_a_king_capture_suppresses_every_quiet_move(self) -> None:
        crowded = position({"a1": LIGHT_MAN, "c3": LIGHT_KING, "f6": DARK_MAN})

        assert notation(crowded) == ["c3xg7", "c3xh8"]

    def test_maximum_capture_filters_king_sequences(self) -> None:
        """International rules. Stopping short on d4 leads to a second
        victim; running past leads to one. Only the pair survives."""
        choice = position({"a1": LIGHT_KING, "c3": DARK_MAN, "c5": DARK_MAN}, variant=INTERNATIONAL)

        assert notation(choice) == ["a1xd4xb6", "a1xd4xa7"]

    def test_the_same_position_keeps_every_sequence_where_the_maximum_is_not_obliged(
        self,
    ) -> None:
        choice = position({"a1": LIGHT_KING, "c3": DARK_MAN, "c5": DARK_MAN})

        assert notation(choice) == [
            "a1xd4xb6",
            "a1xd4xa7",
            "a1xe5",
            "a1xf6",
            "a1xg7",
            "a1xh8",
        ]

    def test_a_king_that_started_the_ply_is_never_promoted(self) -> None:
        """`e5xh8` ends on LIGHT's crownhead and reports nothing. This is
        the case that would have every king move claim a promotion if
        `_sequence_promotion` still read the mover's *current* rank."""
        ending = generator.legal_moves(position({"e5": LIGHT_KING, "f6": DARK_MAN}))

        assert [(str(move), move.promotes_to) for move in ending] == [
            ("e5xg7", None),
            ("e5xh8", None),
        ]


class TestPromotionContinuation:
    def test_a_russian_crowned_man_carries_on_as_a_flying_king(self) -> None:
        """h4 and g5 are both three squares from d8 — neither is reachable
        by a man, so this fails outright if the crown waits for the ply to
        end."""
        crowning = position({"b6": LIGHT_MAN, "c7": DARK_MAN, "f6": DARK_MAN})

        assert notation(crowning) == ["b6xd8xh4", "b6xd8xg5"]

    def test_the_russian_sequence_reports_the_crown(self) -> None:
        crowning = position({"b6": LIGHT_MAN, "c7": DARK_MAN, "f6": DARK_MAN})

        assert all(move.promotes_to is PieceRank.KING for move in generator.legal_moves(crowning))

    def test_an_english_crowned_man_stops_on_the_crownhead(self) -> None:
        """e7 is there to be taken with f6 empty behind it, and the ply ends
        anyway. The identical position under Russian rules continues."""
        crowning = position({"b6": LIGHT_MAN, "c7": DARK_MAN, "e7": DARK_MAN}, variant=ENGLISH)

        assert notation(crowning) == ["b6xd8"]

    def test_the_english_sequence_still_reports_the_crown(self) -> None:
        """The ply ends *because* the piece was crowned, so the promotion is
        the whole point of stopping."""
        crowning = position({"b6": LIGHT_MAN, "c7": DARK_MAN, "e7": DARK_MAN}, variant=ENGLISH)

        assert generator.legal_moves(crowning)[0].promotes_to is PieceRank.KING

    def test_the_same_position_continues_under_russian_rules(self) -> None:
        """The contrast, asserted rather than described: one board, two
        variants, and nothing in the walker knows either name. The crowned
        king takes e7 and may come down on any of three squares — which an
        English king, reaching one square, could not."""
        crowning = position({"b6": LIGHT_MAN, "c7": DARK_MAN, "e7": DARK_MAN})

        assert notation(crowning) == ["b6xd8xh4", "b6xd8xg5", "b6xd8xf6"]

    def test_an_international_man_still_passes_through_uncrowned(self) -> None:
        """A64-014.4's rule, re-asserted here because A64-014.5 touched the
        code that decides it."""
        passing = position({"d8": LIGHT_MAN, "e9": DARK_MAN, "g9": DARK_MAN}, variant=INTERNATIONAL)

        assert generator.legal_moves(passing)[0].promotes_to is None


class TestValidationAndApplication:
    """`MoveValidator` and `MoveApplier` were not changed by this task.
    These hold them to moves neither had ever seen."""

    def test_every_generated_king_move_validates(self) -> None:
        for source in (
            position({"c3": LIGHT_KING}),
            position({"c3": LIGHT_KING, "f6": DARK_MAN}),
            position({"a1": LIGHT_KING, "c3": DARK_MAN, "e5": DARK_MAN}),
            position({"c3": LIGHT_KING}, variant=ENGLISH),
        ):
            for move in generator.legal_moves(source):
                validator.validate(source, move)

    def test_a_king_slide_applies(self) -> None:
        sliding = position({"c3": LIGHT_KING})

        after = applier.apply(sliding, Move(path=(square("c3"), square("h8"))))

        assert after.board.occupied_squares == {square("h8"): LIGHT_KING}

    def test_a_king_capture_applies_and_removes_the_victim(self) -> None:
        taking = position({"c3": LIGHT_KING, "f6": DARK_MAN})

        after = applier.apply(taking, generator.legal_moves(taking)[0])

        assert after.board.occupied_squares == {square("g7"): LIGHT_KING}

    def test_a_crowned_sequence_applies_as_a_king(self) -> None:
        crowning = position({"b6": LIGHT_MAN, "c7": DARK_MAN, "f6": DARK_MAN})

        after = applier.apply(crowning, generator.legal_moves(crowning)[0])

        assert after.board.occupied_squares == {square("h4"): LIGHT_KING}

    def test_applying_a_king_move_hands_over_the_turn(self) -> None:
        after = applier.apply(position({"c3": LIGHT_KING}), Move(path=(square("c3"), square("d4"))))

        assert after.side_to_move is PlayerSide.DARK

    def test_the_original_position_is_unchanged(self) -> None:
        before = position({"a1": LIGHT_KING, "c3": DARK_MAN, "e5": DARK_MAN})
        snapshot = position({"a1": LIGHT_KING, "c3": DARK_MAN, "e5": DARK_MAN})

        for move in generator.legal_moves(before):
            applier.apply(before, move)

        assert snapshot == before


class TestDeterminism:
    def test_a_king_move_list_is_stable(self) -> None:
        source = position({"a1": LIGHT_KING, "c3": DARK_MAN, "e5": DARK_MAN})

        assert generator.legal_moves(source) == generator.legal_moves(source)

    def test_landing_squares_are_ordered_nearest_first_along_the_diagonal(self) -> None:
        """Ordering is by destination, and on an ascending diagonal that is
        also nearest-first — stated here so a reordering shows up as a
        failure rather than as a corpus rewrite."""
        assert notation(position({"c3": LIGHT_KING, "f6": DARK_MAN})) == ["c3xg7", "c3xh8"]

    def test_the_order_does_not_depend_on_how_the_board_was_built(self) -> None:
        one = position({"c1": LIGHT_KING, "e3": LIGHT_MAN})
        other = position({"e3": LIGHT_MAN, "c1": LIGHT_KING})

        assert generator.legal_moves(one) == generator.legal_moves(other)


class TestEnglishVariant:
    """Added by A64-014.5, configuration only. It is the rule set that gives
    `kings_fly`, `men_may_capture_backward` and `mid_sequence_promotion`
    each a second value — without it all three are settings nothing can tell
    apart from constants."""

    def test_its_kings_reach_one_square(self) -> None:
        assert geometry_of(ENGLISH).king_reach == 1

    def test_its_men_do_not_capture_backward(self) -> None:
        """The same position gives a Russian man a backward jump and an
        English one nothing but a step."""
        behind = {"c5": LIGHT_MAN, "b4": DARK_MAN}

        assert notation(position(behind, variant=ENGLISH)) == ["c5-b6", "c5-d6"]
        assert notation(position(behind)) == ["c5xa3"]

    def test_it_is_played_on_an_ordinary_8x8_board(self) -> None:
        geometry = geometry_of(ENGLISH)

        assert (geometry.rows, geometry.columns, geometry.men_per_side) == (8, 8, 12)


class TestTheTemporaryBoundaryIsGone:
    def test_the_unsupported_piece_exception_no_longer_exists(self) -> None:
        """A64-014.3 introduced `UnsupportedPieceMovement` explicitly as
        temporary. Deleting it is part of this task, and an exception left
        behind "just in case" is how temporary becomes permanent."""
        import app.modules.engine as engine

        assert not hasattr(engine, "UnsupportedPieceMovement")
        assert "UnsupportedPieceMovement" not in engine.__all__

    def test_a_position_with_a_king_is_answered_rather_than_refused(self) -> None:
        assert generator.legal_moves(position({"c3": LIGHT_KING}))

    def test_a_king_only_position_with_no_moves_answers_empty(self) -> None:
        """Boxed in by its own pieces. Empty now means one thing — the side
        to move has nothing to play — which is what A64-014.6 needs to read
        it as a loss."""
        boxed = position({"a1": LIGHT_KING, "b2": DARK_MAN, "c3": DARK_MAN}, variant=ENGLISH)

        assert generator.legal_moves(boxed) == ()


class TestKingHeavyPerformance:
    """One contrived position, measured — A64-014.5 §11.

    Three flying kings against twelve men on a 10x10 board, every man on a
    square the kings can reach and every landing square open. Observed on
    the development machine: **32 complete sequences, each taking all
    twelve men, in 5.7–7.1 ms**.

    What that says is that the search is bounded by the material, not by
    the board: a sequence cannot be longer than the number of opponent
    pieces, because every step consumes one and none is taken twice. The
    assertions below are that structural bound plus a loose ceiling — a
    blow-up detector, not a performance target. No optimisation was made,
    because nothing here is evidence that one is needed (CLAUDE.md §10.1),
    and a real budget belongs with a real workload rather than a position
    chosen to be awkward.
    """

    HEAVY = position(
        {
            "a1": LIGHT_KING,
            "e1": LIGHT_KING,
            "a9": LIGHT_KING,
            "c3": DARK_MAN,
            "c5": DARK_MAN,
            "c7": DARK_MAN,
            "e3": DARK_MAN,
            "e5": DARK_MAN,
            "e7": DARK_MAN,
            "g3": DARK_MAN,
            "g5": DARK_MAN,
            "g7": DARK_MAN,
            "i3": DARK_MAN,
            "i5": DARK_MAN,
            "i7": DARK_MAN,
        },
        variant=INTERNATIONAL,
    )

    def test_the_search_terminates_and_finds_every_sequence(self) -> None:
        assert len(generator.legal_moves(self.HEAVY)) == 32

    def test_no_sequence_takes_more_pieces_than_the_opponent_has(self) -> None:
        """The structural bound. Recursion depth is capture count, capture
        count is bounded by material, so the walk cannot run away however
        open the board is."""
        opponents = self.HEAVY.board.piece_count_for(PlayerSide.DARK)

        assert all(len(move.captured) <= opponents for move in generator.legal_moves(self.HEAVY))

    def test_the_maximum_capture_filter_leaves_only_full_sweeps(self) -> None:
        """Every survivor takes all twelve — which is also why there are 32
        of them rather than one."""
        assert {len(move.captured) for move in generator.legal_moves(self.HEAVY)} == {12}

    def test_generation_stays_well_under_a_second(self) -> None:
        """A ceiling two orders of magnitude above the observed 6 ms, so it
        fails on an algorithmic regression and not on a slow machine — the
        only kind of timing assertion worth having in a suite that must not
        be flaky (CLAUDE.md §6.5)."""
        from time import perf_counter

        started = perf_counter()
        generator.legal_moves(self.HEAVY)
        elapsed = perf_counter() - started

        assert elapsed < 1.0, f"king-heavy generation took {elapsed:.3f}s"

    @pytest.mark.parametrize("repetition", range(3))
    def test_the_answer_does_not_drift_between_runs(self, repetition: int) -> None:
        assert generator.legal_moves(self.HEAVY) == generator.legal_moves(self.HEAVY)
