# Game Engine

> **Status:** Partial — **the rules of play are complete** (A64-014.1 – A64-014.7) and games
> serialize and replay (A64-014.8), except that three of the four draw thresholds are **undecided
> product rules** (§7.7). Clocks and PDN notation are not yet specified
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
| `BoardVariant` | Enum | `RUSSIAN_8X8`, `INTERNATIONAL_10X10`, `ENGLISH_8X8` |
| `BoardGeometry` | Frozen value object | `rows`, `columns`, `setup_rows_per_side`, and the derived `men_per_side`. One instance per variant, reached through `geometry_of` |
| `Board` | Immutable, compares by value | Placement of pieces for one variant |
| `initial_board(variant)` | Function | The opening position |

### 1.2 Geometry

| Rule | Statement |
| --- | --- |
| GE-1 | Row 0 is LIGHT's back rank; rows increase away from LIGHT. Column 0 is LIGHT's left |
| GE-2 | A square is playable when `(row + column)` is even, which puts a playable square at `a1` on every variant |
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
`InvalidCoordinate`, `InvalidBoardState`, `PieceNotFound`, `DestinationOccupied` and
`InvalidMove` below it. `IllegalMove` is the one deliberate exception and sits under
`RuleViolationError` instead — see §3.3. `UnsupportedPieceMovement` existed between A64-014.3
and A64-014.5 and is **removed** — see §5.6.

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
| `MidSequencePromotion` | Enum | `CROWNS_AND_CONTINUES`, `PASSES_THROUGH`, `CROWNS_AND_ENDS_PLY` |
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

| Axis | Kind | Russian 8x8 | International 10x10 | English 8x8 | Read by |
| --- | --- | --- | --- | --- | --- |
| `rows`, `columns` | field | 8, 8 | 10, 10 | 8, 8 | A64-014.1 |
| `setup_rows_per_side` | field | 3 | 4 | 3 | A64-014.1 |
| `is_playable` | derived | `(row + column)` even | same | same | A64-014.1 |
| `capture_is_mandatory` | field | `True` | `True` | `True` | A64-014.2 |
| `capture_obligation` | field | `ANY` | `MAXIMUM` | `ANY` | A64-014.4 |
| `men_may_capture_backward` | field | `True` | `True` | `False` | A64-014.2 |
| `kings_fly` | field, read via `king_reach` | `True` | `True` | `False` | A64-014.4 |
| `mid_sequence_promotion` | field | `CROWNS_AND_CONTINUES` | `PASSES_THROUGH` | `CROWNS_AND_ENDS_PLY` | A64-014.4 |
| `king_reach` | derived | `8` | `10` | `1` | A64-014.5 |
| `forward_step(side)` | derived | `+1` / `-1` | same | same | A64-014.2 |
| `promotion_row(side)` | derived | `7` / `0` | `9` / `0` | `7` / `0` | A64-014.2 |
| `step(origin, direction, distance)` | derived | — | — | — | A64-014.2 |

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

### 3.2 Validation by generation

| Rule | Statement |
| --- | --- |
| GE-24 | Legality is `move in move_generator.legal_moves(position)`. The validator holds **no** rules of its own |
| GE-25 | Nothing about quiet moves, captures, the capture obligation, promotion geometry or the side to move is re-derived. Every rule added later is enforced here by construction, without this type knowing it |
| GE-26 | Equality is exact and includes `promotes_to`, so a move that omits a required promotion or claims one the rules do not is refused. A caller echoes the generated move; it does not rebuild one from an origin and a destination |
| GE-27 | `is_legal` answers a boolean and `validate` raises `IllegalMove`. Both are total: since A64-014.5 there is no position the generator declines to answer for |

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

### 3.4 The king boundary — superseded by A64-014.5

**Historical.** GE-28 and GE-29 said a king belonging to the side to move raised
`UnsupportedPieceMovement`, and one belonging to the opponent did not. Kings move now (§5), so
neither applies and the exception is deleted; the rule that survives is the one they existed to
guarantee.

| Rule | Statement |
| --- | --- |
| GE-30 | An empty move set means exactly one thing: the side to move has nothing to play |

