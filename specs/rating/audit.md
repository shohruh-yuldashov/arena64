# Rating — Audit and Stabilisation

> **Status:** Complete — A64-017.6, the closing task of the Rating Epic
> **Owner:** _Unassigned_
> **Audited:** 2026-08-05
> **Scope:** `apps/api/app/modules/rating/`, the `game`, `matchmaking` and `profiles` surfaces it
> touches, `specs/rating.md`, `specs/leaderboard.md`, `docs/07-decisions/ADR-001`
> **Related:** [`specs/rating.md`](../rating.md), [`specs/leaderboard.md`](../leaderboard.md),
> [`ADR-001`](../../docs/07-decisions/ADR-001-glicko2-incremental.md),
> [`specs/live-game/audit.md`](../live-game/audit.md)

## Readiness

# READY WITH DOCUMENTED LIMITATIONS

A match completes, both players' ratings move exactly once from the values captured when they sat
down, the change is explainable from stored data alone, the ladder reflects it in the same
transaction, and a redelivered event changes nothing. Every step is covered by tests that run the
real services against real PostgreSQL.

**One defect was found and fixed by this audit** (§1) and it was severe: the rating consumer had
no caller, so no rating on this platform would ever have moved.

Four limitations remain, each a decision rather than an oversight:

1. **Rating seasons are deferred** (§7). Product decision C-5, blocked on four unresolved
   questions. Not a gap — the feature is out of v0.5.0 scope, which is why it does not make this
   classification `NOT READY`.
2. **PR-5's pending queue is not built** (§8). A frozen rating refuses an adjustment and the
   adjustment is *lost*, not queued.
3. **`rating_adjustment` grows without bound** (§6). Correct — it is the audit trail A-4 depends
   on — but nothing had stated the sizing consequence.
4. **Metrics are still structured log records** (§9). Carried over from A64-015.6.

---

## 1. The defect: a consumer with no caller

**Finding: `MatchRatingService` had no caller outside tests. Fixed.**

Evidence, before the fix:

```
$ grep -rn "MatchRatingService" app --include="*.py" | grep -v match_rating_service.py
(no result)
```

A64-017.3 built the service, tested it against every case §9 names, and never registered an
`EventHandler` for `game.match_completed`. So a match would complete, the outbox row would be
written, the relay would find no consumer that `handles()` it — and **no rating would ever move**.
`rating.player_rating` would have stayed empty, the ladder would have stayed empty, and
`matchmaking` would have kept pairing everybody at 1500.

Every part worked. That is what makes this class of defect dangerous, and it is the **second time
in two epics**: A64-016.8 found the identical shape in the cross-node bus, where `consume()` was
written, tested, and called by nobody.

**The fix:** `MatchCompletionConsumer` (`rating/application/services/`), registered in
`app_factory` beside the other outbox consumers with its own `processed_event` partition.

**The lesson, stated because it will recur:** a unit test proves a component works; nothing in
this repository proves a component is *reachable*. Both defects would have been caught by one
assertion — that every service with a `handle`/`consume` entry point is named by a composition
root. That check does not exist and is the single most valuable thing to add next.

---

## 2. End-to-end flow

| Step | Mechanism | Proven by |
| --- | --- | --- |
| Rating read at pairing | `matchmaking` → `rating.public.RatingReader` | `test_rating_persistence.py` |
| Seat snapshot captured at creation | `CreateMatchRequest` → `game.match` columns | `test_rating_persistence.py`, `test_schema_drift.py` |
| Completion carries the snapshots | `MatchCompleted` payload, expanded additively | `test_match_rating.py::TestTheConsumer` |
| Consumer decodes and applies | `MatchCompletionConsumer` → `MatchRatingService` | `TestTheConsumer` |
| Both players, one transaction | one unit of work over both saves | `test_a_failure_on_the_second_player_leaves_the_first_untouched` |
| Exactly-once | `uq_rating_adjustment__player_match` | `tests/contract/test_rating_persistence.py` |
| `rating.updated` published | transactional outbox, same transaction | `TestExactlyOnceAndAtomicity` |
| Ladder reflects it | derived query over the same rows | `tests/contract/test_leaderboard.py` |
| Profile still renders | `RatingCategory` alias at one boundary | `test_rating_persistence.py` |

