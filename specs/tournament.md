# Feature Specification — Tournaments

| Field | Value |
| --- | --- |
| **Spec ID** | `SPEC-TOURNAMENT` |
| **Status** | Approved for v0.x — Single Elimination only |
| **Owner** | _Unassigned_ |
| **Created** | 2026-08-05 |
| **Last updated** | 2026-08-05 — A64-019.6, final results and public reads (§6f, §6g) |
| **Related specs** | [`rating.md`](./rating.md), [`replay.md`](./replay.md), [`matchmaking.md`](./matchmaking.md) |
| **Related** | `services.md` §11.3, `database.md` §18.3, `domain-model.md` §16.2 and R-25 |

---

## 1. Summary

Single-elimination tournaments: an administrator opens one, players register, a seeded bracket is
built, rounds are played as ordinary rated matches, and the last player standing wins.

`services.md` §11.3 predicted this feature needs **no new mechanism**. §2 below records the one
place that prediction was not yet true, and A64-019.0 makes it true.

## 2. Scope

| In — v0.x | Deferred, explicitly |
| --- | --- |
| **Single elimination** | Swiss, round robin, double elimination, arena, team |
| Up to **128 players**, powers of two, automatic byes | Larger fields |
| **Administrator- and system-created** tournaments | User-created — waits for the Administration epic |
| Rating-seeded brackets | Manual and protected seeding |

## 3. Product decisions

| # | Decision |
| --- | --- |
| T-1 | **Single elimination only.** Every other format is deferred, and adding one is a new `TournamentFormat` member plus a pairing strategy — additive, not a redesign |
| T-2 | **Field size ≤ 128**, rounded up to a power of two. The highest seeds take byes in the first round, which is the standard rule and the only one that keeps the bracket balanced |
| T-3 | **Administrators and the system create tournaments.** No player-created tournaments in v0.x |
| T-4 | Tournament matches use the **existing global rating** — `(ProductVariant, SpeedClass)`. No separate tournament rating, so DM-10 stands and nothing in `rating` changes |
| T-5 | A forfeit or no-show ends the match with `TerminationReason.ADJUDICATION`, which is **not** on `rating`'s allowlist — so it moves no rating. `specs/rating.md` §9 is unchanged |
| T-6 | Tie-breakers are **not needed**: elimination produces one winner per pairing and one winner overall |

## 4. The one missing mechanism — A64-019.0

`domain-model.md` **R-25** requires `Match` to carry an optional originating-context reference so
a tournament can recognise its own matches *without `game` knowing what a tournament is*.
`database.md` §18.3 lists it as already satisfied.

**It was not.** `game.match` had no `origin` and no `origin_ref`, and `CreateMatchRequest`
carried neither — so a tournament could create a match and never find it again. A64-019.0 adds
them, and that is the whole of the "no new mechanism" claim becoming true.

| Column | Meaning |
| --- | --- |
| `origin` | `queue`, `challenge`, `rematch`, `tournament` — where the match came from |
| `origin_ref` | **Opaque** uuid. No foreign key (DB-03), no meaning to `game` |

**A64-019.0 shipped only the outbound half.** The columns were written and
`game.match_completed` did not carry them, so the originating context saw a match end and could
not tell it was one of its own. A64-019.5 adds both fields to that payload, additively and with
defaults, which completes the round trip:

```
tournament -> game.public.CreateMatch   origin = tournament, origin_ref = pairing.id
game       -> game.match_completed      origin, origin_ref, echoed back
```

`game` still knows nothing about brackets. It stores an enum member and an opaque id, and hands
both back.

**`game` stays tournament-agnostic.** It stores an enum member and an opaque id, hands both back
on `match.completed`, and has no idea what a bracket is. A foreign key here would make the two
schemas undeployable apart, which is precisely the seam `architecture.md` §16 keeps open.

## 5. Ownership

