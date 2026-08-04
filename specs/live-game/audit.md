# Live Game — Audit and Stabilisation

> **Status:** Complete — A64-016.8, the closing task of the Live Game Epic
> **Owner:** _Unassigned_
> **Audited:** 2026-08-04
> **Scope:** `apps/api/app/gateway/`, `apps/api/app/modules/game/`, the `friends`, `users` and
> `auth` surfaces the gateway consumes, `docs/01-architecture/websocket.md`,
> `docs/01-architecture/caching.md`, `specs/spectator.md`
> **Related:** [`websocket.md`](../../docs/01-architecture/websocket.md),
> [`specs/matchmaking/audit.md`](../matchmaking/audit.md),
> [`specs/game-engine/audit.md`](../game-engine/audit.md), architecture.md AD-03, AD-09, AD-10,
> AD-11, AD-18, AD-19, AD-20, AD-21, R-7

## Readiness

# READY WITH DOCUMENTED LIMITATIONS

A player can authenticate a socket, join a room, submit a move, have it validated by the engine,
charged against an authoritative clock, appended durably, settled when it ends, fanned out to
both participants and any audience, replayed after a disconnect, adjudicated on time by a worker,
and watched by a third party — and every one of those paths is covered by tests that run the real
services against real PostgreSQL and real Redis.

**One defect was found and fixed in this audit** (§1). It was not cosmetic: the cross-node
transport had a writer and a reader and no loop between them, so every multi-node deployment lost
every cross-node frame while reporting them delivered.

Five limitations remain, each a decision rather than an oversight:

1. **`game.move` and completed matches grow without bound** (§6). Retention reaches only
   `cancelled` and `expired` matches. That is correct product behaviour — a played game is
   history — but nothing on this platform has stated the sizing consequence until now.
2. **A snapshot is an uncached full replay** (§5). Every reconnect and every spectator join
   replays the match's whole move log through the engine. Correct, and linear in the length of
   the game.
3. **The spectator eligibility policy is a defaulted product decision, not a specified one**
   (§4). Documented in `specs/spectator.md` §3 with what it costs.
4. **`game.move` append-only is held by code, not by a grant** (§7). Carried over from A64-016.4
   and unchanged: a runtime role without `UPDATE`/`DELETE` on that table is a privilege change.
5. **Metrics are still emitted as structured log records** (§8). Carried over from A64-015.6.
   The port, call sites, labels and aggregation are real; only the exporter is not.

None blocks a single-node production deployment. Item 1 is the one to revisit first.

---

## 1. The defect: a bus with no consumer

**Finding: `RedisStreamGatewayBus.consume` had no caller outside tests. Fixed.**

Evidence, before the fix:

```
$ grep -rn "\.consume(" app --include="*.py"
(no result inside app/gateway/)
```

`BusRemoteNodePublisher.publish` wrote to `gwbus:v1:<node_id>` and returned `True`.
`REMOTE_PUBLISHES` counted it as a success. The entry was then trimmed by `MAXLEN ~` or expired
by `GATEWAY_BUS_STREAM_TTL_SECONDS`, and no process ever read it.

**Consequence on more than one node:** a player whose opponent was registered on a different
gateway process never received their moves. Every metric on both nodes reported health —
`REMOTE_PUBLISH_FAILURES` stayed at zero, because publishing genuinely succeeded.

This is the failure mode A64-016.3 warned about for `LoggingRemoteNodePublisher` and which
A64-016.5 was supposed to close. It closed half of it: the adapter was written, tested against
real Redis (`tests/contract/test_live_clock.py`), and never wired to a loop.

**The fix**, in this task:

| Piece | File |
| --- | --- |
| `GatewayForwarder.forward_once` — one bounded, non-raising pass | `app/gateway/forwarding.py` |
| `GatewayForwardingTask` on the `realtime` queue | `app/gateway/forwarding_tasks.py` |
| Handler and `PeriodicTaskScheduler` registration | `app/app_factory.py` |
| `GATEWAY_FORWARDING_ENABLED`, `_INTERVAL_SECONDS`, `_BATCH_SIZE` | `app/config/settings.py` |
| `FORWARDED_FRAMES`, `FORWARDING_FAILURES` | `app/gateway/metrics.py` |

