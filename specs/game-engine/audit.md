# Game Engine — Audit and Stabilization

> **Status:** Complete — A64-014.10, the closing task of the Game Engine Epic
> **Owner:** _Unassigned_
> **Audited:** 2026-08-02
> **Scope:** `apps/api/app/modules/engine/`, `apps/api/app/modules/game/`, `specs/game-engine/`
> **Related:** `specs/game-engine.md`, `specs/game-engine/traceability.md`, architecture.md AD-13 – AD-15

## Readiness

# READY WITH DOCUMENTED LIMITATIONS

The rules of draughts are implemented, specified rule by rule, and verified — including against
an **external oracle**, which is the one form of evidence a self-consistent test suite cannot
provide. Nothing found in this audit is a correctness defect.

Three limitations are load-bearing rather than cosmetic, and each is a decision rather than a
bug:

1. **AD-14 is half-met.** There is no TypeScript engine, so the corpus proves conformance to a
   contract and not agreement between two independent implementations (§1).
2. **Three of four draw thresholds are undecided product rules**, and one variant's rules are
   undecided in full (§8). The mechanism is complete and tested; the numbers are not chosen.
3. **Only engine version 2 replays** (§5). Nothing has been persisted, so this costs nothing
   today and will cost something the first time the version moves again.

None blocks matchmaking integration, because matchmaking needs the engine to *play games*, which
it does. All three block anything that stores a rated result.

---

## 1. The headline gap — no TypeScript engine

AD-14: "The Python engine and the TypeScript client engine are two implementations governed by
one versioned corpus of positions, legal move sets, and expected outcomes, executed by both in
CI… divergence is caught by a failing test rather than by a player disputing a result."

**Half of that exists.** The corpus is versioned, language-neutral and executed — by one
implementation. A bug both implementations would share is precisely what the second one exists to
find, and no amount of Python testing substitutes for it. **AD-14 must not be described as
complete.**

### What a TypeScript engine has to implement

| Area | Contract |
| --- | --- |
| Value objects | `BoardCoordinate`, `Piece`, `Board`, `Position`, `Move`, `EngineVersion`, `BoardGeometry`, `DrawRules` — value semantics, no identity |
| Generation | `legal_moves(position)` — complete capture sequences, mandatory capture, the maximum filter, king reach, all three mid-sequence promotion rules |
| Ordering | Ascending `(path, captured)`; a shorter path that is a prefix sorts first. Not JavaScript's default array comparison — a reader implements the comparator |
| Validation | Membership in the generated set, including `promotes_to` |
| Application | Victims removed before relocation; lift-and-place, not a relocation primitive that refuses `origin == destination` |
| Terminal | No pieces, or no legal moves. Material checked first. **Never a draw** |
| Lifecycle | Only if the client needs it. `Match`, its counters, and the draw rules are server-side concerns |
| Serialization | The exact shapes in `engine/serialization.py`: square `"c3"`, piece `{square, side, rank}`, move `{path, captured, promotes_to}`, pieces sorted by square |
| Fingerprint | `"<variant>/<side>/<square>=<side>:<rank>,…"`, squares ascending row-major. Byte-identical or the corpus comparison is worthless |

### Which corpus versions it must consume

Both, with the supersession mechanism: load `v1` and `v2`, collect every `supersedes` entry, drop
those ids. `RejectionCategory.unsupported_piece` must still *parse* — v1 contains it — while no
active case uses it.

All five expectation shapes: `cases`, `rejections`, `terminal_positions`, `draw_sequences`,
`replays`.

### How agreement is compared

Move-for-move on the ordered tuple, not as a set; fingerprints as strings; results by outcome,
reason and winner; and for replays the position count and no-progress counter as well, because a
draw is a property of a sequence.

### Estimated work and risks

| Area | Size | Risk |
| --- | --- | --- |
| Value objects and serialization | Small | Low — the shapes are pinned and round-tripped |
| Move generation with the capture walk | **Large** | **High** — the recursive walk, the Turkish-strike rule and the maximum filter are where two implementations diverge |
| Ordering | Small | **High** — cheap to write, easy to get subtly wrong, and invisible until a replay indexes a move |
| Terminal and draw rules | Medium | Medium — only if the client evaluates them; it may not need to |
| CI wiring for both engines | Small | Medium — the corpus must be read from one place, not vendored into the client |

