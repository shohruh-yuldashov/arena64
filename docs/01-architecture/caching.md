# Redis Keyspaces, Caching and TTL Policy

> **Status:** Approved for the keyspaces that exist — `rl:v1:`, `presence:v1:` and Celery's.
> Sections marked *Not yet allocated* describe workloads with no implementation.
> **Owner:** Backend platform
> **Last reviewed:** 2026-08-01 (A64-012.8)

## Purpose

Redis on this platform is not one store. It is five role-separated instances
(architecture.md AD-03) carrying at least eight distinct workloads, written by
processes that deploy independently of each other. A key written by the realtime
gateway is read by the HTTP API; a key written by a Celery worker is read by
nothing else at all.

That makes the keyspace a **contract between separately deployed processes**, in
exactly the way a database schema is — and unlike a schema, nothing enforces it.
There is no migration, no `NOT NULL`, and no error when two builds disagree: a
renamed prefix is simply a feature that stops working, silently, for whichever
half rolled first.

This document is the registry. Every namespace in use is listed below with its
owner, its instance, its TTL and its expansion path.

## Scope

Server-side Redis usage: key naming, ownership, expiry and failure behaviour.

Excludes: PostgreSQL (see [`database.md`](./database.md)), HTTP and CDN caching
headers, and client-side memoization.

---

## 1. Rules

| # | Rule | Why |
| --- | --- | --- |
| C-1 | **Every namespace appears in the registry below.** | A key nobody documented is one nobody can reason about during an incident, and one the next module will collide with. |
| C-2 | **Every key carries a version segment** (`presence:v1:`). | The one migration a keyspace needs is a *shape* change, and two shapes must be able to coexist while a fleet rolls. Without a version the choices are a flag day or an ambiguous keyspace. |
| C-3 | **Every key has a TTL, or a documented reason it does not.** | Redis is sized for a working set. A namespace that only grows is an outage waiting for enough traffic (CLAUDE.md §10.5). |
| C-4 | **The TTL is set in the same command as the value** where the client allows it. | `SET k v PX n` cannot leave a key without an expiry; `HSET` followed by `PEXPIRE` can, and a crash between the two is a key that never dies. |
| C-5 | **A key is never the sole record of anything competitive** (AD-19). | Redis is configured for speed and subject to eviction. A rating that exists only in Redis is one an eviction policy can delete, with no recovery path. |
| C-6 | **Redis keys never reach a client, a log line or an error message.** | A key embeds a `player_id` and sometimes a session identifier. Logging it puts both in a system with broader read access and different retention than the store (services.md §8.5). |
| C-7 | **A read failure degrades; it does not propagate.** | Every workload below except rate limiting is cosmetic or reconstructible. Failing a request because an indicator could not be computed converts a Redis blip into a user-visible outage (system-design.md T-2). |
| C-8 | **A namespace is owned by exactly one module**, which is the only writer. | Two writers with different shapes is the failure C-2 cannot fix, because both are "current". |

---

## 2. Instances (AD-03)

Five roles, five independent connection pools (`app/database/redis.py`), one URL
each (`RedisSettings`). In `local` all five point at one process on different
database indices; in a deployed tier each is its own instance, sized and
persistence-configured for its own workload.

| Role | Persistence | Eviction | Carries |
| --- | --- | --- | --- |
| `live` | AOF (AD-18) | none | Live match position and clocks. The only role whose loss interrupts play. |
| `bus` | none | n/a | Pub/sub fan-out. No keys — channels only. |
| `broker` | Celery's own | Celery's own | Task queues. Celery owns the keyspace entirely. |
| `cache` | none | yes, by design | Response cache, read models, **presence**. Everything here is reconstructible or expendable. |
| `limits` | none | **none — configured never to evict** | Rate limit counters. A counter evicted under memory pressure is a limit that disappears during exactly the traffic spike it exists for. |