| `tournament` owns | `game` owns | `rating` owns |
| --- | --- | --- |
| `Tournament`, `Registration`, `Round`, `Pairing`, `BracketNode`, `Standing` | `Match`, the move log, the result | `PlayerRating`, `RatingAdjustment` |

`tournament` **never** writes to `game`'s schema (R-3) and never references its relations with a
foreign key (DB-03). It calls `game.public`'s `CreateMatch` — the same port `matchmaking` uses —
and consumes `game.match_completed` through the subscriber-agnostic outbox (BE-10).

A64-019.5 adds one published **read**, `game.public.OriginMatchReader`: what became of the
matches an originating context asked for, keyed by `origin_ref`. It is the counterpart to
`PairingReconciliationReader` and exists for the same reason — creating a match and recording
that it was created are two transactions BE-05 forbids collapsing, so something has to be able
to ask afterwards. The queue asks about its tickets; a tournament has none to ask about.

The import contract is stricter here than for any other consumer of `game.public`:
`tournament-reaches-modules-through-public` covers this module's **composition root** as well,
which every other module's contract exempts. So `game`'s concrete classes are named only in
`app/app_factory.py` and passed in.

## 6. Bracket and pairing

Seeded by rating (`rating.public.RatingReader`), highest first. The bracket is a full binary tree
built once, when registration closes: the number of rounds is known from the field size, so there
is nothing to compute per round.

Pairing is therefore **deterministic and needs no engine**: one layer of the tree is one round.
`matchmaking`'s `PairingEngine` is deliberately **not** reused — it pairs by a widening rating
window over a pool of reserved tickets, which is a different question with a different answer.

**Byes** go to the highest seeds when the field is not a power of two. **Published pairings are
immutable**: once a round is published its pairings do not change, so a player who read the
bracket is looking at the same bracket the results will be recorded against.

## 6a. Seeding and pairing — A64-019.3

### Seed order

```
rating DESC, deviation ASC, player_id ASC
```

**Total**, and that is correctness rather than tidiness: a non-deterministic
seeding produces a *different bracket on a retry*, and the retry is exactly when
it matters. The third key is unique, so no two entrants compare equal.

Deviation second for the leaderboard's reason — between two players on the same
rating, the one the platform is more sure about seeds higher. **Provisional players are
seeded, not excluded**; their large deviation already places them below an established
player on the same number.

Seed numbers start at 1 and are **persisted** on the registration. A later phase must
never re-derive seeding from current ratings: ratings move and a published bracket does
not.

Ratings are read in **one batch**, and the reason is correctness rather than speed —
seeding reads ratings *at a moment*, and a per-player loop spreads that moment across the
field. The key is the tournament's own variant with `CLASSICAL`; nothing infers another.

### Bracket size

The smallest power of two at or above the **active entrant** count — never
`tournament.capacity`, which is the registration maximum. Capacity 10 with 6 entrants
plays an 8-bracket with two byes, not a 16-bracket with ten.

Fewer than two active entrants is refused.

### Seed placement

Standard recursive doubling:

```
order(1)  = [1]
order(2n) = interleave(order(n), [2n + 1 - s for s in order(n)])

size 2   [1, 2]
size 4   [1, 4, 2, 3]
size 8   [1, 8, 4, 5, 2, 7, 3, 6]
```

The list is seed numbers in bracket-slot order; slot `2j` plays slot `2j+1`. What it
guarantees, and naive `1v2, 3v4` does not:

| Property | Why it holds |
| --- | --- |
| Seeds 1 and 2 meet only in the final | They sit at opposite ends, so their halves never intersect |
| Seeds 1–4 are in distinct quarters | Each doubling splits the previous order across the new halves |
| Seed *s* faces `size + 1 - s` | The reward for seeding well is monotone, not incidental |

### Byes

A bye is an **empty bracket slot** — never a fake player, never a match, and no `game`
match is created for one. A seed higher than the entrant count has no player, so the
pairing carries one participant and one `None`.

