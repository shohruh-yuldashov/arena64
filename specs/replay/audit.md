# Match History and Replay — Audit

> **Status:** Complete — A64-018.4, the closing task of the Game History & Replay Epic
> **Owner:** _Unassigned_ · **Audited:** 2026-08-05
> **Scope:** `game.public.history`, `game.application.services.match_{history,visibility}_service`,
> `game.infrastructure.repositories.match_history_repository`, `game.presentation.{router,schemas}`
> **Related:** [`specs/replay.md`](../replay.md), [`specs/live-game/audit.md`](../live-game/audit.md)

## Readiness

# READY WITH DOCUMENTED LIMITATIONS

A completed match is listed, its visibility is decided by a rule that lives in one place, a
supported version replays ply by ply from the durable log, and an unsupported one keeps its
metadata while refusing reconstruction. Every path is reached by a test that issues a real HTTP
request through the built application.

**One defect was found and fixed** (§1). Five limitations remain, all recorded decisions.

---

## 1. The defect: the move log was read before the version was checked

**Finding: an unsupported match paid a full log read. Fixed.**

`PersistedMatchReplay.replay_data` loads `for_replay(match_id)` — every ply — and only then does
`ReplayEngine` examine the engine version. SPEC-REPLAY §4 says *no attempt is made*; the
reconstruction was indeed not attempted, but the log was read and discarded, which for a long
match is the whole cost of the thing being refused.

`GameMatchReplay`'s own docstring claimed the opposite. That claim is now corrected rather than
quietly deleted.

**Fix:** `VisibleMatchReplay` refuses from the match entry it is already holding, so the API path
reads one row. `GameMatchReplay` used directly still loads the log — documented rather than
hidden, because nothing on the API path reaches it that way.

Regression: `test_an_unsupported_version_is_refused_without_reading_the_move_log` gives the match
a log whose hashes would fail a replay, and asserts the answer is still
`unsupported_engine_version` rather than a hash mismatch.

---

## 2. Reachability

| Entry point | Constructed in | Called by | Registered | Reached by |
| --- | --- | --- | --- | --- |
| `history_router` | `game/presentation/router.py` | FastAPI | `api/v1/router.py` | `test_a_rated_match_is_public_and_a_casual_one_is_not` |
| `replay_router` | same | FastAPI | same | `test_a_hidden_match_and_an_unknown_match_are_indistinguishable` |
| `get_visible_history` → `VisibleMatchHistory` | `game/presentation/dependencies` | `player_match_history` | `Depends` | `test_a_participant_sees_their_own_casual_match_and_the_opponent` |
| `get_visible_replay` → `VisibleMatchReplay` | same | `match_replay` | `Depends` | `test_an_unsupported_version_returns_its_own_stable_code` |
| `get_match_history` → `GameMatchHistory` | same | the two above | `Depends` | every history test |
| `get_match_replay` → `GameMatchReplay` | same | `VisibleMatchReplay` | `Depends` | supported-version replay tests |
| `SqlAlchemyMatchHistoryRepository` | `get_match_history` | `GameMatchHistory` | via the factory | same |
| `MatchHistoryResponse.of` / `MatchReplayResponse.of` | the handlers | the handlers | — (pure mappers) | every API test reads the body |
| `decode_cursor` | the handler | the handler | — | the pagination walk |

**Every API test issues a real request through the built app**, so the route, its registration,
`CurrentUser`, the factories, the service, the repository and the mapper are all on the asserted
path. A route file without router registration would fail these, not pass them.

**Background entry points:** `tests/unit/test_reachability.py` (A64-018.1) asserts all 19 are
named by a composition root, and is proven non-vacuous — with the rating consumer's name removed
it reports exactly that class. This epic added none, so the count is unchanged. That is the
reusable contract §2 asked for, and it replaces the one-off checks the previous two audits made
by hand.

---

## 3. Schema and persistence

Verified against an **Alembic-built** database (`tests/contract/test_schema_drift.py`), not
`create_all`.

| Check | Result |
| --- | --- |
| Metadata versus migration drift | **None** — the drift contract passes |
| History query index | `ix_match__*` on the status/player predicates; ordering is `created_at DESC, id DESC` and both are on the row |
| Move ordering gap-free | `uq_move__ply` and `ix_move__replay`; `ReplayEngine._require_contiguous` raises on a gap |
| Production repositories readable | `test_a_match_row_can_be_written_and_read_back` |
| Version checked before the log | §1 — **was not**, now is on the API path |

**No migration was created.** No schema gap was found; §1 was a code-ordering defect.

---

## 4. Privacy and security

