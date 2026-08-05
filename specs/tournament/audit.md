# Tournament Subsystem Audit — A64-019.7

| Field | Value |
| --- | --- |
| **Document ID** | `AUDIT-TOURNAMENT` |
| **Status** | Complete — closes the Tournament epic |
| **Owner** | _Unassigned_ |
| **Created** | 2026-08-05 |
| **Scope** | A64-019.0 … A64-019.6, plus A64-019.5H (Live Tournament Hardening) |
| **Related** | [`../tournament.md`](../tournament.md), [`../rating.md`](../rating.md), [`../matchmaking.md`](../matchmaking.md) |

---

## 1. Readiness

**READY WITH DOCUMENTED LIMITATIONS.**

> **Updated by A64-019.8.** The limitation that drove this classification —
> "the tournament write path has no production entry point" — is **closed**.
> Players enter and withdraw over the real v1 router; an operator runs the
> lifecycle through `python -m app.operator.tournament`. §4.2 below is kept
> as the record of what was found and now states what replaced it.
>
> The classification itself is unchanged, and the reason is narrower: there
> is still no administrator in `auth` or `users`, so operator commands are
> deliberately unreachable over HTTP. See `specs/tournament.md` §6h.

The subsystem is internally complete and correct: every rule in
`specs/tournament.md` is implemented, enforced by both code and database
constraints, and covered by tests against real PostgreSQL. The full suite is
green and every quality gate passes.

The classification is not "ready for production" for one reason, recorded in
§4 below: **the tournament write path has no production entry point.** A
tournament cannot be created, entered, seeded or started by anything the
running application exposes. That is a deliberate deferral — T-3 puts
creation behind administrators and the Administration epic does not exist —
but it means the epic ships as a complete engine with no ignition.

Everything downstream of a started tournament *is* wired and would run: the
match-completed consumer, the reconciler, the no-show worker, the completion
trigger and the four public read endpoints.

---

## 2. What was audited

| Area | Method | Result |
| --- | --- | --- |
| Full flow, steps 1–22 | One end-to-end test through production services and the real HTTP router | Pass |
| Reachability | AST registry, `app_factory` inspection, real-router walk | 1 finding (§4) |
| Schema and migrations | Alembic-built database, `pg_constraint` / `pg_indexes` inspection | Clean |
| Domain invariants | Code and database constraints compared side by side | Clean |
| Concurrency | Real PostgreSQL races, `IntegrityError` handling reviewed per call site | Clean |
| Transactions | Ordering review, targeted sweep for field-dropping reconstruction | Clean (2 prior defects stay fixed) |
| Rating integration | Allowlist untouched; adjudication paths verified to create no `game` result | Clean |
| Bracket and results | Sizes 2, 3, 6, 10, 128; placement tiers; statistics vs actual attempts | Clean |
| No-show and attendance | Durable deadline, real gateway path, stale-worker protection | Clean |
| Reconciliation | Four repairable states, two reported states, bounded and `SKIP LOCKED` | Clean |
| Security | Public-read field set, `404` never `403`, no ORM leakage | Clean |
| API and pagination | Query counts measured; keyset verified; no `OFFSET` | Clean |
| Performance | Query counts at 8 and 128 entrants | Clean (§8) |
| Events | One event per committed fact; consumers reachable | Clean |

---

## 3. Defects found and fixed

### 3.1 `Settings` gained a required section and broke eight tests

**Concrete, and shipped.** A64-019.5H added `TournamentSettings` as a
required field on `Settings`. Eight tests in `tests/unit/test_settings.py`
construct `Settings` by enumerating every section, and all eight began
failing with `Field required [type=missing]`.

It survived because A64-019.5H and A64-019.6 were both instructed to run
**focused suites only**, and neither focused set includes `test_settings.py`.
This is the first thing the audit's full run found, and it is the clearest
argument for the full-suite gate this phase exists to apply.

*Fix:* the eight call sites now pass `tournament=TournamentSettings()`.

### 3.2 Prior defects confirmed still fixed

