# Redis Keyspaces, Caching and TTL Policy

> **Status:** Approved for the keyspaces that exist — `rl:v1:`, `presence:v1:`, `wsticket:v1:`,
> `gwconn:v2:`, `gwroom:v1:`, `gwroomstate:v1:`, `gwmove:v1:`, `game:live:v1:`,
> `friends:v1:` and Celery's. Sections marked *Not yet allocated* describe
> workloads with no implementation.
> **Owner:** Backend platform
> **Last reviewed:** 2026-08-01 (A64-013.8 — full Redis audit, §8)

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
| `cache` | none | yes, by design | Response cache, read models, **presence**, and the **social graph** (`friends:v1:`). Everything here is reconstructible or expendable — every value is derived from PostgreSQL, so eviction costs a query and never a fact. |
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
| **Written by** | `users.application.services.PresenceService`, from `auth`'s lifecycle routes — `POST /auth/login` and `POST /auth/refresh` write online, `POST /auth/logout-all` writes offline (A64-013.6). The realtime gateway (AD-09) becomes a second writer when it exists |
| **Read by** | The HTTP API, composing a public profile |
| **On failure** | Returns "unknown", indistinguishable from a hidden, expired or never-recorded record. Logged at `WARNING` — an operator wants to know; nobody should be paged. |

**The TTL is the liveness protocol, not a tuning knob.** Nothing tells the
platform that a gateway node died, so a record lapsing on its own is the only
thing that stops that node's players being marked online forever. Whatever
writes presence must rewrite it well inside the window — roughly a third of it
leaves room for two missed writes before a present player flickers offline.

**Since A64-013.6 the writer is authentication, not a socket.** A player who
has just proved their identity is at a keyboard, and a client that exchanged a
refresh token still is; those are the two facts the platform observes without
a gateway. The consequence is that the refresh interval and
`PRESENCE_TTL_SECONDS` are now coupled — a token lifetime longer than the
presence window makes a signed-in player flicker offline between refreshes,
and the default 60s window assumes a client that refreshes more often than it
has to. `POST /auth/logout` deliberately writes nothing: presence is per
player, and one device signing out is not the player leaving.

Only `is_online` and `last_seen` are ever published, and both are gated by
privacy flags the `users` module owns. `session_id` and `device_type` are
recorded because the keyspace must have room for them from the first release —
adding a field to a live keyspace means every key written before the change
decodes short — and they reach no response schema.

**A second reader since A64-014.1.** `matchmaking` consults `presence:v1:`
when a player joins a queue, through `users.public.PresenceProvider` — the
same port `profiles` holds, over the same adapter and the same instance.
It holds the *reader* and not `PresenceRecorder`, so it cannot become a
second writer and C-8 is preserved structurally rather than by review. The
rule it applies is one-sided for the reason this section's `On failure` row
gives: only a record that positively says `online: false` refuses a join,
because unknown collapses an expired window, an unrecorded player and an
unreachable Redis, and refusing on that would turn a cache blip into an
outage of matchmaking (C-7).

### 3.2a `presence:v1:roster` — who is due to lapse (A64-013.8)

| | |
| --- | --- |
| **Owner** | `users` — `app/modules/users/infrastructure/presence/` |
| **Instance** | `cache` |
| **Structure** | Sorted set. Member = `player_id`, score = the millisecond that player's record expires |
| **TTL** | **None on the key.** Members leave by explicit sign-out, by a sweep, or by being re-scored on the next observation |
| **Written by** | `RedisPresenceProvider.record_presence`, in the same pipeline as the record |
| **Read by** | `PresenceSweeper`, on a `PRESENCE_SWEEP_INTERVAL_SECONDS` tick |
| **On failure** | The sweep is an idle tick; entries remain for the next one. Logged at `WARNING` |

**Why it exists.** A64-013.7 shipped presence notifications with a hole it
recorded openly: a player who closes the tab produces no `offline` event,
because *nothing observes a key expiring*. An expired key cannot be scanned
for — it is gone — so the only thing a sweeper can read is a record of who
was **expected** to expire. That is this set.

**Why a sorted set.** The score is the query. `ZRANGEBYSCORE roster 0 <now>
LIMIT 0 <n>` returns exactly the closed windows, oldest first, in one
command. A plain set would mean fetching every online player and testing each
against Redis.

