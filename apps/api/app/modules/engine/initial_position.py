"""The board every game starts from.

Framework-free and dependency-free (AD-13), and derived rather than
written out: a literal table of 24 squares would be 24 chances to typo a
file letter, and it would only ever describe one variant.

## Why it is a function and not a constant

`initial_board(RUSSIAN_8X8)` builds a fresh `Board` on each call rather
than handing out a shared one. The boards are immutable, so a module-level
constant would be safe — but it would be a mutable-by-accident invitation
the day someone adds an internal cache to `Board`, and building 24
dictionary entries is not a cost worth trading a guarantee for.
"""

from app.modules.engine.board import Board
from app.modules.engine.piece import Piece, PieceRank, PlayerSide
from app.modules.engine.variant import BoardVariant, geometry_of

_LIGHT_MAN = Piece(side=PlayerSide.LIGHT, rank=PieceRank.MAN)
_DARK_MAN = Piece(side=PlayerSide.DARK, rank=PieceRank.MAN)
"""Two values shared by every square that holds one.

Legitimate precisely because a `Piece` has no identity (domain-model.md
§16.1): every light man on the board *is* the same piece as far as the
domain is concerned, so there is nothing to be gained by allocating 12 of
them.
"""


def initial_board(variant: BoardVariant) -> Board:
    """The opening position for `variant` — men only, no kings.

    Each side fills the playable squares of the ranks nearest it, as many
    ranks as the variant's geometry says: three on Russian 8x8 giving 12
    men a side, four on international 10x10 giving 20. LIGHT occupies the
    low rows, because `BoardCoordinate` fixes row 0 as LIGHT's back rank.

    No kings, in either variant. A game that began with one would not be
    draughts, and there is no rule anywhere in the platform that produces a
    king other than a man reaching the far rank.
    """
    geometry = geometry_of(variant)
    dark_starts_at = geometry.rows - geometry.setup_rows_per_side

    squares = {}
    for coordinate in geometry.playable_squares():
        if coordinate.row < geometry.setup_rows_per_side:
            squares[coordinate] = _LIGHT_MAN
        elif coordinate.row >= dark_starts_at:
            squares[coordinate] = _DARK_MAN

    return Board(variant, squares)


__all__ = ["initial_board"]
