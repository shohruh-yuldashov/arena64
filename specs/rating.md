# Feature Specification — Rating

| Field | Value |
| --- | --- |
| **Spec ID** | `SPEC-RATING` |
| **Status** | Approved |
| **Owner** | _Unassigned_ |
| **Created** | 2026-08-04 |
| **Last updated** | 2026-08-05 |
| **Related ADRs** | `docs/07-decisions/ADR-001-glicko2-incremental.md` |
| **Related specs** | [`leaderboard.md`](./leaderboard.md), [`matchmaking.md`](./matchmaking.md) |

---

## 1. Summary

A player's measured skill in one **rating key** — `(ProductVariant, SpeedClass)` — computed with
**Glicko-2**, applied **incrementally** on each completed rated match, and recorded as a
permanent, auditable adjustment.

This document resolves `domain-model.md` §18 **Q-3** and the rating half of **Q-5**. Every figure
below is a product decision, not an engineering preference; where a decision was deliberately not
made, §19 says so rather than leaving a default to be discovered in code.

---

## 2. Motivation

`architecture.md` A-4: *"Ratings are competitive and permanent — a corrupted result is
unacceptable."* Everything the platform sells is downstream of one number per player per key:
pairing quality (`matchmaking` QT-2), the leaderboard, and the player's own sense that the ladder
means something.

Until this spec, `rating` did not exist. `matchmaking` paired against a constant
(`ProvisionalRatingProvider` returns 1500 for everybody) and `profiles` reported the same constant
to every visitor. The seam was built and documented; this fills it.

---

## 3. Goals

| # | Goal |
| --- | --- |
| G-1 | One rating per `(player, variant, speed class)`, as a Glicko-2 triple |
| G-2 | Exactly one rating adjustment per completed rated match, enforced at the database |
| G-3 | Every adjustment is explainable from stored data alone, without re-running the algorithm |
| G-4 | Inactive players become less certain over time, with **no scheduled writer** |
| G-5 | `matchmaking` pairs on real ratings by satisfying the port it already holds |
| G-6 | The existing public profile contract keeps working unchanged |

---

## 4. Non-Goals

| # | Not in v0.5.0 | Why |
| --- | --- | --- |
| N-1 | Rating periods | Updates are incremental — §7.3 |
| N-2 | Season behaviour, resets, rewards | §12; only the nullable column exists |
| N-3 | Fair-play integration | §13; only `is_frozen` exists |
| N-4 | Speed-class *derivation* from a duration | §8; a control **carries** its class, and A64-020.5A-pre made time-control selection real without ever inventing a boundary |
| N-5 | Rating decay of the **value** | Only *deviation* inflates — §7.4 |
| N-6 | Changing the public profile response shape | §14 |

---

## 5. User Stories

| # | As a… | I want… | So that… |
| --- | --- | --- | --- |
| US-1 | player | my rating to move when I finish a rated game | the ladder reflects how I actually play |
| US-2 | player | to see why my rating changed by that amount | a surprising change is answerable, not mysterious |
| US-3 | player | to be marked provisional until I have played enough | nobody reads my first-week rating as a measurement |
| US-4 | returning player | my rating to be treated as less certain after a long absence | I am re-measured rather than assumed unchanged |
| US-5 | matchmaker | a deterministic rating per pool | the same scan pairs consistently |

---

## 6. Acceptance Criteria

| # | Criterion |
| --- | --- |
| AC-1 | A completed rated match produces exactly one `RatingAdjustment` per player, ever, under concurrent and retried delivery |
| AC-2 | A cancelled, aborted or casual match produces none |
| AC-3 | Both players' new ratings are computed from the triples captured at match creation, never from current values |
| AC-4 | A player with fewer than 25 rated matches in a key is reported provisional everywhere |
| AC-5 | A player returning after an absence has a larger RD than when they left, without any scheduled job having run |
| AC-6 | A frozen rating refuses adjustment and the refusal is visible |
| AC-7 | `GET /profiles/{handle}` returns the same JSON shape as before this spec |

---

## 7. Domain Model

### 7.1 `RatingKey` — the identity of a rating

```
RatingKey = (ProductVariant, SpeedClass)
```

