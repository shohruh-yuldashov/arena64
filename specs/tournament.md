# Feature Specification — Tournaments

| Field | Value |
| --- | --- |
| **Spec ID** | `SPEC-TOURNAMENT` |
| **Status** | Approved for v0.x — Single Elimination only |
| **Owner** | _Unassigned_ |
| **Created** | 2026-08-05 |
| **Last updated** | 2026-08-05 |
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

## 7. Privacy

Tournaments and their brackets are **public**. A private tournament is deferred with
user-created ones.

Where a resource is not visible, the answer is the platform's existing rule: **`404`, never
`403`** — the same one match history and spectating already keep, for the same reason.

## 8. Open questions

| # | Question | Blocked work |
| --- | --- | --- |
| OQ-1 | Who may cancel a tournament, remove a participant, or override a result? | Moderation — waits for the Administration epic and `specs/admin.md` |
| OQ-2 | Is check-in required, and what is the no-show window? | Defaulted to "no check-in" in v0.x |
| OQ-3 | Time control per tournament | `specs/rating.md` OQ-1 and OQ-2 — the catalogue does not exist, so every match is the platform default |
| OQ-4 | Retention for cancelled tournaments | Append-only in v0.x, like everything else |
