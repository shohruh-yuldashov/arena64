"""`PlayerSide`, `PieceRank` and `Piece` — sides, ranks, and crowning.

A64-014.1 asks for piece promotion. `opponent()` is asserted beside it
because it is the other total function on this file's types and because a
side function that was wrong in one direction only is exactly the defect
that survives a casual read.
"""

from app.modules.engine import Piece, PieceRank, PlayerSide


class TestPlayerSide:
    def test_light_faces_dark(self) -> None:
        assert PlayerSide.LIGHT.opponent() is PlayerSide.DARK

    def test_dark_faces_light(self) -> None:
        assert PlayerSide.DARK.opponent() is PlayerSide.LIGHT

    def test_the_opponent_of_an_opponent_is_the_original_side(self) -> None:
        for side in PlayerSide:
            assert side.opponent().opponent() is side

    def test_nobody_is_their_own_opponent(self) -> None:
        for side in PlayerSide:
            assert side.opponent() is not side


class TestPromotion:
    def test_a_man_becomes_a_king(self) -> None:
        man = Piece(side=PlayerSide.LIGHT, rank=PieceRank.MAN)

        assert man.promote() == Piece(side=PlayerSide.LIGHT, rank=PieceRank.KING)

    def test_promotion_keeps_the_side(self) -> None:
        """Crowning changes what a piece may do, never whose it is."""
        man = Piece(side=PlayerSide.DARK, rank=PieceRank.MAN)

        assert man.promote().side is PlayerSide.DARK

    def test_promotion_leaves_the_original_piece_a_man(self) -> None:
        man = Piece(side=PlayerSide.LIGHT, rank=PieceRank.MAN)

        man.promote()

        assert man.rank is PieceRank.MAN

    def test_promoting_a_king_is_idempotent_rather_than_an_error(self) -> None:
        """A crowned piece re-enters its crownhead constantly in ordinary
        play; raising here would make a legal move an exception."""
        king = Piece(side=PlayerSide.DARK, rank=PieceRank.KING)

        assert king.promote() == king


class TestValueSemantics:
    def test_two_pieces_of_one_side_and_rank_are_equal(self) -> None:
        """A piece has no identity (domain-model.md §16.1) — which is what
        lets the initial position share two instances across 24 squares."""
        assert Piece(side=PlayerSide.LIGHT, rank=PieceRank.MAN) == Piece(
            side=PlayerSide.LIGHT, rank=PieceRank.MAN
        )

    def test_rank_distinguishes_two_pieces_of_one_side(self) -> None:
        assert Piece(side=PlayerSide.LIGHT, rank=PieceRank.MAN) != Piece(
            side=PlayerSide.LIGHT, rank=PieceRank.KING
        )

    def test_side_distinguishes_two_pieces_of_one_rank(self) -> None:
        assert Piece(side=PlayerSide.LIGHT, rank=PieceRank.MAN) != Piece(
            side=PlayerSide.DARK, rank=PieceRank.MAN
        )