**Why it is the one key here with no TTL, and why that is still bounded.** It
holds one member per *currently online* player — bounded by concurrency, not
by history. Members leave three ways, and the sweeper is the backstop for the
third: a player who never returns is removed by the first sweep that sees
them. The failure mode a TTL would guard against (unbounded growth) is
therefore already closed by the consumer, and a TTL would instead delete the
evidence a sweep needs.

**Derived, and losing it costs a notification rather than a fact.** The
per-player keys remain the record of who is online. This decides only who is
*told* that somebody left, which is why its write is ordered after the
record's and why neither raises.

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

### 3.4 `friends:v1:` — the social graph (A64-013.6)

| | |
| --- | --- |
| **Owner** | `friends` (architecture.md §6) — `app/modules/friends/infrastructure/cache/` |
| **Instance** | `cache` |
| **Structure** | String per entry, holding a JSON array of player ids |
| **TTL** | `FRIENDS_CACHE_TTL_SECONDS`, default 300 — a **backstop**, not the mechanism (C-3) |
| **Written by** | `CachedSocialGraphReader`, on a miss |
| **Read by** | Every public profile composition, every friend list page and every search — through `friends.public.SocialGraphReader` |
| **On failure** | Reads miss and fall through to PostgreSQL. Invalidation failures log at `ERROR`. Nothing raises. |

**Two entries, and only two:**

| Key | Holds | Invalidated by |
| --- | --- | --- |
| `friends:v1:friends:<player_id>` | Every live friend's id | friend accepted, friend removed, player blocked, player unblocked |
| `friends:v1:blocked:<player_id>` | Every player this one cannot interact with, **both directions** | player blocked, player unblocked |

C-1 is why the list stops there. A64-013.6 permits `blocked_ids_for()` and
`friend_ids_among()` and requires "only implement entries whose invalidation
rules are complete" — and for these two the trigger set is provably
exhaustive, because `friends.friendship` and `friends.blocked_player` have
exactly four writers between them and all four invalidate. The candidates
this section used to list and the reason each is still absent:

| Candidate | Why it is still not written |
| --- | --- |
| Friend **count** | Derivable from the friend-id set the cache already holds, so a separate entry would be a second copy of one fact (C-8 in spirit). If `GET /friends/count` ever needs it, it comes from the same key. |
| `friend_ids_among` **result** | Per-page, so the hit rate is near zero: two viewers of two different pages share no key. The whole friend set is cached instead and the intersection is done in Python — one key answers every page. |
| Pending-request counts | Not a fact about the graph. Its triggers are `send`, `cancel`, `accept`, `decline` and `void`, and only two of those touch anything cached today. |

**Whole sets, not query results.** One key per player answers a page of any
length and any contents, which is what makes a hit free: `friend_ids_among`
on a hit is set intersection and issues no query at all. A miss costs exactly
one indexed read — the same one the uncached reader issues — so an evicted
entry, a cold cache and `FRIENDS_CACHE_ENABLED=false` are all *slower* and
none of them is a regression.

**JSON arrays, not Redis sets.** A Redis `SET` cannot distinguish an empty
set from a missing key, and a player with no friends is the most common state
on this platform — those players would miss on every read, which is the cache
being off for exactly the majority case. `[]` is a real value and therefore a
hit.

**Invalidation is by player, not by key.** `SocialGraphCache.invalidate` takes
the player ids of both parties and drops every key in the namespace for each,
because that is the vocabulary of the four triggers — a request was accepted
*between two players*, a block was lifted *on one*. A third entry added to
`keys_for` is invalidated by all four triggers without any of them changing.

**It runs after the commit, deliberately.** Dropping the entry before the
transaction commits opens a window in which a concurrent read repopulates it
from the pre-commit state, and that stale value then survives for a whole
TTL. After the commit the window is the microseconds between `COMMIT` and
`DEL`, and closing it entirely needs the transactional outbox (AD-16) rather
than a cleverer ordering.

**The outbox now exists** (A64-013.7) and closing that window is a
*candidate*, not a completed migration: a `friends.cache_invalidated` consumer
would drop the keys as part of the same durable event chain, so a process that
died between `COMMIT` and `DEL` would still invalidate on retry. It is not
done, because the change trades microseconds of staleness for the relay's poll
interval of it — seconds — on every social write. That is worse for the common
case and better only for the crash, and which one matters more is a measurement
nobody has taken. Recorded here as the reason the four in-request triggers stay
where they are. Invalidating after a *rollback* is harmless: the
next read repopulates from the unchanged database.

*Future expansion:* the version segment is already there. If the friend-id
set later needs scores (recency, interaction weight) it becomes a sorted set
under `friends:v2:`, written alongside `v1` until the fleet has rolled (C-2).