**No step is a stub**, and since §1 no step is unreachable.

---

## 3. PR-3 — the input is the seat snapshot

The epic's most important correctness property, and the one that was briefly wrong.

A64-017.3's first implementation called `PlayerRating.applied` on the **loaded** aggregate, which
computes from whatever the player rates now. Every single-match test passed, because the stored
value and the snapshot agree when only one game has been played.
`test_the_snapshot_is_used_even_when_the_stored_rating_has_moved_on` stores 2400 and 900 against a
snapshot of 1500 apiece, and failed.

`PlayerRating.based_on` is the fix and makes the split structural: **counters** from the stored
row, **triple** from the snapshot. That is also what makes two matches completing at once safe —
neither sees the other's partial result, because neither reads a current rating at all.

---

## 4. Security

| Check | Result |
| --- | --- |
| Identifiers in metric labels | **None** — there are no metrics yet (§9) |
| Ratings or player ids in logs | Player ids appear in `extra`, never in a label; no rating value is logged |
| `rating.updated` payload | Key, triples, delta, counters, algorithm version. **No match detail beyond its id** — no outcome, no opponent, no move count |
| Private data on the leaderboard | Player id and rating only. No handle, avatar or country |
| Writable public surface | **None.** `RatingReader` and `LeaderboardReader` have no write, rebuild or invalidation method |

Ratings are public by existing policy (`profiles`' privacy settings say so explicitly and do not
cover them), so an adjustment exposing an opponent's rating at match start discloses nothing new.

---

## 5. Concurrency

| Contention | Mechanism |
| --- | --- |
| Two deliveries of one completion | `uq_rating_adjustment__player_match`, translated to `AdjustmentAlreadyApplied` and treated as success |
| Two matches for one player at once | No row lock. Each is a legitimate second game; the unique index stops either applying twice, and PR-3 means neither reads the other's partial state |
| The two seats of one match | One transaction — a partial update is impossible |
| A frozen seat | Both aggregates checked **before** either write, so the refusal is a property of the match rather than of processing order |
| The consumer's batch | Sequential, so two completions sharing a player cannot deadlock on the same aggregate |

**No in-memory deduplication anywhere.** A seen-set is an answer a restart forgets and a second
process never had.

---

## 6. Retention

**Finding: `rating.player_rating` and `rating.rating_adjustment` are never deleted.**

That is correct. PR-4 makes the adjustment the answer to *"why did I lose 14 points"*, and an
audit trail with a horizon answers it only for recent disputes. The foreign key is
`ON DELETE RESTRICT` precisely so deleting a rating cannot silently delete the record of how it
got there.

**The consequence, stated because nothing stated it before:**

| Relation | Grows with | Bounded by |
| --- | --- | --- |
| `player_rating` | Distinct `(player, key)` pairs that have played | Players × reachable keys — one key today |
| `rating_adjustment` | **Two rows per rated match, ever** | Nothing |

At a million rated games that is two million rows, served by
`ix_rating_adjustment__player_history` for the only query that reads them. Ordinary at this size,
and unbounded in principle. The first move when it matters is monthly range partitioning on
`applied_at` — the access pattern is "this player's recent history", never a date range across
players.

---

## 7. Seasons — A64-017.5, deferred by product decision

**Not skipped, and not a gap.** The epic plan lists A64-017.5 *Rating Seasons*; product decision
C-5 removed it from v0.5.0 in the same round that scoped the rest of this specification, in these
words: *"Do not implement Season behaviour. Do not create automatic seasons. Do not create reset
logic. Only make the data model forward compatible."*

What exists is exactly that and nothing more: nullable `season_id` on both relations, always
`NULL`, carried through the aggregate and never read. No entity, repository, service, lifecycle,
reset or rollover.

**This does not make the epic `NOT READY`.** A feature that is out of scope by decision is not a
missing feature; the classification reflects what v0.5.0 set out to deliver.