A64-014.2 skipped kings and returned whatever the men could do, which made two very different
situations identical: "this player has no legal moves" — a loss under the full rules — and "this
build cannot answer". Terminal-state detection reads an empty set as a statement about the game,
so the ambiguity had to go before anything was built on it. A64-014.3 removed it with a refusal;
A64-014.5 removes it by answering.

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

A64-014.5 calls that same scan with a king that started the ply, and adds the quiet slide beside
it. Nothing in §4 changed.

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

## 5. Kings — A64-014.5

No new public type. Kings move through the same `MoveGenerator`, are validated by the same
`MoveValidator` and applied by the same `MoveApplier`, none of which changed.

### 5.1 One pipeline, three answers

A king is not a special case with its own generator. It differs from a man in exactly three
answers, each selected by the piece's rank:

| Question | Man | King |
| --- | --- | --- |
| How far does it travel in one leg? | one square | `geometry.king_reach` |
| Which diagonals may it slide along? | forward only | all four |
| Which diagonals may it jump along? | what the variant allows | all four |

Everything else — the capture walk, the taken-once rule, mandatory capture, the maximum filter,
the ordering — is written once and does not know which it is looking at.

| Rule | Statement |
| --- | --- |
| GE-46 | `king_reach` is the board's long side where `kings_fly`, and one square where it does not. A short king is a flying king that cannot see past its neighbour, so both use one loop |

### 5.2 Quiet slides

| Rule | Statement |
| --- | --- |
| GE-47 | A king slides along any of the four diagonals, and **every empty square it passes is a move of its own** — where it stops decides what it can do next ply |
| GE-48 | The scan stops at the first occupied square, whoever owns it. A quiet move passes over nothing, so a friendly and an enemy piece block identically |
| GE-49 | A king is never promoted, by a slide across its own crownhead or by anything else |

### 5.3 Captures

| Rule | Statement |
| --- | --- |
| GE-50 | A king may begin a capture sequence. The scan finds the first occupied square within `king_reach`; if it is an untaken opponent, **every** empty square beyond it up to the next blocker is a distinct landing, and therefore a distinct move |
| GE-51 | It may not jump a friendly piece, an already-taken piece, or two pieces in one step — all three are "the first obstruction is not takeable", and all three close that diagonal |
| GE-52 | Sequences may change direction between jumps, revisit squares, and must be complete. The A64-014.4 rules (GE-36 – GE-41) apply unchanged |

### 5.4 Promotion, and what a king that started the ply reports

| Rule | Statement |
| --- | --- |
| GE-53 | A move's `promotes_to` depends on the rank the piece **began** the ply with. A king that started as a king reports `None` however far it travels and wherever it stops |
| GE-54 | `mid_sequence_promotion` decides what a man crossing its crownhead does — all three values, all configured |

| Value | Variant | Behaviour |
| --- | --- | --- |
| `CROWNS_AND_CONTINUES` | Russian 8x8 | Crowned on arrival, carries on jumping under **king** rules — flying, all four diagonals |
| `PASSES_THROUGH` | International 10x10 | Crosses uncrowned and carries on as a man; crowned only if the sequence *ends* there |
| `CROWNS_AND_ENDS_PLY` | English 8x8 | Crowned on arrival and the ply stops there, even with another jump available |

GE-53 is the correctness fix this task had to make. Before kings could start a move, "the mover
is a king" was sufficient evidence that it had been crowned along the way; reading it that way
now would have every king move claim a promotion.

### 5.5 `ENGLISH_8X8`

Added as **configuration only** — one enum member and one row in the geometry table, no algorithm
anywhere reads its name. It is here because it is the rule set that gives three axes a second
value:

| Axis | Russian / International | English |
| --- | --- | --- |
| `men_may_capture_backward` | `True` | `False` |
| `kings_fly` | `True` | `False` |
| `mid_sequence_promotion` | crowns-and-continues / passes-through | crowns-and-ends-ply |

Without it all three are settings nothing can tell apart from constants. It is a playable rule
set as far as move generation goes; nothing about first mover, draw rules or ratings has been
considered for it, and it is not offered anywhere.

### 5.6 Removed: `UnsupportedPieceMovement`

