# Game Engine

> **Status:** Partial — the board foundation (A64-014.1), men's move generation (A64-014.2),
> move validation and application (A64-014.3) and complete capture sequences (A64-014.4) are
> specified and implemented; standalone king movement, termination and notation are not yet
> specified
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
is `MoveValidator`'s answer (§3), and `MoveApplier` is the only thing that should be calling this
— which is why it removes captured pieces *before* relocating (GE-31).

### 1.4 Opening position

| Rule | Statement |
| --- | --- |
| GE-5 | Each side fills the playable squares of the `setup_rows_per_side` ranks nearest it — 12 men a side on 8x8, 20 on 10x10 |
| GE-6 | No kings. The only thing on the platform that produces a king is a man reaching the far rank |
| GE-7 | Only playable squares are occupied, and at least one rank separates the two sides |

### 1.5 Failures

`GameDomainError` roots the kernel's taxonomy under `app.core.exceptions.DomainError`, with
`InvalidCoordinate`, `InvalidBoardState`, `PieceNotFound`, `DestinationOccupied`, `InvalidMove`
and `UnsupportedPieceMovement` below it. `IllegalMove` is the one deliberate exception and sits
under `RuleViolationError` instead — see §3.3.

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
| `kings_fly` | field, read via `king_reach` | `True` | `True` | A64-014.4 |
| `mid_sequence_promotion` | field | `CROWNS_AND_CONTINUES` | `PASSES_THROUGH` | A64-014.4 |
| `forward_step(side)` | derived | `+1` / `-1` | same | A64-014.2 |
| `promotion_row(side)` | derived | `7` / `0` | `9` / `0` | A64-014.2 |
| `step(origin, direction, distance)` | derived | — | — | A64-014.2 |

`forward_step` and `promotion_row` are derived rather than stored because `BoardCoordinate`
already fixes the orientation (GE-1); a stored direction could contradict it and nothing would
catch that.

### 2.5 Generation flow

1. Generate every complete capture sequence (§4).
2. Narrow to the longest where `maximum_capture_is_mandatory`.
3. If any survive and `capture_is_mandatory`, **that is the answer** — quiet moves are never
   generated.
4. Otherwise generate quiet moves.
5. Return them in ascending `sort_key` order, as a tuple.

| Rule | Statement |
| --- | --- |
| GE-17 | Mandatory capture binds the **player**, not the piece: one man's available jump suppresses every other man's quiet moves |
| GE-18 | Captures are generated first, never generated-then-filtered. Under `MAXIMUM` the survivors must be compared by length, and a pool that has already mixed quiet moves in has to re-identify which were captures |
| GE-19 | A man steps one square forward diagonally onto an empty playable square. It never steps backward in any configured variant |
| GE-20 | A man jumps an adjacent opponent onto the empty playable square directly beyond, along the directions `man_capture_directions` gives — all four where `men_may_capture_backward`. Whether it must then continue is §4 |
| GE-21 | Only pieces of `side_to_move` generate moves |
| GE-22 | A **quiet** move landing on the mover's promotion row carries `promotes_to = KING`. A capture's answer depends on `mid_sequence_promotion` — §4.4 |
| GE-23 | The same position produces the same ordered tuple on every machine and in every process |

### 2.6 Corpus

`specs/game-engine/corpus/v1/` — the versioned conformance corpus AD-14 requires, in
language-neutral JSON, executed today by `apps/api/tests/unit/test_engine_corpus.py` and by a
TypeScript engine when one exists. Format, field rules and the versioning policy are in that
directory's `README.md`. A version is append-only.

### 2.7 Deliberate scope boundary

Superseded: kings are **refused**, not skipped (§3.4), and captures are complete sequences, not
single jumps (§4).

---

## 3. Validation and application — A64-014.3

### 3.1 Types

| Type | Kind | Contract |
| --- | --- | --- |
| `MoveValidator` | Stateless service over `MoveGenerator` | `is_legal(position, move) -> bool`, `validate(position, move) -> None` |
| `MoveApplier` | Stateless service over `MoveValidator` | `apply(position, move) -> Position` |
| `IllegalMove` | `app.core.exceptions.RuleViolationError` | A well-formed move the rules do not allow here |
| `UnsupportedPieceMovement` | `GameDomainError` | The engine cannot evaluate the position — temporary |

### 3.2 Validation by generation

| Rule | Statement |
| --- | --- |
| GE-24 | Legality is `move in move_generator.legal_moves(position)`. The validator holds **no** rules of its own |
| GE-25 | Nothing about quiet moves, captures, the capture obligation, promotion geometry or the side to move is re-derived. Every rule added later is enforced here by construction, without this type knowing it |
| GE-26 | Equality is exact and includes `promotes_to`, so a move that omits a required promotion or claims one the rules do not is refused. A caller echoes the generated move; it does not rebuild one from an origin and a destination |
| GE-27 | `is_legal` answers a boolean; `validate` raises `IllegalMove`. Neither converts `UnsupportedPieceMovement` into a refusal |