`FORWARDED_FRAMES` is the counter that would have made the gap visible, and is the one to alert
on: **`REMOTE_PUBLISHES` rising while `FORWARDED_FRAMES` stays at zero is a fleet whose nodes
cannot talk to each other.**

A scheduled pass rather than a blocking `XREADGROUP`, because a blocking read parks a Redis
connection for its whole duration and the failure when that connection drops is a node that
silently stops receiving — the exact defect being fixed. The cost is a bounded worst case of one
`GATEWAY_FORWARDING_INTERVAL_SECONDS` (0.25s by default) added to a cross-node move.

---

## 2. End-to-end flow

Verified by reading each seam and by the tests named, not by inspection alone.

| Step | Mechanism | Proven by |
| --- | --- | --- |
| Ticket minted and redeemed once | `GETDEL` on `wsticket:v1:<digest>` (AD-09) | `tests/contract/test_auth_api.py`, `tests/unit/test_gateway_connection.py` |
| Connection registered fleet-wide | `gwconn:v2:<player_id>`, member `connection_id\|node_id` | `tests/contract/test_gateway_redis.py` |
| Presence transitions from the write's own return | `register` → 1 means first; `unregister` → 0 means last | `test_gateway_connection.py::TestMultipleConnections` |
| Room join refused for non-participants | `game.public.MatchRosterReader` | `TestGameRooms` |
| `received_at` captured at the frame boundary | `_read_loop`, before dispatch (MT-9) | `TestMoveSubmission` |
| Move validated by the engine, not the gateway | `SubmitMoveUseCase`, R-7 | `lint-imports` contract + `TestMoveSubmission` |
| Clock charged, deadline superseded | `ClockState.charged` + one Lua script | `tests/contract/test_live_clock.py` |
| Move appended durably, ply serialised | `uq_move__ply` under `FOR UPDATE` | `tests/contract/test_move_log.py` |
| Terminal settlement written once | `MatchRecord.completed` in the move's transaction | `test_move_log.py` |
| Fan-out to participants and audience | one `RoutingPlan`, one `deliver` | `TestRoutingPlanTransport`, `TestSpectating` |
| Cross-node frames delivered | `GatewayForwarder` | `TestCrossNodeForwarding` |
| Buffered before delivered | `append` precedes `deliver` in `_broadcast` | `TestReconnection` |
| Reconnect: events, snapshot or resync | continuity proven by the **oldest** entry | `TestReconnection`, `tests/contract/test_state_sync.py` |
| Timeout adjudicated off the request path | `ClockAdjudicationTask`, `realtime` queue (AD-20, AD-21) | `tests/contract/test_live_clock.py` |
| Spectator admitted, refused, or dropped | `BlockAwareSpectatorPolicy`, `gwspec:v1:` | `TestSpectating`, `tests/contract/test_spectating.py` |
| Cleanup on all five disconnect paths | one `finally` around a block entered once | `TestCleanup` |

**No step is a stub.** `LoggingRemoteNodePublisher` and `InProcessGatewayBus` remain in the tree
as the single-node adapters they are documented to be; the production path is
`BusRemoteNodePublisher` over `RedisStreamGatewayBus`, wired in `app_factory`.

---

## 3. Redis contract audit

Every keyspace the Live Game epic introduced, checked against caching.md's own rules C-1
(registered), C-2 (versioned), C-3 (expiring), C-8 (one writer) and AD-03 (role separation).

| Keyspace | Role | C-1 | C-2 | C-3 | C-8 | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| `clock:v1:deadlines` | `live` | now | ✓ | **by claim, not TTL** | ✓ `game` | Correct — see below |
| `gwbus:v1:<node_id>` | `bus` | now | ✓ | ✓ TTL + `MAXLEN ~` | ✓ gateway | Correct |
| `gwevent:v1:<match_id>` | `cache` | now | ✓ | ✓ TTL + rank cap | ✓ gateway | Correct |
| `gwspec:v1:<match_id>` | `cache` | now | ✓ | ✓ by score + key TTL | ✓ gateway | Correct |
| `gwspecconn:v1:<connection_id>` | `cache` | now | ✓ | ✓ key TTL | ✓ gateway | Correct |

**"now" is the finding.** None of the five was in caching.md §8.1 before this audit — C-1 says a
keyspace is registered before it ships, and five were not. Registered in this task, with growth
figures in §8.2.

