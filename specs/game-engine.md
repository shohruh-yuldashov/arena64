# Game Engine

> **Status:** Partial — the board foundation (A64-014.1) and men's move generation (A64-014.2)
> are specified and implemented; kings, capture sequences, validation, termination and notation
> are not yet specified
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

## 2. Men's move generation — A64-014.2

### 2.1 Types

| Type | Kind | Contract |
| --- | --- | --- |
| `Position` | Frozen value object, hashable | `(board, side_to_move)` and nothing else |
| `Move` | Frozen value object | `(path, captured, promotes_to)` |
| `Direction` | Frozen value object, ordered | One diagonal step. The four are `DIAGONAL_DIRECTIONS` |
| `CaptureObligation` | Enum | `ANY`, `MAXIMUM` |
| `MoveGenerator` | Stateless service | `legal_moves(position) -> tuple[Move, ...]` |

### 2.2 `Position`

| Rule | Statement |
| --- | --- |
| GE-8 | A position is a board and a side to move. No status, no clock, no player identity, no move number — anything identifying *this* game would make every position unique and the repetition rule dead |
| GE-9 | Equality is by value, and the side to move is part of it: the same placement with the other player to move is a different position |
| GE-10 | `fingerprint` is the deterministic primitive reduction — `"<variant>/<side>/<square>=<side>:<rank>,…"`, squares ascending. It is stable across processes and languages, and it is what `__hash__` is defined over |

`Board` stays deliberately unhashable (A64-014.1); the position is the repetition key, which is
what that decision asked for.

### 2.3 `Move` — the path is the move

domain-model.md §2.1: "a multi-jump in draughts can reach the same destination square by
different capture paths, capturing different pieces." An origin/destination pair is therefore an
ambiguous description of several moves, and the ambiguity lands on the piece the paths disagree
about.

| Rule | Statement |
| --- | --- |
| GE-11 | `path` is the ordered sequence of squares the moving piece occupies, origin first, destination last. At least two |
| GE-12 | A quiet move is a two-square path with nothing captured — the shortest member of the same shape, not a special case |
| GE-13 | `captured` is ordered, and each square appears at most once. The order is what distinguishes two paths through the same pieces |
| GE-14 | Adjacent duplicate path squares are refused with `InvalidMove`; a non-adjacent revisit is legal, because a capture sequence may cross its own track |
| GE-15 | `promotes_to` states the rank the move *results* in. Nothing mutates a `Piece`; applying a move is a later task |
| GE-16 | Ordering is ascending `Move.sort_key` = `(path, captured)`. The promotion rank is never a tie-break, because crowning is a function of where the path ends |

### 2.4 `BoardGeometry` rule axes

The generator reads the geometry and **never** the variant. A `BoardVariant` value reaches
`geometry_of` and goes no further; a variant check inside a rules algorithm is invisible from the
variant table and is how a second variant becomes unshippable.

| Axis | Kind | Russian 8x8 | International 10x10 | Read by |
| --- | --- | --- | --- | --- |
| `rows`, `columns` | field | 8, 8 | 10, 10 | A64-014.1 |
| `setup_rows_per_side` | field | 3 | 4 | A64-014.1 |
| `is_playable` | derived | `(row + column)` even | same | A64-014.1 |
| `capture_is_mandatory` | field | `True` | `True` | A64-014.2 |
| `capture_obligation` | field | `ANY` | `MAXIMUM` | A64-014.4 |
| `men_may_capture_backward` | field | `True` | `True` | A64-014.2 |
| `kings_fly` | field | `True` | `True` | A64-014.5 |
| `promotion_ends_ply` | field | `False` | `True` *(provisional)* | A64-014.4 |
| `forward_step(side)` | derived | `+1` / `-1` | same | A64-014.2 |
| `promotion_row(side)` | derived | `7` / `0` | `9` / `0` | A64-014.2 |
| `step(origin, direction, distance)` | derived | — | — | A64-014.2 |

`forward_step` and `promotion_row` are derived rather than stored because `BoardCoordinate`
already fixes the orientation (GE-1); a stored direction could contradict it and nothing would
catch that.

### 2.5 Generation flow

1. Generate captures.
2. If any exist and `capture_is_mandatory`, **that is the answer** — quiet moves are never
   generated.
3. Otherwise generate quiet moves.
4. Return them in ascending `sort_key` order, as a tuple.

| Rule | Statement |
| --- | --- |
| GE-17 | Mandatory capture binds the **player**, not the piece: one man's available jump suppresses every other man's quiet moves |
| GE-18 | Captures are generated first, never generated-then-filtered. Under `MAXIMUM` the survivors must be compared by length, and a pool that has already mixed quiet moves in has to re-identify which were captures |
| GE-19 | A man steps one square forward diagonally onto an empty playable square. It never steps backward in any configured variant |
| GE-20 | A man jumps an adjacent opponent onto the empty playable square directly beyond, along the directions `man_capture_directions` gives — all four where `men_may_capture_backward` |
| GE-21 | Only pieces of `side_to_move` generate moves |
| GE-22 | A move landing on the mover's promotion row carries `promotes_to = KING` |
| GE-23 | The same position produces the same ordered tuple on every machine and in every process |

### 2.6 Corpus

`specs/game-engine/corpus/v1/` — the versioned conformance corpus AD-14 requires, in
language-neutral JSON, executed today by `apps/api/tests/unit/test_engine_corpus.py` and by a
TypeScript engine when one exists. Format, field rules and the versioning policy are in that
directory's `README.md`. A version is append-only.

### 2.7 Deliberate scope boundary

`MoveGenerator` skips kings — a king belonging to the side to move contributes **no** moves, and
is not refused. Until A64-014.5 its answer is complete only for positions with no king of the
side to move, and the corpus asserts that every case satisfies that. Every generated capture is a
single jump; A64-014.4 replaces one private method with a recursive walk without changing a
signature, an ordering, or the flow above.

---

## 3. Not yet specified

Capture sequences longer than one jump, maximum-capture selection, king mobility (flying versus
short), promotion in the middle of a sequence, move validation and application, repetition
hashing as an incremental `PositionHash`, draw rules, termination detection, PDN notation and
serialization, the TypeScript implementation of the corpus (AD-14), and the engine version
recorded per match (AD-15).

## TODO

- [ ] Assign a document owner
- [ ] Specify recursive capture sequences and maximum-capture selection (A64-014.4)
- [ ] Confirm `promotion_ends_ply` for international draughts against corpus cases
- [ ] Specify king mobility (A64-014.5)
- [ ] Specify move validation and application (A64-014.3)
- [ ] Specify the repetition hash, termination and draw detection
- [ ] Add the TypeScript implementation that executes the same corpus (AD-14)
- [ ] Review and promote status from Partial to Approved