`domain-model.md` DM-10, realised directly. **There is no `RatingCategory` entity**: the key is a
value object built from two enums the platform already owns, so a rating, a leaderboard row and a
match all spell the same key the same way with nothing to keep in step.

`SpeedClass` carries all five members from day one — `BULLET`, `BLITZ`, `RAPID`, `CLASSICAL`,
`CORRESPONDENCE` — even though §8 makes only one reachable. Adding an enum member later is a
migration of a permanent competitive record; carrying five costs nothing.

### 7.2 `Glicko2Rating` — the triple

| Field | Meaning | Initial |
| --- | --- | --- |
| `value` | The rating | **1500** |
| `deviation` | RD — how uncertain the value is | **350** |
| `volatility` | σ — how erratic the player's results are | **0.06** |

`domain-model.md` R-17 required storing the triple even if the launch algorithm used only the
first. Glicko-2 uses all three, so this is the model rather than a hedge.

**System constant τ = 0.5.** Glickman's paper recommends 0.3–1.2 and says smaller values prevent
volatility from changing by large amounts. A domain constant, not configuration: two deployments
with different τ produce incomparable ratings.

### 7.3 Update model — incremental, one match at a time

Each completed rated match immediately produces exactly one update per player. **No
`RatingPeriod` entity exists** (`domain-model.md` §11.3, §16.3 — its existence was conditional on
this decision, and the decision is no).

Glicko-2 is specified over rating periods, and running it with one match per period is a
deliberate, well-precedented deviation. Its consequence is §7.4.

### 7.4 Lazy RD inflation — the only inactivity mechanism

Glicko-2 step 6 (`φ* = √(φ² + σ²)`) inflates uncertainty for each elapsed period. With no
periods, an inactive player's RD would never grow and a two-year-old rating would be treated as
freshly measured.

So RD is inflated **at read time on the next rated match**, from the elapsed time since that
key's previous rated match:

```
elapsed_periods = days_since_last_rated_match / RATING_PERIOD_DAYS
φ*              = √(φ² + σ² · elapsed_periods)
```

| Property | Value |
| --- | --- |
| `RATING_PERIOD_DAYS` | **1** — one nominal period per day |
| Ceiling | RD never exceeds its initial 350 |
| Writer | **None.** No scheduled job, no periodic write |
| First match in a key | No inflation; there is no previous match |

The ceiling matters: without it an absent player's RD grows without bound and their return match
moves their rating almost arbitrarily. 350 is the same figure a brand-new player carries, which is
the honest maximum — "we know nothing about this player" cannot be truer than for someone who has
never played.

### 7.5 `PlayerRating` — aggregate root

**Content.** Player, key, the triple, `is_provisional`, `is_frozen`, `games_played`, peak value
and when, `last_rated_at`, `season_id`.

**Lifecycle.** Created on the player's **first rated match** in the key — never at registration,
because a rating with no games is a claim, not a measurement. A player with no row is reported at
the initial triple, provisional, zero games.

```
(absent) ──first rated match──> Provisional ──25 rated matches──> Established
                                     │                                │
                                     └──────── freeze ────────────────┘
                                                  │
                                               Frozen
```

**Business rules** — `domain-model.md` PR-1…PR-6, with this spec's resolutions:

| # | Rule | Resolution here |
| --- | --- | --- |
| PR-1 | A match affects a rating exactly once, enforced at the database | Unique `(player_id, match_id)` on `rating.rating_adjustment` — §10 |
| PR-2 | Only completed, rated matches with two different players | §9 lists exactly which terminations qualify |
| PR-3 | Adjusted from the values captured at match start, never current values | §7.6 |
| PR-4 | Every adjustment records the inputs that produced it | §7.7 |
| PR-5 | A frozen rating accepts no adjustments | §13 — refuses; no pending queue in v0.5.0 |
| PR-6 | Provisional ratings are visibly marked everywhere | **< 25 rated matches in the key** |

**Provisional threshold: 25 rated matches**, counted per key. A product decision, not a Glicko-2
one — the algorithm expresses confidence as RD, and a player's RD may still be large at 25 games.
Both are reported; they answer different questions.

### 7.6 The seat snapshot — PR-3

At match creation each seat stores the player's **complete triple** for the match's key. Rating
calculation reads those persisted values and **never** the player's current rating.