Deleted, with the guard that raised it. It existed from A64-014.3 to stop an empty move set
meaning two things while kings were unimplemented, and was documented as temporary from the day
it was written. A consumer that caught it can delete the handler: nothing raises it and nothing
replaced it. Corpus v2 supersedes the case that asserted it (§5.7).

### 5.7 Corpus v2

`specs/game-engine/corpus/v2/` — king cases, plus the mechanism for retiring a case a rules
change invalidates. A v2 file may carry a `supersedes` array naming ids from earlier versions; a
reader loads every version and drops those ids. **Supersession is data, not prose**, so a
TypeScript engine derives the same active set from the same files.

v1 is untouched on disk. The one case it retires is
`a-king-of-the-side-to-move-cannot-be-evaluated`, replaced by
`king-quiet-moves-along-open-diagonals` — the identical position, with the eleven moves that lone
king now has instead of a refusal.

### 5.8 Performance, observed

One contrived position: three flying kings against twelve men on a 10x10 board, every man
reachable and every landing square open.

| Measure | Observed |
| --- | --- |
| Complete sequences generated | 32 |
| Captures per sequence | 12 — every opponent piece |
| Wall time | 5.7 – 7.1 ms |

The search is bounded by **material, not by the board**: a sequence cannot be longer than the
number of opponent pieces, because every step consumes one and none is taken twice, so recursion
depth is capped by the same number. No optimisation was made, because nothing here is evidence
that one is needed (CLAUDE.md §10.1). The test asserts the structural bound plus a one-second
ceiling — a blow-up detector, not a budget. A real latency budget belongs with a real workload
rather than a position chosen to be awkward.

---

## 6. Game state and lifecycle — A64-014.6

This section spans two modules, and the split is the point.

| | Module | Sees |
| --- | --- | --- |
| `TerminalStateEvaluator`, `EngineVersion` | `engine` | One position. Nothing else |
| `Match` | `game` | The whole game — every position it has held, and how long since anything irreversible |

domain-model.md MT-12 states why: "terminal detection consults game **history**, not just the
position." The engine is the half that does not, `Match` is the half that remembers, and neither
can do the other's job.

### 6.1 `EngineVersion`

| Rule | Statement |
| --- | --- |
| GE-55 | `CURRENT_ENGINE_VERSION` is an explicit constant — never derived from package metadata, git state or a build timestamp. The same source must always stamp the same value, or AD-15's "enumerate exactly which matches were played under the defective version" is a guess |
| GE-56 | It is a single ordered integer, `as_primitive()`-able to an `int`. There is no "minor rules change": two builds either agree about every position or they do not |
| GE-57 | It is stamped on a `Match` at creation and is immutable thereafter (MT-3) |
| GE-58 | It is bumped when a change alters what `MoveGenerator`, `MoveValidator` or `TerminalStateEvaluator` answer for any position — and only then. A corpus expectation changing is the practical test |

Version **1** is A64-014.1 through A64-014.6. It has no draw rules; A64-014.7 makes it 2, and a
game played under 1 replayed under 2 could end earlier — exactly the divergence AD-15 exists to
record rather than discover.

### 6.2 Terminal states

| Rule | Statement |
| --- | --- |
| GE-59 | A side loses when it has **no pieces** or **no legal moves**. Both are properties of the position alone |
| GE-60 | The evaluator does not decide whether a side can move — it asks `MoveGenerator.legal_moves`. A second implementation of that question would eventually disagree, and the disagreement would take the form of a game ending that should not have |
| GE-61 | Material is checked first. Both are true of a side with nothing left, and `ALL_PIECES_CAPTURED` is the more useful answer |
| GE-62 | `evaluate` answers `None` for a position that continues, and a `TerminalState` — which always names a winner — for one that has ended |
| GE-63 | **The evaluator can never report a draw.** Every draw in draughts is historical, so a caller never has to handle a terminal state with nobody winning |

Being blocked in is a **loss** in draughts, not the stalemate draw of chess.

GE-60 is why terminal detection waited for kings: between A64-014.3 and A64-014.5 an empty move
list could also have meant "this build cannot evaluate a king", and an evaluator built then would
have declared a loss for a player who had moves the engine could not see.

### 6.3 `Match` — the lifecycle aggregate

`app/modules/game/domain/match.py`. Named `Match`, not `Game` or `GameState`: domain-model.md
§16.1 rejects the alternative by name — "they are the same concept under two names, and two names
for one thing guarantees that half the codebase means one and half means the other".