The cost is generating the full move set to check one move. For a validator called
once per ply against a board of at most 50 squares that is not measurable; if a profile ever
says otherwise, the lever is a generator that can answer for one origin square — never a second
copy of the rules inside the validator.

### 3.3 `IllegalMove` and the failure split

| | `InvalidMove` | `IllegalMove` |
| --- | --- | --- |
| What | The move's *shape* is broken | Well formed, and not available here |
| Raised by | `Move.__post_init__`, at construction | `MoveValidator`, against a position |
| Root | `GameDomainError` | `RuleViolationError` |
| Means | A caller built a move wrong | A player was told no |
| In play | Never happens | Happens constantly |

`IllegalMove` is the **only** engine failure that does not descend from `GameDomainError`, and
that is the point: everything under that root is a caller bug that should never occur, and this
one is ordinary traffic on every game ever played. `game` wants two handlers — one sends a
message to a client, the other raises an incident.

The message names no rule. The validator genuinely does not know which rule excluded the move —
it checked set membership — and inventing a reason would mean re-deriving the rules in the place
built to avoid that. A player-facing explanation belongs in `game`, which already holds the legal
move set: "you must capture" is `any(move.is_capture for move in legal)`.

### 3.4 The king boundary — temporary, removed by A64-014.5

| Rule | Statement |
| --- | --- |
| GE-28 | A king belonging to the **side to move** raises `UnsupportedPieceMovement` from `MoveGenerator.legal_moves`, and therefore from validation and application |
| GE-29 | A king belonging to the **opponent** raises nothing. It is a piece a man may jump, which this build handles correctly |
| GE-30 | Consequently an empty move set means exactly one thing: the side to move has nothing to play |

A64-014.2 skipped kings and returned whatever the men could do, which made two very different
situations identical: "this player has no legal moves" — a loss under the full rules — and "this
build cannot answer". Terminal-state detection reads an empty set as a statement about the game,
so the ambiguity had to go before anything was built on it.

### 3.5 Move application

    1. validate                    — through `MoveValidator`, never inline
    2. remove every captured square, in the order the move records
    3. relocate the moving piece from origin to destination
    4. crown it, if the move says it is crowned
    5. hand the turn to the opponent

| Rule | Statement |
| --- | --- |
| GE-31 | **Victims come off before the attacker moves.** `Board.move` refuses an occupied destination and knows nothing about capture (A64-014.1), so the board must already be in the state the relocation is legal in |
| GE-32 | `apply` validates every time. There is no unchecked path and no `validate=False` — AD-13's failure mode is "a *plausible but illegal* game that is rated, ranked, and permanently recorded" |
| GE-33 | Neither the position nor its board is modified, on any path including the ones that raise. Every intermediate board is a new value, so a failure part-way through cannot leave a half-applied state |
| GE-34 | The returned position holds the new board and the opponent as side to move |
| GE-35 | The same position and move produce the same result every time |

Promotion replaces the arriving piece with `Piece.promote()` rather than building a king from
`Move.promotes_to`. The field is read as *whether* the move crowns — its only possible value is
`KING`, since validation refuses any move claiming another — and `Piece.promote` stays the one
implementation of *what* crowning means.

Move **undo** is deliberately absent. Positions are values, so undo is holding the previous one;
a caller wanting a stack keeps a list. An `undo` that recomputed a prior position would be a
second implementation of `apply`.

### 3.6 Rejection corpus

`men-rejections.json` adds a `rejections` key beside `men-basic.json`'s `cases`. A file carries
one of the two, and a reader loads the key it understands — which makes a third kind of case an
append rather than a migration of every reader. Categories are `illegal_move`, `malformed_move`
and `unsupported_piece`; the category is part of the expectation, because the three are refused
at different moments by different code. Format in the corpus `README.md`.

---

## 4. Complete capture sequences — A64-014.4

No new public type. The change is inside `MoveGenerator`'s capture generation, plus one axis on
`BoardGeometry` and one correctness fix in `MoveApplier` (§4.6).

### 4.1 Terminal sequences only

| Rule | Statement |
| --- | --- |
| GE-36 | A capture `Move` is a **complete** sequence: it appears only if the piece cannot jump again from where it ends. Prefixes are never offered, because a player who can continue must |
| GE-37 | The search is a depth-first walk over the jumps available from the piece's current square. It terminates because every step consumes one victim, a victim is never taken twice, and victims are finite |
| GE-38 | `path` records every landing in order; `captured` records every victim in the order it was jumped |
| GE-39 | Nothing is mutated. The walk runs against a board derived from the position, and the position is unchanged afterwards |

Terminal-only generation is also what keeps `MoveApplier` correct without it knowing anything
about sequences: every move it is handed is a whole ply.

