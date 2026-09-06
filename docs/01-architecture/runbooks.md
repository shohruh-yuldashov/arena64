# Operational Runbooks

> **Status:** current. Opened by A64-028.6.
> **Owner:** platform engineering.
> **Audience:** whoever is holding the pager. Assume they did not write this
> code and are reading it at 3am.

Every alert in `infrastructure/observability/alerts.yml` links to a section
here, and `apps/api/tests/unit/test_observability_config.py` fails if a link
does not resolve — an alert with nowhere to go is worse than no alert.

Each section is the same seven things: the symptom, which alert fires, how
to confirm it, what to do in the next five minutes, how to recover, how to
know it worked, and what is still at risk afterwards.

**No command here prints a secret.** Where one would, the safe form is
given instead.

---

## The three commands worth knowing before anything is wrong

```bash
# Which instances are serving, and which are draining
docker compose -f infrastructure/production/compose.yml ps

# What one instance thinks of itself. 200 = ready, 503 = not.
curl -si https://arena64.gg/health/ready | head -1

# Everything the platform knows about itself. Not reachable from the
# internet: run it from inside the network.
curl -s -H "Authorization: Bearer $OPS_TOKEN" http://api:8000/metrics
```

---


---

## Failure matrix

What A64-028.6 actually exercised, and what it did not. **SAFE** — the
platform degrades correctly and an operator is told. **DEGRADED** — reduced
function, no data loss, an operator is told. **UNSAFE** — loss or a silent
security change. **UNKNOWN** — not exercised.

| # | Condition | Client sees | Readiness | Liveness | Signal | Loss? | Class |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A | Healthy | normal | 200 | 200 | — | no | **SAFE** |
| B | PostgreSQL unreachable | requests fail | **503** | 200 | readiness body `postgres: false`; `PostgresUnreachable` | no | **DEGRADED** |
| C | PostgreSQL restarted | one failed request per pooled connection at most | 503 → 200 | 200 | as above; `pool_pre_ping` discards dead connections | no | **DEGRADED** |
| D | Redis unreachable | requests served; **rate limits bypassed** | **503** | 200 | `rate_limit_unavailable_total{failed_open}`, ERROR log, `RateLimiterUnavailable` **pages** | no data; **abuse window open** | **DEGRADED** |
| E | Redis restarted | reconnects; live state rebuilds from the durable log | 503 → 200 | 200 | as above | no | **DEGRADED** |
| F | One instance draining | nothing — the balancer routes elsewhere | **503** | **200** | `service_draining` 1; readiness excluded from the alert | no | **SAFE** |
| G | One instance killed mid-game | socket closes `1012`, reconnects in **265 ms** | n/a | n/a | `ApiUnavailable` after 2m | **no** — plies 2 → 3, 0 duplicates | **SAFE** |
| H | Worker killed | matchmaking stops; backlog grows | 200 | 200 | `SchedulerNotRunning`, `OutboxBacklogStale` | no — the backlog drains on restart | **DEGRADED** |
| I | Poison outbox event | that event never delivers | 200 | 200 | `outbox_failed_total{invalid_payload}`, then `OutboxEventsAbandoned` | **yes, that event** | **DEGRADED** |
| J | Email provider unavailable | verification and reset mail delayed | 200 | 200 | `notifications_email_deliveries{outcome}` — **metric only, no alert** (P3-4) | no — retried through the outbox | **DEGRADED** |
| K | Backup job failing | nothing | 200 | 200 | status file records the failure; `BackupStale` after two days | no — until a restore is needed | **DEGRADED** |
| L | Invalid production config | nothing — the old version still serves | n/a | n/a | the process refuses to start and names the variable | no | **SAFE** |
| M | Migration fails | nothing — `api` never starts | n/a | n/a | the deploy fails visibly | no | **SAFE** |
| N | Metrics backend unavailable | nothing | 200 | 200 | Prometheus's own `up` goes to 0 | no | **SAFE** |
| O | Event-loop lag | slow responses | 200 | 200 | the lag histogram; `EventLoopBlocked` **pages** at p99 > 250 ms | no | **DEGRADED** |
| P | Unknown SPA route | branded **404** | — | — | — | no | **SAFE** |
| Q | Private route crawled | `X-Robots-Tag: noindex` | — | — | — | no | **SAFE** |
| R | Missing static asset | **404** | — | — | — | no | **SAFE** |