| Status | Meaning |
| --- | --- |
| `CREATED` | Exists, no move played. Distinct from `ACTIVE` because a match exists before either clock starts (system-design.md §3) |
| `ACTIVE` | Under way. The only status in which a move may be played |
| `COMPLETED` | Finished with a result. Immutable from here except by an `admin` adjudication that is itself recorded (MT-10) |
| `ABORTED` | Ended with **no** result and no rating effect (MT-11) |

| Rule | Statement |
| --- | --- |
| GE-64 | `CREATED → ACTIVE → COMPLETED`; `CREATED → ABORTED`; `ACTIVE → ABORTED`. Every other transition raises `InvalidMatchTransition` (a `ConflictError`, so `409`) |
| GE-65 | `play` sequences existing services and re-derives nothing: `MoveApplier` validates and applies, `TerminalStateEvaluator` decides whether the result has ended. A refused move leaves the match untouched |
| GE-66 | Ply numbers are contiguous from 1 (MT-5); `0` means none has been played |
| GE-67 | `resign(side)` gives the win to the opponent and **changes no board** — a resigned game must still replay to the position it was abandoned in. The side is given explicitly; who may resign for whom is an authorization question this layer has no notion of |
| GE-68 | `abort()` is permitted from `CREATED` and `ACTIVE` and produces `MatchOutcome.NONE`. **Not a draw**: a draw is an outcome two players played to and it counts everywhere; an abort is a match that did not happen |
| GE-69 | `MatchResult` is absent until the match ends (DM-08), and holds outcome, reason and winner together. A win names a winner; a draw and an abort cannot |

`TerminationReason` is populated **in full** from domain-model.md §15 — all eleven members,
including the eight nothing can produce yet. That is R-19: "adding 'abandonment' later, after
months of games were recorded as 'resignation', makes every historical statistic wrong and
unfixable." A64-014.6 produces four.

The aggregate is deliberately incomplete against domain-model.md §10.4. Absent: the two
`MatchParticipant` seats, the append-only move log, `ClockState` and time control, `Offer`, the
per-match sequence number, and the domain events. None is a rules concern.

### 6.4 Position history

| Rule | Statement |
| --- | --- |
| GE-70 | `Position` **is** the repetition key — board plus side to move, hashable, and nothing else. Nothing may be added to it: a ply number or a timestamp would make every position unique and the repetition rule dead |
| GE-71 | The opening position is recorded at creation. A game that returns to its opening has repeated it once, not reached it for the first time |
| GE-72 | Every applied move records the position it produces |
| GE-73 | `position_history` is a read-only view. The counts are the aggregate's to change; a caller that could edit them could desynchronise the history from the moves that produced it |

`occurrences_of(position)` and `current_position_occurrences` answer the questions a repetition
rule asks. **A64-014.6 sets no threshold and declares no draw** — what counts as enough, and what
it means, is A64-014.7's.

### 6.5 History counters

`plies_since_progress` counts plies since anything irreversible happened.

| Move | Effect |
| --- | --- |
| A capture | resets to `0` |
| A man's move, including one that crowns | resets to `0` |
| A non-capturing king's move | increments |

Progress is a capture or a man's move because those are the two things that cannot be undone:
material only decreases and a man only advances. A game shuffling kings around an empty board is
going nowhere, which is the whole reason the counter exists.

It lives on `Match` and not on `Position`, because it is a property of the *path* taken to a
position rather than of the position: two games can reach the same board with different counters.

### 6.6 Corpus — terminal expectations

v2 gains a third top-level key, `terminal_positions`, beside `cases` and `rejections`. "These are
the legal moves" and "this position has ended" are different claims; bending one shape into the
other would make a reader guess which it was looking at. A case states `terminal`, and `winner`
and `reason` which are absent together for a position that continues and present together for one
that has ended — the same shape `evaluate` answers with. Format in the corpus `README.md`.

---

## 7. Draw rules — A64-014.7

### 7.1 Why draws are not in the engine

domain-model.md MT-12: "terminal detection consults game **history**, not just the position."
`TerminalStateEvaluator` is the half that reads a position, and A64-014.7 **did not touch it**.