Two field-dropping reconstruction defects were found in earlier phases:
`MatchSeat.accepting()` (dropped the seat rating snapshot, which would have
meant **no rating on the platform ever moved**) and `MatchRecord._with()`
(dropped `origin` and `origin_ref`, which would have broken R-25's round trip
for every system-activated tournament match).

A targeted AST sweep over `app/` looked for the exact shape — *a method on a
frozen dataclass that returns a new instance of its own class, built by
naming fields and reading `self`*. It found **one** remaining match,
`QueueTicket.requeued()`, which is correct: it is documented as "a new
identity, not a mutation", and the fields it omits (`id`, `status`,
`resolved_at`, `reserved_until`) are precisely the ones a fresh waiting
ticket must not inherit.

**No third instance of the defect class exists.**

### 3.3 Observation, not a defect: refusal ordering at full capacity

`RegistrationRepository.add` counts inside the lock *before* it inserts, so a
duplicate registration against a **full** tournament is reported as
`TournamentIsFull` rather than `AlreadyRegistered`. Both are refusals a
client answers identically, and reordering would move the capacity guard
outside the lock — which is the check-then-insert race §6 forbids. Recorded
rather than changed.

---

## 4. Reachability

### 4.1 Wired, and proven by a test that reaches it

| Entry point | Constructed in | Registered in | Reached by |
| --- | --- | --- | --- |
| `GET /tournaments/{id}` | `get_tournament_results` | `app/api/v1/router.py` | `test_tournament_results.py`, `test_tournament_audit.py` (HTTP) |
| `GET /tournaments/{id}/bracket` | `get_tournament_results` | `app/api/v1/router.py` | same |
| `GET /tournaments/{id}/standings` | `get_tournament_results` | `app/api/v1/router.py` | same |
| `GET /players/{id}/tournaments` | `get_tournament_results` | `app/api/v1/router.py` | same |
| `TournamentDeadlineTask` | `build_deadline_service` | `app_factory` handler + 60 s scheduler | `test_tournament_registration.py` |
| `TournamentMatchCompletionConsumer` | `_tournament_consumer_for` | `app_factory` outbox handler, own `processed_event` partition | `test_tournament_matches.py`, `test_tournament_results.py` |
| `TournamentReconciliationTask` | `_tournament_reconciliation_for` | `app_factory` handler + 300 s scheduler | wiring test |
| `TournamentNoShowTask` | `_tournament_no_show_for` | `app_factory` handler + `TOURNAMENT_NO_SHOW_INTERVAL_SECONDS` scheduler | `test_tournament_matches.py` |
| `TournamentCompletionService` | `build_completion_service` | reached from `TournamentAdvancementService._finish` | `test_tournament_results.py`, `test_tournament_audit.py` |
| `TournamentAttendance` | `get_attendance_ws` | `GameRoomService` via `app/gateway/dependencies.py` | `test_gateway_connection.py` |

The platform's structural registry (`tests/unit/test_reachability.py`) holds
**23** entry points; four are this module's, and all four are named by
`app_factory`.

### 4.2 Was not wired — **closed by A64-019.8**

| Use case | Factory | Status |
| --- | --- | --- |
| Create a tournament | `get_registration_service` | No route, no task, no consumer |
| Open / close registration | `get_registration_service` | No route |
| Register / withdraw a player | `get_registration_service` | No route |
| Seed a tournament | `get_seeding_service` | No caller |
| Materialise a bracket | `get_bracket_service` | Reached only *from* `start_tournament` |
| Start a tournament | `build_start_service` | **No caller** |

Consequence: in a running deployment no tournament can come into existence,
so the deadline sweep claims nothing, the reconciler and no-show worker sweep
an empty set, the consumer sees no tournament matches, and the four read
endpoints answer `404` for every id.

This is a **deferral, not a bug** — T-3 restricts creation to administrators
and the system, and `specs/admin.md` does not exist — but an undocumented
deferral is indistinguishable from the A64-017.6 defect, where an entire
module was built, tested and never called.

