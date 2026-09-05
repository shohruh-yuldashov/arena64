# Data Reliability

> **Status:** current. Opened by A64-028.3.
> **Owner:** platform engineering.
> **Scope:** what is durable, what is not, what happens when each is lost,
> and what an operator does about it. PostgreSQL backup mechanics are in
> [`backup-restore.md`](./backup-restore.md).

---

## 1. Where state lives

| Store | What it holds | Source of truth? | Rebuildable? | Backup required? | Loss impact |
| --- | --- | --- | --- | --- | --- |
| **PostgreSQL** | Accounts, sessions, social graph, matches, **the move log**, ratings, tournaments, notifications, admin audit, outbox, processed-event ledger, analytics | **Yes, for everything** | No | **Yes** | Total. Nothing else holds any of it |
| **Redis** | Rate-limit counters, presence, connection registry, rooms, spectators, move idempotency, event replay buffers, the inter-node bus, clock deadlines, the live position | **No** | **Yes — every family** | **No** | A cold cache and a rebuild. §3 |
| **MinIO** | Avatar images | Yes, for the bytes | No — an upload cannot be re-derived | **Yes, when avatars ship** | Broken images. §7 |

40 tables across 13 schemas. Every one is in the dump.

---

## 2. Classification

| Class | Datasets |
| --- | --- |
| **CRITICAL DURABLE** | `users.user`, `auth.*`, `game.match`, `game.move`, `rating.*`, `tournaments.*`, `admin.audit_entry`, `admin.role_assignment`, `admin.sanction`, `platform.outbox`, `platform.processed_event` |
| **DURABLE BUT REBUILDABLE** | `statistics.*` (derived from matches), `analytics.event` (a record of what happened, not a thing the product reads back) |
| **DERIVED** | `friends` cache in Redis, `gwroomstate` |
| **EPHEMERAL** | Presence, connection registry, rooms, spectators, WebSocket tickets, event buffers, clock deadlines, the live position |
| **CACHE** | Rate-limit counters, the social-graph read model |

### The one that used to be different

`game:live:v1:<match_id>` held the in-flight position, and A64-016.3 treated
it as **authoritative** — `live_match_store.py`'s own header still says the
durable move log "is not built" and that "a Redis primary failure loses an
in-flight game".

That is out of date. A64-016.4 built it: `LiveMoveService` appends the move
row and advances the match in **one PostgreSQL transaction**, and
`_rebuild` replays the durable log to reconstruct the aggregate. The Redis
hash became "a cache of a replay". Losing it costs an O(plies) rebuild.

**So there is no authoritative state in Redis.** That is the finding this
whole section exists to establish, and
`tests/contract/test_redis_recovery.py` holds it in place.

---

## 3. Redis

### Key families

| Prefix | Role | TTL | Rebuilt by | Loss effect |
| --- | --- | --- | --- | --- |
| `rl:` | limits | yes | the next request | Every caller starts a fresh window — a widened allowance for one window, never a bypass |
| `wsticket:` | cache | yes | a new handshake | One reconnect fails and retries |
| `presence:` | cache | yes | the next heartbeat | Everyone shows offline until they act |
| `friends:` | cache | yes | a read from PostgreSQL | A slower first read |
| `gwconn:`, `gwroom:`, `gwconnroom:`, `gwroomstate:`, `gwspec:`, `gwspecconn:` | cache | yes | reconnection | Sockets reconnect and re-join |
| `gwmove:` | cache | yes | — | A retried move may be applied twice *at the transport*; `uq_move__ply` refuses the duplicate at the database |
| `gwevent:` | cache | yes | — | A reconnecting client resyncs instead of replaying |
| `gwbus:` | bus | stream, `MAXLEN` | — | In-flight frames between nodes are lost; single-node today |
| `clock:v1:deadlines` | live | **none — by design** | the next deadline write | Clock adjudication stalls until each match writes its next deadline |
| `game:live:` | live | yes | `_rebuild` from the move log | An O(plies) replay |

### The one key with no expiry

`clock:v1:deadlines` is a single global sorted set — a work queue, not a
record. `claim_expired` removes what it claims in the same Lua call that
reads it, so its bound is "matches being played", which is the right bound
and is not a duration. What it lacks is a *backstop*: a member whose match
ended without being superseded or claimed stays for ever. Recorded as a P3;
the fix belongs with the worker that owns the queue (**A64-028.4**).