The two high-risk items are exactly the two the corpus covers most heavily, which is the argument
for building the client engine against it rather than beside it.

---

## 2. Specification traceability

Full table: `specs/game-engine/traceability.md`.

| Status | Count |
| --- | --- |
| VERIFIED | 96 |
| PARTIALLY VERIFIED | 1 (GE-57) |
| DEFERRED | 1 (GE-97) |
| SUPERSEDED | 2 (GE-28, GE-29) |
| **NOT VERIFIED** | **0** |

**One rule had no evidence when the audit began.** GE-25 — "the validator re-derives nothing" —
was demonstrated only by the validator *agreeing* with the generator, which a second
implementation of the rules would also do for a while. `TestTheValidatorHoldsNoRules` now asserts
against the source that the module names no geometry, direction, rank or variant. That was the
audit's single substantive test addition.

GE-28 and GE-29 are permanent gaps in the numbering. A64-014.5 retired the king-refusal rules and
§3.4 keeps them as prose; numbers are never reused.

---

## 3. Corpus coverage

| Shape | Cases | Introduced |
| --- | --- | --- |
| `cases` (legal move sets) | 27 | A64-014.2 |
| `rejections` | 7 | A64-014.3 |
| `terminal_positions` | 5 | A64-014.6 |
| `draw_sequences` | 6 | A64-014.7 |
| `replays` | 11 | A64-014.8 |
| **Total in force** | **56** | across 7 files, 2 versions |

Audited by `test_corpus_audit.py`: every file parses, declares its directory's version, states a
scope and carries exactly one expectation shape; all five shapes are in use; every id is unique
across the whole corpus; every written position and move round-trips through **production**
serialization and the serializer's output is a fixed point; every superseded id names a case that
still exists with a replacement and a reason.

**No coverage gap was found that a corpus case should close**, so none was added. The gaps that
exist cannot be closed by a corpus case:

- No `draw_sequences` case exercises a move limit, because no variant configures one (§8).
- No `replays` case exercises a version-1 replay succeeding, because none can (§5).

**One finding, not a defect.** Some hand-written v1 and v2 entries list pieces in reading order
while the serializer emits them sorted by square. The two describe the same position and read
identically. The audit compares **values, not bytes** — demanding byte equality would force an
edit to `v1`, which the corpus is append-only to prevent. Recorded in
`test_corpus_audit.py::TestTheFilesRoundTrip` and left alone.

---

## 4. Random legal-game testing

`test_engine_fuzz.py` — new in this task. Ten fixed seeds × three variants, each game played to
its natural end from the opening position, every assertion carrying its seed.

| | |
| --- | --- |
| Games played | 30 per sweep, several sweeps per run |
| Observed length | 30–120 plies; every game reached a real terminal state |
| Safety cap | 400 plies, never approached |
| Runtime | 5.4 s |

Asserted after **every** ply: one record per successful move · ply numbers contiguous from 1 ·
the recorded fingerprint is the position reached · `last_move` reads off the log · material never
increases · the turn passes · `0 ≤ plies_since_progress ≤ ply` · occurrence counts within what
the history can hold · every position counted exactly once · no piece on a square the board does
not have · a result exists exactly when the match has ended · a winner exactly when the outcome
is a win · the winner still has pieces · the position played from was not mutated.

Asserted per game: the whole log replays to the same position, result, status, counters and
history; a finished match refuses a further move; the same seed plays the same game. Two seeds
per variant additionally walk **every prefix** and check that `replay(log[:n])` reaches the
position ply *n* recorded, and re-derive the no-progress counter independently of the aggregate's
own bookkeeping.

**No invariant violation was found.**

---

## 5. Replay audit

`ReplayEngine` reconstructs by playing every ply through the same `MoveValidator`, `MoveApplier`,
`TerminalStateEvaluator` and `DrawRuleSet` a live game uses. Confirmed by inspection and test:

| Must never | Evidence |
| --- | --- |
| Infer the engine version | `engine_version_from_primitive` refuses absent, string and boolean input |
| Restore derived counters | `ReplayData` has exactly five fields; a test asserts the serialized keys |
| Skip validation | `Match.play` is the only path; there is no unchecked apply |
| Accept ply gaps | `_require_contiguous` runs before any move is applied |
| Accept moves after termination | `InvalidMatchTransition` → `CorruptMoveLog`, cause chained |