| | Sees | Can report |
| --- | --- | --- |
| `TerminalStateEvaluator` (`engine`) | One `Position` | A loss — no pieces, or no moves |
| `DrawRuleSet` (`game`) | A `MatchHistory` snapshot | A draw — repetition, or a move limit |

| Rule | Statement |
| --- | --- |
| GE-74 | The terminal evaluator's contract is unchanged: it takes a position and nothing else. Widening it would give the kernel a memory, which AD-13 forbids, and would make "is this position terminal" answer differently depending on how the game got there |
| GE-75 | `DrawRuleSet.evaluate(rules, history)` takes two immutable values, not the `Match`. It cannot change the aggregate, and it can be exercised against configurations no variant has — which matters, because three of the four rules are configured by none |

### 7.2 Repetition — the first occurrence counts

| Rule | Statement |
| --- | --- |
| GE-76 | The key is `Position` — board plus side to move, unchanged. Nothing was added to it; a ply number or a clock would make every position unique and the rule dead |
| GE-77 | A draw fires when `occurrences_of(position) >= repetition_threshold`. **Occurrences, not returns** |
| GE-78 | The opening position has occurred **once** before anybody moves, so a threshold of 3 fires on the *second* return. `Match` records its starting position at creation for exactly this reason |

    opening            occurrence 1
    first return       occurrence 2   — not a draw
    second return      occurrence 3   — draw

### 7.3 The no-progress counter

Unchanged from A64-014.6 in behaviour, and now **configured** rather than hard-coded:
`captures_reset_progress` and `man_moves_reset_progress` are variant axes. Both are true in
every configured variant, and the counter still resets on a capture or a man's move — including
one that crowns, because the advance is what was irreversible — and increments on a
non-capturing king move.

Thresholds are counted in **plies**, one per player turn. database.md §6.1 settles the unit by
naming the column `moveless_draw_plies`.

### 7.4 Variant configuration

`BoardGeometry.draw_rules` is a `DrawRules` record, an axis like every other. `DrawRuleSet`
reads it and **names no variant** — a test asserts the source contains no `BoardVariant` member
name or value, because a branch on a variant nobody has configured yet would pass every
behavioural test.

| Axis | Meaning |
| --- | --- |
| `repetition_threshold` | Occurrences of one position that end the game. `None` disables the rule |
| `no_progress_ply_limit` | Plies without a capture or a man's move |
| `king_only_ply_limit` | The shorter limit that applies once no men remain |
| `material_ply_limits` | Bands that shorten the limit as material thins, narrowest first, first match wins |
| `captures_reset_progress` | Whether a capture restarts the count |
| `man_moves_reset_progress` | Whether a man's move restarts it |

### 7.5 Evaluation order, and priority

Inside `DrawRuleSet`: repetition → king-only → material band → plain no-progress. Most specific
first, because all four can be true at once in a thin endgame and the reason a player reads
should describe their game.

Inside `Match.play`:

    1. validate and apply          — MoveApplier, unchanged
    2. record the position         — one occurrence
    3. update the counter          — per the variant's reset axes
    4. TerminalStateEvaluator      — a win short-circuits
    5. DrawRuleSet                 — only if no decisive result
    6. otherwise the match stays ACTIVE

| Rule | Statement |
| --- | --- |
| GE-79 | **A decisive result takes priority.** A game that was won is never recorded as drawn |
| GE-80 | A draw is a `MatchResult` with `MatchOutcome.DRAW` and no winner — the existing seam, validated so that a draw naming a winner is unrepresentable |

### 7.6 Engine version 2

`CURRENT_ENGINE_VERSION` is **2**. A64-014.7 makes the engine answer differently about games it
already answered about — a match that shuffles back twice is drawn under 2 and ran on under 1 —
which is precisely AD-15's case. Every match created from here records 2; a match created under
1 keeps its 1, because the point of stamping a version is that it describes what happened.

Every `draw_sequences` corpus case states `engine_version: 2`, and a test asserts the corpus and
the build agree.

### 7.7 Undecided thresholds — a product decision, not an implementation gap

**Only `repetition_threshold` is configured.** The other three are `None` on every variant.

