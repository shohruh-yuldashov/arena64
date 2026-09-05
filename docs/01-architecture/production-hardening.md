# Production Hardening — A64-028

> **Status:** living. Opened by A64-028.1 (2026-09-05); closed by A64-028.7.
> **Owner:** platform engineering.
> **Purpose:** to say exactly what can fail in production, why, how severe it
> is, and which task will prove or fix it. Not "the app looks ready".

Every claim below is evidence-backed. Where the answer is not known, the
status is `UNKNOWN` and a task is named — an unknown is a finding, not a gap
in this document.

---

## 1. The headline

**Arena64 has no production tier.** What exists is one staging definition
(`infrastructure/staging/compose.yml`) which, by its own header, "is **not**
production", deviates from `architecture.md` in three recorded ways, and
has never run anywhere but `localhost`
([`deployment.md`](./deployment.md) §5).

Two structural facts follow from the code and bound everything else:

| Fact | Evidence |
| --- | --- |
| The platform runs as **one process** | `main.py` calls `uvicorn.run` with no `workers=`; `apps/api/Dockerfile` `CMD ["python", "main.py"]` |
| That process can only ever be **one instance** | `app/gateway/bus.py` — the production bus adapter does not exist, and "a deployment running more than one gateway node today has silently undelivered frames … single-node is the only supported topology". `app/platform/tasks/scheduler.py` has no lock, leader election or advisory lock, so a second replica runs all 16+ scheduled jobs a second time |

Horizontal scaling is therefore not a tuning exercise; it is unbuilt work.
Capacity planning (§12) must assume a single process until A64-028.4 says
otherwise.

---

## 2. Architecture inventory

| Component | Responsibility | Stateful? | Scaling model | Fails if | Production config source |
| --- | --- | --- | --- | --- | --- |
| `api` (single process) | HTTP, WebSocket gateway, and all 16+ schedulers in one lifespan | No (state in PG/Redis) | **None — one instance only** (§1) | PG or Redis unreachable | `compose.yml` (staging only) |
| `migrate` (one-shot) | `alembic upgrade head`, exits before `api` starts | No | n/a | Any migration fails | `compose.yml` |
| Caddy | TLS termination, reverse proxy, `/media` → MinIO | No | n/a | Certificate issuance fails | `infrastructure/staging/Caddyfile` |
| PostgreSQL 17 | Every durable fact; outbox; audit; analytics | **Yes** | Single container, named volume | — | `compose.yml` |
| Redis 8 | 5 logical DBs: live, bus, broker, cache, limits | **Yes** (`live`, AOF on) | Single container, named volume | — | `compose.yml` |
| MinIO | Avatars and media, one public bucket | **Yes** | Single container | — | `compose.yml` |
| Outbox relay | Drains `platform.outbox`; `FOR UPDATE SKIP LOCKED` | No | Cooperative — safe with N relays | PG unreachable | `OUTBOX_WORKER_ENABLED` |
| Presence sweeper | Expires stale presence, writes outbox rows | No | **No singleton guard** | PG/Redis unreachable | `PRESENCE_*` |
| 16+ periodic schedulers | Pairing, clock adjudication, retention, notification/push/email/broadcast delivery, tournament deadlines, metrics flush | No | **No singleton guard** | — | `app_factory.build_task_schedulers` |
| `apps/web`, `apps/admin` | Static SPA bundles | No | n/a | — | **None — not deployed anywhere** |

**Not present anywhere in the repository:** Celery (the dispatcher is
in-process — `InlineTaskDispatcher`), a metrics backend, an error tracker, a
log pipeline, a backup mechanism, a production compose/manifest.

---

## 3. Risk register

> **A64-028.2 closed P0-1, P1-1 and P2-4.** Remaining: 3 P0, 6 P1, 5 P2, 3 P3.
> See `specs/authentication.md` "Rotation under concurrency" for the design.


Severity: **P0** launch blocker · **P1** serious reliability/operational risk ·
**P2** hardening · **P3** improvement.

### P0-1 — Email links point at `localhost` in any deployed tier

> **RESOLVED — A64-028.2.** Both templates are now *derived* from
> `PUBLIC_APP_URL` when unset, so the misconfiguration is unreachable rather
> than merely loud: there is one place the public origin is configured, it
> is already refused at its local default in every deployed tier, and both
> links are downstream of that single decision. An explicit template still
> wins, and a deployed tier refuses one whose host is a loopback address.
> `tests/unit/test_email_link_config.py` — 24 tests, mutation-checked.