Without this, two matches completing concurrently each compute against the other's partial result
and neither is reproducible. With it, an adjustment is a pure function of data recorded before the
game was played.

The snapshot is captured by `matchmaking`, which already reads a rating to stamp the queue ticket
(QT-2), and travels to `game` on the match-creation request. **`game` never reads a rating**
(`services.md` §10.2: gameplay core may not depend on projections).

### 7.7 `RatingAdjustment` — entity within `PlayerRating`

Immutable, one per `(player, match)`. Records: match, key, the triple **before** and **after**,
the opponent's triple, expected score, actual score, `algorithm_version`, `season_id`, and when.

**Why `algorithm_version` is stored:** rating systems get retuned. Without it a retune makes every
historical adjustment inexplicable — the stored numbers no longer follow from any algorithm the
platform can run — and the rating history becomes undefendable in a dispute.

**This is not "rating history".** History is what you get when you order adjustments by time. The
entity is the adjustment; the history is the query. Naming the query would invite a second,
denormalised write path that eventually disagrees.

---

## 8. Speed class

**A rated match belongs to the class its time control names** — A64-020.5A-pre. Four of the five
members of `SpeedClass` are reachable, one per seeded control (`reference.time_control`), and
`CORRESPONDENCE` remains storable and unreachable pending a clock model the flag worker does not
have.

This section previously read "every rated match belongs to `CLASSICAL`", and the extension point
it described is what shipped — with one correction worth recording, because it is the whole reason
`reference` exists as a module.

**The class is not derived; it is looked up.** The old text said `SpeedClass` would be "derived
from `(base_time, increment)` at match creation". It is not, and must not be: §19 leaves the
boundaries between bullet, blitz and rapid an open product decision, so a derivation would be
whichever module ran it guessing them for every rating on the platform. Instead each catalogue row
*carries* its class, the player picks a row, and the choice travels as data:

    reference.time_control    the mapping, one row per offered control
    QueuePool                 carries the identifier — pool identity
    QueueTicket               carries a snapshot: base, increment, class
    CreateMatchRequest        carries base and increment
    SeatRating.speed_class    the class the seat was rated in

`DEFAULT_SPEED_CLASS` survives as `RatingKey.of`'s answer for a caller with no control in hand —
the leaderboard's unqualified default — and is no longer on the pairing path. Before this change
it was, and every rated match on the platform would have been recorded as `classical` whatever the
players chose.

**What did not change**, exactly as this section predicted: the key, the storage, the leaderboard
and the profile mapping were already per-class, and none of them was touched.

---

## 9. Which matches rate

A rating update is produced when **all** are true:

1. The match is rated
2. The match completed normally
3. The two players are different identities
4. The match was not cancelled
5. The match was not aborted before becoming official

| Termination | Rated |
| --- | --- |
| Normal win | ✓ |
| Draw | ✓ |
| Resignation | ✓ |
| Timeout | ✓ |
| Cancelled | ✗ |
| Aborted | ✗ |
| Administrative cancellation | ✗ |

**A disconnected player who eventually loses on time is rated.** Disconnection is not a
termination — the clock is, and it ran out. Treating it otherwise would make disconnecting a way
to avoid a loss, which is the cheapest rating-manipulation attack there is.

**"Two distinct participants" means two different player identities.** It does **not** exclude
provisional players — reading it that way would mean nobody can ever complete the 25 matches that
end provisional. Self-play is impossible by platform rules (`matchmaking` never pairs a player
with themselves), so this is a guard against a data defect rather than a policy.

---

## 10. Persistence

| Relation | Purpose |
| --- | --- |
| `rating.player_rating` | The aggregate. Unique `(player_id, variant, speed_class)` |
| `rating.rating_adjustment` | The permanent per-match record. **Unique `(player_id, match_id)`** |

**The unique index is the exactly-once mechanism** (PR-1, `repositories.md` BE-06), not a safety
net over application logic. A redelivered `game.match_completed` inserts a duplicate, violates the
constraint, and the handler treats that as success — the work was already done. Checking first and
inserting second has a window that concurrent delivery lands in.

Both relations are PostgreSQL and **never Redis** (AD-19, caching.md C-5): a rating that exists
only in Redis is one an eviction policy can delete, with no recovery path.

