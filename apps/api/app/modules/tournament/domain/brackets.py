"""`BracketNode` — the shape of a single-elimination tree. §6.

The **model**, not the algorithm. A64-019.1 defines what a node is;
A64-019.3 and A64-019.4 fill and walk the tree.

## Coordinates, not links

A node is identified by `(depth, position)` and finds its neighbours by
arithmetic:

    depth 0                    [0]              the final
    depth 1              [0]         [1]        the semi-finals
    depth 2          [0]  [1]     [2]  [3]      the quarter-finals

    parent of (d, p)   = (d - 1, p // 2)
    children of (d, p) = (d + 1, 2p), (d + 1, 2p + 1)

Storing parent and child ids instead would be three columns that can
disagree with each other and with the depth. The arithmetic cannot: a
balanced binary tree's shape *is* its coordinates, and `2**depth` nodes per
depth is checkable at construction.

**Depth counts from the final**, not from the first round. That is the
direction advancement runs, and it means the final is `(0, 0)` in every
tournament regardless of size — so "who won" is one lookup rather than a
computation from the field.

## Slots are optional because a bracket exists before it is filled

Both slots are `None` on a freshly built tree, one is filled by a bye or by
an advancing winner, and a node is playable only when both are present.
Modelling them as required would mean the tree could not be built until the
whole field was known to fit it, which is backwards: the tree's shape comes
from the capacity and the players arrive into it.
"""

from dataclasses import dataclass, replace
from uuid import UUID

from app.modules.tournament.domain.exceptions import InvalidBracketPosition


@dataclass(frozen=True, slots=True)
class BracketNode:
    """One match slot in the tree.

    Frozen, like every other value here: filling a slot returns a new node,
    so a caller holding the old one still sees what it held.
    """

    tournament_id: UUID
    depth: int
    """Distance from the final. `0` is the final itself."""

    position: int
    """Index within the depth, left to right. `0 <= position < 2**depth`."""

    light_player_id: UUID | None = None
    dark_player_id: UUID | None = None
    winner_id: UUID | None = None

    match_id: UUID | None = None
    """The `game` match this node was played as, once one exists.

    An opaque id — this module never dereferences it, and `game` holds the
    other half of the pair as `origin_ref` (A64-019.0, R-25). There is no
    foreign key in either direction (DB-03).
    """

    def __post_init__(self) -> None:
        if self.depth < 0:
            raise InvalidBracketPosition(f"depth cannot be negative, got {self.depth}")
        if not 0 <= self.position < 2**self.depth:
            raise InvalidBracketPosition(
                f"depth {self.depth} holds {2**self.depth} nodes, so position "
                f"{self.position} is outside it"
            )

    @property
    def is_final(self) -> bool:
        return self.depth == 0

    @property
    def is_playable(self) -> bool:
        """Whether both seats are filled and nobody has won yet."""
        return (
            self.light_player_id is not None
            and self.dark_player_id is not None
            and self.winner_id is None
        )

    @property
    def is_bye(self) -> bool:
        """Exactly one seat filled — the player advances without playing.

        Distinguished from "not yet filled" by *which* is missing being
        irrelevant: one player and no opponent is a bye whichever seat they
        sit in, and the field size decides how many exist (T-2).
        """
        return (self.light_player_id is None) != (self.dark_player_id is None)

    def parent(self) -> tuple[int, int] | None:
        """The node this one's winner advances into, or `None` for the final."""
        if self.is_final:
            return None
        return (self.depth - 1, self.position // 2)

    def children(self) -> tuple[tuple[int, int], tuple[int, int]]:
        """The two nodes that feed this one.

        Returned even at the deepest layer, where they do not exist: a
        bracket knows its own depth and stops there, and returning `None`
        would push that check into every walk.
        """
        return ((self.depth + 1, self.position * 2), (self.depth + 1, self.position * 2 + 1))

    def with_winner(self, player_id: UUID) -> "BracketNode":
        """This node decided. Raises if the winner did not play in it.

        Checked here rather than by the caller because it is the one thing
        that makes a bracket wrong in a way nothing downstream can detect:
        an advancement into the next round by somebody who was never in this
        one produces a final between players who never met.
        """
        if player_id not in (self.light_player_id, self.dark_player_id):
            raise InvalidBracketPosition("the winner of a node must be one of its two participants")
        return replace(self, winner_id=player_id)


__all__ = ["BracketNode"]
