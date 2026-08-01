"""`MoveGenerator` — quiet moves, single jumps, and the capture obligation.

A64-014.2 asks for men only. Nothing here tests a king or a capture
sequence longer than one jump; those are A64-014.5 and A64-014.4, and a
test written now would encode a guess about rules that have not been
implemented.

Positions are written out square by square rather than started from
`initial_board`, because a five-piece position states what a test is about
and a twenty-four-piece one does not. The opening position is exercised
once, through the corpus.
"""

from app.modules.engine import (
    Board,
    BoardCoordinate,
    BoardVariant,
    Move,
    MoveGenerator,
    Piece,
    PieceRank,
    PlayerSide,
    Position,
)

RUSSIAN = BoardVariant.RUSSIAN_8X8

LIGHT_MAN = Piece(side=PlayerSide.LIGHT, rank=PieceRank.MAN)
DARK_MAN = Piece(side=PlayerSide.DARK, rank=PieceRank.MAN)

generator = MoveGenerator()


def square(notation: str) -> BoardCoordinate:
    return BoardCoordinate.parse(notation)


def position(placement: dict[str, Piece], side: PlayerSide) -> Position:
    return Position(
        board=Board(RUSSIAN, {square(name): piece for name, piece in placement.items()}),
        side_to_move=side,
    )


def notation(moves: tuple[Move, ...]) -> list[str]:
    """Moves as `a3-b4` / `c3xe5`, which is what a failure should print."""
    return [str(move) for move in moves]


class TestQuietMoves:
    def test_a_light_man_steps_forward_along_both_diagonals(self) -> None:
        moves = generator.legal_moves(position({"c3": LIGHT_MAN}, PlayerSide.LIGHT))

        assert notation(moves) == ["c3-b4", "c3-d4"]

    def test_a_dark_man_steps_the_other_way(self) -> None:
        """The same rule read through the opposite forward direction — the
        one thing that would break if the direction were hard-coded rather
        than asked of the geometry."""
        moves = generator.legal_moves(position({"c5": DARK_MAN}, PlayerSide.DARK))

        assert notation(moves) == ["c5-b4", "c5-d4"]

    def test_a_man_never_steps_backward(self) -> None:
        moves = generator.legal_moves(position({"c5": LIGHT_MAN}, PlayerSide.LIGHT))

        assert notation(moves) == ["c5-b6", "c5-d6"]

    def test_a_man_on_the_edge_has_one_step(self) -> None:
        """Walking off the board is the ordinary case for a rim piece, not
        an error."""
        moves = generator.legal_moves(position({"a3": LIGHT_MAN}, PlayerSide.LIGHT))

        assert notation(moves) == ["a3-b4"]

    def test_an_occupied_destination_is_excluded(self) -> None:
        moves = generator.legal_moves(
            position({"c3": LIGHT_MAN, "b4": LIGHT_MAN}, PlayerSide.LIGHT)
        )

        assert "c3-b4" not in notation(moves)

    def test_a_destination_occupied_by_an_opponent_is_excluded(self) -> None:
        """It is not a quiet move; whether it is a capture depends on the
        square beyond, which this position leaves occupied."""
        moves = generator.legal_moves(
            position(
                {"c3": LIGHT_MAN, "b4": DARK_MAN, "a5": DARK_MAN},
                PlayerSide.LIGHT,
            )
        )

        assert "c3-b4" not in notation(moves)

    def test_a_fully_blocked_man_has_nothing_to_play(self) -> None:
        blocked = position(
            {
                "c3": LIGHT_MAN,
                "b4": DARK_MAN,
                "d4": DARK_MAN,
                "a5": DARK_MAN,
                "e5": DARK_MAN,
            },
            PlayerSide.LIGHT,
        )

        assert generator.legal_moves(blocked) == ()