**`clock:v1:deadlines` has no TTL, and that is correct.** It is a sorted set whose members are
removed when claimed or superseded, so it is bounded by the number of *live timed matches* rather
than by time. A TTL on the key would delete deadlines for matches still being played, which is
the one thing that must never happen: a lost deadline is a match that hangs instead of flagging.

**Role separation holds.** `clock:v1:` and `game:live:v1:` are on `live` because losing either
costs a game; everything the gateway derives is on `cache`; cross-node traffic is on `bus`,
which AD-03 allocated and nothing used until A64-016.5. Verified by reading every construction
site:

```
$ grep -n "pools\.\(live\|cache\|bus\)" app/gateway/dependencies.py \
      app/modules/game/presentation/dependencies/__init__.py
```

**`game:live:v1:` changed posture and the document had not caught up.** caching.md called it
"the one keyspace on this platform that is not reconstructible". Since A64-016.4 the durable move
log makes it a cache of a replay. Corrected.

---

## 4. Security

| Check | Result |
| --- | --- |
| Player identity taken from a payload | **None.** `grep 'payload.get("player' app/gateway` is empty; no client-sendable frame has a player field |
| Identifiers in metric labels | **None.** No `match_id`, `player_id`, `connection_id`, `request_id` or `node_id` in any `labels={...}` |
| Board state or move paths in logs | **None.** No `path`, `fingerprint` or `pieces` in any `extra={...}` |
| Error codes distinguishing unknown from forbidden | **Correct.** `not_a_participant` covers an unknown match; `not_spectatable` covers both; live match identifiers stay unenumerable |
| Internal exceptions reaching a client | **None.** Every handler maps to a `GatewayErrorCode`; `_rejection_for` and `_REFUSAL_CODES` are exhaustive mappings, so an unmapped case fails at import |
| Node identity in a client payload | **None.** `node_id` is registry-internal |
| A failed block check | **Refuses.** `BlockAwareSpectatorPolicy._is_blocked` returns `True` on error — admitting would make a database blip a privacy bypass |

**The one place a coarse code is deliberately not coarse** is `RejectionReason.INVALID_TICKET`
versus `REGISTRATION_FAILED` on the handshake. That distinguishes the *client's* problem from the
*server's*, which the client needs in order to retry correctly, and discloses nothing about
another user. Argued in `GatewayConnectionService._refuse`.

---

## 5. Concurrency and performance

### 5.1 Concurrency

| Contention | Mechanism | Why not something else |
| --- | --- | --- |
| Two moves on one match | `SELECT ... FOR UPDATE` **without** `SKIP LOCKED`, then `uq_move__ply` | A skipped lock is a move silently dropped. The unique index is the belt to the lock's braces |
| A move racing the clock worker | The same row lock; the adjudicator re-checks ply, side and expiry inside it | Ordering cannot be relied on; the lock makes one of them observe the other's result |
| Two adjudication passes | `ZRANGEBYSCORE` + `ZREM` in one Lua claim | Two workers must take disjoint sets |
| Retention sweeps | `FOR UPDATE SKIP LOCKED` | Here skipping is right: another worker is already deleting that row |
| A repeated `spectator.join` | `ZADD` on `(player, connection)` | Idempotent by member |
| A redelivered bus frame | Idempotent by ply at the client | Cheaper than exactly-once |

**No process-local lock is a correctness mechanism anywhere in the tier.** `InMemoryLocalSockets`
is process-local by nature — a socket is this process's file descriptor — and the fleet-wide
question of which node holds a connection is `gwconn:v2:`'s.

### 5.2 Performance

| Path | Cost | Assessment |
| --- | --- | --- |
| One move | 1 row lock, 1 insert, 1 Redis script, 1 plan (1 read per recipient), N socket writes | Bounded by recipients |
| One fan-out to a room | 2 registry reads + 1 spectator read | No N+1 — the plan is batched and deduplicated |
| One reconnect | 1 buffer read, or a full replay | See below |
| One spectator join | 1 snapshot (full replay) + 1 block read | See below |
| One forwarding pass | 1 `XREADGROUP` per node per interval | 4/s per node at the default |

**A snapshot is an uncached full replay, and that is the accepted cost.** `GameMatchSnapshot`
reads the whole move log and applies every ply through the engine. It is linear in the length of
the game, uncached, and paid on every reconnect and every spectator join.