| Threshold | Status |
| --- | --- |
| Repetition = 3 | **Decided.** domain-model.md states the "three-fold repetition draw rule" throughout, unqualified by variant |
| `no_progress_ply_limit` | **Undecided.** database.md §6.1 names the column `moveless_draw_plies`; no document in this repository gives it a value |
| `king_only_ply_limit` | **Undecided.** Russian draughts has a fifteen-move king-only rule; this repository does not state it |
| `material_ply_limits` | **Undecided.** Russian draughts scales the limit by material; the bands are nowhere stated |
| International 10x10 | **Undecided in full.** Its draw rules are not the Russian ones, and none of them is documented here. It currently carries the same configuration, which is a placeholder rather than a claim |

The mechanism is complete and every branch is tested against explicit configurations. Guessing a
number would end rated games on a threshold nobody chose, and AD-15 makes changing one an
engine-version event — so the numbers wait for a decision, and are one table edit away.

**A second open question.** `DrawReason` distinguishes `NO_PROGRESS`, `KING_ONLY_MOVE_LIMIT` and
`MATERIAL_MOVE_LIMIT`, and all three are recorded as `TerminationReason.MOVE_LIMIT` because that
is what domain-model.md §15 enumerates and R-19 fixes its members. A statistic that wanted to
tell them apart cannot. Widening §15 is a documented-model change and was not made unilaterally.

### 7.8 Corpus — draw sequences

A **fourth** expectation shape, `draw_sequences`, beside `cases`, `rejections` and
`terminal_positions`. A draw is a property of a game rather than of a board, so a case states an
opening position, an ordered list of moves, and what the match looks like afterwards: status,
outcome, reason, winner, the current position's occurrence count and the no-progress counter.
Each case also states the engine version it was written for. Format in the corpus `README.md`.

No case exercises a move limit, because no variant configures one.

---

## 8. Serialization and replay — A64-014.8

### 8.1 One encoding

`engine.serialization` projects every kernel value to and from JSON-shaped primitives, and
`game.domain.serialization` does the same for the aggregate's own shapes. **They are the shapes
the conformance corpus is written in**, and `tests/corpus.py` reads through them — a corpus in
one encoding and a stored game in another would be two definitions of what a move is.

| Rule | Statement |
| --- | --- |
| GE-81 | A move serializes as its **complete path**, plus captured squares in order and the promotion result. Never as an origin and a destination: §2.1's multi-jump reaches one square by different routes taking different pieces, and a from/to record cannot tell them apart |
| GE-82 | Every `*_from_primitive` requires every field. **No hidden defaults** — a promotion flag that quietly defaulted would replay into a different position, which is the failure AD-15 exists to surface rather than absorb |
| GE-83 | Boards serialize with their squares sorted, so two identical boards built by different move orders produce identical text |
| GE-84 | Kernel projections live in `engine` because `replay` and `fairplay` may import it and not `game` (R-2) |

### 8.2 Engine version

| Rule | Statement |
| --- | --- |
| GE-85 | The stored value is `EngineVersion.as_primitive()` — a plain integer |
| GE-86 | Deserialization requires an explicit integer. **Never inferred** from a timestamp, a schema version, a file format, a creation date, or the version this build happens to be. A boolean is refused, because `True == 1` would silently become version 1 |

### 8.3 The move log

MT-5 and MT-6, implemented. `Match` owns an append-only log of `MoveRecord`.

| Rule | Statement |
| --- | --- |
| GE-87 | Ply numbers are contiguous from 1. A gap makes the game unreplayable, which invalidates the result, the analysis and the fair-play record at once |
| GE-88 | One record per **successful** move. A refused move appends nothing and is indistinguishable from one nobody sent |
| GE-89 | The log is handed out as a tuple; records are frozen. "Append-only" is a property, not a convention |
| GE-90 | `Match.last_move` reads off the log rather than being stored beside it — two histories of one game are two answers to what was played |

`MoveRecord` carries `think_time_ms` and `remaining_clock_ms`, both `None` on every record this
build writes, because there are no clocks. They exist now for MT-6's reason: `fairplay` cannot be
retrofitted (AD-05), and a log written without them would leave a hole in the games recorded
before clocks arrive. `None` says "not measured"; a zero would say "measured, and it was instant".