| | |
| --- | --- |
| **Area** | Configuration |
| **Status** | ~~FAIL~~ → **PASS** |
| **Evidence** | `settings.py:632,666` default both templates to `http://localhost:3000/...`. `EmailSettings._url_templates_must_carry_the_token` checks only that `{token}` is present. `Settings._forbid_local_defaults_outside_local` guards `POSTGRES_DSN`, five `REDIS_*`, `PUBLIC_APP_URL`, `EMAIL_VERIFICATION_OTP_SECRET`, `JWT_SECRET_KEY` and `BROWSER_SESSION_TRUSTED_ORIGINS` — **and neither template**. `infrastructure/staging/compose.yml` sets neither; `staging.env.example` names neither. |
| **Failure mode** | The process starts normally and sends email verification and password-reset links pointing at `http://localhost:3000`. Nothing raises; the mail is delivered; the links are dead. |
| **Impact** | No new account can be verified and no password can be recovered. Registration and account recovery are both broken, silently. |
| **Likelihood** | **Certain** with the configuration as it stands today. |
| **Action** | Extend the existing validator to refuse both localhost defaults in a production-like tier, exactly as it already refuses five others; add both to `staging.env.example` and the compose file. |
| **Owner** | **A64-028.2** |
| **Blocker?** | **YES** |

### P0-2 — The web and admin clients have no deployment

| | |
| --- | --- |
| **Area** | Deployment |
| **Status** | **FAIL** |
| **Evidence** | `compose.yml` defines `api`, `migrate`, `caddy`, `postgres`, `redis`, `minio` — no client. `.github/workflows/ci.yml` builds and publishes the API image only; the web and admin jobs lint, type-check and test but never build a bundle. `VITE_PUBLIC_ORIGIN` is set nowhere in the repository. `deployment.md` §6 P-4 records the gap. |
| **Failure mode** | There is nothing to serve. The Caddyfile proxies every path to the API, so `/` returns the API's 404. |
| **Impact** | No product. Additionally, `apps/web/scripts/generate-seo.mjs` writes a `robots.txt` that **blocks everything** when `VITE_PUBLIC_ORIGIN` is unset — a fail-safe that becomes a launch defect if a bundle is ever built without it. |
| **Likelihood** | Certain. |
| **Action** | Define the client build and host; set `VITE_PUBLIC_ORIGIN=https://arena64.gg`; keep the client and API on **one origin** — the refresh cookie is HttpOnly and same-site by design (`apps/web/.env.example`), which is also why the API carries no CORS middleware and must not gain one. |
| **Owner** | **A64-028.6** |
| **Blocker?** | **YES** |

### P0-3 — No production edge configuration

| | |
| --- | --- |
| **Area** | SEO / Edge / Security headers |
| **Status** | **FAIL** |
| **Evidence** | `infrastructure/staging/Caddyfile` sets `encode` and two `reverse_proxy` rules and nothing else: no `Content-Security-Policy`, `Strict-Transport-Security`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, no `X-Robots-Tag`, no SPA fallback, no true 404. The API adds none either — `create_app` registers only `CorrelationIdMiddleware` and `RequestIdMiddleware`. |
| **Failure mode** | Missing transport and framing protections; private and auth paths indexable; an SPA host that rewrites everything to `index.html` returns HTTP 200 for URLs that do not exist. |
| **Impact** | Security headers absent in production; `/admin`, `/auth/*` and other private paths eligible for indexing; soft-404s pollute the index and mislead crawlers. |
| **Likelihood** | Certain — none of it exists. |
| **Action** | Own the edge configuration in this repository. Required: HSTS, CSP, `Referrer-Policy`, `Permissions-Policy`, frame policy; `X-Robots-Tag: noindex` on every path `robots.txt` disallows and on all private/admin/auth paths; a genuine HTTP 404 for unknown routes; www/non-www canonical policy. |
| **Owner** | **A64-028.6** |
| **Blocker?** | **YES** |

### P0-4 — No backups and no tested restore