It is correct rather than merely acceptable: the durable log is the source of truth, the replay
is the same code path live play uses, and a cache would be a second thing able to disagree with
the position. It is also bounded — the draw rules terminate a game, so plies do not grow without
limit — and rare, because the common reconnect misses one or two plies and is answered from the
buffer without a snapshot.

**What has not been done is measure it.** Nothing here has been profiled under load, and
CLAUDE.md §10.1 forbids optimising without a number. The first move when there is one is a
snapshot cache keyed by `(match_id, ply)`, which is trivially invalidated because the ply changes
on every move.

---

## 6. Retention

**Finding: completed matches and their move logs are never deleted.**

```
_ABANDONED = (MatchRecordStatus.CANCELLED, MatchRecordStatus.EXPIRED)
```

`SqlAlchemyMatchRetentionStore.delete_settled_before` claims through `ix_match__abandoned`, whose
predicate excludes every other status. A `completed` match cannot be reached by any configuration
of the retention horizon — deliberately, and `ix_match__abandoned`'s docstring says so: the index
predicate is the safety property.

That is the right behaviour. A played game is history: it is what a profile, a rating dispute and
a replay are built from, and a platform that deleted finished games after ninety days would be
deleting the product.

**The consequence, stated because nothing stated it before:**

| Relation | Grows with | Bounded by |
| --- | --- | --- |
| `game.match` (`completed`) | Every game ever finished | **Nothing** |
| `game.move` | Every ply ever played, `ON DELETE CASCADE` from its match | **Nothing** |

At a draughts game's ~40–80 plies and ~200 bytes per row, a million games is roughly 60 million
move rows — sizeable but ordinary, and served by `ix_move__replay` for the only query that reads
them. What does not exist is a stated policy, an archival tier, or a partitioning strategy. The
first move when the table is large enough to matter is monthly range partitioning on
`created_at`, because the access pattern is "one match's moves" and never "a date range".

Everything else in the epic is bounded: every Redis keyspace has a TTL or a rank cap (§3), and
abandoned matches are swept by `matchmaking`'s existing horizon.

---

## 7. Architecture

| Rule | Result |
| --- | --- |
| R-7 — the gateway contains no domain logic | **Held.** `lint-imports` contract `gateway-reaches-modules-through-public` forbids `game.domain`, `game.application`, `game.infrastructure`, `engine` and every other module's internals. 21/21 contracts pass |
| Gateway → `game` only via `game.public` | Held. Four types: `SubmitMoveUseCase`, `MatchRosterReader`, `MatchSnapshotReader`, and the error taxonomy |
| Gateway → `friends` only via `friends.public` | Held. `PairingExclusions`, composed by `friends.presentation.dependencies` |
| `app.platform` imports no bounded context | Held |
| Domain layers import no framework | Held |
| Redis stores behind ports | Held. Every store is a `Protocol` in `ports.py` or `bus.py`, injected at a composition root |
| FastAPI routes are thin | Held. `app/gateway/router.py` is 84 lines and imports no repository or store |
| No direct Celery dependency | Held. Both background jobs are `platform.tasks.TaskHandler` (AD-17) |

**One structural change was made in this audit beyond the fix**: `snapshot_payload` moved from
`resume.py` to `app/gateway/projections.py`, so the resume path and the spectator path share one
projection rather than two that must be kept in step. A64-016.7 introduced the second consumer,
which is the third-use-case threshold CLAUDE.md §1.7 asks for.

**`game.move` append-only remains held by code, not by a grant.** There is no `UPDATE` or
`DELETE` method on `SqlAlchemyMoveLogRepository` and no transition on `MoveRecord`, but a runtime
role without those privileges on that table would make it structural. Carried over from
A64-016.4 unchanged; it is a privilege change rather than a migration.

---

## 8. Observability

| Signal | Exists | Notes |
| --- | --- | --- |
| Connections accepted, rejected, closed by reason | ✓ | Bounded label sets |
| Connection duration | ✓ | Histogram |
| Route resolutions by locality | ✓ | By locality, never by node — cardinality grows with the fleet otherwise |
| Local deliveries, remote publishes, publish failures | ✓ | |
| **Forwarded frames, forwarding failures** | ✓ | **New in this task.** The round-trip counter §1 needed |
| Moves accepted and rejected by reason | ✓ | |
| Resumes by outcome | ✓ | `current`, `incremental`, `snapshot`, `resync_required`, `not_a_participant` |
| Spectator joins, rejections, leaves, delivery failures | ✓ | |
| Clock adjudication claimed/settled/superseded/failed | ✓ | |

