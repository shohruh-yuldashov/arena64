# ADR-001 — Glicko-2, applied incrementally, with lazy RD inflation

| Field | Value |
| --- | --- |
| **Status** | Accepted |
| **Date** | 2026-08-04 |
| **Deciders** | Product owner |
| **Consulted** | — |
| **Supersedes** | — |
| **Superseded by** | — |
| **Related** | [`specs/rating.md`](../../specs/rating.md), [`domain-model.md`](../01-architecture/domain-model.md) §11, §18 Q-3, Q-5 |

---

## Context

`domain-model.md` §18 listed **Q-3 — "Elo or Glicko-2 (or another system)?"** among the questions
that *"cannot be resolved by an engineer"*, and §20's checklist required an answer *before database
design began*. It was never answered. The consequences accumulated:

- `rating` was never built. `matchmaking` paired every player against a hardcoded 1500
  (`ProvisionalRatingProvider`), and `profiles` reported the same constant to every visitor.
- `domain-model.md` §11.3 made the `RatingPeriod` entity **conditional on this decision** and said
  so rather than guessing — so the model had a hole in it by design.
- `profiles.domain.ratings` shipped a rating as an *object* rather than a number, explicitly so
  that a triple could be added later without breaking a client.

Three constraints applied:

1. **`architecture.md` A-4** — ratings are competitive and permanent. A corrupted result is
   unacceptable, and a rating system swapped after launch invalidates every historical adjustment,
   because the inputs the old system recorded are not the inputs the new one needs.
2. **`domain-model.md` R-17** — a rating is stored as a triple (value, deviation, volatility) even
   if the launch algorithm uses only the first. Retrofitting uncertainty is impossible: the
   deviations that produced each historical change were never recorded.
3. The platform has one variant, no time-control selection, and a small population. Whatever
   ships must be correct at ten players and at ten million.

## Decision

> We will use **Glicko-2** with the standard parameters, apply it **incrementally** — one update
> per completed rated match, with **no rating periods** — and handle inactivity by **inflating
> rating deviation lazily** at the moment a player's next rated match is processed.

Concretely, and specifically enough that a future change is recognisable as a violation:

| Parameter | Value |
| --- | --- |
| Initial rating | 1500 |
| Initial RD | 350 |
| Initial volatility (σ) | 0.06 |
| System constant (τ) | 0.5 |
| Nominal rating period | 1 day, used **only** for RD inflation |
| RD ceiling | 350 |
| Provisional threshold | 25 rated matches per key |

Ratings are keyed by `(ProductVariant, SpeedClass)`. `RatingPeriod` **does not exist** as an
entity. No scheduled worker writes to a rating, ever.

## Options Considered

### Option 1 — Glicko-2, incremental, lazy RD inflation *(chosen)*

**Summary:** Full Glicko-2 arithmetic, run once per match rather than once per period, with
uncertainty growth computed from elapsed time at the next match instead of by a sweeper.

| Pros | Cons |
| --- | --- |
| Uses all three stored values, so R-17's triple is the model rather than a hedge | A deviation from the paper, which specifies periods — the deviation must be documented and understood |
| Rating is current the instant a game ends; no window where the ladder disagrees with reality | The volatility solver runs per match rather than per period — more arithmetic, though still microseconds |
| No scheduled writer, so no job to fail, monitor, or scale | Inactivity is only corrected when a player returns, so a stale RD is *displayed* until then |
| Well-precedented — this is how live chess platforms run Glicko-2 | |

### Option 2 — Elo

**Summary:** A single number and a K-factor.

| Pros | Cons |
| --- | --- |
| Trivial to implement and to explain to a player | Discards R-17's deviation and volatility — they would be stored and never used, which is worse than not storing them |
| No solver, no convergence concerns | Cannot express confidence, so PR-6's provisional mark has no basis beyond a game count |
| | Migrating Elo → Glicko-2 later is impossible without inventing the historical deviations |

### Option 3 — Glicko-2 with real rating periods

**Summary:** Batch matches into windows and rate them together, as the paper specifies.

| Pros | Cons |
| --- | --- |
| Faithful to the published algorithm; volatility estimates are better with 10–15 games per period | A player's rating does not move until the period closes — for a small population that is hours or days of a visibly stale ladder |
| RD inflation falls out naturally, with no special mechanism | Requires the `RatingPeriod` entity, a scheduler, and a batch that must be exactly-once across a fleet |
| | Introduces a second failure mode: a period that fails to close silently freezes every rating |

### Option 4 — Do nothing

Keep `ProvisionalRatingProvider`'s constant 1500.

Every player is paired against every other player at random, the leaderboard cannot exist, and
`profiles` continues to report a number that is not a measurement. The platform has no competitive
product. This option is evaluated only to be rejected.