`tests/contract/test_tournament_audit.py::test_the_write_path_has_no_production_entry_point`
pins the list. The day any of these names appears in a router or in
`app_factory`, that test fails and points at this section.

---

## 5. Schema and migrations

Verified against a database built by `alembic upgrade head`, never by
`create_all`.

- **One head**, `e91b47c05fa3`; `current` equals `head`.
- **No drift** — `compare_metadata` reports nothing outside the two
  documented exclusions.
- Six migrations touch this subsystem, each reversible; up → down → up is
  clean for every one.

| Guarantee | Mechanism |
| --- | --- |
| One registration per player, ever | `pk_registration (tournament_id, player_id)` — no status column |
| Capacity | Row lock + count inside one transaction (no row-level `CHECK` can see siblings) |
| A round's plan is written once | `pk_pairing (tournament_id, round_number, slot)` |
| Pairing identity is stable | `uq_pairing__id`, the `origin_ref` handed to `game` |
| A winner played in the node | `ck_pairing__winner_played_here` |
| A reason accompanies a winner | `ck_pairing__reason_iff_winner` |
| One attempt per number | `uq_pairing_attempt__pairing_number` |
| One attempt per match | `uq_pairing_attempt__match` |
| No third attempt | `ck_pairing_attempt__number_in_range` (1…2) |
| A draw names nobody, everything else names somebody | `ck_pairing_attempt__winner_iff_decisive` |
| Exactly one champion | `uq_standing__one_champion`, partial unique on `final_rank = 1` |
| The champion is the one player not eliminated | `ck_standing__champion_is_not_eliminated` |
| Status and rank cannot disagree | `ck_standing__champion_iff_first`, `..._runner_up_iff_second` |

`game.match.light_ticket_id` / `dark_ticket_id` are **nullable**, matching
the domain: a queue match requires both (enforced in
`CreateMatchRequest.__post_init__` and `MatchRecord.__post_init__`, keyed on
`origin`), and every other origin has none. The unique indexes are unaffected
— PostgreSQL treats each `NULL` as distinct.

Indexes serve every path: `ix_registration__active` (capacity count),
`ix_registration__by_player` (history keyset), `pk_pairing` prefix (bracket
read), `uq_pairing__id` (`locate`), `uq_pairing_attempt__*` (consumer and
reconciler), `ix_pairing_attempt__no_show_due` (sweep),
`ix_standing__placement` (standings read), `ix_tournament__overdue`
(deadline sweep).

**One index deliberately absent.** `TournamentRepository.in_progress`
filters on `status = 'in_progress'` with no supporting index. At v0.x scale
the `tournament` relation is small and the two sweeps that use it run every
30 s and 300 s; adding an index without a measurement would be the
speculative optimisation CLAUDE.md §10.1 forbids.

---

## 6. Concurrency and transactions

Every race is decided by the database. No process-local lock is used for
correctness anywhere in the subsystem.

| Race | Mechanism | Covered by |
| --- | --- | --- |
| Parallel registrations near capacity | `SELECT … FOR UPDATE` + count in one transaction | `test_parallel_registrations_cannot_exceed_capacity` |
| Duplicate registration | `pk_registration` | `test_a_player_enters_once…` |
| Two seeding workers | `pk_pairing`; loser re-reads | `test_two_workers_cannot_write_two_plans`, `test_a_losing_worker_can_read_the_winning_plan…` |
| Two materialisation workers | `pk_pairing` / `pk_round`; loser re-reads | materialisation idempotency test |
| Two start workers | `uq_pairing_attempt__pairing_number` + `game`'s derived key | start idempotency test |
| Duplicate match creation | `uq_match__pairing_id` + `SAVEPOINT` | `test_match_repository.py` |
| Duplicate `match.completed` | `processed_event` ledger + CAS | `test_a_decisive_result_advances…` |
| Conflicting winners | `UPDATE … WHERE winner_id IS NULL` | `test_two_workers_cannot_advance_two_different_winners` |
| Duplicate rematch | `uq_pairing_attempt__pairing_number` | draw/rematch tests |
| Stale no-show vs real result | Re-read of `game` state, attendance guard, CAS | `test_a_completed_match_beats_a_stale_no_show_worker` |
| Duplicate completion | `pk_standing`, `uq_standing__one_champion`, terminal status | `test_a_duplicate_completion_returns_the_same_immutable_result` |
| API read during completion | MVCC — standings commit in one transaction, so a reader sees none or all | by construction |
| Reconciler vs normal processing | `FOR UPDATE SKIP LOCKED` + idempotent apply | reconciler design |