### How each was exercised

**Really run:** A, B, D, F, G, L, P, Q, R. B and D by pointing an instance at
an unreachable dependency; F and G by draining and terminating an instance
during a live game; P, Q and R against a real Caddy over HTTP; L by
constructing a production `Settings` and reading the refusal.

**Reasoned from mechanism, not run:** C, E, H, I, J, K, M, N, O. Each rests
on behaviour tested elsewhere — `pool_pre_ping` in A64-028.3, the outbox
retry ledger in this task's contract tests, the config guards in the unit
suite — but the end-to-end condition was not staged. Recorded as reasoned
rather than measured, because the distinction is the point.

**Not exercised at all:** a Redis outage on the *shared* instance. The
machine running these checks has a Redis the developer uses for other work,
and stopping it was not something to do to somebody else's system. The
unreachable-instance form (D) proves the application's behaviour; what it
does not prove is what a real mid-flight disconnection does to an
established connection pool.


---

## First boot

**The one procedure that is not a deploy.** Everything below happens once,
on a machine that has never run Arena64, and every step is here because
skipping it produces a failure that looks like something else.

### 1. Server prerequisites

Docker Engine with Compose v2. Nothing else — every dependency runs in a
container.

### 2. DNS

`arena64.gg` and `admin.arena64.gg`, both A records to this host, **before**
anything starts. The ACME HTTP-01 challenge resolves the name from the
outside; a certificate cannot be issued for a name that does not point here.

### 3. Firewall

Inbound 22, 80/tcp, 443/tcp and 443/udp; nothing else. **Port 80 is not
optional** — it carries the ACME challenge, and a firewall allowing only 443
leaves the site working and makes every renewal impossible. The full
contract is `deployment.md` §8.8.

### 4. Secrets

```bash
cd infrastructure/production
cp production.env.example production.env
chmod 600 production.env

openssl rand -hex 32          # JWT_SECRET_KEY
openssl rand -hex 32          # EMAIL_VERIFICATION_OTP_SECRET
openssl rand -base64 32       # OPS_TOKEN
openssl rand -base64 32       # OPS_BACKUP_ENCRYPTION_KEY  ← store this elsewhere
```

**The backup key goes in a secret manager, not only in this file.** Losing
it loses every backup taken with it, and keeping the only copy on the host
the backups protect defeats the point of having them off it.

### 5. Off-host backup target

Create the bucket at whatever S3-compatible provider is being used, an
access key scoped to it, and a lifecycle rule for retention — the platform
does not implement retention on the remote, because every provider already
has it. Fill the four `OPS_BACKUP_OFFSITE_*` variables; the tier refuses to
start with some of them.

### 6. Bring up the data tier

```bash
docker compose --env-file production.env up -d postgres redis minio
docker compose --env-file production.env run --rm minio-provision
```

Wait for `postgres` and `redis` to report healthy before continuing —
`depends_on` enforces it for the services below, and doing it by hand here
makes a failure visible now rather than as a confusing startup order.

### 7. Schema

```bash
docker compose --env-file production.env run --rm migrate
```

Must exit zero. Nothing that serves traffic migrates anything.

### 8. Clients and edge

```bash
docker compose --env-file production.env up -d web admin
docker compose --env-file production.env up -d certbot-init
docker compose --env-file production.env up -d nginx certbot
```

`certbot-init` writes a self-signed stopgap, nginx starts on it, the
challenge is served over port 80, and the real certificate replaces it.
A browser reaching the site in that window sees a warning — that is the
correct signal for a deployment that is not finished.

### 9. Application

```bash
docker compose --env-file production.env up -d api-1 api-2 worker
```

### 10. Monitoring and backup

```bash
docker compose --env-file production.env up -d node-exporter backup
```

### 11. Smoke tests