## Rationale

Three criteria decided it.

**Irreversibility (A-4).** Option 2 is the only one that cannot be undone. Elo records no
uncertainty, so a later move to Glicko-2 would have to invent the deviations behind every
historical adjustment or discard the history. Options 1 and 3 differ in *scheduling*, which is
changeable; Option 2 differs in *what is recorded*, which is not.

**Staleness.** Option 3's correctness comes at the cost of a ladder that lags reality by a period.
For a platform whose whole competitive loop is "finish a game, see the effect", that is the wrong
trade at this size.

**Operational surface.** Option 3 needs a scheduler that must be exactly-once across a fleet, and
its failure mode — a period that quietly stops closing — freezes every rating on the platform
while every metric looks healthy. Option 1 has no such component. Lazy inflation buys that at the
cost of an RD that is stale *in display* until the player returns, which affects nobody's
calculation, because the calculation happens at return time.

## Consequences

### Positive

- `RatingPeriod` is removed from the domain model rather than left conditional (`domain-model.md`
  §11.3, §16.3). One fewer entity, and one fewer open question.
- No scheduled writer means no job to monitor, no batch to make idempotent, and no fleet-wide
  coordination.
- The triple is used, so R-17's storage requirement and the algorithm agree.

### Negative

- **Running Glicko-2 with one match per period is a documented deviation from the paper.**
  Volatility estimates are noisier than they would be with 10–15 games per period. Accepted:
  τ = 0.5 is at the conservative end of Glickman's recommended range precisely to damp this.
- **A displayed RD can be stale.** A player absent for a year shows the RD they left with until
  their next rated match. No calculation uses the stale value — inflation happens before the
  update — but a leaderboard or profile reading RD sees an optimistic number.
- **PR-5's pending queue is not built.** A frozen rating refuses an adjustment and the adjustment
  is *lost*, not queued. Recorded in `specs/rating.md` §13.

### Neutral

- Every `SpeedClass` member exists in the model from day one while only `CLASSICAL` is reachable
  (`specs/rating.md` §8). Storing five enum members costs nothing; adding one later is a migration
  of a permanent record.
- `profiles`' `RatingCategory` becomes a presentation-layer mapping rather than a domain concept.

## Impact

| Area | Impact |
| --- | --- |
| Architecture | New `rating` bounded context. `game` → `rating` → `leaderboard` (R-4) becomes real rather than planned. No new scheduled worker |
| Data model | `rating.player_rating`, `rating.rating_adjustment`. Seat rating snapshots on `game.match`. `game.match_completed` expanded additively. Nullable `season_id` on both rating relations |
| Security | None. Ratings are public by existing policy; the adjustment exposes an opponent rating that was already public |
| Operations | No new job to run. One new consumer on `q.critical` |
| Developer workflow | `matchmaking`'s `RatingSnapshotProvider` port is satisfied by `rating.public` instead of a constant, and widens to carry the triple |

## Compliance & Enforcement

| Mechanism | Enforces |
| --- | --- |
| `import-linter` contract `rating-internals-are-private` | No module reaches past `rating.public` |
| Unique `(player_id, match_id)` on `rating.rating_adjustment` | PR-1's exactly-once, at the database rather than in code |
| Unit test against **Glickman's published worked example** | That the implementation is Glicko-2 and not something that merely behaves plausibly |
| `algorithm_version` stored on every adjustment | That a future retune cannot make history inexplicable |
| Absence of any `PeriodicTaskScheduler` for `rating` | That no scheduled writer was reintroduced |

## Follow-Up Actions

- [ ] Answer `specs/rating.md` OQ-1 — speed-class boundaries — before any class beyond `CLASSICAL`
      is activated
- [ ] Answer OQ-4 — fair-play freeze semantics — before `fairplay` ships, so refused adjustments
      have a defined fate
- [ ] Remove the deprecated `ratings.{classic,rapid,blitz}` profile field in a major version

## Revisit Criteria

Revisit this decision if any of the following becomes true:

1. **Population growth makes per-match volatility estimates visibly noisy** — measured as
   volatility oscillating across consecutive matches for established players. The fix is periods
   (Option 3), and the data model already supports it because `RatingAdjustment` records the
   inputs.
2. **Correspondence time controls ship** (`domain-model.md` Q-19). A match lasting weeks makes
   "days since last rated match" a poor inflation clock.
3. **A second variant ships**, multiplying the reachable keys. Nothing here breaks, but the
   assumption that a player has one or two ratings stops holding.
4. **Glickman publishes a successor system.** `algorithm_version` exists so this is a
   forward-only change rather than a rewrite of history.