| | |
| --- | --- |
| **Area** | Data reliability |
| **Status** | **FAIL** |
| **Evidence** | No `pg_dump`, backup script, cron, retention policy or restore procedure exists anywhere in `infrastructure/`, `docker/`, `.github/` or the docs. PostgreSQL and MinIO hold named Docker volumes on one host. `deployment.md` §6 P-3: "A named volume is not a backup, and an untested restore is not one either." |
| **Failure mode** | Any volume loss, host loss or destructive migration is unrecoverable. |
| **Impact** | Total, permanent loss of every account, match, rating, tournament and audit record. |
| **Likelihood** | Low per-day, catastrophic and irreversible when it occurs. |
| **Action** | Automated encrypted off-host PostgreSQL backups with a stated frequency and retention; a written restore procedure; **a restore actually performed and its result recorded**. Decide Redis persistence separately: `live` (AOF on) holds match state and is *not* reconstructable; `cache`, `limits`, `broker` and `bus` are. |
| **Owner** | **A64-028.3** (restore proof is a deliverable, not a note) |
| **Blocker?** | **YES** |

### P1-1 — A second browser tab signs the user out of both

| | |
| --- | --- |
| **Area** | Auth / session |
| **Status** | ~~FAIL~~ → **PASS** — A64-028.2 |
| **Evidence** | Deterministic probe, two independent connections, one token, real PostgreSQL: tab A rotated successfully; tab B found the row revoked with reason `rotated`, `_handle_reuse` fired and revoked the **whole family including A's new successor**. Result: `family rows: 2, live: 0`, and a `refresh_token_reuse_detected` WARNING. A64-027A.5's frontend single-flight is per-tab and cannot see the other tab. |
| **Failure mode** | Two tabs whose refreshes overlap → both signed out at random, plus a false security alert. |
| **Impact** | Users signed out mid-session with no explanation. Worse: the platform's **only** token-theft signal fires on benign multi-tab use, so any alert on that rate is unusable. |
| **Likelihood** | High — any user with two tabs and overlapping token expiry. |
| **Action** | A server-side rotation grace window. The seam already exists in the data: `revoked_reason = 'rotated'` distinguishes a benign rotation from a sign-out or a prior reuse. Within a short window, a token revoked *by rotation* and presented once should return the successor (or a retry signal) rather than burning the family. **Reuse detection must not be weakened for any other revocation reason, for a second presentation, or outside the window.** |
| **Owner** | A64-028.2 — **done** |
| **Resolution** | A revoked token is a *concurrent rotation* rather than a reuse when three conditions hold together: the reason is `rotated`, it was revoked within `SESSION_ROTATION_GRACE_SECONDS` (10), and the family still has a live session. That case is answered `409` with a retry hint and **never with a credential**, so the window costs alarm latency and not authority. Everything else takes the reuse path unchanged. `rotate_refresh_token` also reads `FOR UPDATE`, so one token yields exactly one successor. Proven by `tests/contract/test_refresh_concurrency.py` (16 tests, real PostgreSQL, each guard individually mutation-checked) and in a real browser: three simultaneous two-tab races → 3 × `409`, 0 sign-outs, family `live=1 reuse_detected=0`. |
| **Blocker?** | No — resolved |

### P1-2 — Realtime is silently broken above one instance

| | |
| --- | --- |
| **Area** | Realtime |
| **Status** | **FAIL** (as a scaling property) |
| **Evidence** | `app/gateway/bus.py`: a port, an envelope and an **in-process adapter**. The production adapter `RedisStreamGatewayBus` is named in the docstring as future work and does not exist. Quoted there from A64-016.3: "a deployment running more than one gateway node today has silently undelivered frames … single-node is the only supported topology". |
| **Failure mode** | With two instances, a move made on node A never reaches an opponent held by node B. No error is raised on either side. |
| **Impact** | Games freeze for one player with no visible cause. Silent, so it would reach users before it reached an operator. |
| **Likelihood** | Certain if a second instance is ever started; zero today. |
| **Action** | Build the Redis-stream bus adapter, or state single-instance as a hard operational constraint and enforce it in the deployment definition. |
| **Owner** | **A64-028.4** |
| **Blocker?** | No — provided the deployment is pinned to one instance and that pin is enforced |

### P1-3 — Schedulers have no singleton guard