### 3.5 Not yet allocated

These are named in architecture.md §13 and domain-model.md and have **no
implementation and no namespace**. Listed so the next module allocates a prefix
rather than inventing one, and so nobody assumes a missing key means a bug.

| Workload | Owner-to-be | Instance | Notes |
| --- | --- | --- | --- |
| Live match position | `game` | `live` | Hash per match. AD-18: authoritative for in-flight state. |
| Clock deadlines | `game` | `live` | Sorted set, score = flag timestamp. |
| Matchmaking queue **index** | `matchmaking` | `live` | Sorted set per pool, score = rating. **Not the ticket** — since A64-014.1 `QueueTicket` is a row in `matchmaking.queue_ticket` (database.md §8.1a), because QT-1 and QT-4 are constraints under concurrency. What belongs here is a *derived* ordering for QT-5's widening scan, rebuildable from that table (AD-19), and it is not allocated until a measurement asks for it. |
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
| Social graph | `FRIENDS_CACHE_TTL_SECONDS`, default 300 | `SET ... EX` | Yes, on every repopulation |
| Celery results | `result_expires` | Celery | n/a |

Two properties hold across all of them, and both are C-3 and C-4 applied:

- **No sweeper exists, and none is needed.** Every key expires by construction.
  A cron job that deletes stale keys is a job that can fail silently, and the
  failure looks like a memory leak weeks later.
- **The expiry is never a separate round trip.** Every namespace above sets
  value and TTL in one command, so no crash sequence can leave a key immortal.

`friends:v1:` is the first namespace whose TTL is **not** the correctness
mechanism. Its entries are dropped by the four triggers in §5, and the 300
seconds exist only to bound how long a bug in those triggers can be wrong. A
shorter TTL would hide such a bug; a longer one would extend it.

## 5. Invalidation rules

`rl:v1:` and `presence:v1:` are TTL-decayed, so "invalidation" there is the
passage of time. `friends:v1:` is the first namespace on this platform with
real invalidation, added by A64-013.6, and these are its complete triggers:

| Event | Written by | Invalidates |
| --- | --- | --- |
| Friend request **accepted** | `FriendRequestService._transition` | requester and addressee |
| Friend **removed** | `FriendshipService.remove_friend` | both former friends |
| Player **blocked** | `BlockingService.block` | blocker and blocked |
| Player **unblocked** | `BlockingService.unblock` | blocker and blocked |

Three properties make the list complete rather than merely long:

- **Every writer of the cached relations is here.** `friends.friendship` is
  written by acceptance, removal and the block cascade; `friends.blocked_player`
  by block and unblock. There is no fifth writer, so there is no fifth trigger.
- **Both parties, always.** A friendship and a block are facts about a *pair*;
  invalidating only the actor leaves the other side reading a friend they no
  longer have.
- **Every key for a player, not the one the trigger is named after.** Blocking
  changes a block set *and* ends a friendship, so a trigger that dropped only
  the entry matching its own name would leave the other stale.

Transitions that deliberately invalidate **nothing**: sending, cancelling and
declining a friend request. None of them changes the graph, and invalidating on
send would cost two players their cache for an event that changed nothing.

C-1 still applies to every future entry: layer, key, TTL and invalidation
trigger get written down here *before* the first key is written.

## 6. Consistency guarantees

| Namespace | Guarantee |
| --- | --- |
| `rl:v1:` | **Atomic.** Read-prune-count-decide-write is one Lua script, so a concurrent burst cannot overshoot the limit. All-or-nothing across the rules on one endpoint: a request refused by one rule has not spent another's allowance. |
| `presence:v1:` | **Last writer wins, whole record.** Two nodes observing the same player produce one of two complete records, never a mixture. No coordination, no node affinity — which is what makes it correct on one process and on fifty. |
| `friends:v1:` | **Eventually consistent, bounded by the TTL and normally by the commit.** PostgreSQL is the system of record (AD-19); every entry is a copy. Invalidation runs after the writing transaction commits, so the stale window is the interval between `COMMIT` and `DEL` — microseconds — plus the TTL if that `DEL` fails. Closing it entirely requires AD-16's outbox. |

None offers cross-key atomicity, and nothing needs it.

## 7. Failure behaviour