**Highest seeds receive byes first**, and that falls out of the placement rather than
being applied: seed `size` is opposite seed 1, so the absent seeds land against the top
of the field. A64-019.4 advances the present player without a match.

### Side assignment

The higher seed takes the light seat on **even** slots and the dark seat on **odd** ones.
Deterministic, and alternating so moving first is not always the better player's.

No historical colour balancing: single elimination gives a player at most `log2(size)`
games, so there is no history to balance.

### Immutability and idempotency

Once a round's plan is written it does not change. The primary key
`(tournament_id, round_number, slot)` is the mechanism — a second plan cannot be
inserted — so:

- a **retry** reads the persisted plan and returns it unchanged;
- **two workers** racing both compute a plan, one inserts, and the loser re-reads the
  winner's.

The second is safe only because seeding is deterministic: if the two could differ,
re-reading would silently accept a bracket the loser did not compute.

## 6b. Bracket materialisation and advancement — A64-019.4

### The tree is built whole, once

Every round and every node is written in **one transaction**, before any match exists.
Later rounds are materialised **empty** rather than created when the previous one finishes.

A bracket generated lazily from current results can differ from the one players read, and
"who could I meet in the semi-final" stops being answerable in advance. The cost is
`size - 1` rows written once; the benefit is that placement is never recomputed.

A partial bracket is impossible: the transaction has every round and every node or none.

### Rounds

`tournaments.round`, unique `(tournament_id, round_number)`, numbered from 1. The status
machine lives in the domain aggregate — a repository that decided transitions would be a
second copy of the rule.

Round one is created **published**, because its participants are known the moment the
bracket exists and publication is what freezes them. Later rounds are `PENDING` until
their participants are known.

### Bye propagation

A node with exactly one participant has a winner without a match, and filling its parent
may leave *that* node with one participant — so propagation runs to a **fixed point**.

The rule that stops it deciding too early: a node above round one is a bye only when
**nothing beneath it can still deliver a participant**. A semi-final holding one player
because the other semi has not been played is *waiting*, not a bye, and deciding it would
skip a match that has to happen.

Two empty seats stop the chain — there is nothing to decide, and inventing a winner would
be a phantom advancement.

Idempotent: applied to its own output nothing changes.

### Winner advancement

```
UPDATE pairing SET winner_id = :w WHERE … AND winner_id IS NULL
```

The guard is in the `WHERE`, so two workers processing one completed match cannot both
write. The loser reads the stored winner: if it agrees, the work was done and it returns
idempotently; if it disagrees, that is a **conflict** and it raises rather than
overwriting. On a bracket an overwrite means a player advancing out of a node they lost,
visible only after the rounds above are recorded.

Read-then-write would let both through.

A winner who did not play in the node is refused by the aggregate *and* by a check
constraint — the one bracket error nothing downstream detects.

### Persisted seeds

`PersistedSeed` holds **only what is stored**: the tournament, the player and the seed
number. The rating and deviation that produced the seed are deliberately absent.