### 8.4 The position hash is a fingerprint, not a Zobrist hash

`resulting_position_hash` is `Position.fingerprint` — a sorted deterministic string of the
variant, the side to move and every occupied square. Stable across processes, machines and
languages, which is what a replay and a cross-implementation corpus need.

It is **not** a Zobrist hash and must not be called one. It is not incremental, not fixed-width,
and has no XOR structure to update in place. domain-model.md §10.1 reserves `PositionHash` for
that; when it arrives it will be a second, narrower thing beside this one, adopted with a
measurement behind it.

### 8.5 Replay

`ReplayData` carries the engine version, the variant, the opening position, the ordered records,
and optionally the result the record claims. **Nothing derived**: no position counts, no
no-progress counter, no final board.

    1. check the engine version is one this build reproduces
    2. check the log is contiguous from 1 — whole, before anything is applied
    3. open a Match at the recorded position and start it
    4. play every record through Match.play — the same validator, applier,
       terminal evaluator and draw rules a live game uses
    5. verify each ply's resulting fingerprint against the record
    6. check the stated result, if one was stated

| Rule | Statement |
| --- | --- |
| GE-91 | A replay reproduces **why** a game ended, not merely where. Position counts and the no-progress counter are recomputed by applying the log, never restored from the record |
| GE-92 | Verification is **per ply**. A mismatch caught on the move that caused it names the rule that changed; the same mismatch caught at the end names nothing |
| GE-93 | A replay re-derives no rule. Everything goes through `Match.play`, so there is one source of truth for what a game is |

### 8.6 Replay refusals

| Failure | Raised for |
| --- | --- |
| `UnsupportedEngineVersion` | Rules this build cannot reproduce |
| `MalformedMoveLog` | Ply numbers not contiguous from 1 — checked before any move is applied |
| `CorruptMoveLog` | A move the rules refuse, or one recorded after the game ended. The original refusal is **chained**, because which rule refused it is the diagnostic |
| `PositionHashMismatch` | A recorded position and the one the rules produce disagree |
| `ReplayResultMismatch` | The reconstructed match ended differently from the record |

All five descend from `ReplayError`, deliberately a different family from
`InvalidMatchTransition`: a lifecycle refusal is a caller with a stale view, and one of these is
**a record that cannot be true**.

### 8.7 Historical version support — engine version 2 only

`SUPPORTED_ENGINE_VERSIONS` holds version 2 and nothing else.

Version 1 had no draw rules (A64-014.7 added them and bumped the version for exactly this
reason). A version-1 game replayed under version 2 could end earlier than it did — a repetition
that ran on in the real game would draw in the replay — which is AD-15's scenario word for word.
So **a version-1 replay is refused, not approximated**.

Nothing has been persisted under version 1: no store exists yet, so the refusal costs nothing
today and is the honest position when it does. The seam is an injectable set rather than an `if`,
so supporting an older build means adding a rules profile beside the current one.

**The compatibility question this leaves open** is the §7.7 one: when the undecided draw
thresholds are settled, `CURRENT_ENGINE_VERSION` becomes 3, and every version-2 game becomes
unreplayable by the same argument. That is correct behaviour and an operational cost, and it is
the reason to settle the thresholds before anything is stored rather than after.

---

## 9. Not yet specified

Three of the four **draw thresholds** — see §7.7. The rules are implemented and tested; the
numbers are a product decision.

Also absent: clocks, flag falls and abandonment; `Offer` and the draw-agreement flow; the rest of
the `Match` aggregate (the two seats, sequence numbers, domain events); persistence and transport
of any kind; move undo; an incremental `PositionHash`; PDN notation; and the TypeScript
implementation of the corpus (AD-14).

For `english_8x8` specifically: first mover, draw rules and rating category are unconsidered. It
is configured for move generation and is offered nowhere.

## TODO

- [ ] Assign a document owner
- [ ] **Decide the three undecided draw thresholds, and International's in full (§7.7)**
- [ ] Decide whether `TerminationReason` should distinguish the three move-limit draws (§7.7)
- [ ] Decide whether `english_8x8` is a product variant or stays a configuration fixture
- [ ] Add the TypeScript implementation that executes the same corpus (AD-14)
- [ ] Review and promote status from Partial to Approved
