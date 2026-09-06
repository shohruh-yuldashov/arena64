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

**Alert:** none yet — see the residual risk.

**Confirm.** `docker compose logs caddy | grep -i certificate`. Caddy
obtains and renews automatically; a failure is almost always DNS or rate
limiting at the ACME provider.

**Mitigate.** Existing certificates remain valid until expiry, so a renewal
failure is days of warning rather than an outage.

**Recover.** Check that `ARENA64_DOMAIN` resolves to this host and that port
80 is reachable — the HTTP-01 challenge needs it, and a firewall that allows
only 443 breaks renewal while leaving the site working.

**Residual risk.** **Nothing alerts on certificate expiry.** Caddy's own
logs are the only signal, and `ARENA64_ACME_EMAIL` is what gets the
provider's warning. Wiring a probe is open work.

---

## Disk pressure

**Alert:** none — see the residual risk.

**Confirm.** `df -h` on the host, and the sizes of the `postgres_data`,
`backup_data` and `caddy_data` volumes.

**Mitigate.** Backups are the usual growth: `DEFAULT_KEEP` retains seven
generations and prunes on every successful run, so an unpruned destination
means backups have been failing.

**Recover.** Retention for the durable tables is documented in
`data-reliability.md` §4 and runs on the worker.

**Residual risk.** **No disk metric is collected.** The exporter reports the
application's own memory and nothing about the host. A node exporter is the
obvious answer and is open work.

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