### 4.2 The board the walk sees

Two adjustments, both made once per piece, both by deriving a new immutable board:

| Adjustment | Why |
| --- | --- |
| **The moving piece is lifted off its origin** | Otherwise a sequence that circles back to a square it has already stood on would find itself in the way. This is what makes GE-41 work |
| **Victims are left standing** | A taken piece leaves the board when the ply ends, not when it is jumped — the "Turkish strike" rule. Until then it blocks |

`captured` is carried down the recursion and records which of the standing pieces have already
been taken.

### 4.3 Captured-piece semantics

| Rule | Statement |
| --- | --- |
| GE-40 | A piece already taken this ply can be neither jumped again nor passed through. It is an obstacle, and the diagonal it stands on is closed |
| GE-41 | A path may revisit a square, including the origin. `Move` refuses only *adjacent* duplicates, which is a malformed step rather than a legal loop |

GE-40 is what makes the walk terminate at all: without it a man between two victims would jump
back and forth forever. It is enforced once, in the jump scan — `Move.__post_init__`'s
uniqueness check is a shape invariant, not a second implementation of this rule.

### 4.4 Promotion during a capture

`BoardGeometry.mid_sequence_promotion` decides, and the walker never names a variant.

| Value | Variant | Behaviour |
| --- | --- | --- |
| `CROWNS_AND_CONTINUES` | Russian 8x8 | Crowned on arrival, and carries on jumping **as a king** in the same ply |
| `PASSES_THROUGH` | International 10x10 | Crosses the crownhead uncrowned and carries on as a man; crowned only if the sequence *ends* there |

| Rule | Statement |
| --- | --- |
| GE-42 | A sequence's `promotes_to` is `KING` when the piece is a king by the end — crowned along the way — or when it is still a man and stopped on the crownhead |
| GE-43 | Crowning mid-sequence means the rest of the ply uses **king** jump rules: all four diagonals, and a reach of `king_reach` |

This replaces A64-014.2's `promotion_ends_ply` boolean, which was recorded as provisional
because it had no observable meaning until sequences existed. Given one, it turned out neither
configured variant took either of its values: the boolean describes English draughts, where
crowning ends the ply, and that is the enum's absent third member.

### 4.5 King jumps, and what is still deferred

A crowned man has to keep jumping, so king *capture* generation exists: it scans a diagonal for
the first obstruction within `king_reach`, and if that is an untaken opponent, every empty square
beyond it is a distinct landing. `king_reach` is the board's long side where `kings_fly` and one
square where it does not, which is why a short king needs no second code path.

Still deferred to **A64-014.5**: king *quiet* moves, and starting a ply from a king that was
already on the board — `UnsupportedPieceMovement` (§3.4) still refuses that. The scan above would
answer correctly for such a king; nothing calls it with one.

### 4.6 Maximum capture

| Rule | Statement |
| --- | --- |
| GE-44 | Under `MAXIMUM`, only the sequences taking the most pieces survive. Under `ANY` the player chooses, so all of them do |
| GE-45 | The filter runs **after** the search, over finished sequences — never as pruning inside it |

GE-45 is the one that matters. A branch that opens by taking a single piece can end up the
longest on the board, so a walker that preferred the wider first jump would return the wrong move
— and only in positions rare enough to reach production. The corpus pins this with one position
generated under both variants: Russian offers a one-piece and a two-piece sequence, international
keeps only the two-piece one.

### 4.7 Ordering

Unchanged: ascending `Move.sort_key` = `(path, captured)`. Longer paths compare against shorter
ones lexicographically, so a prefix sorts first — though a prefix is never present beside its
extension. Two sequences of equal length are separated by their first differing landing, which
the corpus pins with a ring position offering two four-capture loops in opposite directions.

### 4.8 One fix in `MoveApplier`

`Board.move` refuses to relocate a piece onto the square it already stands on — correct where it
lives, since a bare self-relocation is a caller with a bug (A64-014.1). GE-41 makes
`origin == destination` an ordinary legal ply, so step 3 of application now lifts the piece and
places it rather than calling `Board.move`. Every guarantee is kept: `remove` refuses an empty
origin, `place` refuses an occupied or unplayable destination, and `Board` is unchanged.

---

## 5. Not yet specified

Standalone king movement — king quiet moves, and starting a ply from a king already on the board
— move undo, repetition hashing as an incremental `PositionHash`, draw rules, terminal-state
detection, PDN notation and serialization, the TypeScript implementation of the corpus (AD-14),
and the engine version recorded per match (AD-15).

## TODO

- [ ] Assign a document owner
- [ ] Specify standalone king movement and remove the `UnsupportedPieceMovement` boundary (A64-014.5)
- [ ] Specify the repetition hash, termination and draw detection
- [ ] Add the TypeScript implementation that executes the same corpus (AD-14)
- [ ] Review and promote status from Partial to Approved