Verified to reproduce, per ply and per game: every intermediate position, every recorded
fingerprint, occurrence counts, progress counters, status transitions, draw and termination
reasons, the final result, and the recorded engine version.

**No replay defect was found.** The version gate is a limitation, not a defect: version 1 had no
draw rules, so replaying a version-1 game under version 2 could end it earlier than it really
ended — AD-15's scenario exactly — and it is refused rather than approximated. Nothing has been
persisted under version 1.

---

## 6. Suppressions and untested branches

### Suppressions

| Kind | Count | Disposition |
| --- | --- | --- |
| `# type: ignore` | 5, all in tests | **Kept.** Each deliberately does something the type system forbids to prove it raises at runtime — writing through a `MappingProxyType`, assigning to a frozen dataclass field. Narrow codes (`[misc]`, `[index]`), never bare |
| `noqa` | 0 | — |
| `pragma: no cover` | 0 | — |
| `xfail` | 0 | — |
| `skipif` | 2 | **Kept.** `ENGINE_PERFT_DEEP` (depth-6 perft, ~4 s/variant) and `lint-imports` not installed. Both name a reason |

No suppression exists in `app/modules/engine/` or `app/modules/game/`. No project-wide rule was
weakened.

### Deliberately untested branches

| Branch | Disposition |
| --- | --- |
| `BoardGeometry.capture_is_mandatory = False` | **Documented extension point.** No variant sets it, so the branch is unreachable in production — and `test_board_variant.py::test_every_variant_obliges_a_capture` pins that fact, so a future variant setting it False fails a test and forces the branch to be covered. Not removed: A64-014.2 asked for the axis by name |
| Draw rules with a configured move limit | **Covered.** `DrawRuleSet.evaluate(rules, history)` takes the configuration, so `test_draw_rules.py` exercises every branch against configurations no variant has |
| `captures_reset_progress` / `man_moves_reset_progress` = False | **Covered**, the same way, via `is_progress` |
| Historical engine-version paths | **Covered.** The supported set is injectable and `test_replay.py::TestVersionResolution` exercises both refusal and acceptance |
| `MaterialPlyLimit.max_kings_per_side` | **Covered** by unit test |

### GE-57 — the one PARTIALLY VERIFIED rule

MT-3 makes the engine version immutable after creation. `Match` is a mutable entity, so
`match.engine_version = …` is not prevented — only unwritten. No engine code writes it after
construction, and a test pins that it is recorded at creation.

**Not fixed here.** Enforcing per-field immutability on a mutable aggregate needs `__setattr__`
machinery, which is invasive for an audit and would sit oddly beside every other aggregate on the
platform. The correct place is the repository that persists a match: an update that changes
`engine_version` should be refused at the boundary, and `database.md`'s `match` relation should
carry the constraint. Recorded as debt.

---

## 7. Architecture

| Check | Result |
| --- | --- |
| `lint-imports` | **16 contracts kept, 0 broken** |
| Engine imports API, persistence, Redis, Celery, WebSocket | **None.** `engine-is-a-dependency-free-kernel` forbids each by name, including stdlib `logging`, `random` and `datetime` |
| Circular imports within `engine` / `game` | **None** (verified by an import-graph walk) |
| `app.platform` imports a bounded context | **No** — contract 1 |
| Public surface deliberate | Yes. `game/public/` is deliberately empty: R-3 says consumers subscribe to events rather than call in |
| Matchmaking coupling | **None.** `app/modules/matchmaking/` contains no source files on this branch; nothing in `engine` or `game` references it, and the kernel contract forbids the reverse |
| Who consumes the engine | Tests only. No production module imports `engine` or `game` yet, which is correct — `matchmaking` is the first that will |

No new contract was needed. Two dead public exports were removed (§9).

---

## 8. Draw-rule decisions

`specs/game-engine.md` §7.7, re-audited against the repository's documentation.