### Session poisoning

Every caught `IntegrityError` in the subsystem was reviewed. A failed
statement aborts the enclosing PostgreSQL transaction, so a handler that
catches one and then **re-reads in the same session** must scope the failure
to a `SAVEPOINT`.

| Call site | Re-reads after catching? | `SAVEPOINT` |
| --- | --- | --- |
| `SqlAlchemyPairingRepository.save_plan` | Yes | Yes |
| `SqlAlchemyBracketRepository.materialise` | Yes | Yes |
| `SqlAlchemyPairingAttemptRepository.record` | Yes | Yes |
| `SqlAlchemyStandingRepository.record` | Yes | Yes |
| `SqlAlchemyRegistrationRepository.add` | No — propagates through the unit of work's rollback | Not needed |

### Ordering of permanent records

- An attempt's **result is persisted before** the advancement that can
  complete the tournament. A64-019.6 reversed A64-019.5's order here after
  the eight-player placement test caught the deciding match counting for
  nobody.
- **Standings are written before** the transition to `COMPLETED`, in the same
  transaction, because `COMPLETED` is terminal and the reverse order would be
  unrepairable.
- Match origin and seat snapshots survive every state transition —
  guaranteed by `dataclasses.replace` rather than by hand-written copies
  (§3.2).
- Every event is enqueued in the transaction that wrote the state it
  announces (AD-16).

---

## 7. Rating integration

`specs/rating.md`'s termination allowlist is **unchanged**, and no change was
required.

| Path | Rating effect |
| --- | --- |
| Decisive tournament match, `rated` | Moves the global rating, like any other game |
| Drawn tournament match | Moves the global rating |
| Rematch after a draw | An independent rated game |
| Two draws → higher seed advances | **None** — no third match is created |
| No-show adjudication | **None** — the `game` match is left untouched |
| Bye | **None** — no match exists |

No `game` result is ever fabricated. The two adjudication paths write
`AttemptOutcome.NO_SHOW` or leave the attempt drawn, advance the bracket, and
touch nothing in `game`.

Rating calculations use the seat snapshot captured at match creation, which
now survives acceptance (§3.2) and every subsequent transition.

---

## 8. Performance

Measured against real PostgreSQL with a statement counter. No timing
assertion is made; the numbers below are query counts and are the reason no
cache was added.

| Operation | 8 entrants | 128 entrants | Behaviour |
| --- | --- | --- | --- |
| `register` (per player) | 3 | 3 | O(1) — lock, count, insert |
| Seed | 23 | 263 | O(n) — one `assign` statement per seed, documented at ≤ 128 |
| Materialise bracket | 10 | 10 | **Flat** — one flush for all `size − 1` nodes |
| Start (create round-one matches) | 36 (4 matches) | 396 (64 matches) | O(matches), ~6 each |
| `advance_winner` | 6 | 6 | **Flat** — one tree read plus the writes that changed |
| `GET /bracket` | 3 | 3 | **Flat** |
| `GET /` (detail) | 3 | 3 | **Flat** |
| `GET /standings` | 1 | 1 | **Flat** |
| `GET /players/{id}/tournaments` | 3 | 3 | **Flat** per page |

**The known concern is resolved.** "Bracket propagation may read the complete
tree" is true and is *one* bounded read of at most 127 rows, not an N+1: a
128-node bracket costs the same six statements per advancement as a two-node
one. Materialisation of a bye-heavy field costs one statement per resolved
bye (66 entrants in a 128-bracket: 72 statements) and happens **once** per
tournament.

No read path exhibits an N+1 over players, ratings or attempts. Attempts are
batched into the bracket read by a single `IN` query.