| Namespace | Redis down or slow | Bound |
| --- | --- | --- |
| `rl:v1:` | Fail open by default: the request is allowed, and `rate_limit_unavailable` is logged at `ERROR`. Set `RATE_LIMIT_FAIL_OPEN=false` for a `503` instead. | `RATE_LIMIT_REDIS_TIMEOUT_MS`, default 100 |
| `presence:v1:` | Presence reads as unknown; the profile is served in full. Writes are dropped and self-heal on the next observation. Logged at `WARNING`. | `PRESENCE_REDIS_TIMEOUT_MS`, default 50 |
| `friends:v1:` | Reads miss and fall through to PostgreSQL — the platform is slower, never wrong. Failed **invalidations** log at `ERROR`, because a missed drop is a correctness defect that the TTL bounds but does not prevent. | `FRIENDS_CACHE_TIMEOUT_MS`, default 50 |

**The timeout is what makes the policy real.** A Redis that is *slow* rather
than *down* is the common failure, and without a bound it would hang every
request for the driver's default — taking the platform down while being
perfectly available itself.

**Kill switches.** `RATE_LIMIT_ENABLED`, `PRESENCE_ENABLED` and
`FRIENDS_CACHE_ENABLED` all wire an inert implementation rather than removing a
dependency. `FRIENDS_CACHE_ENABLED=false` returns the platform to exactly what
it did before A64-013.6 — one query per social-graph read — so it is a
legitimate configuration and not a stub. The alternative to a
documented switch is somebody commenting out a dependency under pressure and
forgetting to restore it.

---

## 8. Redis audit — A64-013.8

The Social Platform epic is complete, so every keyspace it produces now
exists and can be audited rather than predicted. This section is the one
table an operator needs: **who owns each key, what expires it, how it grows,
and what happens when it stops fitting.**

### 8.1 Ownership and expiry

| Keyspace | Owner | Instance | Expiry | Failure posture |
| --- | --- | --- | --- | --- |
| `rl:v1:<rule>:<digest>` | platform (`app/database/rate_limiter.py`) | `limits` | The rule's window, `PEXPIRE` inside the Lua | Fail **open** by default; `RATE_LIMIT_FAIL_OPEN=false` for `503` |
| `presence:v1:<player_id>` | `users` | `cache` | `PRESENCE_TTL_SECONDS`, `SET … PX`, reset per write | Reads unknown, writes dropped and self-healing |
| `presence:v1:roster` | `users` | `cache` | **None** — see §3.2a; bounded by the sweeper | Sweep is an idle tick; entries survive |
| `friends:v1:friends:<player_id>` | `friends` | `cache` | `FRIENDS_CACHE_TTL_SECONDS` — a *backstop*, not the mechanism | Miss, then query |
| `friends:v1:blocked:<player_id>` | `friends` | `cache` | Same | Miss, then query |
| `wsticket:v1:<digest>` | gateway (`app/gateway/`, minted by `auth`) | `cache` | `GATEWAY_TICKET_TTL_SECONDS`, `SET … EX` | **Propagates.** The one exception to C-7: an unstorable ticket cannot be redeemed, and treating an unreachable store as "valid" would be an authentication bypass |
| `gwconn:v2:<player_id>` | gateway | `cache` | `GATEWAY_CONNECTION_TTL_SECONDS` per member, by score; `EXPIRE` on the key | **Propagates.** A connection that cannot be registered is one nothing can route to. Member is `connection_id\|node_id` since A64-016.2 — see websocket.md §5, including why v1 needs no migration |
| `gwroom:v1:<match_id>` | gateway | `cache` | `GATEWAY_ROOM_TTL_SECONDS` per member, by score; `EXPIRE` on the key | **Propagates.** A room membership that cannot be written is a socket nothing can route a move to |
| `gwconnroom:v1:<connection_id>` | gateway | `cache` | Same as the room key | Degrades. A stale entry costs one wasted `ZREM`; the room key is authoritative — websocket.md §15.1 |
| `gwroomstate:v1:<match_id>` | gateway | `cache` | `GATEWAY_ROOM_TTL_SECONDS`, `PEXPIRE` inside the script | Degrades. A projection; `game` is authoritative. Written by a **monotonic** CAS so an out-of-order fan-out cannot move it backwards — websocket.md §17.9 |
| `gwmove:v1:<connection_id>:<request_id>` | gateway | `cache` | `GATEWAY_MOVE_IDEMPOTENCY_TTL_SECONDS` | Degrades. A lost entry costs a retry being reprocessed, which the ply CAS then refuses as stale — websocket.md §17.5 |
| `game:live:v1:<match_id>` | `game` | **`live`** | `GAME_LIVE_STATE_TTL_SECONDS`, `PEXPIRE` inside the script | **Propagates, and is the one keyspace on this platform that is not reconstructible.** AD-18's live position; the durable move log that would allow replay does not exist yet — websocket.md §18 |
| `celery-*` | Celery | `broker` | `result_expires` | Celery's |