**Every counter reaches `MetricsFlushTask`**, because `get_gateway_metrics()` returns
`process_metrics()` — the same accumulator the composition root and every HTTP route write into.
That was the defect A64-015.6 found and closed, and the gateway was built on the fixed shape.

**The exporter is still a structured log record.** There is no Prometheus, StatsD or
OpenTelemetry collector in this deployment. Carried over from `specs/matchmaking/audit.md` §7
unchanged.

---

## 9. Test suite

| Suite | Count | What it proves |
| --- | --- | --- |
| `tests/unit` | 2633 | Handler decisions, protocol codec, domain rules, fan-out arithmetic |
| `tests/contract` + `tests/integration` | 1043 | The same paths against real PostgreSQL and real Redis |
| **Whole suite** | **3676 passed, 2 skipped, 0 failed** | Run in full for this audit, not sampled |

Live-game-specific contract coverage — `test_live_clock.py`, `test_move_log.py`,
`test_state_sync.py`, `test_spectating.py` — is 17 tests, each asserting something **only** the
real store can prove: Lua atomicity, `FOR UPDATE` serialisation, `ZADD` idempotence, score-range
expiry, and consumer-group acknowledgement.

The split is deliberate and is stated in each contract file's docstring: a fake models the two or
three rules a decision rests on, and the contract test proves the store actually behaves that
way. Neither duplicates the other.

---

## 10. Migrations

Verified on a **clean database**, not on a developer's own — a round trip against a database that
has already been migrated proves only that it is already migrated.

```
$ POSTGRES_DSN=…/arena64_migrationcheck alembic upgrade head    28 migrations
$ … alembic downgrade base                                     to empty
$ … alembic upgrade head                                       28 migrations
$ … alembic heads                                              076977bf9233 (head)
```

**Exactly one head**, and the round trip is clean in both directions. The two Live Game
migrations — `6926ccefaef6` (durable move log and settlement) and `076977bf9233` (clocks) — apply
and reverse without manual intervention.

One asymmetry is deliberate and is recorded in `076977bf9233` itself: the `completed` member
added to `game.match_status` is **not** removed on downgrade. Rebuilding a PostgreSQL enum with
seven dependent objects fails on the type comparison, so the downgrade instead refuses to run
while any row is `completed`. That is the safe direction — a downgrade that silently orphaned
settled matches would be worse than one that stops.

**An operator note that is not a repository defect:** a development database migrated to
`076977bf9233` *before* `received_at` was added to that revision will fail to downgrade, because
the column the downgrade drops was never created. The fix is to rebuild that database; the
migration file is correct, as the clean round trip above proves.

---

## 11. Remaining technical debt

| Item | Cost today | First move |
| --- | --- | --- |
| `game.move` unbounded (§6) | None at current scale | State a policy; partition on `created_at` when it matters |
| Snapshot is an uncached replay (§5.2) | Linear per reconnect and per spectator join | Measure first; then cache on `(match_id, ply)` |
| Spectator policy is defaulted, not specified (§4) | A product decision is encoded in code | `specs/spectator.md` §6 |
| No spectator delay | AD-10's engine-assistance concern unmitigated | A product decision, then one function |
| Append-only by code, not grant (§7) | A future writer could mutate the log | A privilege change |
| Metrics exporter is a log record (§8) | No dashboards | Deployment work |
| Draw thresholds undecided | Three of four draws cannot fire | `specs/game-engine/audit.md` §8 |

---

## 12. Before the next epic

1. **Decide the spectator delay, or record that there is none.** It is the only open item that
   is a *competitive-integrity* question rather than a scaling one.
2. **Put `FORWARDED_FRAMES` on a dashboard before running a second gateway node.** §1's defect
   was invisible precisely because no signal distinguished "published" from "delivered", and the
   new counter is only useful if somebody is looking at it.
3. **State a retention policy for finished games** (§6), even if the policy is "keep forever".
   An unbounded relation with no stated intent is one nobody knows whether they may prune.