class TestSideToMove:
    def test_only_the_side_to_move_generates_moves(self) -> None:
        both = position({"c3": LIGHT_MAN, "f6": DARK_MAN}, PlayerSide.LIGHT)

        assert notation(generator.legal_moves(both)) == ["c3-b4", "c3-d4"]

    def test_the_other_side_generates_the_other_moves(self) -> None:
        both = position({"c3": LIGHT_MAN, "f6": DARK_MAN}, PlayerSide.DARK)

        assert notation(generator.legal_moves(both)) == ["f6-e5", "f6-g5"]

    def test_a_side_with_no_pieces_has_no_moves(self) -> None:
        assert generator.legal_moves(position({"c3": LIGHT_MAN}, PlayerSide.DARK)) == ()


class TestSingleCaptures:
    def test_a_man_jumps_the_opponent_in_front_of_it(self) -> None:
        moves = generator.legal_moves(position({"c3": LIGHT_MAN, "d4": DARK_MAN}, PlayerSide.LIGHT))

        assert notation(moves) == ["c3xe5"]

    def test_the_jumped_square_is_recorded(self) -> None:
        moves = generator.legal_moves(position({"c3": LIGHT_MAN, "d4": DARK_MAN}, PlayerSide.LIGHT))

        assert moves[0].captured == (square("d4"),)

    def test_a_man_jumps_backward_when_the_variant_allows_it(self) -> None:
        """Russian draughts lets a man take behind it although it may never
        step behind it — the axis `men_may_capture_backward` exists for."""
        moves = generator.legal_moves(position({"c5": LIGHT_MAN, "b4": DARK_MAN}, PlayerSide.LIGHT))

        assert notation(moves) == ["c5xa3"]

    def test_an_own_piece_is_not_jumped(self) -> None:
        moves = generator.legal_moves(
            position({"c3": LIGHT_MAN, "d4": LIGHT_MAN}, PlayerSide.LIGHT)
        )

        assert notation(moves) == ["c3-b4", "d4-c5", "d4-e5"]

    def test_a_jump_needs_an_empty_landing_square(self) -> None:
        moves = generator.legal_moves(
            position({"c3": LIGHT_MAN, "d4": DARK_MAN, "e5": DARK_MAN}, PlayerSide.LIGHT)
        )

        assert notation(moves) == ["c3-b4"]

    def test_a_jump_needs_a_landing_square_on_the_board(self) -> None:
        """An opponent standing against the far rank cannot be jumped —
        there is nowhere to come down."""
        moves = generator.legal_moves(position({"c7": LIGHT_MAN, "b8": DARK_MAN}, PlayerSide.LIGHT))

        assert notation(moves) == ["c7-d8"]

    def test_one_man_may_have_more_than_one_jump(self) -> None:
        """Both are offered. Choosing between them is the player's under
        Russian rules, and comparing them is A64-014.4's."""
        moves = generator.legal_moves(
            position({"c3": LIGHT_MAN, "b4": DARK_MAN, "d4": DARK_MAN}, PlayerSide.LIGHT)
        )

        assert notation(moves) == ["c3xa5", "c3xe5"]


class TestMandatoryCapture:
    def test_a_capture_suppresses_the_capturing_piece_own_quiet_moves(self) -> None:
        """`c3-b4` is available and is not offered."""
        moves = generator.legal_moves(position({"c3": LIGHT_MAN, "d4": DARK_MAN}, PlayerSide.LIGHT))

        assert notation(moves) == ["c3xe5"]
        assert "c3-b4" not in notation(moves)

    def test_a_capture_suppresses_every_other_piece_quiet_moves(self) -> None:
        """The obligation binds the player, not the piece. This is the case
        a generate-then-filter design gets wrong the day the filter is
        scoped to one square."""
        moves = generator.legal_moves(
            position({"a1": LIGHT_MAN, "c3": LIGHT_MAN, "d4": DARK_MAN}, PlayerSide.LIGHT)
        )

        assert notation(moves) == ["c3xe5"]

    def test_quiet_moves_return_when_no_capture_exists(self) -> None:
        moves = generator.legal_moves(
            position({"a1": LIGHT_MAN, "c3": LIGHT_MAN}, PlayerSide.LIGHT)
        )

        assert notation(moves) == ["a1-b2", "c3-b4", "c3-d4"]

    def test_every_returned_move_is_a_capture_when_one_exists(self) -> None:
        moves = generator.legal_moves(
            position(
                {"a1": LIGHT_MAN, "c3": LIGHT_MAN, "d4": DARK_MAN, "f6": DARK_MAN},
                PlayerSide.LIGHT,
            )
        )

        assert all(move.is_capture for move in moves)


