"""`engine` — the rules kernel (architecture.md §11).

"A **pure rules kernel**: no I/O, no clock, no randomness, no logging, no
framework, no database, no configuration. It exposes deterministic
functions over immutable value objects." That is AD-13, and it is enforced
rather than asserted: `apps/api/.importlinter` fails the build on an import
from this package to anything but the shared kernel in `app.core`.

## A64-014.1 implements the board, and only the board

Present: `BoardCoordinate`, `PlayerSide`, `PieceRank`, `Piece`,
`BoardVariant`, `BoardGeometry`, `Board`, `initial_board`, and the failure
taxonomy they raise.

Absent, by the task's constraints: move generation, move validation,
captures and multi-jumps, king mobility, promotion-on-arrival, draw
detection, repetition hashing, `Position` (a board plus the side to move),
`Move`, serialization and PDN notation. Nothing here anticipates their
shape beyond keeping the board variant-parameterised.

## Why this module has no four-layer interior

Every other module under `app/modules/` has `domain/`, `application/`,
`infrastructure/`, `presentation/` and `public/` (architecture.md §8), and
uniformity there is worth more than local optimisation. This one is the
exception the same document draws: the module map gives `engine` **no
aggregate roots** — "pure functions and value objects" — and AD-13 forbids
it the I/O that the other four layers exist to separate from rules.

So the layers would be one real package and four permanently empty ones,
and an empty `infrastructure/` in the module whose entire guarantee is that
it has no infrastructure reads as an oversight rather than a rule. The
package is flat, and every name below is published: unlike a bounded
context, the kernel has no internals to hide — the whole of it is the
contract that `game`, `replay` and `fairplay` are held to, and that the
TypeScript client engine mirrors under one shared test corpus (AD-14).

## Who may import it

`game`, `replay` and `fairplay` only, and only `game` may use it to mutate
state (R-2). None of the three exists yet, so today's contract in
`.importlinter` names every module that does exist and forbids all of them.
"""

from app.modules.engine.board import Board
from app.modules.engine.coordinate import MAX_BOARD_DIMENSION, BoardCoordinate
from app.modules.engine.exceptions import (
    DestinationOccupied,
    GameDomainError,
    InvalidBoardState,
    InvalidCoordinate,
    PieceNotFound,
)
from app.modules.engine.initial_position import initial_board
from app.modules.engine.piece import Piece, PieceRank, PlayerSide
from app.modules.engine.variant import BoardGeometry, BoardVariant, geometry_of

__all__ = [
    "MAX_BOARD_DIMENSION",
    "Board",
    "BoardCoordinate",
    "BoardGeometry",
    "BoardVariant",
    "DestinationOccupied",
    "GameDomainError",
    "InvalidBoardState",
    "InvalidCoordinate",
    "Piece",
    "PieceNotFound",
    "PieceRank",
    "PlayerSide",
    "geometry_of",
    "initial_board",
]