| | |
| --- | --- |
| **Area** | Workers / deployment |
| **Status** | **RISK** |
| **Evidence** | `PeriodicTaskScheduler` is a bare `asyncio` interval loop — no advisory lock, no leader election. `app_factory.build_task_schedulers` starts 16+ job types (plus one pairing scheduler **per pool**) in every process's lifespan. `deployment.md` §2: "a second replica … would run a second copy of the outbox relay, the pairing sweep and the clock adjudicator. Several claim work with `SKIP LOCKED` and would be safe; **that has never been tested**." |
| **Failure mode** | N replicas → N executions per interval. The outbox relay is cooperative (`FOR UPDATE SKIP LOCKED`); the pairing sweep, clock adjudicator, tournament reconciliation, no-show sweep and retention prunes are **unverified**. |
| **Impact** | Unknown per job. Candidates include double pairing, double adjudication of one clock, and duplicated tournament state transitions. |
| **Likelihood** | Certain if scaled; zero today. |
| **Action** | Prove idempotency per job under two concurrent runners, or give the scheduler a PostgreSQL advisory lock. |
| **Owner** | **A64-028.4** |
| **Blocker?** | No — same pin as P1-2 |

### P1-4 — Nothing can be observed in production

| | |
| --- | --- |
| **Area** | Observability |
| **Status** | **FAIL** |
| **Evidence** | `app/platform/metrics/__init__.py`: "There is no metrics backend in this deployment: no Prometheus, no StatsD, no OpenTelemetry collector." No `/metrics` endpoint. No error tracker, no dashboard, no alert rule anywhere in the repository. Structured JSON logs are written to stdout and no pipeline collects them. `deployment.md` §6 P-6 records it. |
| **Failure mode** | An incident is invisible until a user reports it, and unexplainable afterwards because nothing was retained. |
| **Impact** | No mean-time-to-detect. Every other risk in this register becomes harder to diagnose. |
| **Likelihood** | Certain. |
| **Action** | The instrumentation is real and already emits at the right instants with bounded labels — what is missing is an exporter, storage, dashboards and alerts. See §10 and §11 for the minimum set. |
| **Owner** | **A64-028.6** |
| **Blocker?** | No — but launching blind is a deliberate decision that should be recorded as one |

### P1-5 — Readiness cannot fail