**Sessions are not in this table, and that is the finding.** `auth`'s refresh
tokens and session rows live in **PostgreSQL** (`auth.user_session`), not in
Redis — deliberately, because a session must be revocable and auditable, and
an evicted session is a signed-in player thrown out at random. The only
session-adjacent thing in Redis is the *rate limit* on the endpoints that
issue them. Anybody looking for a session keyspace should stop looking.

### 8.2 Growth expectations

Sized against system-design.md's target concurrency rather than against
registrations, because every keyspace here is keyed by *activity*.

| Keyspace | Grows with | Bounded by | Rough size per unit |
| --- | --- | --- | --- |
| `rl:v1:` | Distinct (rule, actor) pairs **in the current window** | The shortest window, 60s for most rules | ~100 B per counter |
| `presence:v1:<id>` | Concurrent players | `PRESENCE_TTL_SECONDS` after each leaves | ~150 B per online player |
| `presence:v1:roster` | Concurrent players | The sweeper, then explicit sign-out | ~60 B per member |
| `friends:v1:friends:` | Players **read** in the TTL window, not players who exist | `FRIENDS_CACHE_TTL_SECONDS` + invalidation | ~40 B per friend, per cached player |
| `friends:v1:blocked:` | Same | Same | ~40 B per block |

The one entry that is not self-limiting by count is `friends:v1:friends:` for
a player with an unusually large friend list, and `blocked:` for one with a
large block list — BL-4 already flags block capacity as an unset product
decision. Neither is a problem at any plausible per-player number; both are
the reason `blocked_ids_for` is documented as "unbounded today, bounded by
design later" in `block_repository.py`.

### 8.3 Future scaling — recorded, not implemented

A64-013.8 introduces **no Redis micro-optimisation**, deliberately: nothing
here has been measured under load, and an optimisation without a measurement
is superstition (CLAUDE.md §10.1). What follows is what to reach for *when*
there is a number.

| Pressure | First move | Why not now |
| --- | --- | --- |
| `cache` instance under memory pressure | Split presence onto a sixth AD-03 role | Presence and the social graph have the same posture — derived, expendable, evictable — so they share correctly until one of them is large enough to evict the other |
| Social-graph cache hit rate too low | Raise the TTL, not the entry count | The TTL is a backstop; raising it lengthens the window a *failed invalidation* is wrong for, which is a correctness trade and must be a decision |
| Presence writes dominating the instance | `SET … GET` to collapse the transition read | Widens the published `PresenceRecorder` port for an optimisation nothing has measured — recorded since A64-013.7 |
| Roster reads dominating a large fleet | Shard the roster by a player-id prefix | One `ZRANGEBYSCORE` with a `LIMIT` per sweeper tick is not a workload yet |
| A second consumer needs the friend cache | Nothing — it is already a per-player set | The shape was chosen for exactly this: one key answers a page of any length |

### 8.4 What the audit found, and what it did not

**Found and fixed.** The social-graph cache port took a pre-built string key,
which meant the *application* layer imported `infrastructure.cache.keys` to
build one — a dependency pointing the wrong way (CLAUDE.md §3.1). The port
now takes a `SocialGraphEntry`, and the keyspace is named in exactly one
file. `keys_for` is a loop over that enum, so a third entry is invalidated
the moment it is declared rather than when somebody remembers.

**Found and accepted.** Nothing else. Every namespace has one owner, one
instance, a documented expiry and a documented failure posture, and no key is
written by more than one module.

---

## Related documents

- [`architecture.md`](./architecture.md) — AD-03 (role separation), AD-18, AD-19
- [`database.md`](./database.md) — what lives in PostgreSQL and why
- [`system-design.md`](./system-design.md) — §626, freshness and staleness budgets
- [`domain-model.md`](./domain-model.md) — DM-04 (ephemeral state), §299
- `apps/api/app/database/redis.py` — the five pools
- `apps/api/app/modules/users/infrastructure/presence/keys.py` — the presence keyspace in code
- `apps/api/app/modules/friends/infrastructure/cache/keys.py` — the social-graph keyspace in code
- `apps/api/app/database/rate_limiter.py` — the rate-limit keyspace in code