**No Redis, no cache, no denormalisation was added**, and none is justified
by these numbers.

---

## 9. Security and authorisation

- **Creation is not exposed.** There is no HTTP route that creates, opens,
  closes, seeds or starts a tournament (§4.2). Administrator mutations remain
  unavailable until the Administration epic.
- **The four reads are public** in the §7 sense — no viewer is narrower than
  another. They are still authenticated, like every route outside `/health`.
- Published fields are an explicit allowlist. Withheld: `created_by`,
  compare-and-set targets, no-show deadlines, attendance instants,
  `processed_event` and outbox rows, and every ORM model.
- An unknown tournament is `404`; there is no path on which a real one
  answers `403`, because a tournament is present for everybody or absent for
  everybody.
- No client-supplied identity is trusted: the reads take a path parameter for
  *whose* data to show and never for who is asking.
- Errors flow through the platform's handlers; no SQL, Python class name or
  stack trace reaches a client.
- **Private tournaments are not partially implemented.** There is no
  visibility column, no flag and no branch — the deferral is total, which is
  the only safe shape for a deferred privacy feature.

---

## 10. Reconciliation

| State | Action |
| --- | --- |
| Node owed a match, `game` has none | Launch it |
| `game` has a match, no attempt row | Record the attempt, numbered by creation order |
| Match decided or drawn, nothing followed | Re-apply through the consumer's own service |
| Attempt names a match `game` no longer has | **Reported** — `ERROR` + counter |
| Match ended with no result at all | **Reported** — `ERROR` + counter |

The two reported states are the same undecided question and are recorded in
`specs/tournament.md` OQ-2's successor note: who advances when a tournament
match ends with no result is not decided, and inventing an answer would write
a permanent competitive record nobody chose. A64-019.5H removed their most
common cause by making tournament matches system-activated, so a match can no
longer expire unanswered.

The reconciler is bounded (`DEFAULT_BATCH_SIZE = 20` tournaments per tick),
multi-worker safe (`FOR UPDATE SKIP LOCKED`), never raises, and guards every
tournament individually so one failure cannot cost the page its tick.

**Outcomes are bounded counters** — `scanned`, `launched`, `recorded`,
`advanced`, `orphaned`, `abandoned` — and no participant, match, pairing or
tournament id is used as a metric label. Identifiers appear only in log
`extra` fields, where cardinality is not a storage cost.

---

## 11. Events

| Event | Emitted when | Consumer |
| --- | --- | --- |
| `tournament.created` | The aggregate is written | None yet — documented |
| `tournament.registration_opened` | Transition committed | None yet |
| `tournament.registration_closed` | Manual close or deadline sweep | None yet |
| `tournament.round_published` | Seeding, and each later round opening | None yet |
| `tournament.started` | Move into play | None yet |
| `tournament.round_completed` | Every node of a round decided | None yet |
| `tournament.completed` | Standings materialised, in the same transaction | None yet |
| `tournament.cancelled` | Declared; nothing emits one (no cancellation path) | None yet |

Every emitted event corresponds to committed authoritative state, is
published through the outbox from an application service (never from a route
handler or a repository), and carries a bounded payload with no collection
and no nested aggregate.

**No duplicate semantic event exists.** A64-019.6 deliberately did *not* add
`tournament.results_materialized`: completion and materialisation are one
transaction, so a consumer that saw one and not the other would be observing
a state that cannot exist.

Unconsumed events are retained deliberately. `OutboxRelay` marks an entry no
handler wanted as published and counts it separately, so an unsubscribed
event costs one row — and the alternative, adding the producer later, would
mean the platform has no record of any tournament run before notifications
shipped. `tournament.cancelled` is the one event nothing *emits*; it is kept
because cancellation is a modelled lifecycle transition whose driver waits on
the Administration epic (OQ-1), and R-19's argument applies — a member added
after tournaments have been recorded makes every historical query wrong.

---

## 12. Retention

**Nothing in this subsystem is deleted or archived.** Current policy, stated
so the gap is visible rather than assumed:

| Relation | Policy |
| --- | --- |
| `tournaments.tournament` | Retained indefinitely |
| `tournaments.registration` | Retained indefinitely |
| `tournaments.round` | Retained indefinitely |
| `tournaments.pairing` | Retained indefinitely |
| `tournaments.pairing_attempt` | Retained indefinitely |
| `tournaments.standing` | Retained indefinitely |
| Outbox rows | The platform's own horizon (`OUTBOX_RETENTION_DAYS`) |

A **completed** tournament is permanent competitive history (A-4), and
retaining it is the correct policy rather than an absent one.

A **cancelled or never-started** tournament is different and is **unresolved**:
it is churn rather than history, it accumulates registrations and possibly a
whole materialised bracket, and nothing prunes it. It is the same class of
gap `matchmaking` closed for abandoned matches in A64-015.5, and it is
recorded here rather than implemented, because a horizon is a product
judgement (`how long is "why was my tournament cancelled" answerable?`) and
this phase adds no product decisions.

Note that `pairing_attempt.match_id` points at `game.match` with **no foreign
key** (DB-03), and `game` prunes abandoned matches on its own horizon. A
tournament attempt can therefore outlive the match it names — which the
reconciler already detects and reports (§10).

---

## 13. Documented limitations

Carried forward, all still true:

- Single elimination only; Swiss, round robin, double elimination, arena and
  team formats are deferred.
- Maximum **128** entrants, rounded up to a power of two.
- `SpeedClass.CLASSICAL` for every seeding read; per-tournament time control
  waits on `reference.time_control` (`specs/rating.md` OQ-1, OQ-2).
- No check-in, no waitlist, no late registration.
- No prizes, rewards, trophies, achievements or season points.
- No admin corrections, no manual result overrides, no cancellation driver.
- No private tournaments.
- No archival or deletion policy (§12).
- **Unresolved:** who advances when a tournament match ends with no result at
  all — abandoned, or (before A64-019.5H) unaccepted. Detected and reported,
  never guessed (§10).
- No tournament notifications; every event is published and unconsumed (§11).
- ~~No production entry point for the write path~~ — **closed by A64-019.8**
  (§4.2). What remains is narrower: there is no administrator in `auth` or
  `users`, so the operator lifecycle commands are a process entry point
  rather than an HTTP surface, and an `/api/v1/admin` API waits on the
  Administration epic.

---

## 14. Recommendations for the Frontend epic

1. **The read surface is stable and complete.** Four endpoints, all
   authenticated, all flat in query cost. Build against them directly.
2. **Ranks are not dense.** An eight-player bracket ends 1, 2, 3, 3, 5, 5, 5,
   5. Render the gap; do not renumber, or you publish a comparison nobody
   made.
3. **`advancement_reason` is what a bracket cell should render**, not the
   winner alone: `played`, `bye` and `adjudication` are three visually
   different outcomes, and only the first is a game somebody won.
4. **A node can carry two attempts.** Render both — the second is the
   side-swapped rematch, and hiding it makes a drawn pairing look like a
   single game with no result.
5. **Follow `attempts[].match_id` to `GET /matches/{id}/replay`** for the
   game itself. The bracket deliberately reconstructs no replay.
6. **`final_rank` and `final_status` are `null` while a tournament runs.**
   That is the signal to render "in progress" rather than a placing.
7. **Page player history with the opaque `next_cursor`** and send it back
   unread. Do not construct one.
8. **Players can now enter and leave a tournament over HTTP** —
   `POST /tournaments/{id}/registrations` and
   `DELETE /tournaments/{id}/registrations/me`, both acting only on the
   authenticated user. Build the lobby's join and leave against those.
9. Refusals are bounded codes, not messages: branch on `tournament_full`,
   `already_registered`, `registration_deadline_passed` and
   `registration_not_open` rather than on prose.
10. **Nothing an ordinary user can call creates or starts a tournament**, and
    that is deliberate — those are operator commands run on the host until
    the Administration epic ships a role. A frontend admin panel has no API
    to call yet.