Four decisions block it, and are recorded in `specs/rating.md` §12.2: what opens and closes a
season, **what happens to ratings at a boundary**, whether finished standings are retained, and
who fills `season_id`. The second is the blocking one — a reset rewrites the permanent record A-4
protects, so guessing it would mean writing logic that edits played history.

---

## 8. Frozen ratings — the limitation that is carried, not closed

`is_frozen` exists, refuses adjustment, and **nothing sets it**: `fairplay` does not exist.

PR-5's full rule queues the refused matches and applies or discards them when the case resolves.
That queue is **not built**, so a refused adjustment is **lost**. The consumer does not retry it
either — retrying would spin until a hold is lifted by a module that does not exist.

Recorded here and in `specs/rating.md` §13 rather than discovered later, because retrofitting the
queue cannot recover what was refused before it existed.

---

## 9. Observability

**Finding: `rating` emits no metrics.** SPEC-RATING §17 specifies four signals — adjustments
applied, refusals by reason, RD inflation hitting the ceiling, and update latency — and none is
implemented.

Not fixed in this audit, deliberately: adding a metrics recorder to `MatchRatingService` is a
change to a service the audit is meant to assess, and the counters have no consumer until an
exporter exists (carried over from `specs/matchmaking/audit.md` §7). It is the first item in §11.

What does exist: `MatchRatingOutcome` returns a bounded reason from every path, so the call sites
are ready and only the recorder is missing.

---

## 10. Architecture

| Rule | Result |
| --- | --- |
| `rating` internals private | `rating-internals-are-private` — no module reaches past `rating.public` |
| `rating` reaches others through `public` | `rating-reaches-modules-through-public` |
| Layers point inward | `layers-rating` |
| `game` does not import `rating` | Held — `game` stores the seat snapshot and has no reader |
| R-4's one-way chain | `game → rating → leaderboard`. The leaderboard is a query over `rating`'s own relation, so the arrow cannot reverse |
| Domain is framework-free | Glicko-2 and both aggregates import nothing but the standard library |

**24/24 contracts pass**, three of them added by this epic.

One structural note: the leaderboard is a **derived read**, not a projection table
(`specs/leaderboard.md` §6). It is consistent with the source by construction rather than by a
relay, which is why §2's "ladder reflects it" row has no lag column.

---

## 11. Remaining technical debt

| Item | Cost today | First move |
| --- | --- | --- |
| **No reachability check** (§1) | Two epics, two unreachable components | Assert every handler/consumer is named by a composition root |
| No metrics (§9) | No visibility into refusals or duplicates | Wire `MetricsRecorder` into `MatchRatingService` |
| `rating_adjustment` unbounded (§6) | None at current scale | State a policy; partition on `applied_at` when it matters |
| Seasons deferred (§7) | None — out of scope | Answer S-2 first |
| Frozen queue absent (§8) | A refused adjustment is lost | Build with `fairplay`, not before |
| Speed class fixed to `CLASSICAL` | One reachable key | `specs/rating.md` OQ-1 — the boundaries |

---

## 12. Epic task mapping

| Task | Status |
| --- | --- |
| A64-017.1 Rating Domain | **COMPLETED** |
| A64-017.2 Rating Persistence and Integration | **COMPLETED** |
| A64-017.3 Match Completion Processing | **COMPLETED** |
| A64-017.4 Leaderboard | **COMPLETED** |
| **A64-017.5 Rating Seasons** | **DEFERRED BY PRODUCT DECISION** — §7 |
| A64-017.6 Rating Audit | **COMPLETED** |

---

## 13. Before the next epic

1. **Add the reachability check.** It is the only item here that would have prevented a defect
   this audit had to find by hand, twice.
2. **Answer S-2** — what happens to ratings at a season boundary — before anyone starts A64-017.5.
   Everything else about seasons follows a decision; that one *is* the decision.
3. **Wire the metrics** before the first real traffic, so the duplicate and refusal rates are
   visible from the start rather than reconstructed from logs afterwards.
