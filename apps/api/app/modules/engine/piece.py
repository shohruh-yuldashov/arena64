"""`PlayerSide`, `PieceRank` and `Piece` — what stands on a square.

Framework-free and dependency-free (AD-13).

## Why `Piece` exists at all, given domain-model.md §16.1

That table lists `Piece` under "rejected", with the verdict "not modelled
at all". Read in full, the rejection is of a piece **entity**: "a piece has
no identity in draughts... an entity per piece at ~5,000 moves per second
would also be the largest write volume on the platform, in service of data
nobody queries."

Every one of those objections is an objection to identity, to a row, and to
a lifecycle. This is a frozen value object with none of the three: two
pieces of the same side and rank are the same piece, nothing persists one,
and crowning produces a different value rather than mutating a subject.
The table's own phrase for what a piece is — "an encoding detail of
`Position`" — is what this type *is*, named instead of left implicit as a
character in a string or a pair of parallel bitboards.

The alternative, encoding side and rank as raw integers inside the board,
is the version that costs something real: nothing type-checks a light king
against a dark man, and every rule that reads a piece has to remember the
encoding. domain-model.md §16.1's note has been amended to record this
reading rather than left to contradict the code (CLAUDE.md §3.11).
"""

from dataclasses import dataclass
from enum import StrEnum


class PlayerSide(StrEnum):
    """Which player a piece belongs to.

    "Light" and "dark", not "white" and "black". The pieces on a draughts
    board are not white and black in any set anyone owns — they are cream
    and red, or buff and green — and the two rules that actually depend on
    side are about *direction of travel* and *who moves first*, neither of
    which is a colour. Every board theme the client ships renames these;
    the domain should not be tied to one of them.
    """

    LIGHT = "light"
    """Moves first (Russian draughts), and toward increasing rows."""

    DARK = "dark"

    def opponent(self) -> "PlayerSide":
        """The other side. Total, and its own inverse."""
        return PlayerSide.DARK if self is PlayerSide.LIGHT else PlayerSide.LIGHT


class PieceRank(StrEnum):
    """What a piece may do — the only distinction draughts draws between
    pieces of one side (domain-model.md §2.1)."""

    MAN = "man"
    """Unpromoted. Moves forward only."""

    KING = "king"
    """Crowned on reaching the far rank. Mobility is variant-defined —
    flying in Russian and international rules, short in English — and lives
    with movement, which this task does not implement."""


@dataclass(frozen=True, slots=True)
class Piece:
    """One piece: a side and a rank, and nothing else.

    No square. A piece does not know where it stands any more than it knows
    when it was placed — the board owns placement, and a piece that carried
    its own coordinate would make every move two writes that could disagree.
    """

    side: PlayerSide
    rank: PieceRank

    def promote(self) -> "Piece":
        """This piece as a king, leaving this one untouched.

        **Idempotent, not an error on a king.** A crowned piece re-entering
        the crownhead is ordinary play — kings cross their own back rank
        constantly — so promotion is expressed as "be a king", which is
        true afterwards regardless of what was true before. Raising here
        would turn a legal move into an exception at the one moment the
        rules say nothing happened.

        Whether promotion *applies* is geometry, and belongs to move
        application: this answers only what the piece becomes.
        """
        return Piece(side=self.side, rank=PieceRank.KING)


__all__ = ["Piece", "PieceRank", "PlayerSide"]