### Persistence

| Category | Verdict |
| --- | --- |
| `live` (AOF on today) | **Useful, not required.** AOF turns a restart into "warm" instead of "cold". Nothing depends on it |
| `cache`, `limits`, `bus`, `broker` | **Unnecessary.** Every one is rebuilt by the next request |

Keeping AOF on `live` is worth its cost — a restart that preserves clock
deadlines avoids a stall — but it is an optimisation. **No backup of Redis
is required, and taking one would imply a guarantee the architecture does
not make.**

### Total loss

`tests/contract/test_redis_recovery.py` populates every family, flushes, and
asserts what remains. It remains nothing, and that is the expected result.

| Capability | After a total loss |
| --- | --- |
| Login, authenticated API | **Recovers automatically** |
| Rate limits | **Degraded** — one widened window |
| Presence | **Recovers automatically** — next heartbeat |
| Matchmaking | **Degraded** — queue entries are in PostgreSQL; Redis holds the pairing scan's working set |
| Live game | **Recovers automatically** — the next move replays the log |
| Cache, social graph | **Recovers automatically** |
| Notifications, outbox, analytics | **Unaffected** — none of them is in Redis |

**No operator action is required.** No path is `UNSAFE`.

### Outage, as distinct from loss

| Capability | Failure mode | Rationale |
| --- | --- | --- |
| Rate limiting | **FAIL OPEN** | Deliberate. Failing closed converts a limiter outage into a total authentication outage. Argon2id, `users.locked_until` and 256-bit reset tokens remain (A64-028.1 P1-7) |
| Live game, realtime | **DEGRADE** | Moves still commit to PostgreSQL; the cache is optional |
| Presence | **DEGRADE** | Stale, then correct |

The gap is not the behaviour, it is that **nothing alerts on it** — carried
as P1-7 for **A64-028.6**.

### Eviction

`cache` is expected to evict under pressure and everything on it is
rebuildable. `limits` and `live` must **not** evict: a rate-limit counter
evicted during the spike it exists for is a limit that disappears, and an
evicted live position costs a replay per move.

**Deployment requirement (A64-028.6):** `maxmemory-policy allkeys-lru` on
`cache`; `noeviction` on `limits` and `live`, sized so they do not need to
evict. Staging runs one instance with five logical databases, so it cannot
express this — recorded in `deployment.md` as AD-03's deviation.

### Logical databases

Five roles on five logical databases (`live` 0, `bus` 1, `broker` 2,
`cache` 3, `limits` 4). Isolation is by database, so prefixes cannot
collide across roles, and `FLUSHDB` on one leaves the others alone.

**Limitation, not a defect:** Redis Cluster has no logical databases. If
horizontal scaling ever needs Cluster, the five roles become five instances
or five prefixes — which is what AD-03 describes for a deployed tier anyway.
No change today.

---

## 4. Retention

| Dataset | Retention | Cleaned by | Frequency | If cleanup stops |
| --- | --- | --- | --- | --- |
| `analytics.event` | **400 days** | `AnalyticsRetentionTask` | 6 h | Unbounded growth — the largest table by far |
| `platform.outbox` | Configured horizon | Outbox prune | Configured | Growth; delivery unaffected |
| `platform.processed_event` | 30 days | Same prune | Configured | Growth; idempotency unaffected |
| `auth.user_sessions` | 30 d absolute / 14 d idle | Expiry sweep | Scheduled | Revoked rows accumulate |
| `matchmaking.queue_ticket` | Configured | Queue retention | Configured | Growth |
| `notifications.*` | **None** | — | — | **Unbounded** |
| `admin.audit_entry` | **None — deliberate** | — | — | **Unbounded, and correct.** §6 |
| Redis families | TTL per key | Redis | continuous | §3 |

**Unbounded durable tables: `notifications.notification` and
`admin.audit_entry`.** The audit trail is meant to be. Notifications are
not, and their growth is proportional to activity — a new P2.

### Analytics and partitioning

400 days of raw events is the largest table the platform will have. It is
**not partitioned**, deliberately: partitioning is a schema change that
costs migrations and query complexity, and nothing yet shows it is needed.