| Check | Result |
| --- | --- |
| Rated history public | ✓ asserted |
| Casual history participant-only | ✓ asserted |
| Hidden and unknown identical | ✓ **same status and same code**, asserted directly |
| Player identity from payload | **Never.** The viewer is `CurrentUser`; `player_id` says whose history, the token says who asks |
| Cursor errors | One `invalid_cursor` for every decode failure — distinguishing them would describe the encoding to whoever is probing it |
| Replay errors | Stable codes only. No class name, no stack trace; the platform handler maps by exception type |
| Raw ORM returned | **Never.** Every response is a Pydantic model built from `game.public` values |
| Identifiers as metric labels | **None** — this epic added no metrics |

---

## 5. Pagination

Keyset, newest first, `(created_at DESC, id DESC)`. `id` is unique, so the order is **total** and
a cursor cannot skip or repeat. No `OFFSET` anywhere. The cursor round-trips through
`encode_cursor`/`decode_cursor` and is opaque.

**Documented behaviour: visibility filtering makes pages sparse.** The query returns `limit` rows
and the filter removes hidden ones, so a stranger paging a mostly-casual record sees short pages —
sometimes empty — while `next_cursor` is still set. That is accepted, not a defect: the
alternative is filtering inside the query, which SPEC-REPLAY §3 rejects because the cursor would
then come from a row the caller cannot see.

**Nothing is lost or repeated**, which is the property that matters, and
`test_visibility_filtering_makes_pages_sparse_without_losing_entries` walks every page to prove
it. A client should page until `next_cursor` is `null` rather than until a page is short.

---

## 6. Replay correctness

Reproduced through the production `ReplayEngine` path from persisted data: the opening position
(derived from the variant, never stored), every ply in order, full paths, captured squares,
promotions, a fingerprint the **engine** produced, clocks and think time where stored, the final
result, and the engine version.

The position hash on each row is *checked* against the recomputation rather than returned — a
replay that produced a different board fails instead of reporting the stored value. The engine
corpus is not duplicated: these tests assert that what was persisted is what comes back.

---

## 7. Performance

| Operation | Queries | Cost |
| --- | --- | --- |
| First history page | 1 | One index scan, `limit + 1` rows |
| Deep keyset page | 1 | Identical — keyset does not degrade with depth |
| Short replay | 2 | Match row + full log, then one engine application per ply |
| Long replay | 2 | Same, linear in ply count |
| Unsupported-version rejection | **1** | One row, since §1's fix. Was 2 including the whole log |

**No timing assertions.** Query counts and asymptotics are the durable facts; a laptop-specific
threshold is a flaky test.

**Asymptotic risks:** a replay loads the whole move log and applies every ply, so it is linear in
the game — bounded above by the draw rules, which terminate it. The plies are replayed a second
time to produce per-ply boards, which doubles a constant rather than changing the order; it is
recorded here because it is the first thing to remove if replay latency ever matters.

**No caching added.** A finished match is immutable, so the correct cache is HTTP's, and adding
Redis without a measurement would duplicate an append-only record for no demonstrated need.

---

## 8. Retention

**Accepted v0.6.0 policy: append-only.** Completed matches and their move logs are never deleted
and never archived. No deletion policy, no archival policy.

`specs/live-game/audit.md` §6 recorded the growth; this epic is the feature that reads it. Two
rows per rated match in `rating`, one row per ply in `game.move`, one per game in `game.match` —
all unbounded. The first move when it matters is monthly range partitioning on `created_at`; the
access pattern is "one player's recent matches" and "one match's moves", never a date range.

Recorded as a **documented limitation**, not implemented.

---

## 9. Architecture

| Rule | Result |
| --- | --- |
| Replay internals behind `game.public` | ✓ `ReplayEngine` stays in `game.domain`; consumers receive boards |
| Presentation imports `game.domain` | **None** — the router imports `game.public` and `game.presentation.*` only |
| Replay is read-only | ✓ no write path exists on either reader |
| Engine logic duplicated in API or mappers | **None** — mappers convert values; the rules run once, in `game` |
| `import-linter` | **24/24 green** |
| Circular imports | None — the suite imports the whole app |
| PDN and analysis | Deferred, `specs/replay.md` §2 and §8 |

---

## 10. Limitations

1. **No PDN export.** The dialect is an undecided rules question — OQ-1.
2. **No analysis playback.** Needs engine evaluation, which does not exist — OQ-2.
3. **No archival or deletion policy** — §8.
4. **Only supported engine versions replay.** Version 2 today; others keep their history and
   refuse reconstruction.
5. **Replay cost is linear in move count**, and the log is loaded whole — §7.
6. **Visibility filtering makes pages sparse** — §5, documented rather than redesigned.
7. `GameMatchReplay` used **outside** `VisibleMatchReplay` still loads the log before refusing —
   §1. No API path does.