| | |
| --- | --- |
| **Area** | Health |
| **Status** | **RISK** |
| **Evidence** | `app/api/v1/health.py`: `/health/ready` returns HTTP **200** with `status: "degraded"` when PostgreSQL or Redis is unreachable. It never returns a non-2xx. |
| **Failure mode** | A load balancer or orchestrator that reads the HTTP status — which is what they read — considers a process with no database perfectly ready and keeps sending it traffic. |
| **Impact** | A degraded instance stays in rotation. (Liveness at `/health` is correctly dependency-free, and the Dockerfile's `HEALTHCHECK` correctly calls it — so there is no restart storm.) |
| **Likelihood** | Certain when a dependency fails. |
| **Action** | Return `503` from `/health/ready` when a required dependency is down, keeping the diagnostic body. Do **not** change `/health`. |
| **Owner** | **A64-028.6** |
| **Blocker?** | No |

### P1-6 — Every deploy drops every live game

| | |
| --- | --- |
| **Area** | Deployment / realtime |
| **Status** | **RISK** (known, accepted for staging) |
| **Evidence** | `deployment.md` §1: "**Every live game is dropped** — one process holds the WebSocket connections and the schedulers alike". No socket drain exists in `lifespan`'s shutdown path, which stops schedulers, the sweeper and the outbox relay, then closes Redis and the database. |
| **Failure mode** | A deploy severs every WebSocket mid-game. |
| **Impact** | Players lose games in progress. With one instance there is no rolling window in which to avoid it. |
| **Likelihood** | Certain, on every deploy. |
| **Action** | AD-02's `gateway`/`api`/`worker` split (`deployment.md` §6 P-1), or a maintenance window and an in-app warning. Reconnect behaviour on the client should be measured before deciding which. |
| **Owner** | **A64-028.4** (behaviour), **A64-028.6** (deploy procedure) |
| **Blocker?** | No |

### P1-7 — A Redis outage silently removes rate limiting

| | |
| --- | --- |
| **Area** | Abuse |
| **Status** | **RISK** (deliberate, documented) |
| **Evidence** | `RateLimitSettings.fail_open = True`, with a full written rationale: failing closed would convert a limiter outage into a total authentication outage. Argon2id, `users.locked_until` and 256-bit reset tokens remain. |
| **Failure mode** | While Redis is unreachable every rate limit is bypassed and nothing signals it except a log line. |
| **Impact** | An abuse window whose length is the outage's length. The trade is correct; the **absence of an alert on it** is not. |
| **Likelihood** | As likely as a Redis outage. |
| **Action** | Keep fail-open. Add a P0 alert on Redis availability and on the fail-open counter (§11). |
| **Owner** | **A64-028.6** |
| **Blocker?** | No |

### P2 findings

| ID | Area | Status | Finding | Evidence | Owner |
| --- | --- | --- | --- | --- | --- |
| P2-1 | PostgreSQL | RISK | No `pool_pre_ping` and no `pool_recycle`. Behaviour after a PostgreSQL restart is untested — SQLAlchemy invalidates a pool generation on a detected disconnect, so the expected cost is a small burst of errors, but that is inference, not measurement | `app/database/engine.py` sets `pool_size`, `max_overflow`, `pool_timeout`, `statement_timeout` and nothing else | **A64-028.3** |
| P2-2 | Logging | RISK | No redaction filter at the logging boundary. `_JsonFormatter` emits whatever `extra={…}` a call site passed. `CLAUDE.md` §8.3 requires redaction *at the boundary* "so redaction cannot be forgotten"; today it is call-site discipline. That discipline is currently good (`session_service` logs identifiers only, never the token, user agent or IP) | `app/common/logging.py` | **A64-028.6** |
| P2-3 | Migrations | RISK | Three migrations create indexes non-concurrently; one uses `CONCURRENTLY`. A plain `CREATE INDEX` blocks writes to the table for its duration — harmless on an empty first deploy, not harmless on a populated `analytics.event` | `alembic/versions/` | **A64-028.3** |
| P2-4 | Notifications | ~~RISK~~ → **PASS** | **Resolved — A64-028.2.** The audit sharpened the finding: absent is a supported decision and a malformed pair already raised, but a **half** pair was silently treated as "push not configured" — an operator who set one key got a tier that refused every subscription and said nothing. `PushSettings` now refuses a half pair at startup, naming the missing variable and never the value | `settings.py`, `tests/unit/test_push_config.py` | A64-028.2 — **done** |
| P2-5 | Capacity | UNKNOWN | One uvicorn worker; 15 PostgreSQL connections per process (`pool_size=10` + `max_overflow=5`); one instance. No load test has ever run. Every throughput figure is unknown | `main.py`, `engine.py` | **A64-028.5** |
| P2-6 | Docs | RISK | The Dockerfile's `HEALTHCHECK` comment says the endpoint "reports the database and Redis". It calls `/api/v1/health`, which is liveness and reports neither. The behaviour is right; the comment describes `/health/ready` | `apps/api/Dockerfile` | **A64-028.6** |

### P3 findings

| ID | Finding | Owner |
| --- | --- | --- |
| P3-1 | `apps/web/.env.example` does not document `VITE_PUBLIC_ORIGIN`, which the SEO build reads | **A64-028.6** |
| P3-2 | Runtime version ambiguity: `requires-python >= 3.13`, the image is 3.13, the development venv is 3.14, and nothing pins it | **A64-028.3** |
| P3-3 | `compose.yml` gives `RESEND_API_KEY` an empty default, implying it is optional. It is not — `ConsoleEmailProvider` refuses to construct in a deployed tier, so the stack fails to boot | **A64-028.6** |

---

## 4. What passed

Recorded so that later tasks do not re-litigate them.

| Area | Result | Evidence |
| --- | --- | --- |
| Configuration guards | **PASS** | `Settings._forbid_local_defaults_outside_local` refuses the local default for `POSTGRES_DSN`, five `REDIS_*`, `PUBLIC_APP_URL`, the OTP secret, the JWT key and an empty trusted-origin list. The first staging boot failed on the last of these — the guard working |
| Rate-limit profile | **PASS** | Forced from the environment, never read from `RATE_LIMIT_PROFILE`; an unrecognised environment gets **production** limits |
| Rate-limit bypass | **PASS** | No localhost, IP or User-Agent bypass exists in `rate_limiter.py` |
| SQL injection | **PASS** | 20 `text()` sites, all parameter-bound; no f-string interpolation into SQL |
| Insecure patterns | **PASS** | No `verify=False`, `debug=True`, wildcard CORS, `eval`, `exec`, `pickle.loads` or `shell=True` in `apps/api/app` |
| Deferred work | **PASS** | One `TODO` in the entire source tree, and it is a docstring recording a *closed* one |
| CORS absence | **PASS — by design** | The client and API share one origin because the refresh cookie is HttpOnly and same-site (`apps/web/.env.example`). Adding CORS would require `SameSite=None` and reintroduce the CSRF exposure the design avoids |
| CSRF | **PASS** | `browser_csrf.enforce_trusted_origin` — `Origin` with a `Referer` fallback, checked against an allowlist that a deployed tier must populate |
| Cookies | **PASS** | `HttpOnly`, `Secure` resolved from the environment, `SameSite`, and a path that matches between `set_cookie` and `delete_cookie` |
| Token rotation | **PASS** | Rotation on every use; the successor inherits `token_family` and `expires_at`; reuse burns the family. (Its interaction with concurrency is P1-1) |
| Statement timeout | **PASS** | 5000 ms, set per connection |
| External call timeouts | **PASS** | Resend and WebPush both construct `httpx.AsyncClient(timeout=…)` |
| Time handling | **PASS** | The clock is injected everywhere; every `datetime.now()` occurrence in the tree is a comment forbidding it |
| Migrations | **PASS** | Single head, 57 revisions, every one with a real `downgrade`. Verified this task on a scratch database: empty → head → base → head, clean |
| Outbox | **PASS** | Transactional outbox with `FOR UPDATE SKIP LOCKED`, a `processed_event` ledger, bounded retry, and a retention prune |
| Redis access patterns | **PASS** | No `KEYS` on any request path; `scan` appears only in an operator tool |
| Analytics retention | **PASS** | Raw events pruned at 400 days |
| Image hygiene | **PASS** | Non-root user, `uv sync --frozen`, no dev dependencies, minimal runtime packages |
| Migration ordering | **PASS** | `migrate` runs to completion before `api` starts (`service_completed_successfully`) |
| CI gating | **PASS** | The image is published only from `main` and only after lint, types and tests pass on all three apps |

---

## 5. Environment and configuration checklist

| Variable | Class | Required in production | Guarded today |
| --- | --- | --- | --- |
| `ENVIRONMENT` | runtime | yes | — (drives every other guard) |
| `POSTGRES_DSN` | secret | yes | **yes** |
| `REDIS_{LIVE,BUS,BROKER,CACHE,LIMITS}_URL` | secret | yes | **yes** |
| `PUBLIC_APP_URL` | runtime | yes | **yes** |
| `JWT_SECRET_KEY` | secret | yes | **yes** |
| `EMAIL_VERIFICATION_OTP_SECRET` | secret | yes | **yes** |
| `BROWSER_SESSION_TRUSTED_ORIGINS` | runtime | yes | **yes** |
| `EMAIL_VERIFICATION_URL_TEMPLATE` | runtime | **yes** | **NO — P0-1** |
| `EMAIL_PASSWORD_RESET_URL_TEMPLATE` | runtime | **yes** | **NO — P0-1** |
| `RESEND_API_KEY` | secret | yes | indirect (provider refuses to construct) |
| `EMAIL_FROM_ADDRESS`, `EMAIL_FROM_NAME` | runtime | yes | no |
| `STORAGE_PROVIDER`, `STORAGE_S3_*` (4) | secret/runtime | yes | **yes** |
| `STORAGE_PUBLIC_BASE_URL` | public | yes | no |
| `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` | secret | if push is offered | **no — P2-4** |
| `GATEWAY_NODE_ID` | runtime | recommended | no (random fallback; legibility only) |
| `RATE_LIMIT_TRUSTED_PROXY_COUNT` | runtime | yes | no — must match the proxy depth |
| `OUTBOX_WORKER_ENABLED` | runtime | yes | no |
| `VITE_PUBLIC_ORIGIN` | public build-time | **yes** | **NO — P0-2** |
| `VITE_API_URL` | public build-time | no (relative default is correct) | n/a |

---

## 6. Failure scenario matrix

| Scenario | Expected behaviour | Class |
| --- | --- | --- |
| API instance dies | Total outage — one instance, no redundancy | **UNSAFE** |
| Deploy while games are live | Every WebSocket severed (P1-6) | **DEGRADED** |
| PostgreSQL restarts | Pool holds dead connections; SQLAlchemy invalidates on a detected disconnect. Cost unmeasured (P2-1) | **UNKNOWN** |
| PostgreSQL unavailable | Requests fail; readiness reports `degraded` **with HTTP 200** (P1-5); liveness stays up, so no restart storm | **DEGRADED** |
| Redis unavailable | Rate limiting fails open (P1-7); live match state in the `live` role is unavailable | **DEGRADED** |
| Redis restarts | `live` recovers from AOF; `cache`, `limits`, `broker`, `bus` reconstruct | **SAFE** |
| Email provider timeout | `httpx` timeout bounds it; delivery retried by the scheduled poller | **SAFE** |
| Push provider timeout | Same | **SAFE** |
| Duplicate outbox delivery | `processed_event` ledger, `ON CONFLICT DO NOTHING` — at-least-once delivery, exactly-once effect | **SAFE** |
| Duplicate scheduled task (two replicas) | Outbox relay cooperates via `SKIP LOCKED`; other jobs unverified (P1-3) | **UNKNOWN** |
| Token refresh race (two tabs) | Family burned, both tabs signed out — **reproduced** (P1-1) | **UNSAFE** |
| Two API instances, realtime | Frames silently undelivered (P1-2) | **UNSAFE** |
| Clock skew between processes | One process today, so no inter-process comparison exists | **N/A** |
| Volume or host loss | Unrecoverable (P0-4) | **UNSAFE** |
| Network partition | Untested | **UNKNOWN** |

---

## 7. Capacity dimensions

No throughput figure is asserted. A64-028.5 measures them.

| Dimension | Config limit | Current limit | Status |
| --- | --- | --- | --- |
| API instances | 1 | 1 (P1-2, P1-3) | **hard ceiling** |
| Uvicorn workers | not set → 1 | 1 | **hard ceiling** |
| PostgreSQL connections | `pool_size` 10 + `max_overflow` 5 | 15 per process | configured |
| Statement duration | 5000 ms | — | configured |
| Concurrent WebSockets | none set | unknown | **TEST NEEDED** |
| Concurrent live games | none set | unknown | **TEST NEEDED** |
| Matchmaking joins/sec | rate-limited per identity | unknown | **TEST NEEDED** |
| API requests/sec | rate-limited per identity | unknown | **TEST NEEDED** |
| Broadcast audience size | `BroadcastSettings` batch size | unknown at scale | **TEST NEEDED** |
| Analytics events/sec | rate-limited | unknown | **TEST NEEDED** |
| Worker concurrency | in-process, shares the event loop | unknown | **TEST NEEDED** |

---

## 8. Load-test plan (A64-028.5)

Run against a staging tier that mirrors production configuration. Measure
PostgreSQL pool usage, Redis latency and event-loop lag for every scenario.
**No SLO is defined by this document** — the targets below are *candidates*
for A64-028.5 to accept, reject or replace with measured baselines.

| # | Scenario | Load | Ramp / duration | Measures | Error budget |
| --- | --- | --- | --- | --- | --- |
| A | Public browsing | 200 VU, unauthenticated landing/discovery | 1 min / 10 min | p50/p95/p99 latency, 5xx | < 0.1% |
| B | Login and refresh | 100 VU signing in, then refreshing on expiry | 2 min / 15 min | refresh success rate, **`refresh_token_reuse_detected` rate (must be 0 after P1-1)** | 0 unexpected sign-outs |
| C | Matchmaking burst | 500 joins in 30 s across pools | instant / 5 min | pairing latency, queue depth, duplicate pairings (**must be 0**) | 0 duplicates |
| D | Live game WebSockets | Ramp connections until saturation; half of them playing | 5 min / 20 min | connections held, frame latency, dropped frames, memory | 0 dropped frames |
| E | Tournament traffic | One tournament filling and starting under concurrent registration | 1 min / 10 min | transition correctness, no-show sweep behaviour | 0 incorrect transitions |
| F | Notification broadcast | Broadcast to the largest realistic audience | instant / until drained | DB write rate, outbox backlog and oldest-pending age, duplicate deliveries (**must be 0**) | 0 duplicates |
| G | Analytics ingestion | Sustained event rate at the collector's rate limit | 2 min / 15 min | ingestion latency, `analytics.event` growth, query plans on admin analytics | < 0.1% loss |
| H | Mixed realistic load | A+B+C+D+G together at a fraction of each ceiling | 5 min / 30 min | everything above; the number that matters is where it first breaks | — |

**Find the ceiling, do not assume it.** Each scenario ramps until something
degrades, and the finding is the degradation point and its first symptom.

---

## 9. Production checklist

| Category | Item | State |
| --- | --- | --- |
| CONFIG | Every required variable guarded against its local default | **TODO** (P0-1) |
| CONFIG | Secrets from a manager, not a file on the host | **TODO** (`deployment.md` P-2) |
| SECURITY | Security headers at the edge | **TODO** (P0-3) |
| SECURITY | `X-Robots-Tag` on private paths | **TODO** (P0-3) |
| SECURITY | Rotation grace window for concurrent refresh | **TODO** (P1-1) |
| DB | Automated encrypted off-host backups | **TODO** (P0-4) |
| DB | Restore performed and recorded | **TODO** (P0-4) |
| DB | `pool_pre_ping`; restart behaviour measured | **TODO** (P2-1) |
| DB | Migrations verified zero → head → base → head | **DONE** (this task) |
| REDIS | Persistence decision per role, recorded | **TODO** |
| WORKERS | Scheduler singleton or proven per-job idempotency | **TODO** (P1-3) |
| REALTIME | Multi-node bus, or a single-instance pin that is enforced | **TODO** (P1-2) |
| REALTIME | Deploy procedure for live games | **TODO** (P1-6) |
| FRONTEND | Web and admin build and host defined | **TODO** (P0-2) |
| FRONTEND | `VITE_PUBLIC_ORIGIN` set for the production build | **TODO** (P0-2) |
| OBSERVABILITY | Metrics exporter and dashboards | **TODO** (P1-4) |
| OBSERVABILITY | Error tracking | **TODO** (P1-4) |
| OBSERVABILITY | Log pipeline with retention | **TODO** (P1-4) |
| OBSERVABILITY | Redaction at the logging boundary | **TODO** (P2-2) |
| DEPLOYMENT | A production definition that is not the staging file | **TODO** |
| DEPLOYMENT | Readiness returns 503 when degraded | **TODO** (P1-5) |
| DEPLOYMENT | Rollback procedure | **TODO** |
| SEO/EDGE | True 404 and SPA fallback policy | **TODO** (P0-3) |
| SMOKE TEST | Post-deploy smoke suite | **TODO** |

---

## 10. Minimum production metrics (A64-028.6)

The instrumentation exists; the exporter does not. Keep label cardinality
bounded — **never** a node id, user id, match id or IP as a label.

HTTP request count / latency / status class · PostgreSQL pool in-use,
overflow and connection errors · Redis operation errors, latency, and the
rate-limiter **fail-open counter** · WebSocket connections held,
connect/disconnect rate, publish failures · matchmaking queue depth, pairing
latency, pairing failures · scheduler task success/failure/duration per job
name · outbox backlog size and **oldest pending age** · notification
send/retry/failure by channel · auth login and refresh failure rates, and
`refresh_token_reuse_detected` as its own series.

## 11. Alert candidates

Few, and each actionable.

**P0:** PostgreSQL unreachable · Redis unreachable · API 5xx rate above
baseline · outbox oldest-pending age beyond threshold · backup job failed.
**P1:** rate-limiter fail-open engaged · WebSocket publish-failure spike ·
matchmaking pairing-failure spike · notification failure-rate spike ·
`refresh_token_reuse_detected` rate above zero *after* P1-1 is fixed ·
disk/volume threshold.

Deliberately **not** alerted: individual task retries, single 4xx responses,
normal queue fluctuation.

---

## 12. Task mapping

Every non-PASS finding has an owner. No orphans.

| Task | Findings |
| --- | --- |
| **A64-028.2** Auth, session & security | P0-1, P1-1, P2-4 |
| **A64-028.3** PostgreSQL, Redis & data reliability | P0-4, P2-1, P2-3, P3-2 |
| **A64-028.4** Realtime, matchmaking & workers | P1-2, P1-3, P1-6 (behaviour) |
| **A64-028.5** Performance & load testing | P2-5, and §8 in full |
| **A64-028.6** Observability, deployment & operational safety | P0-2, P0-3, P1-4, P1-5, P1-6 (procedure), P1-7, P2-2, P2-6, P3-1, P3-3 |
| **A64-028.7** Final hardening audit & closure | Re-verify every row above |

---

## 13. Test coverage gaps

Production behaviours with no test today, in the order they matter:

1. Concurrent refresh across two connections (**P1-1** — probed by hand this task; must become a committed test in A64-028.2)
2. Two API instances and WebSocket delivery (P1-2)
3. Two schedulers running one job (P1-3)
4. PostgreSQL restart with a warm pool (P2-1)
5. Redis restart and recovery per role
6. Backup restore (P0-4)
7. Worker crash mid-task
8. Any load test at all (P2-5)

---

## Related Documents

- [`deployment.md`](./deployment.md) — what exists, and P-1…P-6 which this register absorbs
- [`architecture.md`](./architecture.md) — AD-02 and AD-03, the two deviations that bound scaling
- [`security.md`](./security.md), [`database.md`](./database.md), [`websocket.md`](./websocket.md)