**Reconsider when** any of: the table passes ~50 M rows; a prune run stops
finishing inside its interval; an admin analytics query's plan turns into a
sequential scan over the horizon; or `VACUUM` on it stops keeping up. Until
one of those is *measured*, partitioning is speculative generality.

---

## 5. Migrations against a live database

Historical migrations are safe: Arena64 has not launched, a production
database is built by `alembic upgrade head` against an empty one, so every
one of them meets empty tables. A64-028.1's P2-3 named three; the true
number of migrations that build a blocking index is **thirty-nine**, because
`op.create_index` cannot be concurrent inside Alembic's transaction — and
all thirty-nine run at `t=0`.

Eleven index a table an *earlier* migration created. Those are the shape
that would matter after launch, and
`tests/unit/test_migration_policy.py` lists them so the next one is a
decision rather than an accident.

### Rules, from the first production deploy onwards

1. **Additive first.** Add a nullable column, deploy code that writes it,
   backfill, then make it `NOT NULL`. Never in one migration.
2. **An index on a populated table uses `CREATE INDEX CONCURRENTLY`**, in a
   migration that opts out of Alembic's transaction. It cannot be combined
   with other statements.
3. **Destructive changes last**, after every deployed version has stopped
   using what is being dropped.
4. **No long `ACCESS EXCLUSIVE` locks.** `ALTER TABLE` rewrites and
   non-concurrent index builds hold one for their duration.
5. **Enum changes:** add values (safe, and `ALTER TYPE … ADD VALUE` cannot
   run in a transaction on older servers); never remove one while any row or
   any deployed version still uses it.
6. **No bulk rewrite in one transaction.** Batch it, and let it resume.
7. **Take a backup before a destructive migration.** §6 of
   [`backup-restore.md`](./backup-restore.md).
8. **Set a lock timeout** for a live migration, so a blocked `ALTER` fails
   instead of queueing every writer behind it.

**No zero-downtime claim.** One process holds the HTTP API, the WebSocket
gateway and every scheduler, so a deploy already drops live games
(`deployment.md` §2). Migration ordering is about not making it worse.

---

## 6. Audit log, erasure, and what a restore means for privacy

`admin.audit_entry` is append-only and unbounded by design: an audit trail
with a retention policy answers "what happened" only for as long as
somebody chose. It is in every backup, and the drill restores it with the
rest.

**Erasure is where backups and privacy meet.** A64-027 gave analytics an
irreversible unlink: erasing a subject severs the link between events and
the person, and cannot be undone.

A backup taken **before** an erasure contains the state before it. Restoring
that backup restores the link.

That is a property of backups, not a defect, and no backup system can be
honest and claim otherwise. What follows from it is an operational
requirement:

> **After restoring a backup older than an erasure request, the erasure must
> be re-applied.** The list of erasures is itself in the database (the
> subject directory records that a subject was unlinked), so the procedure
> is: restore, then re-run erasure for every subject erased between the
> backup's `created_at` and now, using the audit trail to enumerate them.

Backup retention therefore bounds how long an erasure can be undone by a
restore. With `--keep 7` and daily backups that is a week.

---

## 7. Object storage

MinIO holds avatars — bytes a person uploaded, which nothing can re-derive.
Today the bucket is empty in every environment that matters and avatars are
not shipped, so **no backup is configured**.

**When avatars ship, the bucket needs one.** Same rule as §4 of
[`backup-restore.md`](./backup-restore.md): a bucket on the production host
is not a backup of that host. MinIO replication is not a backup either — it
copies a deletion as faithfully as a write.

---

## 8. Integrity checks after a restore

```sql
-- the revision the schema is on
SELECT version_num FROM public.alembic_version;

-- constraints came back with the data
SELECT contype, count(*) FROM pg_constraint c
JOIN pg_namespace n ON n.oid = c.connamespace
WHERE n.nspname NOT IN ('pg_catalog','information_schema')
GROUP BY contype;

-- a move with no match, which foreign keys should already make impossible
SELECT count(*) FROM game.move m
LEFT JOIN game.match x ON x.id = m.match_id WHERE x.id IS NULL;

-- both seats of every match resolve to accounts
SELECT count(*) FROM game.match m
LEFT JOIN users.user l ON l.id = m.light_player_id
LEFT JOIN users.user d ON d.id = m.dark_player_id
WHERE l.id IS NULL OR d.id IS NULL;

-- the outbox is not silently full of undeliverable work
SELECT count(*) FILTER (WHERE published_at IS NULL) AS pending,
       min(occurred_at) FILTER (WHERE published_at IS NULL) AS oldest
FROM platform.outbox;
```

