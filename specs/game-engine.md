# Game Engine

> **Status:** Partial — the board foundation is specified and implemented (A64-014.1);
> movement, termination and notation are not yet specified
> **Owner:** _Unassigned_
> **Related:** `templates/feature-spec.md`, `docs/01-architecture/architecture.md` §11,
> `docs/01-architecture/domain-model.md` §2.1 and §16.1

## Description

Checkers rules enforcement, move validation, board state, and game termination conditions.

Implemented in `apps/api/app/modules/engine/`, a **pure rules kernel**: no I/O, no clock, no
randomness, no logging, no framework, no database, no configuration (AD-13). Two `import-linter`
contracts enforce that and the rule that only `game`, `replay` and `fairplay` may import it (R-2).

---

## 1. Board foundation — A64-014.1

### 1.1 Types

| Type | Kind | Contract |
| --- | --- | --- |
| `BoardCoordinate` | Frozen value object | `(row, column)`, both zero-based, both within `0 .. MAX_BOARD_DIMENSION - 1`. Hashable, ordered row-major. Anything outside the bound raises `InvalidCoordinate` |
| `PlayerSide` | Enum | `LIGHT`, `DARK`. `opponent()` is total and its own inverse |
| `PieceRank` | Enum | `MAN`, `KING` |
| `Piece` | Frozen value object | `(side, rank)`. `promote()` returns a king of the same side and is idempotent |
| `BoardVariant` | Enum | `RUSSIAN_8X8`, `INTERNATIONAL_10X10` |
| `BoardGeometry` | Frozen value object | `rows`, `columns`, `setup_rows_per_side`, and the derived `men_per_side`. One instance per variant, reached through `geometry_of` |
| `Board` | Immutable, compares by value | Placement of pieces for one variant |
| `initial_board(variant)` | Function | The opening position |

### 1.2 Geometry

| Rule | Statement |
| --- | --- |
| GE-1 | Row 0 is LIGHT's back rank; rows increase away from LIGHT. Column 0 is LIGHT's left |
| GE-2 | A square is playable when `(row + column)` is even, which puts a playable square at `a1` on both variants |
| GE-3 | A geometry with an odd file count, no filled rank, or starting ranks that meet is refused with `InvalidBoardState` |
| GE-4 | No variant may exceed `MAX_BOARD_DIMENSION` (10), the bound `BoardCoordinate` validates against |

### 1.3 Board operations

Every operation returns a **new** board; none mutates its receiver, and `occupied_squares` is a
read-only view, so no caller can reach past these refusals.

| Operation | Result | Refuses |
| --- | --- | --- |
| `Board(variant, squares)` | A board holding a copy of `squares` | `InvalidBoardState` — a square off the board or a light square |
| `Board.empty(variant)` | A board of the right shape, unoccupied | — |
| `piece_at(coordinate)` | The piece, or nothing | Nothing. An empty or unusable square answers `None` |
| `place(coordinate, piece)` | A board with the piece added | `InvalidCoordinate`, `DestinationOccupied` |
| `remove(coordinate)` | A board without the piece | `PieceNotFound` |
| `move(origin, destination)` | A board with the piece relocated | `PieceNotFound`, `InvalidCoordinate`, `DestinationOccupied` |
| `piece_count()` / `piece_count_for(side)` | A count | — |

`move` is a **relocation, not a move**: it consults no rule of draughts, refuses only what is
mechanically impossible, and permits every geometrically absurd relocation in between. Legality
is move generation's answer and does not exist yet.

### 1.4 Opening position

| Rule | Statement |
| --- | --- |
| GE-5 | Each side fills the playable squares of the `setup_rows_per_side` ranks nearest it — 12 men a side on 8x8, 20 on 10x10 |
| GE-6 | No kings. The only thing on the platform that produces a king is a man reaching the far rank |
| GE-7 | Only playable squares are occupied, and at least one rank separates the two sides |

### 1.5 Failures

`GameDomainError` roots the kernel's taxonomy under `app.core.exceptions.DomainError`, with
`InvalidCoordinate`, `InvalidBoardState`, `PieceNotFound` and `DestinationOccupied` below it.
None carries a wire code of its own: the engine has no HTTP surface, and the task that gives
`game` an endpoint is the one that can judge which of them a client must distinguish.

---

## 2. Not yet specified

Move generation, mandatory capture and maximum capture, multi-jump paths, king mobility
(flying versus short), promotion on arrival and whether it ends the ply, `Position` (a board plus
the side to move), `Move` as an ordered path plus captured squares, repetition hashing, draw
rules, termination detection, PDN notation and serialization, the conformance corpus shared with
the TypeScript client engine (AD-14), and the engine version recorded per match (AD-15).

## TODO

- [ ] Assign a document owner
- [ ] Specify move generation and the capture obligation per variant
- [ ] Specify `Position`, `Move`, and the repetition hash
- [ ] Specify termination and draw detection
- [ ] Define the conformance corpus format and its CI execution (AD-14)
- [ ] Review and promote status from Partial to Approved
