"""`Position` — a board and whose turn it is.

Framework-free and dependency-free (AD-13).

## Why move generation takes this and not a `Board`

Because "what may be played here" has no answer without a side to move,
and a generator that took a board plus a side as two arguments would let a
caller pass a side that belongs to a different position. One value, one
answer.

domain-model.md §10.1 names it and states its content exactly — "placement
of men and kings by side, side to move" — and gives the reason it must be a
value object rather than an entity: "**repetition detection requires value
equality.** An entity would compare by identity and the three-fold rule
would never fire."

## What it deliberately does not contain

No game status, no clocks, no player identity, no move number, no
persistence concern. Two positions reached by different games in different
years are the same position, and that is the whole point — anything
identifying *this* game would make every position unique and the repetition
rule dead. `Match` owns all of it (domain-model.md §10.3).

The draw-relevant state that a full repetition check needs beyond this —
whether either side may still castle has no analogue here, but the
fifteen-move king-only counter does — belongs with draw detection, which is
a later task and may extend this type additively.

## `fingerprint`, and hashing

`fingerprint` is the deterministic primitive reduction: one string, stable
across processes, machines and languages. It is what makes this type usable
as a repetition key and what a TypeScript engine can reproduce byte for
byte under the shared corpus (AD-14).

`__hash__` is defined over it because `Board` is deliberately unhashable —
A64-014.1 argued that hashing a board without the side to move "would
compare two positions that the rules consider different", and this is the
type that fixes that. So the board stays unhashable and the position is the
key, which is the arrangement that argument asked for.

Recomputed per call rather than cached: a fingerprint is O(pieces) and
positions are cheap and numerous. `PositionHash` in domain-model.md §10.1
is where an incremental Zobrist hash belongs, and it should arrive with a
measurement (CLAUDE.md §10.1) rather than as a guess made here.
"""

from dataclasses import dataclass

from app.modules.engine.board import Board
from app.modules.engine.piece import PlayerSide


@dataclass(frozen=True, slots=True)
class Position:
    """A complete, timeless description of a game in progress."""

    board: Board
    side_to_move: PlayerSide

    @property
    def fingerprint(self) -> str:
        """A deterministic primitive rendering of this position.

        `"<variant>/<side to move>/<square>=<side>:<rank>,..."`, squares in
        ascending row-major order — for example
        `"russian_8x8/light/a1=light:man,c1=light:man"`.

        Ordered explicitly rather than taken from the board's mapping,
        because dictionary order is an artefact of how a position was built
        and two identical positions built by different move orders must
        produce identical text.

        Not a wire format and not a storage format. It is a comparison key
        and a corpus notation; a client protocol and a persisted position
        are separate decisions with separate compatibility obligations.
        """
        placement = ",".join(
            f"{square}={piece.side.value}:{piece.rank.value}"
            for square, piece in sorted(
                self.board.occupied_squares.items(), key=lambda entry: entry[0]
            )
        )
        return f"{self.board.variant.value}/{self.side_to_move.value}/{placement}"

    def __hash__(self) -> int:
        return hash(self.fingerprint)

    def __str__(self) -> str:
        return self.fingerprint


__all__ = ["Position"]