| Threshold | Classification |
| --- | --- |
| Repetition = 3 | **Confirmed and implemented.** domain-model.md states the "three-fold repetition draw rule" throughout, unqualified by variant |
| Ply versus full-move counting | **Confirmed and implemented.** database.md §6.1 names the column `moveless_draw_plies`, which settles the unit as plies |
| `no_progress_ply_limit` | **PRODUCT DECISION REQUIRED.** database.md §6.1 names the column; no document gives it a value |
| `king_only_ply_limit` | **EXTERNAL RULES RESEARCH REQUIRED**, then a product decision. Russian draughts has a fifteen-move king-only rule; this repository does not state it |
| Piece-count-dependent limits | **EXTERNAL RULES RESEARCH REQUIRED**, then a product decision. The bands are nowhere stated |
| International 10x10 draw rules | **EXTERNAL RULES RESEARCH REQUIRED.** They are not the Russian ones. The variant currently carries the same configuration as a **placeholder, not a claim** |

**No threshold was guessed and no engine version was bumped**, because no rule changed. A
documentation-only audit must not move the version, and this one did not.

**Second open question, unchanged:** `DrawReason` distinguishes `NO_PROGRESS`,
`KING_ONLY_MOVE_LIMIT` and `MATERIAL_MOVE_LIMIT`, and all three record as
`TerminationReason.MOVE_LIMIT` because domain-model.md §15 is a closed enumeration fixed by R-19.
A statistic wanting to tell them apart cannot. Widening §15 touches two architecture documents and
a future database constraint, and was not done unilaterally.

---

## 9. `english_8x8` — decision

**Decision: a testing and configuration fixture, not a product variant.**

It was added in A64-014.5 as configuration only — one enum member and one geometry row — and it
earns its place twice over:

| Use | Why it cannot simply be deleted |
| --- | --- |
| **Perft verification** | The published English/American checkers series is the engine's only external oracle. Removing the variant removes the oracle |
| `men_may_capture_backward = False` | The only second value the axis has |
| `kings_fly = False` → `king_reach == 1` | The only second value |
| `mid_sequence_promotion = CROWNS_AND_ENDS_PLY` | The only variant with this rule |
| `capture_obligation` | `ANY`, same as Russian — no additional coverage |

Without it those three axes are settings nothing can distinguish from constants, and
`test_engine_regression.py::TestVariantAxesAreNotConstants` exists to say so.

**What "fixture, not product" means concretely:** it is complete for *move generation* and
nothing else. First mover, draw rules, time controls and rating category are unconsidered. It is
offered nowhere and no product surface should list it. Promoting it to a product variant is a
separate decision that starts with its draw rules.

`BoardVariant.ENGLISH_8X8` is public — the enum has to be, since geometry is keyed on it — so the
decision is recorded here and in `specs/game-engine.md` §5.5 rather than enforced by hiding it.
Anything that enumerates variants for a **player** must filter it.

---

## 10. Performance

Measured on the development machine (Apple silicon, CPython 3.14), against CP-1's **p99 < 25 ms**
for a whole submit-move round trip.

| Operation | Position | Observed | Share of CP-1 |
| --- | --- | --- | --- |
| `legal_moves` | Russian opening | 83 µs | 0.3% |
| `legal_moves` | contrived king-heavy 10x10 | 5.9 ms | 24% |
| `MoveApplier.apply` (validates by generating) | opening | 86 µs | 0.3% |
| Whole ply: apply + terminal evaluation | opening | 170 µs | **0.7%** |
| `Position.fingerprint` | opening | 9 µs | — |
| Replay of a corpus game | with per-ply verification | 125 µs | — |
| `perft(depth=4)` | English opening, 1,469 leaves | 170 ms | — |

**The engine is not the thing that will blow CP-1.** An ordinary ply costs 0.7% of the budget,
which also has to contain transport, authentication, clock charging, persistence and fan-out.

The king-heavy position costs a quarter of the budget alone. It was built in A64-014.5 to be as
awkward as the rules allow — three flying kings against twelve men with every landing square open
— and does not occur in play. It bounds the tail; it does not describe a ply. The structural
guarantee behind it is that a sequence cannot be longer than the opponent's piece count, so the
search is bounded by material rather than by the board.

**No hot path was identified and no optimisation was made.** CLAUDE.md §10.1 forbids optimising
without a measured bottleneck, and there is none. The candidates if one ever appears, in order:
`MoveApplier` validating by regenerating (doubles the cost of an apply), the fingerprint being
recomputed rather than incremental, and `Board` copying its mapping on every operation.

Timing assertions in the suite are one to two orders of magnitude above these numbers — blow-up
detectors, not budgets, so they cannot go flaky on a busy runner.

---