---

## 11. Events

| Event | Direction | Carries |
| --- | --- | --- |
| `game.match_completed` | consumed | Match, variant, rated, outcome, termination, winner, **both players and their seat snapshots** |
| `rating.updated` | published | Player, key, triple before and after, match, provisional flag |

`game.match_completed` is **expanded additively** so `rating` needs no read-back: the payload gains
player identities and seat snapshots and loses nothing, so every existing consumer keeps working
(`services.md` §10.2 — payloads are bounded and self-contained).

Published through the transactional outbox (AD-16) inside the adjustment's own transaction, so a
rating that was applied is always announced and one that was not never is.

---

## 12. Seasons — **A64-017.5: DEFERRED BY PRODUCT DECISION**

> **Status:** Deferred, not skipped. Deliberately out of v0.5.0 scope by the C-5 decision, and
> **blocked on unresolved product decisions** — §12.2.

Only the **data model** is forward compatible. `season_id` is a nullable column on
`player_rating` and `rating_adjustment`, always `NULL` in v0.5.0.

No `Season` entity, no repository, no service, no lifecycle, no reset and no rollover logic. The
column exists because a rating adjustment is permanent: a season introduced later cannot be
written onto adjustments that have already happened, so the field has to exist before the first
one is recorded.

### 12.1 Why it was deferred rather than missed

The epic's plan lists **A64-017.5 Rating Seasons** as a task. It was not built, and it was not
overlooked: product decision **C-5** removed it from v0.5.0 in the same round that scoped the
rest of this specification, in these words —

> Introduce nullable `season_id` where required. Do **not** implement Season behaviour. Do **not**
> create automatic seasons. Do **not** create reset logic. Only make the data model forward
> compatible.

Building it during A64-017.5 would have contradicted a decision marked final. The task is
therefore **deferred by product decision**, and the audit records it as a documented limitation
rather than a gap (`specs/rating/audit.md` §7).

### 12.2 What must be decided before it can be built

| # | Question | Why it blocks |
| --- | --- | --- |
| S-1 | What opens and closes a season? | Operator action, a schedule, or neither — this decides whether a worker exists at all |
| S-2 | What happens to ratings at a boundary? | Full reset, soft reset toward the mean, or nothing. **This one touches A-4 data**: a reset rewrites the permanent record, so guessing it would mean writing logic that edits played history |
| S-3 | Are a finished season's standings retained? | If yes they are a permanent competitive record and need their own retention policy |
| S-4 | Who fills `season_id`, and when? | The active season at write time, or a backfill — and a backfill over immutable adjustments is a contradiction that needs resolving before the first row is written |

S-2 is the blocking one. Everything else can follow a decision; that one *is* the decision.

---

## 13. Frozen ratings

`is_frozen` on `PlayerRating`, default `false`. A frozen rating **refuses** adjustment.

No fair-play module exists, so **nothing sets it** in v0.5.0. That is the whole extension point:
when `fairplay` ships it sets the flag and this module already refuses.

**What is deliberately not built** is PR-5's pending queue — *"the matches queue and are applied
or discarded when the case resolves"*. A refused adjustment in v0.5.0 is **lost**, not queued.
Recorded here rather than discovered later, because retrofitting the queue means the matches
refused before it existed cannot be recovered.

---

## 14. Compatibility with the public profile

`GET /profiles/{handle}` and `GET /profile` ship `ratings.{classic, rapid, blitz}` today. That
contract **does not change**.

| Layer | Vocabulary |
| --- | --- |
| Domain, application, persistence, leaderboard | `SpeedClass` — **only** |
| Profile presentation | `RatingCategory` — a mapping, deprecated |

`RatingCategory.CLASSIC → SpeedClass.CLASSICAL`, `RAPID → RAPID`, `BLITZ → BLITZ`. Inactive
classes report the initial triple, provisional, zero games — which is exactly what they are.

**No business logic is duplicated.** The mapping is one function at the presentation boundary;
nothing below it knows `RatingCategory` exists.

**Migration path.** The profile response gains a `ratings_by_key` object keyed by
`variant/speed_class`; `ratings` is marked deprecated in OpenAPI and removed in a major version
once clients have moved.

