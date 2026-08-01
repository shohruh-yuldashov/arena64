"""`engine` — the rules kernel (architecture.md §11).

"A **pure rules kernel**: no I/O, no clock, no randomness, no logging, no
framework, no database, no configuration. It exposes deterministic
functions over immutable value objects." That is AD-13, and it is enforced
rather than asserted: `apps/api/.importlinter` fails the build on an import
from this package to anything but the shared kernel in `app.core`.

## What is implemented

**A64-014.1 — the board.** `BoardCoordinate`, `PlayerSide`, `PieceRank`,
`Piece`, `BoardVariant`, `BoardGeometry`, `Board`, `initial_board`.

**A64-014.2 — men's moves.** `Position`, `Move`, `MoveGenerator`,
`Direction`, `CaptureObligation`, and the rule axes on `BoardGeometry`.
Quiet moves and single jumps for men, mandatory-capture priority,
promotion detected on arrival, one deterministic move order, and the first
versioned corpus in `specs/game-engine/corpus/v1/`.

**A64-014.3 — validation and application.** `MoveValidator` (which holds no
rules of its own — it asks the generator), `MoveApplier` and `IllegalMove`.

**A64-014.8 — serialization.** `engine.serialization` projects every
kernel value to and from JSON-shaped primitives, in the same encoding the
conformance corpus is written in. Reachable from `replay` and `fairplay`,
which may import `engine` and not `game` (R-2).

**A64-014.7 — draw configuration.** `DrawRules` and `MaterialPlyLimit`, a
variant axis like every other. The *evaluation* is not here: a draw is a
property of a game's history (MT-12) and the kernel has none, so
`game.DrawRuleSet` reads these. Only the repetition threshold is
configured — the rest are undecided product rules, recorded in
`draw_rules.py`.

**A64-014.6 — terminal states and versioning.** `EngineVersion`,
`CURRENT_ENGINE_VERSION`, `TerminalState`, `TerminalReason` and
`TerminalStateEvaluator`. The evaluator is pure over a single position and
can only report a *loss*; every draw in draughts is a property of the
game's history, which belongs to `game.Match` (A64-014.7).

**A64-014.5 — kings.** Flying and short king quiet moves and captures,
kings starting a ply, `BoardVariant.ENGLISH_8X8`, and the corpus's second
version. `UnsupportedPieceMovement` is **removed**: it was A64-014.3's
temporary refusal for a position containing a king of the side to move, and
there is no such position any more. A consumer that caught it can delete
the handler — nothing raises it, and nothing replaced it.

Absent, by the tasks' constraints: **king movement** — a king of the side
to move is now *refused* rather than ignored, so that an empty move set
means what it says — **capture sequences longer than one jump**,
maximum-capture selection, move undo, terminal-state and draw detection,
`PositionHash` as an incremental hash, PDN notation, and serialization.
Nothing here anticipates their shape beyond keeping the rules
variant-parameterised and the move a path.

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
from app.modules.engine.coordinate import (
    DIAGONAL_DIRECTIONS,
    MAX_BOARD_DIMENSION,
    BoardCoordinate,
    Direction,
)
from app.modules.engine.draw_rules import (
    THREEFOLD_REPETITION_ONLY,
    DrawRules,
    MaterialPlyLimit,
)
from app.modules.engine.exceptions import (
    DestinationOccupied,
    GameDomainError,
    IllegalMove,
    InvalidBoardState,
    InvalidCoordinate,
    InvalidMove,
    PieceNotFound,
)
from app.modules.engine.initial_position import initial_board
from app.modules.engine.move import Move
from app.modules.engine.move_application import MoveApplier
from app.modules.engine.move_generation import MoveGenerator
from app.modules.engine.move_validation import MoveValidator
from app.modules.engine.piece import Piece, PieceRank, PlayerSide
from app.modules.engine.position import Position
from app.modules.engine.terminal import TerminalReason, TerminalState, TerminalStateEvaluator
from app.modules.engine.variant import (
    BoardGeometry,
    BoardVariant,
    CaptureObligation,
    MidSequencePromotion,
    geometry_of,
)
from app.modules.engine.version import CURRENT_ENGINE_VERSION, EngineVersion

__all__ = [
    "CURRENT_ENGINE_VERSION",
    "THREEFOLD_REPETITION_ONLY",
    "DIAGONAL_DIRECTIONS",
    "MAX_BOARD_DIMENSION",
    "Board",
    "BoardCoordinate",
    "BoardGeometry",
    "BoardVariant",
    "CaptureObligation",
    "DestinationOccupied",
    "Direction",
    "DrawRules",
    "EngineVersion",
    "GameDomainError",
    "IllegalMove",
    "InvalidBoardState",
    "InvalidCoordinate",
    "InvalidMove",
    "MaterialPlyLimit",
    "MidSequencePromotion",
    "Move",
    "MoveApplier",
    "MoveGenerator",
    "MoveValidator",
    "Piece",
    "PieceNotFound",
    "PieceRank",
    "PlayerSide",
    "Position",
    "TerminalReason",
    "TerminalState",
    "TerminalStateEvaluator",
    "geometry_of",
    "initial_board",
]