class TestPromotion:
    def test_a_quiet_move_onto_the_crownhead_records_the_rank(self) -> None:
        moves = generator.legal_moves(position({"g7": LIGHT_MAN}, PlayerSide.LIGHT))

        assert [move.promotes_to for move in moves] == [PieceRank.KING, PieceRank.KING]

    def test_a_move_short_of_the_crownhead_records_nothing(self) -> None:
        moves = generator.legal_moves(position({"c3": LIGHT_MAN}, PlayerSide.LIGHT))

        assert [move.promotes_to for move in moves] == [None, None]

    def test_dark_is_crowned_on_the_first_rank(self) -> None:
        moves = generator.legal_moves(position({"b2": DARK_MAN}, PlayerSide.DARK))

        assert [move.promotes_to for move in moves] == [PieceRank.KING, PieceRank.KING]

    def test_a_capture_landing_on_the_crownhead_records_the_rank(self) -> None:
        """Correct because every move this task generates is a complete
        one. A64-014.4 makes it conditional, when a sequence may continue
        past the crownhead."""
        moves = generator.legal_moves(position({"b6": LIGHT_MAN, "c7": DARK_MAN}, PlayerSide.LIGHT))

        assert [(str(move), move.promotes_to) for move in moves] == [("b6xd8", PieceRank.KING)]

    def test_the_moving_piece_is_not_mutated(self) -> None:
        """Promotion is a statement about the result; applying it belongs
        to a later task."""
        crowning = position({"g7": LIGHT_MAN}, PlayerSide.LIGHT)

        generator.legal_moves(crowning)

        assert crowning.board.piece_at(square("g7")) == LIGHT_MAN


class TestDeterminism:
    def test_the_same_position_produces_the_same_moves_every_time(self) -> None:
        opening = position(
            {"a1": LIGHT_MAN, "c3": LIGHT_MAN, "e3": LIGHT_MAN, "g7": LIGHT_MAN},
            PlayerSide.LIGHT,
        )

        assert generator.legal_moves(opening) == generator.legal_moves(opening)

    def test_the_order_does_not_depend_on_how_the_board_was_built(self) -> None:
        """The defect this guards against reproduces on one machine only:
        a move list ordered by dictionary layout."""
        one = position({"a1": LIGHT_MAN, "c3": LIGHT_MAN, "e3": LIGHT_MAN}, PlayerSide.LIGHT)
        other = position({"e3": LIGHT_MAN, "a1": LIGHT_MAN, "c3": LIGHT_MAN}, PlayerSide.LIGHT)

        assert generator.legal_moves(one) == generator.legal_moves(other)

    def test_moves_are_ordered_by_origin_then_destination(self) -> None:
        moves = generator.legal_moves(
            position({"c3": LIGHT_MAN, "e3": LIGHT_MAN, "a1": LIGHT_MAN}, PlayerSide.LIGHT)
        )

        assert notation(moves) == ["a1-b2", "c3-b4", "c3-d4", "e3-d4", "e3-f4"]

    def test_the_returned_collection_is_immutable(self) -> None:
        assert isinstance(
            generator.legal_moves(position({"c3": LIGHT_MAN}, PlayerSide.LIGHT)), tuple
        )