```bash
curl -sI https://arena64.gg/ | head -1                    # 200, HTTP/2
curl -sI https://arena64.gg/login | grep -i x-robots-tag  # noindex, nofollow
curl -sI https://arena64.gg/gmaes/abc | head -1           # 404
curl -sI https://arena64.gg/metrics | head -1             # 404 — not public
curl -s  https://arena64.gg/health/ready                  # postgres and redis true
curl -sI https://admin.arena64.gg/ | grep -i x-robots-tag # noindex, nofollow
```

Then register an account, verify the email arrives, sign in, and play one
move against a second browser. The last of those is the only test that
exercises the WebSocket, the gateway and the durable move log together.

### 12. First backup, by hand

```bash
docker compose --env-file production.env run --rm backup \
  python -m app.operator.backup create --into /var/backups/arena64
```

Then confirm it reached the remote — the object should be in the bucket, and
`arena64_backup_last_offsite_timestamp_seconds` should exist. **A backup
nobody has watched arrive is a backup nobody has.**

### 13. External monitoring — DEPLOYMENT GATE

Configure an off-host uptime check against `https://arena64.gg/`. This is
not optional and is not in this repository: Prometheus runs on the same host
as everything it watches, so it cannot report that the host is gone. See
[monitoring the monitoring](#monitoring-the-monitoring).

---

## Monitoring the monitoring

**No alert.** By construction — this is the gap alerts cannot cover.

Prometheus, Grafana, the node exporter and the application all run on one
host. If that host stops, every one of them stops with it, and the last
thing an operator sees is silence that looks exactly like a quiet night.

**What is covered from inside.** A container that dies while the host lives:
`up{}` goes to zero and `ApiUnavailable`, `HostMetricsMissing` and
`SchedulerNotRunning` fire. Prometheus's own restart loses no history if its
volume survives.

**What is not, and cannot be.**

| Failure | Detected from inside? |
| --- | --- |
| One container dies | **yes** |
| Prometheus dies | no — nothing scrapes the scraper |
| The host loses power | no |
| The host loses network | no |
| The disk fills so Prometheus cannot write | partly — the disk alert fires first, if it can be delivered |

**The requirement, stated as a gate.** Production needs at least one
external availability check that does not run on this host:

- `https://arena64.gg/` returns 200;
- the certificate is more than fourteen days from expiry — a second,
  independent opinion on the thing `CertificateExpiringSoon` watches, and
  the one that still works when the host is gone;
- a notification path that is not email through this platform.

Any external uptime provider does this. **None is configured in this
repository, and pretending otherwise would be the most dangerous sentence in
this file.** It is a LIVE DEPLOYMENT GATE in the final risk register.


---

## Deploy

**Not an incident.** Written first because every other section assumes it.

1. Build and publish, or pin an existing tag in `production.env`.
2. Run the schema step **to completion** before anything serves:
   `docker compose run --rm migrate`. It exits non-zero on failure and
   nothing else starts — see [migration failure](#migration-failure).
3. For each API replica in turn:
   - `curl -X POST -H "Authorization: Bearer $OPS_TOKEN" http://<instance>:8000/health/drain`
   - wait until the balancer has stopped routing to it — readiness answers
     503 immediately, so this is the balancer's own check interval, not a
     guess;
   - stop the container. `stop_grace_period` is 30s.
   - start the replacement and wait for readiness to answer 200.

**What players see.** Their socket closes with code `1012` and the client
reconnects to a surviving instance and resumes from the durable move log.
Measured end to end in A64-028.6: **265 ms** from close to a resumed session,
with the next legal move accepted and no ply lost or duplicated.

**What draining does not do.** It does not refuse anything. An instance that
is draining serves every request it is still given; the only thing that
changed is what it says when asked whether it wants more.

---

## Rollback

1. Set `ARENA64_TAG` to the previous image and repeat the deploy above.
2. **Do not roll the schema back by default.** Migrations are written
   expand-then-contract (`docs/05-operations/data-reliability.md` §5), so the
   previous image runs against the new schema. A schema rollback is only
   needed if the deploy included a destructive step, and that is the one
   case where the answer is a restore rather than a downgrade.
3. If the previous image will not start, the fault is configuration rather
   than code: read the startup error, which names the variable.

Residual risk: a rollback does not un-send email or un-deliver events.

---

## API unavailable

**Alert:** `ApiUnavailable`.

**Confirm.** `docker compose ps` — is the container running? If it is,
`curl -si http://<instance>:8000/health | head -1` from inside the network.
Liveness has no dependencies, so a failure here is the process, not
PostgreSQL.

**Mitigate.** If some replicas are serving, the balancer has already stopped
routing to the failed one and the product is degraded rather than down. If
none are, this is an outage.

**Recover.** Restart the container. If it exits immediately, read the
startup log: a configuration error names the variable and refuses to start
on purpose ([bad production config](#bad-production-config)).

**Verify.** Readiness returns 200 and `arena64_http_requests_total` starts
moving again.

**Residual risk.** A process that was killed mid-tick may have burned an
outbox attempt without recording an outcome — watch
`arena64_outbox_exhausted_total{reason="unrecorded"}`.

---

## Readiness failing

**Alert:** `ReadinessFailing`.

**Confirm.** `curl -s http://<instance>:8000/health/ready` and read the body.
It names which dependency: `postgres: false`, a false entry under `redis`,
or `draining: true`.

**Mitigate.** A single instance failing readiness is already out of rotation
and needs no action beyond finding out why. Every instance failing readiness
is [a PostgreSQL](#postgresql-outage) or [a Redis](#redis-outage) outage.

**If it says `draining: true` and no deploy is running**, an instance was
drained and never replaced. Restart it; the flag is process-local and does
not survive.

**Verify.** Readiness returns 200 and the balancer routes to it again.

---

## Elevated 5xx

**Alert:** `ElevatedServerErrors`.

**Confirm.** Open the API health dashboard and group the request rate by
route. One route failing is a defect in that handler; every route failing is
a dependency.

**Mitigate.** If the errors began with a deploy, [roll back](#rollback) —
that is faster than diagnosing, and the diagnosis is easier afterwards.

**Recover.** Find the exception in the logs by `request_id`. Every response
carries `X-Request-Id` and every log line carries the same field, which is
what makes one user's report reconstructable.

**Verify.** The error ratio returns below 2%.

**Residual risk.** A64-028.5A measured **zero** unexpected failures across
every scenario, so any sustained non-zero rate is new behaviour rather than
a tuning question.

---

## High latency

**Alert:** `SevereLatency`.

**Confirm.** Compare p95 with event-loop lag on the same dashboard.

- **Lag up as well:** the process is CPU-bound. A64-028.5A established that
  one uvicorn process pins one core while the connection pool never waits
  and Redis is far from its limit, so the lever is worker processes per
  host, not a bigger database.
- **Lag flat:** the process is waiting on something. Check the database.

**Mitigate.** Add API replicas. They are stateless and the compose file's
`deploy.replicas` is the only thing to change.

**Verify.** p95 returns inside the provisional target in
`docs/05-operations/performance.md` §2.

---

## Event loop lag

**Alert:** `EventLoopBlocked`.

**Confirm.** The lag panel, per instance. A healthy loop is under a
millisecond; anything above 250 ms at p99 means the process spends quarter
seconds not answering anybody.

**Mitigate.** Restart the affected instance. It is a blunt instrument and it
works: the loop is blocked by something in this process, and nothing else is
affected.

**Recover.** Something synchronous is running on the loop. Look at what
deployed last — a blocking call in a request path is the usual cause, and an
unbounded loop over a large result set is the next.

**Verify.** Lag returns below a millisecond at p50.

---

## PostgreSQL outage

**Alert:** `PostgresUnreachable`, and every instance failing readiness.

**Confirm.** `docker compose ps postgres` and the container's own log.

**Mitigate.** Nothing in the application. Liveness is deliberately
independent of the database, so the processes stay up and recover on their
own — do **not** restart them, which would only add cold starts to an
outage.

**Recover.** Restore the database service. `pool_pre_ping` is on
(A64-028.3), so pooled connections that died with it are discarded rather
than served, and no request fails twice for the same reason.

**Verify.** Readiness returns 200 across the fleet.

**Residual risk.** Events enqueued during the outage are in the outbox and
drain when the relay can reach the database again. Watch
`arena64_outbox_oldest_pending_age_seconds` come back down.

---

## Redis outage

**Alert:** `RateLimiterUnavailable`, and readiness failing.

**This is the security-relevant one.** While Redis is unreachable the rate
limiter **fails open** — deliberately, because failing closed would turn a
limiter outage into a total authentication outage. Every rate limit on the
platform is bypassed for the length of the outage.

**Confirm.** `arena64_rate_limit_unavailable_total{outcome="failed_open"}`
is increasing.

**Mitigate.** Restore Redis. There is no application-side mitigation that
does not make things worse: turning the limiter to fail-closed mid-incident
takes login, registration and password reset down.

**Watch while it is down.** Registration and login volume. The abuse window
is exactly as long as the outage, and Argon2id, `users.locked_until` and
256-bit reset tokens are what remain in place meanwhile.

**Recover.** Redis is a cache and a bus, not a source of truth
(`docs/05-operations/data-reliability.md` §3). Live game state rebuilds from
the durable move log. Sessions survive; the ticket store does not, so
players reconnecting during the outage may need to retry.

**Verify.** `arena64_rate_limit_decisions_total` starts moving and
`unavailable` stops.

**Residual risk.** Anything an abuser did during the window. Review
registrations from the period.

---

## Realtime relay

**Alert:** `CrossInstanceFramesFailing`.

**Confirm.** `arena64_gateway_remote_publish_failures_total` and
`arena64_gateway_forwarding_failures_total`. Then grep for
`gateway_stream_group_recreated`.

**What it means.** A move published to another node's mailbox and refused
never reaches the opponent, and neither player is told. The durable log is
unaffected, so nothing is lost — the game is recoverable and the experience
is not.

**Mitigate.** Restore Redis if that is the cause. A player can force a
resync by reconnecting.

**Recover.** Repeated `gateway_stream_group_recreated` means nodes are idle
long enough to lose their consumer groups to the mailbox TTL. That path is
fixed and tested (P1-9, A64-028.5A), and a high rate of it means the TTL is
short for how quiet this deployment is.

**Verify.** Publish failures return to zero.

---

## Reconnect storm

**Alert:** `ReconnectStorm`.

**Confirm.** Was there a deploy? A network event upstream? Both are expected
and need no action beyond knowing.

**Mitigate.** Nothing, usually. A64-028.5A held **2 550 reconnects** across
three waves with zero failures and no slowdown in later waves.

**Recover.** If it was neither a deploy nor upstream, look for an instance
closing connections it should be keeping —
`arena64_gateway_connections_closed_total{reason}` says which reason.

---

## Scheduler failure

**Alert:** `SchedulerNotRunning`.

**Confirm.** `arena64_matchmaking_pairing_scans_total` has stopped
increasing. That job runs every second per pool, so its silence is
unambiguous.

**What has stopped.** Everything on the worker: pairing, expiry sweeps,
clock adjudication, retention, the outbox relay. Players see matchmaking
stop first.

**Mitigate.** Restart the worker container.

**Recover.** A64-028.4 found every scheduled job dead behind a
healthy-looking process, for weeks, because one handler raised on every
tick. If the restart does not fix it, read the first `task_schedule_tick_failed`
or `outbox_tick_failed` in the log.

**Verify.** The scan counter moves and the outbox backlog drains.

**Residual risk.** Exactly one worker runs by design. It is a single point
of failure for scheduled work, and that is a deliberate trade recorded in
`docs/01-architecture/deployment.md`.

---

## Outbox backlog

**Alert:** `OutboxBacklogStale`.

**Confirm.** `arena64_outbox_oldest_pending_age_seconds`, not the backlog
count. A backlog of ten thousand that is draining is healthy; a backlog of
three whose oldest entry is from yesterday is a stuck consumer.

**Mitigate.** Check the worker is running at all — see
[scheduler failure](#scheduler-failure).

**Recover.** `arena64_outbox_failed_total{reason}` names the shape:

| reason | meaning |
| --- | --- |
| `handler_error` | a consumer is raising. Read `event_delivery_failed` in the log. |
| `timeout` | a consumer is exceeding its budget. It fails its own slice only. |
| `invalid_payload` | a schema mismatch. Retrying cannot fix it — see [abandoned events](#outbox-exhausted). |

**Verify.** Oldest age returns to seconds. A64-028.5A drained 1 807 events
to zero in **30 seconds** under load.

---

## Outbox exhausted

**Alert:** `OutboxEventsAbandoned`, `OutboxTicksIncomplete`.

**This is permanent loss.** An abandoned event is not retried again by
anything.

**Confirm.** Read the `reason` label.

**`repeated_failure`** — a consumer could not handle its events five times.
If `arena64_outbox_failed_total{reason="invalid_payload"}` is also moving,
this is a schema mismatch and retrying will never work: A64-028.5A's P1-11
abandoned **1 850** queue joins this way, in every environment, for months,
because a projection could not satisfy its own schema. Fix the projection or
the schema, then replay from `platform.outbox` — the rows are retained
(AD-17) and a subscriber added later replays from the table.

**`unrecorded`** — the relay spent the attempts without writing an outcome.
Check `arena64_outbox_incomplete_ticks_total` at the same time: a tick that
claimed entries, reported no failures and published nothing is the P2-9
signature, and these two counters together are the reproduction it has been
missing. Capture them and file them; see
`docs/01-architecture/production-hardening.md` P2-9 for what is known.

**Verify.** The counter stops increasing.

**Residual risk.** Whatever those events were for did not happen. Analytics
gaps are cosmetic; a missed notification is not.

---

## Backup stale

**Alert:** `BackupStale`, `BackupNeverSucceeded`.

**Confirm.**

```bash
docker compose -f infrastructure/production/compose.yml logs --tail 50 backup
cat /var/backups/arena64/arena64-backup-status.json
```

The status file keeps the last success **and** the last failure: "the last
attempt failed" and "the last good copy is from Tuesday" are different facts
and both matter.

**Mitigate.** Run one by hand:
`docker compose run --rm backup python -m app.operator.backup create --destination /var/backups/arena64`
and read the failure. A full destination is the usual cause.

**Recover.** Fix the destination, then confirm the next scheduled run
succeeds rather than assuming it will.

**Verify.** `time() - arena64_backup_last_success_timestamp_seconds` drops
to near zero.

**Residual risk — state it plainly.** Backups are on a volume on the same
host as the database. A host loss takes both. Off-host storage and
encryption at rest are open as **P2-8** and are not closed by this alert
existing.

---

## Restore

**Destructive.** Read `docs/05-operations/backup-restore.md` §6 first; this
is the abbreviated form for somebody who already has.

1. Stop `api`, `worker` and `backup`. Leave `postgres` running.
2. `python -m app.operator.backup verify --dump <file>` — checksum and
   listing, before anything is overwritten.
3. `python -m app.operator.backup restore --dump <file> --target <dsn> --i-understand-this-overwrites`
4. Bring `migrate` up, then `api` and `worker`.
5. Run the integrity checks in `data-reliability.md` §8.

**Residual risk.** Everything since the backup is gone, including
registrations and completed games. Redis state is rebuilt from the durable
log; the ticket store is not, so every player reconnects.

---

## Migration failure

**Alert:** none — the deploy fails visibly instead.

**Confirm.** `docker compose logs migrate`. The `api` service will not have
started: it waits on `migrate` completing successfully.

**Mitigate.** The old version is still serving. There is no outage, and
there is no rush.

**Recover.** Read the Alembic error. A migration that failed **partway** is
the case that needs care: check whether its transaction committed, and if
the migration was not written to be safely re-runnable, restore rather than
re-run.

**Verify.** `alembic current` matches `alembic heads`.

**Residual risk.** All thirty-nine index creations run in a lock
(A64-028.3). At `t=0` on an empty database that is free; after launch it is
not, and `data-reliability.md` §5 states the rules.

---

## Bad production config

**Alert:** none — the process refuses to start, which is the point.

**Confirm.** The startup log names the variable. Every guard is written to
say which one and why:

| message names | meaning |
| --- | --- |
| `POSTGRES_DSN` / `REDIS_*_URL` | a local default survived into a deployed tier |
| `JWT_SECRET_KEY` | the development signing key is in production |
| `OPS_TOKEN` | the operator surface would be open — set it, or set `OPS_ALLOW_UNAUTHENTICATED=true` to say the network is the boundary |
| `RESEND_API_KEY` | set to something that is not a Resend credential |
| `BROWSER_SESSION_TRUSTED_ORIGINS` | empty in a deployed tier, disabling half the CSRF defence |

**Mitigate.** The previous version is still serving. Fix `production.env`
and deploy again.

**Recover.** Never work around a guard by unsetting the thing it checks.
Every one of them exists because the failure it prevents is silent.

---

## Certificate and HTTPS

**Alert:** `CertificateExpiringSoon`, `CertificateExpired`,
`CertificateMissing` — A64-028.6A, closing the certificate half of P3-4.

**Confirm.**

```bash
docker compose -f infrastructure/production/compose.yml logs --tail 50 certbot
docker compose -f infrastructure/production/compose.yml exec worker \
  python -c "from pathlib import Path; from app.operator import certificate_status as c; \
             print(c.days_remaining(Path('/etc/letsencrypt/live/arena64.gg/fullchain.pem')))"
```

The metric is read from **the certificate on disk**, not from the renewal
job's exit status — a job that reports success and writes nothing produces
no failure log and an expiring certificate.

**Mitigate.** Nothing is broken until the certificate expires. Let's Encrypt
issues for ninety days and `certbot renew` acts from thirty days out, so the
`ExpiringSoon` threshold of fourteen days means roughly thirty attempts have
already failed — there is time, and it is not unlimited.

**Recover.** Run one by hand and read the error:

```bash
docker compose -f infrastructure/production/compose.yml run --rm certbot \
  certbot renew --webroot --webroot-path /var/www/certbot --dry-run
```

The two usual causes both break the HTTP-01 challenge while leaving the site
working, which is why neither is noticed without this alert:

- `ARENA64_DOMAIN` no longer resolves to this host;
- **port 80 is closed.** A firewall that allows only 443 looks correct and
  makes renewal impossible. See `deployment.md` §8.8.

After a successful renewal, nginx picks it up within six hours on its own
reload timer, or immediately with
`docker compose exec nginx nginx -s reload`.

**Verify.** `arena64_certificate_expiry_timestamp_seconds` jumps forward by
about ninety days.

**Residual risk.** The private key lives in a Docker volume on this host and
is mounted read-only into nginx. It is in no image, no repository and no
application container.

---

## Nginx upstream

**Alert:** `ApiUnavailable` if every replica is gone; nothing if one is.

**Confirm.** The edge resolves `api` through Docker's DNS every five seconds
and round-robins across whatever it returns. Which instance answered is in
the access log:

```bash
docker compose -f infrastructure/production/compose.yml logs nginx \
  | grep '"uri":"/api/' | tail -20
```

`upstream` holds the address; two addresses separated by a comma means
nginx retried after the first failed.

**What is covered, measured.** A stopped container costs about one request
in twenty for up to one resolver TTL and then heals with no action. An
application that has died while its container is still running is covered
by the retry — 8 of 8 requests were answered by the survivor.

**What is not.** A container that **accepts a connection and then hangs**.
No retry helps, because the connection succeeded. Its own `HEALTHCHECK`
marks it unhealthy and plain Compose does not act on that; an orchestrator
with health-aware service discovery would drop it.

**Mitigate.** `docker compose restart api-1`. It is a blunt instrument and
it is the right one: the instance is not serving and nothing else is
affected.

**Recover.** If it recurs, the event-loop lag metric will say whether the
process is blocked — see [event loop lag](#event-loop-lag).

**Why there is no `upstream` block.** Nginx resolves those names at parse
time and refuses to start if one fails, so a single crashed backend would
take the edge down entirely. `deployment.md` §8.3 has the reasoning and the
numbers.

---

## Disk pressure

**Alert:** `DiskNearlyFull`, `DiskWillFillSoon`, `InodesNearlyExhausted` —
A64-028.7, closing half of P3-4.

**Confirm.** The host dashboard, or on the machine:

```bash
df -h ; df -i
docker system df
du -sh /var/lib/docker/volumes/* 2>/dev/null | sort -h | tail -5
```

**What fills it, in the order it usually happens.**

| Consumer | Bound |
| --- | --- |
| Backups | `DEFAULT_KEEP` = 7 generations, pruned on every **successful** run |
| Prometheus | its own retention, set at deploy |
| PostgreSQL | table retention (outbox, analytics, matchmaking, notifications) |
| WAL | checkpoints, unless a replication slot is stuck |
| Redis AOF | rewritten as it grows |
| Docker images | **unbounded** — nothing prunes old layers |
| Container logs | `json-file` with rotation, see [log rotation](#log-rotation) |

**Mitigate.** `docker image prune -a --filter "until=168h"` is the safest
first move: it touches no volume and no running container.

**Recover.** An unpruned backup destination means backups have been
*failing* — pruning only runs after a successful one, so
[a stale backup](#backup-stale) and a full disk arrive together and the
first causes the second.

**Verify.** `DiskNearlyFull` clears, and `DiskWillFillSoon` with it.

**Residual risk.** Docker image layers have no automatic bound. A deploy
that runs often enough will fill a small disk with images nothing runs, and
the only thing that catches it is these alerts.

---

## Memory pressure

**Alert:** `MemoryPressure`.

**Confirm.** `free -m` on the host, then `docker stats --no-stream`.

`MemAvailable`, not `MemFree`: Linux uses free memory for page cache by
design, so `MemFree` is near zero on every healthy machine.

**Mitigate.** The next thing to happen is the OOM killer choosing a process,
and it is not obliged to choose a good one — PostgreSQL is a large, long-
lived process and an attractive target. Restarting the container that grew
is better than letting the kernel decide.

**Recover.** PostgreSQL's `shared_buffers` and Redis's dataset are supposed
to be large. The API's RSS is measured separately
(`arena64_http_requests_in_flight` and the process metrics) and should not
be: A64-028.5A measured about 100 KB per idle WebSocket and 519 MB at two
thousand of them.

**Verify.** Available memory returns above 10%.

**Residual risk.** No container has a memory limit — see **HOST-SIZING
GATE** in `deployment.md`. Until the server exists there is no honest number
to set, and a wrong limit turns a busy hour into an OOM kill.

---

## Host metrics missing

**Alert:** `HostMetricsMissing`.

**Confirm.** `docker compose ps node-exporter`, then from inside the
network: `curl -s http://node-exporter:9100/metrics | head -1`.

**Why it matters more than it looks.** Every rule in the host group reads
this target, so its silence disables all of them — and a disabled alert is
indistinguishable from a healthy one. This rule exists to make that
difference visible.

**Mitigate.** Restart it. It holds no state, publishes no port, and its
absence affects nothing but the monitoring.

**Recover.** If it will not start, check the read-only host mounts: it needs
`/proc`, `/sys` and `/` and writes to none of them.

**Verify.** `up{job="arena64-host"}` returns to 1 and the disk panels
repopulate.

---

## Email provider failure

**Alert:** none — see the residual risk.

**Confirm.** `arena64_notifications_email_deliveries{outcome}` on the
external delivery dashboard.

**What is affected.** Registration verification and password reset. Both are
first-contact flows, so the people affected are the least able to report it.

**Mitigate.** Nothing platform-side. Delivery is retried through the outbox
with bounded attempts.

**Recover.** Check the provider's status. If the credential is the problem,
the process would have refused to start — so a running platform that cannot
send has a provider problem, not a configuration one.

**Residual risk.** **No alert exists on this yet.** The metric is there and
the rule is not, because a threshold that distinguishes "the provider is
down" from "several addresses bounced" needs a baseline this deployment has
not yet produced. Stated rather than guessed.
