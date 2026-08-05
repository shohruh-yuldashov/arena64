# Player statistics

> **Status:** implemented — A64-020.5F
> **Owner:** platform
> **Scope:** the projection that turns completed matches into a player's record.

`statistics` is a **projection** context (DM-03): every number is a count
over match history, nothing here is the system of record for anything, and
the whole relation is rebuildable by definition.

A64-012.6 built the reading half and recorded why there was no writer:
"the writing half is a consumer of `match.completed` and there is no `game`
module to emit one". A64-020.5F adds it.

---

## 1. The record

| Field | Source |
| --- | --- |
| `games_played`, `wins`, `losses`, `draws` | This projection |
| `current_streak` (signed), `best_win_streak` | This projection |
| `win_rate` | **Derived, never stored** — `wins / games_played` |
| `current_rating`, `highest_rating` | **Not this projection's.** See §6 |

`win_rate` is computed on read because the moment it is a column it is a
number that can disagree with the four counts printed beside it — and that
divergence is not hypothetical: it happens the first time a result is
corrected or a rebuild recounts.

---

## 2. What counts

Every completed match counts for both seats, by its result, **whatever
ended it**: a resignation, a flag, an agreed draw, an abandonment, an
adjudication and a checkmate are all games that happened.

`MatchOutcome.NONE` — an **abort** — counts for nobody. MT-11 calls it "a
match that did not happen", and recording it as a draw would put a game on
two permanent records that neither player played.

An outcome this build does not recognise is **rejected**, not guessed: a
contract change must not be written into a permanent record by a default.

---

## 3. Exactly once

`statistics.processed_match`, `PRIMARY KEY (match_id, player_id)`, claimed
with `ON CONFLICT DO NOTHING` **before** the counters move and in the same
transaction.

Structural rather than procedural. A read-then-check would be a race under
two relay processes; the platform's `processed_event` ledger cannot serve
here because it is keyed by *event* id and the backfill has no event.

**The match and the player is the pair both paths share**, which is what
makes a backfill safe beside live consumption: a match counted live is
refused by the key when the backfill reaches it, and vice versa. There is
no other mechanism, and none is needed.

Claiming first is deliberate. A crash between claim and count rolls both
back; the reverse order could leave a match counted and unmarked, which
double-counts on retry.

---

## 4. Ordering

Only the **streak** depends on the order matches are folded in. Counts
commute; a streak is a statement about the most recent games.

Events do not arrive in order — the relay retries, a backfill runs beside
live consumption, and two matches can finish in the same millisecond. So a
row carries a watermark:

```
(counted_at, counted_match_id)
```

compared against `(completed_at, match_id)`. A match **at or behind** the
watermark moves the counts and leaves the streak alone; a match ahead of it
moves both and advances the watermark.

Two fields because a timestamp alone is not a total order: two simultaneous
completions would compare equal and "which came last" would depend on
arrival order. The match id breaks the tie identically in the live consumer
and in the backfill.

---

## 5. The consumer

`statistics.match_completed`, its own `processed_event` partition. Three
modules now subscribe to `game.match_completed` and none may mark another's
work done.

Reads everything from the **payload** — both seats' player ids, the outcome,
the winner — and the completion instant from the envelope
(`OutboxEntry.occurred_at`), because `MatchCompleted` carries none. No live
match read, no profile read, no rating read.

| Outcome | Meaning |
| --- | --- |
| `applied` | Counted |
| `already_processed` | A duplicate, a retry, or a backfill overlap |
| `ignored_non_counting` | An abort |
| `rejected_invalid` | The payload cannot be attributed to two players. **Not retried** — it will be just as unreadable next time |

---

## 6. What this projection does not own

`current_rating` and `highest_rating`. They are in the record because a
profile renders them beside the counts, but they are `rating`'s facts.
Deriving them here would be a second, competing answer to what a player
rates.

They therefore sit at their defaults until a rating projection writes them.
**That is a known gap, not a bug** — see §8.

---

## 7. Backfill

An **operator command**. Never a startup task, never a migration.

```
dry run:   python -m app.operator.statistics backfill --dry-run
apply:     python -m app.operator.statistics backfill
resume:    python -m app.operator.statistics backfill      (the same command)
```

`--batch-size N` bounds the page; the default is 200.

It shares the claim, the rules and the ordering with the live consumer —
the same service, the same primary key, the same total order — so running
it twice, stopping it halfway, or running it during live consumption are
all the same case. It keeps **no state of its own**: "where did I get to" is
answered by `processed_match`, so a restart re-scans and skips what is
marked.

Keyset on `(ended_at, id)`, oldest first. Never `OFFSET`: an offset scan
re-reads every row it skips, and a row inserted mid-run shifts every
subsequent offset — which is how a resumable job silently skips a match.
Oldest first because a newest-first rebuild produces correct counts and a
nonsense streak.

It never truncates and never resets. A rebuild that cleared first would
have a window in which the platform's totals were zero.

Exit code `0` when every match was scanned, `1` when any failed. A failure
is counted rather than fatal, so one unreadable row does not block the rest.

**Verified on the live dataset:** 74 matches, 148 markers, wins totalling 74
against losses totalling 74, zero rows violating
`counts_sum_to_games_played`. A dry run wrote nothing; a second real run
reported 74 already processed and applied none.

---

## 8. Known limitations

| Limitation | Why |
| --- | --- |
| `current_rating` and `highest_rating` stay at their defaults | They are `rating`'s facts and need a rating projection — §6 |
| No per-variant or per-speed-class breakdown | `database.md` §9.5 specifies one keyed `(player_id, rating_category_id)`; this is the flat record a profile renders |
| No head-to-head, no colour split, no termination breakdown | Same reason. Additive when a surface needs them |
| A rebuild cannot *correct* an existing row | There is no rebuild mode, deliberately: correcting means deciding what "wrong" is, and nothing has claimed a row is |

---

## Related documents

- `docs/01-architecture/domain-model.md` — DM-03's projection classification
- `docs/01-architecture/database.md` §9.5 — the wider record this grows into
- `specs/frontend.md` §18 — the match-history surface these counters describe