A64-019.3's repository returned the live `Seed` type with `rating=0.0, deviation=0.0,
is_provisional=False`, and those read like measurements — a caller that trusted them would
have reseeded a tournament to all-equal. A64-019.4 replaced the type rather than the
values, so the absence is in the signature.

## 6c. Draws in single elimination — the bounded rematch

Single elimination needs one winner per pairing, and this platform's games **can draw**:
threefold repetition is live on the only variant. The v0.x policy:

| Attempt | Result | What happens |
| --- | --- | --- |
| 1 | decisive | The winner advances |
| 1 | **draw** | Exactly one rematch, **sides swapped**, same pairing, new `game` match |
| 2 | decisive | The winner advances |
| 2 | **draw** | The **higher seed** advances, reason `ADJUDICATION`. No third match |

**Bounded at two, and the bound is the point.** An unbounded rematch chain is a
tournament that can never finish, and nothing would force one — every match is untimed
today (`specs/rating.md` §8).

**Why the higher seed rather than a third game or a coin.** A third game repeats the
question that twice failed to answer it. A random winner is a permanent competitive record
decided by chance. Manual adjudication needs an `admin` module that does not exist, and
until it did the tournament would be frozen. The seed is the one answer already earned —
the rating the field was seeded on, recorded before anyone played.

**This is a v0.x policy and is expected to be revisited** once `reference.time_control`
exists: a faster rematch under a real time control is the better tie-break, and it is
unavailable only because the catalogue is not built (`specs/rating.md` OQ-1, OQ-2).

### Rating behaviour

Each drawn game is an **ordinary rated draw** and moves ratings normally when the
tournament is rated. The adjudicated bracket advancement on top of them is **not a
game**: it creates no third match and therefore no rating adjustment, and
`specs/rating.md`'s termination allowlist is unchanged.

### The attempt model

A pairing may have up to two `game` matches, held as rows in
`tournaments.pairing_attempt` rather than a list in a column:

| Constraint | Rule |
| --- | --- |
| `unique (pairing_id, attempt_number)` | One row per attempt |
| `unique match_id` | One `game` match belongs to at most one attempt |
| `attempt_number` in 1…2 | A third attempt cannot exist |
| Attempt 2 requires attempt 1 completed as `DRAW` | A rematch has a cause |
| Attempt 2 swaps the seats | The first move is not the same player's twice |

`pairing.match_id` is **removed**: it could no longer truthfully represent a pairing with
two matches, and two competing sources of truth is worse than a migration. The feature is
unreleased, so the model is corrected now rather than preserved.

### Identity across the boundary

Every pairing carries a stable surrogate UUID. A match created for it records
`origin = TOURNAMENT` and `origin_ref = pairing.id` — **never** an encoding of the
tournament, round or slot, so the reference stays opaque and the coordinates stay free to
be an implementation detail.

### Idempotency

A duplicate `match.completed` delivery must not create two rematches, advance a player
twice, create a third attempt, or overwrite an advancement. The guarantees are the
database's: `unique (pairing_id, attempt_number)`, `unique match_id`, and the
`winner_id IS NULL` compare-and-set A64-019.4 already uses.

### The queue ticket a tournament does not have — corrected in A64-019.5H

A64-019.5 shipped a derived uuid5 as each seat's `queue_ticket_id`, because
`game.public.MatchParticipant` required one and `game.match` stored it `NOT NULL`. That
recorded a queue ticket that never existed — a fabricated fact in a permanent record (A-4), and
one `settlements_for` would have answered questions about.

A64-019.5H makes the column nullable instead. A tournament seat now passes `None`, and the
requirement is **origin-specific rather than dropped**:

| Origin | `queue_ticket_id` |
| --- | --- |
| `queue` | **Required** — both seats, distinct. `CreateMatchRequest` and `MatchRecord` refuse a queue match without them, because a queue match with no tickets is one no reconciler can recover |
| `tournament`, `challenge`, `rematch` | `None`. Where the match came from is `origin` and `origin_ref` (R-25), which is what it always should have been |

The unique indexes are unchanged: PostgreSQL treats each `NULL` as distinct, so two matches
still cannot claim one ticket while any number of ticketless matches coexist.

## 6d. Live tournament matches — A64-019.5

### Starting

| Step | Guarantee |
| --- | --- |
| Materialise the bracket | Idempotent, its own transaction (A64-019.4 §5) |
| `REGISTRATION_CLOSED` → `IN_PROGRESS` | Under the row lock; a retry finds it already moved |
| Round one `PUBLISHED` → `IN_PROGRESS` | The aggregate's table, not a repository's opinion |
| One `game` match per node that `needs_a_match` | **A bye creates none** — it has one participant, so it never qualifies |
| `tournament.started` | Outbox, same transaction (AD-16) |

Two transactions rather than one, because materialisation is composed rather than reimplemented.
Both halves are idempotent, so a worker that dies between them leaves a bracket the retry reuses.

**Effects before bookkeeping.** The `game` match is created *before* the attempt row is written,
and the winner is advanced *before* the attempt is marked completed. The recoverable state is
the one where more has happened than has been recorded: asking `game` again returns the same
match (its unique key on the derived `pairing_id`), whereas an attempt row naming a match that
was never created is unrecoverable.

### `game`'s idempotency key is derived, not stored

`CreateMatchRequest.pairing_id` is `uuid5(pairing.id, "attempt:<n>")` — **per attempt**, because
a pairing may have two matches and `game`'s key admits one match each. Deriving it is what makes
every retry above safe: the same input computes the same key, and `game` returns the existing
match with `created = False`.

### Completion

`tournament.match_completed`, its own `processed_event` partition. An entry is **skipped, not
failed**, when `origin` is not `tournament`, when there is no reference, when no attempt names
the match, or when the outcome is not a result — none becomes true by being retried. Anything
else is a per-entry failure; the consumer never raises, so one poison entry cannot stop a batch.

An **aborted** match (`MatchOutcome.NONE`) advances nobody. It is not a draw — nothing was
played — and §6c's rematch is a rule about games that were.

### Reconciliation

`tournament.bracket.reconcile`, maintenance queue, five minutes, bounded, `FOR UPDATE SKIP
LOCKED`, never raises. It compares `game`'s matches for a node against this module's attempts:

| State | Action |
| --- | --- |
| Node owed a match, `game` has none | Launch it |
| `game` has a match, no attempt row | Record the attempt, numbered by creation order |
| Match decided or drawn, nothing followed | Re-apply it through the same service the consumer uses |
| Attempt names a match `game` no longer has | **Reported, not repaired** |
| Match ended with no result at all | **Reported, not repaired** |

The last two are logged at `ERROR` with a counter rather than repaired, because neither has an
answer this module may invent. **A64-019.5H removed their most common cause**: a tournament match
is now system-activated (§6e), so it cannot expire unanswered, and a fixture nobody turns up for
is decided by the no-show deadline rather than left for the reconciler to find.

## 6e. Activation and the no-show deadline — A64-019.5H (Live Tournament Hardening)

### A tournament match is a fixture, not an offer

Tournament matches do **not** use the bilateral acceptance handshake. The participants
committed when they entered the tournament, so there is nobody to ask: the match is created
already `ACTIVE` (`game.public.AcceptancePolicy.SYSTEM`) and both players may join it through
the existing live gateway immediately. Acceptance polling is not required for `TOURNAMENT`
origin, and such a match can never expire unanswered.

The policy is a **named enum on the request**, not a boolean and not inferred from `origin`: a
boolean parameter selecting behaviour is what CLAUDE.md §2.3 forbids, and inferring it would
silently decide the question for `challenge` and `rematch` the day either ships — those are
offers somebody may refuse.

### What replaced it: attendance

The question stops being "did they answer" and becomes **"did they turn up"**. A player turns
up by joining the match's room through the gateway, which is the only component that observes
one; it tells the tournament through `tournament.public.TournamentAttendance` — one guarded
`UPDATE`, no read, on every join. A match no tournament owns matches no row.

Attendance records the **first** arrival and is never cleared, so a transient disconnect after
somebody turned up is not a no-show, and a reconnect before the deadline counts as present.
A liveness flag would make a dropped socket indistinguishable from an absence.

### The deadline

| Setting | Default | Meaning |
| --- | --- | --- |
| `TOURNAMENT_NO_SHOW_SECONDS` | **300** | How long a match waits for its two players |
| `TOURNAMENT_NO_SHOW_INTERVAL_SECONDS` | 30 | How often lapsed deadlines are claimed |
| `TOURNAMENT_NO_SHOW_BATCH_SIZE` | 100 | How many one pass claims |

Written **onto the attempt** when the match is created, never read from configuration at
adjudication time: a deploy that lengthens the setting must not reprieve a player whose
deadline already passed, and one that shortens it must not eliminate somebody who was inside
the window they were given.

### The policy

| At the deadline | What happens |
| --- | --- |
| Both present | Nothing. Play proceeds; what happens next is the game's business |
| Exactly one present | The **present player** advances, reason `ADJUDICATION` |
| Neither present | The **higher seed** advances, reason `ADJUDICATION` |

The higher seed for §6c's reason: it is the one answer already earned, recorded before anyone
played, and there is nobody present to award it to instead.

### Rating behaviour

**No game result is fabricated.** The `game` match is left exactly as it is — no outcome, no
winner, no termination reason — so there is no completion, no `match.completed`, and no rating
adjustment. `specs/rating.md`'s termination allowlist is unchanged, exactly as for §6c's
two-draw adjudication. The attempt is closed as `AttemptOutcome.NO_SHOW` rather than
`DECISIVE`, because nobody won a game and a permanent record saying otherwise would be
indistinguishable afterwards from a played result.

### A real result always beats a stale worker

Held in three independent places rather than by ordering alone:

1. **The claim is not the decision.** A claimed attempt is re-read against `game`'s
   authoritative state, and one whose match is `DECIDED` or `DRAWN` is handed to the ordinary
   advancement path — the same service the outbox consumer drives, so a repair and a delivery
   cannot disagree about what a result means.
2. **Attendance stops it.** A match both players reached is never adjudicated for absence. A
   match being *played* is by definition one both reached, so a stale deadline cannot reach a
   started match.
3. **Every write is a compare-and-set.** `outcome IS NULL` on the attempt and
   `winner_id IS NULL` on the node, so two workers cannot both adjudicate and the loser cannot
   overwrite.

The sweep is a `platform.tasks` handler with an injected clock, a durable row-held deadline,
`FOR UPDATE SKIP LOCKED`, a bounded batch and no in-process timer — a deadline held in process
lives on one node, and a deploy takes every one it held with it (AD-21).

## 6f. Final results — A64-019.6

### Placement is the elimination round

Single elimination ranks by how far you got, so every player knocked out in the same round
finished in the same place:

```
rank(champion)               1
rank(eliminated in round r)  2 ** (rounds - r) + 1
```

| 8 players, 3 rounds | Rank |
| --- | --- |
| Champion | 1 |
| Final loser | 2 |
| Semi-final losers | 3, 3 |
| Quarter-final losers | 5, 5, 5, 5 |

**Ties are not broken** — not by rating, not by seed, not by a head-to-head that never happened,
not by move count and not by duration. Two players knocked out in the same round have the same
evidence about them, and inventing an order would publish a comparison nobody made. The seed
stays metadata: it explains the draw, it does not rank the result.

Ranks are therefore **not dense**: two players share third, so nobody is fourth and the next
tier starts at fifth. That is how every bracket sport reports a draw sheet.

### Standings are an immutable snapshot

Before completion the **bracket** is authoritative — nodes, attempts and recorded advancements.
At completion, `tournaments.standing` is materialised **once** and never recomputed. A read
endpoint pages over stored rows: a derivation that ran per request would make a published result
depend on code that can change, and the bracket it came from is already terminal.

C1, a permanent record: `created_at` alone, no `updated_at`. Corrections and overrides are
deferred with the Administration epic (OQ-1), as are prizes, rewards, trophies, achievements,
season points and team standings.

### Statistics count games, never advancements

| Event | Effect |
| --- | --- |
| Decisive real match | One win, one loss |
| Drawn real match | One draw for **both** |
| Two draws → higher seed advances (§6c) | `adjudicated_advancements` +1 for the advancing player |
| No-show adjudication (§6e) | `adjudicated_advancements` +1. **Not** a win |
| Bye | **Nothing** — no win, no loss, no adjudication |

A bye is arithmetic, not a ruling: the bracket had an empty seat, and nobody adjudicated
anything. No `game` result is ever fabricated.

### Completion

`complete_tournament(tournament_id)` — lock, require `IN_PROGRESS`, verify the final has a
winner and every contested pairing is settled, derive placement, materialise every standing,
transition to `COMPLETED`, stamp `completed_at`, publish through the outbox. **One transaction.**

The standings are written *before* the transition, deliberately: the state §5 forbids —
`COMPLETED` with no standings — would be unrepairable, because `COMPLETED` is terminal and
nothing would run again to write the other half. A failure leaves the tournament `IN_PROGRESS`,
which the next completed match reaches again.

**The trigger is automatic** (§14): the final's winner finishes the tournament through the
advancement flow. There is no operator endpoint, and no `POST /complete` — an endpoint that
could finish a bracket is one that could finish an unfinished one.

Idempotent by three database facts: `pk_standing`, `uq_standing__one_champion`, and `COMPLETED`
being terminal. A second completion returns the stored result unchanged.

**Ordering note.** A64-019.6 reversed A64-019.5's "effects first, bookkeeping last" for one
step: an attempt's result is now recorded *before* the advancement. Because the advancement can
finish the tournament, the old order counted the deciding match in nobody's record — the
champion showed one win instead of two. The recovery argument is unaffected: `apply` re-derives
from the payload and does not gate on the write.

### One event, not two

`tournament.completed` carries the winner and the entrant count. A separate
`tournament.results_materialized` is deliberately **not** emitted: completion and
materialisation are one transaction, so a consumer that saw one and not the other would be
observing a state that cannot exist.

## 6g. Public reads — A64-019.6

| Endpoint | Returns |
| --- | --- |
| `GET /api/v1/tournaments/{id}` | Configuration, entrant count, current round, lifecycle instants |
| `GET /api/v1/tournaments/{id}/bracket` | Rounds, nodes, seeds, winners, advancement reasons, attempt summaries with `match_id` |
| `GET /api/v1/tournaments/{id}/standings` | The immutable placement, ordered `final_rank`, `seed_number`, `player_id` |
| `GET /api/v1/players/{id}/tournaments` | Participation, newest first, **keyset** paginated |

**Public** means no viewer is narrower than another — no owner check, no friends-only variant,
no configurable privacy. It does not mean unauthenticated: every route outside `/health` is
behind a session. An unknown tournament is `404` and never `403`, and here that cannot be got
wrong: a tournament is present for everybody or absent for everybody.

Withheld: `created_by`, compare-and-set targets, no-show deadlines, attendance instants, outbox
and audit rows, and ORM models. `match_id` **is** published, so a reader can follow a node to
the game that decided it — the replay itself is `game`'s, on `game`'s endpoint, under `game`'s
visibility rule.

A player's history uses `(registered_at, tournament_id)` descending — both keys, because a
single-key order over an unbounded history pages unstably. Never `OFFSET`.

## 7. Privacy

Tournaments and their brackets are **public**. A private tournament is deferred with
user-created ones.

Where a resource is not visible, the answer is the platform's existing rule: **`404`, never
`403`** — the same one match history and spectating already keep, for the same reason.

## 8. Open questions

| # | Question | Blocked work |
| --- | --- | --- |
| OQ-1 | Who may cancel a tournament, remove a participant, or override a result? | Moderation — waits for the Administration epic and `specs/admin.md` |
| OQ-2 | ~~Is check-in required, and what is the no-show window?~~ **Answered by A64-019.5H** — §6e: no check-in, and a 300-second attendance deadline enforced by a bounded sweep. What remains open is only the *rating* treatment of a repeat offender, which waits on `fairplay` | — |
| OQ-3 | Time control per tournament | `specs/rating.md` OQ-1 and OQ-2 — the catalogue does not exist, so every match is the platform default |
| OQ-4 | Retention for cancelled tournaments | Append-only in v0.x, like everything else |