### 14.1 The dedicated read surface — A64-020.0A

Two routes, both authenticated, both read-only. There is deliberately no endpoint anywhere on
this surface that could *request* an adjustment: a rating moves in response to
`game.match_completed` and nowhere else (§9).

| Route | Answers |
| --- | --- |
| `GET /api/v1/ratings/me` | Every speed class for the authenticated caller, in `SpeedClass` order |
| `GET /api/v1/players/{player_id}/ratings` | The same summary for anybody |

**Every key, always.** A class the player has never played is present and marked provisional with
zero games, because `RatingSnapshot.unrated()` is what §7.5 answers for an absent row. Omitting
them would push "has this player played blitz?" onto every client, and a client getting it wrong
would render a missing rating as a rating of zero. One query serves all of them —
`ratings_across` exists precisely so a page showing several classes is not one query per class.

**`value`, `deviation`, `games_played` and `is_provisional` are published; `volatility` is not.**
It is an input to the next calculation rather than a fact about the player, and publishing it
would invite a client to render a number whose scale is an implementation detail of §7.2.

**No `404` for an unknown player**, deliberately: `rating` answers every id with a snapshot, so
"no such account" and "never played" are indistinguishable here — and making them distinguishable
would turn the endpoint into an account-existence oracle. Whether a player exists is `users`'
question. A *ranking* is the opposite case and does answer `404`
([`leaderboard.md`](./leaderboard.md) §7.2).

---

## 15. Performance & limits

| Path | Cost |
| --- | --- |
| One rating update | 2 indexed reads, 2 upserts, 2 inserts, 1 outbox row — one transaction |
| Lazy RD inflation | Arithmetic on values already read. **No extra query** |
| Leaderboard read | One indexed range scan, paginated |

Glicko-2's volatility step is an iterative solver (Illinois algorithm). It is bounded at a fixed
iteration cap and is microseconds of arithmetic on values already in memory.

---

## 16. Security & privacy

Ratings are **always public** — `profiles`' privacy settings say so explicitly and do not cover
them. An adjustment reveals the opponent's rating at match start, which was already public.

No rating value, player id or match id appears in a metric label. Log lines carry the correlation
id of the move that ended the match (`services.md` §9), which is what makes "why did I lose 14
points" answerable at 3am.

---

## 17. Observability

| Signal | Why |
| --- | --- |
| Adjustments applied, by outcome | The volume the ladder is built from |
| Adjustments refused, by reason (`frozen`, `duplicate`, `not_rated`) | A rising `duplicate` rate is a relay redelivering; a rising `frozen` rate is a fair-play sweep |
| RD inflation applied, by whether it hit the ceiling | Says how much of the population is returning from absence |
| Rating update latency | It sits on `q.critical` (`services.md` §10.4) — delay here delays the leaderboard |

---

## 18. Test plan

| Level | Covers |
| --- | --- |
| Unit | Glicko-2 against **Glickman's own worked example**; the inflation ceiling; the provisional boundary at exactly 25; refusal of every non-rating termination |
| Contract | The unique index under concurrent duplicate delivery; the upsert; the outbox row in the same transaction |
| Integration | `match_completed` → two adjustments → `rating.updated` |

Glickman's paper gives a worked example with known outputs. It is the one test that proves the
implementation is Glicko-2 rather than something that merely behaves plausibly.

---

## 19. Open questions — deliberately not decided

| # | Question | Blocked work |
| --- | --- | --- |
| OQ-1 | Speed-class boundaries: which `(base_time, increment)` ranges are `BULLET`/`BLITZ`/`RAPID`/`CLASSICAL`/`CORRESPONDENCE`? | Activating any class beyond `CLASSICAL` |
| OQ-2 | Are time controls reference data or code constants? (`domain-model.md` Q-4) | The time-control catalogue |
| OQ-3 | Season semantics: what opens and closes one, and what happens to ratings at a boundary? | Anything beyond the nullable column |
| OQ-4 | Fair-play: what freezes a rating, and are refused adjustments recoverable? | PR-5's pending queue |
| OQ-5 | Does the **value** decay with inactivity, as distinct from RD inflating? | A scheduled writer, if ever |

None blocks v0.5.0. Each is an extension point that is documented, isolated, and invents no
future business rule.