**Why presence is on `cache` and not a sixth role.** Presence is derived,
expendable, self-expiring, and its loss is a cosmetic defect
(system-design.md §626) — which is precisely the posture `cache` is configured
for. It is deliberately not on `live`, because a reconnect storm after a deploy
is a write burst of one key per returning player and must not compete with the
positions of games in progress (AD-03's own worked example); and not on
`limits`, which is configured to evict nothing and would be put under pressure
by exactly this workload. See *Future expansion* below for when that changes.

---

## 3. Namespace registry

### 3.1 `rl:v1:<rule>:<digest>` — rate limit counters

| | |
| --- | --- |
| **Owner** | `app/database/rate_limiter.py` (platform) |
| **Instance** | `limits` |
| **Structure** | Sorted set — one member per request, scored by arrival time in milliseconds |
| **TTL** | The rule's own window, reset by `PEXPIRE` on every write. So a key outlives its last request by at most one window. |
| **Written by** | The HTTP API, on every guarded endpoint |
| **Read by** | The same, atomically — read, prune, count, decide and write are one Lua script |
| **On failure** | `RateLimitSettings.fail_open` decides: allow (default) or `503`. Never `429` — "our dependency is down" is not "you did too much". Logged at `ERROR`, because six endpoints are then running unprotected. |

The member carries a nonce, because two requests in the same millisecond would
otherwise be one member — `ZADD` overwrites by member, not by score, so a
limiter keyed on the timestamp alone under-counts precisely when traffic is
fastest.

The subject is a **digest**, not an address or an email: the key is derived by
hashing, so a Redis instance holding rate-limit state holds no addresses and no
account identifiers in plaintext.

*Future expansion:* a per-endpoint policy currently lives in each module's
`presentation/rate_limits.py` and the numbers in `RateLimitSettings`. Adding a
rule adds a name, not a namespace.

### 3.2 `presence:v1:<player_id>` — online presence

| | |
| --- | --- |
| **Owner** | `users` (domain-model.md §299) — `app/modules/users/infrastructure/presence/` |
| **Instance** | `cache` |
| **Structure** | String, holding one JSON object: `online`, `last_seen`, `session_id`, `device_type` |
| **TTL** | `PRESENCE_TTL_SECONDS`, default 60, reset on every write via `SET ... PX` |
| **Written by** | The realtime gateway (AD-09) — **not yet implemented**, so no key currently exists in any deployment |
| **Read by** | The HTTP API, composing a public profile |
| **On failure** | Returns "unknown", indistinguishable from a hidden, expired or never-recorded record. Logged at `WARNING` — an operator wants to know; nobody should be paged. |

**The TTL is the liveness protocol, not a tuning knob.** Nothing tells the
platform that a gateway node died, so a record lapsing on its own is the only
thing that stops that node's players being marked online forever. Whatever
writes presence must rewrite it well inside the window — roughly a third of it
leaves room for two missed writes before a present player flickers offline.

Only `is_online` and `last_seen` are ever published, and both are gated by
privacy flags the `users` module owns. `session_id` and `device_type` are
recorded because the keyspace must have room for them from the first release —
adding a field to a live keyspace means every key written before the change
decodes short — and they reach no response schema.

*Future expansion:* multiple devices per player. It does not fit this shape: it
wants a key per session with the player's presence derived from the set of them,
which is a different keyspace rather than a wider value. It arrives as
`presence:v2:` written and read alongside `v1` until every node has rolled —
which is only possible because of the version segment (C-2).

### 3.3 `celery-*` — task queues

| | |
| --- | --- |
| **Owner** | Celery |
| **Instance** | `broker` |
| **Structure** | Celery's own — lists, sorted sets and result keys |
| **TTL** | Celery's own (`result_expires`) |
| **Written by** | Celery producers and workers |
| **Read by** | Celery workers |
| **On failure** | Task submission fails; the caller decides. No worker entrypoint exists yet, so nothing writes here today. |

**Not ours to name, and deliberately not shared.** Celery owns the whole
keyspace on `broker`, which is why no other workload may use that instance: a
prefix collision with a task queue is a corrupted queue rather than a stale
read. Documented here for completeness rather than as something this platform
controls.

*Future expansion:* AD-16's outbox relay is the first real producer. It changes
what is *in* the queue, not the namespace.

### 3.4 Not yet allocated

These are named in architecture.md §13 and domain-model.md and have **no
implementation and no namespace**. Listed so the next module allocates a prefix
rather than inventing one, and so nobody assumes a missing key means a bug.

| Workload | Owner-to-be | Instance | Notes |
| --- | --- | --- | --- |
| Live match position | `game` | `live` | Hash per match. AD-18: authoritative for in-flight state. |
| Clock deadlines | `game` | `live` | Sorted set, score = flag timestamp. |
| Matchmaking queues | `matchmaking` | `live` | Sorted set per time control, score = rating. |
| Connection registry | `gateway` | `live` | Hash: player → node. Written beside presence, by the same process, but a different fact — see below. |
| Match update replay window | `game` | `live` | Bounded stream, backs AD-12's gap-fill. |
| Leaderboard read models | `leaderboard` | `cache` | Sorted set. |
| Response cache | platform | `cache` | Needs an invalidation rule per entry *before* the first one is added (C-1 and CLAUDE.md §10.7). |
| Idempotency keys, match locks | platform | `limits` or its own | Coordination primitives with a TTL. Not `cache` — an evicted lock is not a lock. |

**Presence and the connection registry are different facts** and must not share
a namespace, though the same gateway writes both. Presence answers *is this
person here*, is published to other players, and is governed by privacy flags.
The registry answers *which node holds their socket*, is internal routing, and
is meaningless after that node restarts.

---

## 4. TTL policy

| Workload | TTL | Set by | Reset on write |
| --- | --- | --- | --- |
| Rate limit counters | The rule's window (60s–1h) | `PEXPIRE` inside the Lua script | Yes |
| Presence | `PRESENCE_TTL_SECONDS`, default 60 | `SET ... PX` | Yes |
| Celery results | `result_expires` | Celery | n/a |

Two properties hold across all of them, and both are C-3 and C-4 applied:

- **No sweeper exists, and none is needed.** Every key expires by construction.
  A cron job that deletes stale keys is a job that can fail silently, and the
  failure looks like a memory leak weeks later.
- **The expiry is never a separate round trip.** Both namespaces above set value
  and TTL in one command, so no crash sequence can leave a key immortal.

## 5. Invalidation rules

Nothing on this platform is invalidated *by hand* today, and that is worth
stating plainly rather than leaving as an absence: both live namespaces are
TTL-decayed, so "invalidation" is the passage of time.

The first namespace that needs real invalidation is the response cache (§3.4),
and C-1 applies to it before it is added: layer, key, TTL and invalidation
trigger get written down here first. A cache without a documented invalidation
rule is a source of stale data, and the trigger is the part that is impossible
to reconstruct later.

## 6. Consistency guarantees

| Namespace | Guarantee |
| --- | --- |
| `rl:v1:` | **Atomic.** Read-prune-count-decide-write is one Lua script, so a concurrent burst cannot overshoot the limit. All-or-nothing across the rules on one endpoint: a request refused by one rule has not spent another's allowance. |
| `presence:v1:` | **Last writer wins, whole record.** Two nodes observing the same player produce one of two complete records, never a mixture. No coordination, no node affinity — which is what makes it correct on one process and on fifty. |

Neither offers cross-key atomicity, and nothing needs it.

## 7. Failure behaviour

| Namespace | Redis down or slow | Bound |
| --- | --- | --- |
| `rl:v1:` | Fail open by default: the request is allowed, and `rate_limit_unavailable` is logged at `ERROR`. Set `RATE_LIMIT_FAIL_OPEN=false` for a `503` instead. | `RATE_LIMIT_REDIS_TIMEOUT_MS`, default 100 |
| `presence:v1:` | Presence reads as unknown; the profile is served in full. Writes are dropped and self-heal on the next observation. Logged at `WARNING`. | `PRESENCE_REDIS_TIMEOUT_MS`, default 50 |

**The timeout is what makes the policy real.** A Redis that is *slow* rather
than *down* is the common failure, and without a bound it would hang every
request for the driver's default — taking the platform down while being
perfectly available itself.

**Kill switches.** `RATE_LIMIT_ENABLED` and `PRESENCE_ENABLED` both wire an
inert implementation rather than removing a dependency. The alternative to a
documented switch is somebody commenting out a dependency under pressure and
forgetting to restore it.

---

## Related documents

- [`architecture.md`](./architecture.md) — AD-03 (role separation), AD-18, AD-19
- [`database.md`](./database.md) — what lives in PostgreSQL and why
- [`system-design.md`](./system-design.md) — §626, freshness and staleness budgets
- [`domain-model.md`](./domain-model.md) — DM-04 (ephemeral state), §299
- `apps/api/app/database/redis.py` — the five pools
- `apps/api/app/modules/users/infrastructure/presence/keys.py` — the presence keyspace in code
- `apps/api/app/database/rate_limiter.py` — the rate-limit keyspace in code