## 11. Dead code and duplication

| Finding | Action |
| --- | --- |
| `DrawRules.repetition_is_enabled` | **Removed.** A public property referenced nowhere, not even in its own module — `DrawRuleSet` checks `repetition_threshold is None` directly |
| `engine.serialization.moves_from_primitive` | **Removed.** Exported and never called |
| Every other `__all__` entry | Resolves and is referenced |
| Duplicate rule evaluation | **None found.** `MoveValidator` asks the generator (now asserted against the source), `MoveApplier` asks the validator, `ReplayEngine` asks `Match.play`, `DrawRuleSet` is the only draw evaluator |
| Duplicate serialization | **None.** `tests/corpus.py` reads through `engine.serialization`, so the corpus and the store share one encoding by construction |
| Temporary guards | **None left.** `UnsupportedPieceMovement` and `_reject_unsupported_pieces` were deleted in A64-014.5 as designed, and a test asserts the name is gone |
| Stale comments | None found. Superseded spec sections (§2.7, §3.4) are marked superseded rather than deleted |

No renaming and no stylistic refactoring was performed.

---

## 12. Test totals

Run with PostgreSQL and Redis available.

| | |
| --- | --- |
| **Passed** | **2,833** |
| **Failed** | **0** |
| **Skipped** | 2 (opt-in depth-6 perft) |
| **xfailed** | 0 |
| Execution time | 146 s full suite; 21 s for `tests/unit` alone |
| Engine + game suites | 926 tests, 3.3 s |

| Gate | Result |
| --- | --- |
| `ruff format --check app tests` | 436 files, clean |
| `ruff check app tests` | clean |
| `mypy app` (strict) | 312 source files, clean |
| `lint-imports` | 16 contracts kept, 0 broken |
| `ENGINE_PERFT_DEEP=1 pytest tests/unit/test_perft.py` | 29 passed |

---

## 13. Remaining technical debt

Ordered by what it costs to leave.

| # | Item | Cost of leaving it |
| --- | --- | --- |
| 1 | **No TypeScript engine** (§1) | AD-14 unmet; no defence against a bug one implementation would make |
| 2 | **Undecided draw thresholds** (§8) | Every decision bumps the engine version and makes earlier games unreplayable. Cheap now, expensive after anything is stored |
| 3 | **International draw rules are a placeholder** (§8) | The variant would ship with Russian's rules, which are not its rules |
| 4 | **Only version 2 replays** (§5) | Correct today; each future bump orphans the previous generation of stored games |
| 5 | **GE-57 not enforced** (§6) | `engine_version` is immutable by convention. The repository boundary is where to enforce it |
| 6 | **`MoveApplier` validates by regenerating** | Doubles the cost of an apply. Not a bottleneck at 0.7% of CP-1 |
| 7 | **`PositionHash` is a fingerprint, not a Zobrist hash** | Correct and O(pieces). Only matters if a search is ever built |
| 8 | **No published Russian perft table** (§2) | Depths 5+ are a regression baseline rather than a verification |
| 9 | **Corpus piece ordering not canonical** (§3) | Cosmetic. Normalise if a `v3` is ever opened for other reasons |
| 10 | **`TerminationReason` conflates three move-limit draws** (§8) | A statistic nobody can compute later |
| 11 | **`Match` is missing seats, clocks, offers, sequence numbers, events** | Known and deliberate — the rest of domain-model.md §10.4, owned by the tasks that build transport and persistence |

---

## 14. Before matchmaking integration

1. **Nothing here blocks it.** Matchmaking needs the engine to create and play games; it does.
2. `matchmaking` will be the **first production consumer** of `game`. `game/public/` is
   deliberately empty, so publishing whatever it needs is a decision to make once, in that task,
   rather than by reaching into `game.domain`.
3. `.importlinter`'s `game-internals-are-private` contract already covers the modules that exist.
   `matchmaking` must be added to its source list in the same change.
4. **Decide the draw thresholds before anything persists** (§8) — after that, each decision
   orphans stored games.
5. **Do not offer `english_8x8` to players** (§9).
6. The engine's collaborators — `MoveGenerator`, `MoveValidator`, `MoveApplier`,
   `TerminalStateEvaluator`, `DrawRuleSet`, `ReplayEngine` — are stateless and safe to share. One
   instance each, wired at the composition root.