Deliberately short. Most invariants are enforced by constraints — 98 check
constraints, 12 foreign keys, 7 unique constraints came back in the drill —
and a query that re-checks what the database already refuses is a second
definition that will drift.

---

## 9. Runbook

> Commands marked **DESTRUCTIVE** change or delete data. None of them
> contains a real secret; every one reads its credentials from the
> environment.

### Take a backup now

```bash
python -m app.operator.backup create --into /var/backups/arena64 --keep 7
```

### Check a backup is still good

```bash
python -m app.operator.backup verify /var/backups/arena64/<name>.dump
```

Run it against the *off-host* copy, not the local one. The failure this
catches is corruption in transit or at rest.

### Restore — **DESTRUCTIVE**

[`backup-restore.md`](./backup-restore.md) §6. Never over a live database.

### PostgreSQL restarted

Nothing to do. The pool checks a connection before handing it out
(`pool_pre_ping`), so a restart costs no failed request. Verified in
`tests/contract/test_pool_resilience.py`.

### PostgreSQL unavailable

The API stays up and fails requests. `/health` (liveness) still answers, so
an orchestrator will not restart it for the wrong reason. `/health/ready`
reports `degraded` — **but with HTTP 200**, which is A64-028.1's P1-5 and is
A64-028.6's to fix. Do not add the instance back to a load balancer on the
strength of a 200 from that endpoint.

### Redis restarted

Nothing to do. §3.

### Redis lost entirely

Nothing to do. §3. Expect one widened rate-limit window and presence to look
empty until players act.

### A migration must be rolled back — **DESTRUCTIVE**

```bash
alembic downgrade -1
```

Every migration has a real `downgrade`, asserted by
`tests/unit/test_migration_policy.py`. Take a backup first.

### Disk pressure

PostgreSQL, Redis AOF and backups share one disk on one host. In order:
prune old backups (`--keep`), check `platform.outbox` and
`analytics.event` are being pruned, then check Redis memory. **Never delete
a backup to make room for a database that is failing** — that is the moment
the backup is worth most.

### A backup failed

It exits non-zero and logs `backup_failed`. There is no partial file to
mistake for a good one. Fix the cause and run it again; do not wait for
tomorrow's schedule.

---

## 10. Failure matrix

| # | Scenario | Expected | Actual | Data loss? | Recovery | Operator |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | PostgreSQL process restart | No failed request | Confirmed | No | Automatic | None |
| 2 | Stale pooled connection | Replaced before use | Confirmed (`InterfaceError` without `pool_pre_ping`) | No | Automatic | None |
| 3 | PostgreSQL unavailable | Requests fail, process lives | Confirmed | No | On return | Watch readiness (P1-5) |
| 4 | Redis restart | Cache warm or cold; nothing lost | Confirmed | No | Automatic | None |
| 5 | Redis total loss | Everything rebuilds | Confirmed | No | Automatic | None |
| 6 | Backup destination unavailable | Non-zero exit, no artefact | Confirmed | n/a | Re-run | Fix destination |
| 7 | Backup interrupted | `.partial` left, never usable as a backup | Confirmed | n/a | Re-run | None |
| 8 | Corrupt backup | `verify` refuses on checksum | Confirmed | n/a | Use the previous generation | Investigate storage |
| 9 | Restore into a clean database | Every row, every constraint | Confirmed | No | — | Follow §6 |
| 10 | Application against a restored database | Repository reads succeed | Confirmed (`for_replay`) | No | — | — |
| 11 | Duplicate outbox delivery | At-least-once delivery, exactly-once effect | `processed_event` + `ON CONFLICT DO NOTHING` | No | Automatic | None |
| 12 | Cleanup task failure | Growth, no incorrectness | By design | No | Next run | Watch table size |
| 13 | Disk pressure | Writes fail | **Untested** — no alerting exists | Possible | Manual | §9 |

---

## Related Documents

- [`backup-restore.md`](./backup-restore.md)
- [`../01-architecture/production-hardening.md`](../01-architecture/production-hardening.md)
- [`../01-architecture/database.md`](../01-architecture/database.md), [`caching.md`](../01-architecture/caching.md)
