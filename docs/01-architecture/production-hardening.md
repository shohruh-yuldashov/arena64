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

> **A64-028.2** closed P0-1, P1-1 and P2-4.
> **A64-028.3** closed **P0-4, P2-1, P2-3 and P3-2**, and added two P2s and
> one P3 it found on the way.
> **A64-028.4** found a **new P0** — the outbox relay had been dead — closed
> it, disproved **P1-2**, reclassified **P1-3**, closed **P3-4** after
> raising it to P1, and resolved the application half of **P1-6**.
> **A64-028.5** partially resolved **P2-5** (capacity), found a **new P1** in
> the realtime bus, and left most of its scenario matrix unrun.
> **A64-028.5A** closed **P1-9** — and found that its "remaining cause" was
> the benchmark rather than the platform — completed the scenario matrix,
> and found **two new P1s** by running it: an outbox deadlock between
> instances and a projection no schema would accept. Both closed in the
> same task.
> **A64-028.6** closed **P0-2, P0-3, P1-4, P1-5, P1-6, P1-7, P2-2, P2-6,
> P3-1, P3-3** — and **P2-9**, whose root cause it found by instrumenting
> the relay and reading what the instrument said.
> **A64-028.6A** replaced the Caddy edge with nginx and **revalidated every
> risk that touched it** — P0-2, P0-3, P1-4, P1-5, P1-6, P2-2, P2-6, P3-1 —
> over real HTTP rather than carrying the previous task's evidence forward.
> It also closed the certificate half of **P3-4**.
> **A64-028.7** is the epic's final audit. It closed **P2-7**, **P2-8**
> (code) and **P3-4**, revalidated every previously closed risk against the
> current code rather than against the reports that closed them, and found
> **two more**: a deployed tier that could rate-limit its entire fleet as one
> client (**P1-12**), and three production images on `:latest` (**P2-10**).
> Both closed in the same task.
> Remaining: **0 P0, 0 P1, 0 P2, 0 P3** in code. What is left is four
> **LIVE DEPLOYMENT GATES**, each requiring infrastructure or credentials
> that do not exist yet — listed in `deployment.md` §9.
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
| **Status** | ~~FAIL~~ → **PASS** (A64-028.6) |
| **Evidence** | No compose file defined a client, so the Caddyfile proxied every path to the API and `/` returned the API's 404. `VITE_PUBLIC_ORIGIN` was set nowhere in the repository |
| **Action taken** | `apps/web/Dockerfile` and `apps/admin/Dockerfile` build to `scratch` images whose only content is `dist/`, copied into a volume the edge serves — not a second web server behind Caddy, which would be a second place for cache headers and a second place to forget a security header. `infrastructure/production/compose.yml` defines the whole topology; the origin is set once from `ARENA64_DOMAIN` |
| **Verified** | A real build with `VITE_PUBLIC_ORIGIN=https://arena64.gg`: `<link rel="canonical" href="https://arena64.gg/">`, `og:url` and the absolute `og:image` the same, `sitemap.xml` listing `https://arena64.gg/`, and `Sitemap: https://arena64.gg/sitemap.xml` in `robots.txt`. The only `localhost` anywhere in the output is prose inside an HTML comment explaining that no host is named |
| **The fail-safe is now a gate** | `generate-seo.mjs` writes a `robots.txt` that blocks everything when the origin is unset — correct, and invisible. The image refuses to build with the origin empty, non-`https`, or naming a development host, so a preview build cannot become a deployment |
| **Owner** | A64-028.6 — **done** |

### P0-3 — No production edge configuration

| | |
| --- | --- |
| **Area** | SEO / Edge / Security headers |
| **Status** | ~~FAIL~~ → **PASS** (A64-028.6) |
| **Evidence** | `infrastructure/staging/Caddyfile` set `encode` and two `reverse_proxy` rules and nothing else |
| **Action taken** | `infrastructure/production/Caddyfile`: HSTS, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, a CSP written against what this application loads — two SHA-256 hashes for the blocking theme script rather than `'unsafe-inline'`, `frame-ancestors 'none'`, no `unsafe-eval` — `X-Robots-Tag: noindex` on every private path, a genuine 404 for unknown routes, and `/metrics` and `/health/drain` refused from outside |
| **Revalidated on nginx — A64-028.6A** | Every assertion below was re-run through the nginx edge rather than carried forward. Headers present on `/` **and on the 404** (all four). `X-Robots-Tag: noindex, nofollow` on `/login`, `/register`, `/settings/privacy`, `/games/*`, `/players/*`, `/tournaments/*`, `/notifications`, `/friends/requests`; **absent on `/`**. `/gmaes/abc`, `/settings/nope`, `/friends/nope`, `/assets/missing.js` → **404** with the branded page; `/`, `/login`, `/games/<id>`, `/tournaments/<id>`, `/settings/privacy`, `/friends/requests` → **200** with the shell. `/metrics` and `/health/drain` → 404 at the edge, 200 direct with the token |
| **A defect only the live check could find** | The first version wrote `handle @private { import no_index }`. `handle` is mutually exclusive routing, so every private route — `/login` included — matched, set a header, terminated the chain and returned an **empty 200**. The application was completely broken and every static policy assertion still passed, because the list of paths was right and the routing semantics were not |
| **The nginx equivalent, and why it is worse** | `add_header` is **not inherited** into a location that declares one of its own, so a block adding a `Cache-Control` silently serves no security headers at all. It has no Caddy counterpart, and it is checked two ways: `TestHeaderInheritance` refuses a location that sets a header without including the snippet, and the 404 is asserted over real HTTP to carry all four |
| **Regression cover** | `apps/api/tests/unit/test_edge_policy.py`, rewritten for nginx — 46 tests. Twelve mutations run, **twelve caught**, including the two the first version of the suite missed: a catch-all `try_files $uri /index.html` making every URL a page, and `Alt-Svc` advertising HTTP/3 with no QUIC listener |
| **Residual** | `/games/*`, `/players/*` and `/tournaments/*` are prefix matchers because their next segment is an identifier, so `/games/<nonsense>` is a 200 the application resolves to its own not-found view. Stated in `deployment.md` §7.5 |
| **Owner** | A64-028.6 — **done** |

### P0-4 — No backups and no tested restore

> **RESOLVED — A64-028.3.** `python -m app.operator.backup` takes, verifies
> and restores a `pg_dump -Fc` backup, and a drill against real PostgreSQL
> restores a seeded database into a clean one and counts every row back
> (`tests/contract/test_backup_restore.py`). A partial backup cannot look
> finished; a checksum mismatch is refused; the password never reaches
> `argv` or a log. **Off-host storage and scheduling remain A64-028.6's** —
> the command is ready and tested, and where it runs and where its output
> goes are deployment decisions. PITR is deliberately deferred with the
> residual risk stated (24 hours). See
> `docs/05-operations/backup-restore.md`.

| | |
| --- | --- |
| **Area** | Data reliability |
| **Status** | ~~FAIL~~ → **PASS** (mechanism) · off-host wiring → A64-028.6 |
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

> **DISPROVED — A64-028.4.** The finding came from `bus.py`'s header, which
> still quotes A64-016.3. The transport was built by A64-016.5
> (`RedisStreamGatewayBus`) and connected by A64-016.8
> (`GatewayForwarder`); what was missing was any test of it end to end,
> which is why it was possible to believe it was absent. Measured with two
> uvicorn processes, distinct node ids, one PostgreSQL and one Redis:
> delivery works in **both** directions with **zero** duplicates, and the
> durable move log agrees. See `docs/05-operations/data-reliability.md` §11.
>
> The second stale header in two tasks — A64-028.3 found the same in
> `live_match_store.py`. Both described a platform that no longer existed
> and both were scarier than reality.

| | |
| --- | --- |
| **Area** | Realtime |
| **Status** | ~~FAIL~~ → **PASS** — the claim was wrong |
| **Evidence** | `app/gateway/bus.py`: a port, an envelope and an **in-process adapter**. The production adapter `RedisStreamGatewayBus` is named in the docstring as future work and does not exist. Quoted there from A64-016.3: "a deployment running more than one gateway node today has silently undelivered frames … single-node is the only supported topology". |
| **Failure mode** | With two instances, a move made on node A never reaches an opponent held by node B. No error is raised on either side. |
| **Impact** | Games freeze for one player with no visible cause. Silent, so it would reach users before it reached an operator. |
| **Likelihood** | Certain if a second instance is ever started; zero today. |
| **Action** | Build the Redis-stream bus adapter, or state single-instance as a hard operational constraint and enforce it in the deployment definition. |
| **Owner** | **A64-028.4** |
| **Blocker?** | No — provided the deployment is pinned to one instance and that pin is enforced |

### P1-3 — Schedulers have no singleton guard

> **RECLASSIFIED — A64-028.4.** The observation is true and the conclusion
> does not follow. This platform does not coordinate schedulers; it
> **claims work durably**, which is the stronger design: `FOR UPDATE SKIP
> LOCKED` for the relay, the pairing sweep, queue and challenge expiry, and
> an atomic Lua claim for clock deadlines. Two runners share work rather
> than repeating it.
>
> Two tasks *must* run on every instance — the gateway forwarder drains that
> node's own mailbox and the metrics flush drains that process's counters —
> so a global scheduler leader would have broken realtime on every node but
> one. Measured with three concurrent workers: every event and every
> deadline claimed exactly once, none twice, none missed.

| | |
| --- | --- |
| **Area** | Workers / deployment |
| **Status** | ~~RISK~~ → **PASS** — by claim, not by lock |
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
| **Status** | ~~FAIL~~ → **PASS** (A64-028.6) |
| **Evidence** | No `/metrics`, no exporter, no dashboard, no alert rule anywhere in the repository. The audit sharpened it: of 41 metrics, **none was about an HTTP request**, and `uvicorn.access` is pinned to `WARNING`, so there was no request log either |
| **Action taken** | `PrometheusMetrics` as the second implementation of the port the design always described; `/metrics` guarded by a bearer token and by the edge; HTTP request/latency/in-flight metrics; nine outbox metrics; three rate-limiter metrics; five gauges read at scrape time; four version-controlled dashboards; 15 alert rules |
| **Verified** | `/metrics` returns 401 without the token and the exposition with it; `promtool check rules` accepts all fifteen; the exporter's own `arena64_metrics_dropped_total` stayed at zero through a 500-game ladder |
| **Regression cover** | `test_observability_config.py` refuses a dashboard panel or an alert rule naming a series nothing emits, and a runbook link that does not resolve — it caught one on the first run |
| **What is still unobserved** | Host CPU/memory/disk, certificate expiry, email-provider health, tracing. Each is listed with its reason in `observability.md` §10 |
| **Owner** | A64-028.6 — **done** |

### P1-5 — Readiness cannot fail

| | |
| --- | --- |
| **Area** | Health |
| **Status** | ~~RISK~~ → **PASS** (A64-028.6) |
| **Evidence** | `/health/ready` returned HTTP **200** with `status: "degraded"` when PostgreSQL and Redis were both unreachable. A load balancer reads the status line; nothing in a fleet parses the body |
| **Action taken** | 503 for an unreachable required dependency or a draining instance, with the diagnostic body unchanged. Liveness deliberately unchanged — an orchestrator restarts what fails liveness, and a database outage must not become a fleet-wide restart storm |
| **Verified against real failures** | PostgreSQL unreachable: liveness **200**, readiness **503**, body `postgres: false`. Redis unreachable: liveness **200**, readiness **503**, all five roles `false`. Draining: liveness **200**, readiness **503**, `draining: true` with `postgres: true` — the body keeps the two apart |
| **Regression cover** | `tests/unit/test_health.py`. Two mutations caught: readiness answering 200 unconditionally, and draining no longer failing readiness |
| **Owner** | A64-028.6 — **done** |

### P1-6 — Every deploy drops every live game

> **CLOSED — application side A64-028.4, deployment side A64-028.6.**

| | |
| --- | --- |
| **Area** | Deployment / realtime |
| **Status** | ~~OPEN~~ → **PASS** |
| **What A64-028.4 established** | `SIGTERM` closes live sockets with `1012 service restart` — uvicorn's own shutdown, before the lifespan teardown. A game survives a process restart; a socket does not, and a socket surviving one was never the requirement. An application-level drain was written and removed because it found zero sockets: uvicorn closes them first |
| **What was left open** | Readiness routing, stop timeouts and a rolling procedure — the part that decides whether the balancer stops sending work *before* the process is signalled |
| **Action taken** | `POST /health/drain` turns readiness 503 while liveness stays 200. A request rather than a signal handler, because uvicorn closes every socket before the lifespan hears about `SIGTERM` — by then the connections are gone and the balancer has been told nothing. This is a `preStop` hook wherever one exists. `stop_grace_period` 30s for `api`, 60s for `worker` |
| **Verified end to end** | Two instances, one live game, one drained and terminated mid-game: readiness 200 → **503** on drain, liveness **200** throughout, the other instance untouched, socket closed with **1012**, client reconnected to the survivor in **265 ms**, `game.resume` answered with a snapshot, the next legal move **accepted**, durable plies 2 → 3, **0 duplicates** |
| **Residual** | An unplanned exit — a crash, an OOM kill — skips the drain. That is correct: there is nothing to co-ordinate with a process that is already gone, and the durable move log plus `game.resume` is what makes it survivable |
| **Owner** | A64-028.4 (behaviour), A64-028.6 (procedure) — **done** |

### P1-7 — A Redis outage silently removes rate limiting

| | |
| --- | --- |
| **Area** | Abuse |
| **Status** | ~~RISK~~ → **MITIGATED** (A64-028.6) |
| **Evidence** | `fail_open = True` with a written rationale that is correct. The finding was never the trade-off — it was that while Redis is unreachable every limit on the platform is bypassed and nothing signals it except one log line |
| **Decision, restated** | **Fail-open is kept.** Failing closed converts a limiter outage into a total authentication outage: login, registration, password reset and OTP all stop. What remains in place meanwhile is Argon2id, `users.locked_until` and 256-bit reset tokens. The abuse window is exactly as long as the outage |
| **Action taken** | `rate_limit.unavailable_total{outcome}` separates failing open from failing closed — opposite risks with opposite responses. `rate_limit.decisions_total{rule,outcome}` gives HTTP 429s their first counter; until now a refusal was visible only on the WebSocket paths. `RateLimiterUnavailable` pages, and the runbook says to watch registration volume while it is down |
| **Verified against a real outage** | An instance pointed at an unreachable Redis: readiness **503**, login answered **401** rather than 503 (fail-open working), `rate_limit_unavailable` logged at ERROR, and `arena64_rate_limit_unavailable_total{outcome="failed_open"} 1.0` on the next scrape |
| **Residual risk** | An abuser active during a Redis outage is unimpeded by rate limits. That is the accepted trade; what has changed is that an operator now knows the window is open while it is open |
| **Owner** | A64-028.6 — **done** |

### P2 findings

| ID | Area | Status | Finding | Evidence | Owner |
| --- | --- | --- | --- | --- | --- |
| P2-1 | PostgreSQL | ~~RISK~~ → **PASS** | **Resolved — A64-028.3.** Measured rather than inferred: killing a pooled backend and making three requests gives `1:InterfaceError 2:ok 3:ok` without `pool_pre_ping` and `1:ok 2:ok 3:ok` with it — up to one failed request per pooled connection after a database restart. `pool_pre_ping` is now on, and an explicit 10s `connect_timeout` replaces asyncpg's 60s default. `pool_recycle` was evaluated and **not** added: pre-ping covers the failure it would address and there is no idle-timeout middlebox between the app and the server | `tests/contract/test_pool_resilience.py` | A64-028.3 — **done** |
| P2-2 | Logging | ~~RISK~~ → **PASS** | **Resolved — A64-028.6.** `RedactingFilter` sits on the logging handler, so it applies whichever formatter the environment chose — a redaction that only ran for JSON would leave every developer's machine unprotected, and that is where a token is most likely to be printed by hand. Matches by field **name**, not by sniffing values: a value sniffer is a regex arms race that fails open on the first credential shaped differently. Keeps the identifiers an incident is reconstructed from, with `token_family` explicitly allowed. 27 tests; removing the filter fails 16 of them. Original finding: no redaction filter at the logging boundary. `_JsonFormatter` emits whatever `extra={…}` a call site passed. `CLAUDE.md` §8.3 requires redaction *at the boundary* "so redaction cannot be forgotten"; today it is call-site discipline. That discipline is currently good (`session_service` logs identifiers only, never the token, user agent or IP) | `app/common/logging.py` | **A64-028.6** |
| P2-3 | Migrations | ~~RISK~~ → **PASS** | **Resolved — A64-028.3, and the finding was understated.** Not three migrations but **thirty-nine**: `op.create_index` cannot be concurrent inside Alembic's transaction, so every index in the schema is built in a lock. All thirty-nine run at `t=0` — Arena64 has not launched and a production database is built from empty — so none is unsafe. Eleven index a table an *earlier* migration created, which is the shape that would matter after launch; they are declared in `tests/unit/test_migration_policy.py`, which fails when a new one appears undeclared. Live-migration rules are in `docs/05-operations/data-reliability.md` §5 | `tests/unit/test_migration_policy.py` | A64-028.3 — **done** |
| P2-4 | Notifications | ~~RISK~~ → **PASS** | **Resolved — A64-028.2.** The audit sharpened the finding: absent is a supported decision and a malformed pair already raised, but a **half** pair was silently treated as "push not configured" — an operator who set one key got a tier that refused every subscription and said nothing. `PushSettings` now refuses a half pair at startup, naming the missing variable and never the value | `settings.py`, `tests/unit/test_push_config.py` | A64-028.2 — **done** |
| P2-5 | Capacity | ~~UNKNOWN~~ → **PASS (this environment only)** | **A64-028.5, completed by A64-028.5A.** The matrix is now run: refresh, matchmaking 100→1 000, live games 10→500, idle sockets 100→2 000, reconnect storm, cross-instance realtime, the analytics pipeline, a mixed workload and a 35-minute soak. **Zero unexpected failures at any level of any scenario.** The constraint is one Python process on one core (99–100 % at every saturated level) while the pool never waited once (`db_waiting_peak` 0, peak 32 of 100) and Redis peaked at 16 450 ops/s — so the first lever is worker processes per host, not a bigger database. Two things are labelled rather than claimed: refresh and login are **RATE-LIMIT BOUNDED**, and every per-IP figure was taken under the `development` profile, which is **20×** production's. The live-game plateau was tested against the generator rather than assumed: two generator processes bought 4.8 %, so the numbers are server-bound. **Nothing here is a production capacity claim** — client and server shared eleven cores. See `docs/05-operations/performance.md` | `tests/load/`, `performance.md` | A64-028.5A — **done**; production hardware **A64-029** |
| P2-6 | Docs | ~~RISK~~ → **PASS** | **Resolved — A64-028.6.** The comment was wrong and the code was right, which is the direction that matters: HEALTHCHECK feeds a **restart** decision, and a probe failing on an unreachable database would restart every container in the fleet during a database incident. The comment now says liveness, says why, and points at readiness for the other question. Original finding: the comment said the endpoint "reports the database and Redis". It calls `/api/v1/health`, which is liveness and reports neither. The behaviour is right; the comment describes `/health/ready` | `apps/api/Dockerfile` | **A64-028.6** |

### Found by A64-028.5

### P1-9 — A quiet instance stops receiving realtime frames

| | |
| --- | --- |
| **Area** | Realtime |
| **Status** | ~~PARTIAL~~ → **RESOLVED** (A64-028.5A) |
| **Evidence** | `gwbus:v1:<node>` carries a TTL refreshed only on publish. A node with no cross-node traffic for that long loses the key **and its consumer group**, while `_ensure_group` has already cached the group as created — so every `XREADGROUP` fails `NOGROUP` for ever, until restart. One instance's log held **4812** `gateway_stream_consume_failed` warnings before a benchmark noticed |
| **Failure mode** | Silent. The publisher succeeds, the frame is trimmed, and the opponent's moves simply stop arriving |
| **Impact** | Cross-instance realtime degrades to nothing on an idle node. Durable state is unaffected: PostgreSQL is authoritative and `game.resume` resyncs |
| **Action taken** | `consume` forgets the cached group on `NOGROUP` and recreates it **at `0`, not `$`** — a group recreated at `$` skips everything already in the stream, which would have been a second silent loss surviving the first fix |
| **The "remaining cause" was the benchmark, not the platform** | A64-028.5 reported 20 of 30 frames undelivered after the fix and concluded another cause existed. It did not. The harness replayed a fixed six-ply opening past the point where it stayed legal; from ply three the engine answered `game.move.rejected`, no broadcast was ever published, and the absent frame was counted as transport loss. Traced per ply: `xlen 0→1 watcher=GOT` for plies 1–2, `ack=game.move.rejected` with `xlen` unchanged for plies 3–6. The harness now generates moves with the same `MoveGenerator`/`MoveApplier` the server judges them with, and records a rejection as `HarnessIllegalMove` — never as a missing frame |
| **Verified** | Two real uvicorn processes, distinct node ids, one Redis: **220 cross-instance frames, 112 A→B and 108 B→A, 0 missing and 0 duplicated**, p50 103 ms / p95 182 ms / p99 183 ms. Repeated after deleting both mailboxes to force the recreation path: **220 frames, 0 missing, 0 duplicated**. The fix was also observed firing in ordinary operation (`gateway_stream_group_recreated node=node-1`) |
| **Regression cover** | `tests/contract/test_gateway_bus_lifecycle.py`, 5 tests, against a real Redis. Three mutations each caught by 3 failures: never recreating on `NOGROUP`; never clearing the stale cache; recreating at `$` instead of `0` |
| **Owner** | **A64-028.6** for the alert that would have caught it in production |

### Found by A64-028.5A

### P1-10 — Two relays deadlocked over the rows they had just delivered

> **RESOLVED in the same task.** Found by measurement, not review: it needs
> two instances and enough load for claims to overlap.

| | |
| --- | --- |
| **Area** | Background work |
| **Status** | ~~FAIL~~ → **PASS** |
| **Evidence** | Two instances under a 1 000-user matchmaking burst logged `DeadlockDetectedError` three times each. `platform.outbox` held **809** `users.presence_online`/`presence_offline` events at exactly five attempts, abandoned |
| **Failure mode** | A tick records itself in two shapes — successes as one batched `UPDATE ... WHERE id IN (...)`, failures one row at a time in claim order — so one transaction takes its locks partly as a set and partly in its own order. Two relays with overlapping claims take the same locks in opposite orders; PostgreSQL breaks the cycle by killing one. The killed side was recording deliveries it had **already made**, so each kill counted as another failed attempt against rows that had succeeded, and five of those retired the event permanently. Overlapping claims are ordinary: a lease lapses while a slow handler is still running |
| **Impact** | Silent, permanent loss of delivered events, worsening with instance count — the opposite of what horizontal scaling is for |
| **Action taken** | `lock_in_order` takes every lock the tick needs first, in one statement, in ascending id order, so the second relay waits instead of dying. Sorting the two write groups separately would not have sufficed: it is the order *between* a success and a failure that differed |
| **Verified** | The same ladder on the fixed build: **1 850 matchmaking joins and 500 concurrent live games, `outbox_exhausted` unchanged at 2 880** (all pre-existing), and the 1 807 new retryable rows drained to 0 within 30 s |
| **Regression cover** | `tests/contract/test_outbox_repository.py` against real PostgreSQL, with the interleaving forced by explicit events — an earlier version let both coroutines run to commit in turn, passed against the unfixed code and proved nothing. Removing `lock_in_order` now raises `DeadlockDetectedError` exactly as production did |

### P2-9 — Fifty events spent their whole retry budget without recording a reason

| | |
| --- | --- |
| **Area** | Background work |
| **Status** | ~~OPEN~~ → **PASS** (A64-028.6) |
| **Original evidence** | During A64-028.5A's 35-minute soak, `outbox_exhausted` rose by exactly 50. All carried `attempt_count = 5` with **`last_error` and `next_attempt_at` NULL**, and across both instances the relay logged 4 866 ticks and **not one reported a failure** |
| **How it was found** | Instrumentation written for it, then read. `outbox.incomplete_ticks_total` counts a tick that claimed entries, reported no failures and published fewer than it claimed; `outbox.unrecorded_attempts_total{observation}` classifies what each claimed row was already carrying. A64-028.5A's 500-game ladder on the unfixed build: **3 incomplete ticks, 150 attempts spent recording nothing, claimed 2 900 against published 2 750** — a difference of exactly 150 — and 152 rows that reached `published` only on their second or later attempt with no error ever recorded |
| **Root cause** | `claim` set `claimed_at` and `claimed_by` and incremented `attempt_count`, and **did not touch `next_attempt_at`**. The due predicate is `next_attempt_at IS NULL OR <= now`, so a just-claimed row still satisfied it. `SKIP LOCKED` keeps two relays apart for the length of one *statement* and the claim commits immediately afterwards — deliberately, so the claim is visible to everybody. A second relay polling a second later claimed the same batch; both delivered, one published, and the other's `mark_published` matched nothing because the row was already published, so it recorded no outcome and spent an attempt for nothing. Five of those retires an event that never failed |
| **Action taken** | The claim writes `next_attempt_at = now + lease` — the visibility timeout this design already had the field for. `mark_published` and `mark_failed` both overwrite it, so the lease governs only a tick that reached neither. Sixty seconds, because it must exceed the slowest tick and the default consumer budget is thirty; too long costs a delayed retry after a crash, too short is the defect, and that asymmetry chooses the default. `lease` is a **required** argument on the port: the contract is not "disjoint within a call", which is what `SKIP LOCKED` gives and what was never enough |
| **Verified** | The same ladder on the fixed build: **`claimed == published` on both instances** — 2 837 and 2 775 — with **no incomplete ticks and no unrecorded attempts at all** |
| **Regression cover** | Two contract tests against real PostgreSQL. Both mutations caught: removing the lease lets a second relay reclaim a batch another has taken; making it never expire strands a batch a crashed relay was holding |
| **Owner** | A64-028.6 — **done** |

### P1-11 — Every queue join was projected into an event the schema refused

> **RESOLVED in the same task.**

| | |
| --- | --- |
| **Area** | Analytics |
| **Status** | ~~FAIL~~ → **PASS** |
| **Evidence** | `QueueJoined` required `speed_class`; `_queue_ticket_enqueued` has never supplied one, because a ticket carries a variant and a queue type and not a time control. Its own docstring said the schema made the field optional. It did not. **1 850 poisoned rows** surfaced in one load run, `last_error=ValidationError`, all at five attempts |
| **Failure mode** | Every queue join in every environment produced an outbox entry that failed validation five times and was abandoned. `finalise` validates a projection's output — correctly — but nothing checked that a projection *could* satisfy its schema at all, so the only place the mismatch appeared was at the moment the event was thrown away |
| **Impact** | The third stage of funnel F-B has been empty since it was written, in every environment. M7b's denominator is affected. The rejected rows accumulate permanently |
| **Action taken** | `speed_class` is now optional, matching `QueueLeft` beside it — which already had the correct shape, and whose consistency is what let the mismatch survive review. The field stays declared rather than deleted: matchmaking owes it additively (A64-027.1 §49) |
| **Verified** | Same ladder, fixed build: 1 850 joins, zero new exhausted rows, backlog drained within 30 s |
| **Regression cover** | `tests/unit/test_analytics_contracts.py` asserts a queue ticket projects into an event that `finalise` accepts. Restoring the required field fails it |

### Found by A64-028.4

### P0-5 — The outbox relay was dead, and burned every event's retry budget

> **RESOLVED in the same task.** Recorded because it was live in every
> environment running the code and nothing had noticed for weeks.

| | |
| --- | --- |
| **Area** | Background work |
| **Status** | ~~FAIL~~ → **PASS** |
| **Evidence** | `AnalyticsRetentionTask` — a `TaskHandler` — was appended to `build_outbox_worker`'s `EventHandler` list. The relay called `handles()` on it on every tick, inside the comprehension that builds its work list, so the `AttributeError` escaped before any consumer ran and failed the **whole pass**. A64-028.1 logged it as a side observation |
| **Failure mode** | Nothing `platform.outbox` carries was delivered: notifications, rating application, analytics projections, tournament and social events. And `_claim` commits before `_dispatch` runs, so every failing tick incremented `attempt_count` and committed — within five ticks every claimable event hit `max_attempts` and was **permanently abandoned**. The development database holds 898 such events, all at exactly five attempts |
| **Impact** | Total loss of event delivery, plus destruction of the retry path that would have recovered it |
| **Action taken** | The registration moved to the dispatcher, where it also fixed a second silent failure: `analytics_prune_request` was scheduled with nothing answering to it, so the 400-day retention had never run. Both composition points now fail fast — `OutboxWorker` refuses a consumer that cannot answer the protocol, `PeriodicTaskScheduler` refuses a request its dispatcher cannot route. Two layers of type suppression (`list[TaskHandler \| object]` and a `# type: ignore[arg-type]`) removed |
| **Verified** | Two running instances: `outbox_tick_failed` from 25 occurrences to 0 |

### Found by A64-028.3

| ID | Area | Status | Finding | Owner |
| --- | --- | --- | --- | --- |
| P2-7 | Retention | ~~RISK~~ → **PASS** | **Resolved — A64-028.7.** The finding named one table; the audit found **four**. `notification` keeps 90 days (how far back a player can scroll — a product decision, so a setting), both delivery tables keep 30 (they answer "why did this person not get their email", asked within days, and carry the provider's message id), and `push_subscription` keeps 30 days **after revocation** — a live one has no horizon, because a player away for a year still expects their notifications. `notification_preference` and `notification_broadcast` are deliberately untouched: one is current state, the other an operator record. **There are no foreign keys between these tables**, so the policy refuses to construct unless the delivery horizon is at or inside the notification horizon and the service deletes deliveries first — an orphan delivery row is one nothing else would ever remove. Bounded batches, `SKIP LOCKED`, indexed predicates. Five mutations caught, including a composite-key delete that would have removed every device's row for a notification | `test_notification_retention.py`, `test_notification_retention_repository.py` | A64-028.7 — **done** |
| P2-8 | Backup | ~~RISK~~ → **PASS (code)** · off-host **LIVE DEPLOYMENT GATE** | **Resolved — A64-028.7, and the two halves close differently.** *Plaintext:* AES-256-GCM, streamed in 4 MiB chunks, and the plaintext **never touches the disk** — `pg_dump` writes to stdout and the bytes are sealed as they stream past, because a dump-then-encrypt would leave every account on disk for the length of the encryption and leave them there for good if the process died between. Authenticated, so a wrong key and a corrupted archive both fail rather than restoring rubbish. *Off-host:* an S3-compatible uploader over the existing `httpx`, no new dependency, signed SigV4 with the checksum `create` already computes. The production tier refuses to start without a key, without a target, or with a half-configured one. **What is proven and what is not:** the pipeline, the encryption, the refusals and a full restore drill are proven; the upload is proven against a **MinIO on this laptop**, which is not off-host storage. A real provider bucket is a live deployment gate | `test_backup_crypto.py`, `test_backup_offsite.py`, `test_backup_restore.py`, drill | A64-028.7 — **code done** |
| ~~P3-4~~ **P1-8** | Redis | ~~RISK~~ → **PASS** | **Reclassified and resolved — A64-028.4.** Filed as a P3 about unbounded growth; the growth was the small half. The set has no durable backing, so a Redis loss took every active game's deadline with it — and `ClockAdjudicationService` has said since A64-018 that a lost deadline means "the match stops flagging … for a game nobody is moving in it stays open". A player who walks away never lost on time. `ClockDeadlineReconciliationTask` re-derives every active match's deadline from `clock_turn_started_at` and the side-to-move's remaining milliseconds — durable columns the move committed — so the queue is a cache of a derivation and a loss is a rebuild. Idempotent, so it is safe on every instance | A64-028.4 — **done** |

### Found by A64-028.7

### P1-12 — A deployed tier could rate-limit its whole fleet as one client

> **RESOLVED in the same task.** Found by auditing what nothing checked.

| | |
| --- | --- |
| **Area** | Abuse / configuration |
| **Status** | ~~FAIL~~ → **PASS** |
| **Evidence** | `RATE_LIMIT_TRUSTED_PROXY_COUNT` defaults to **0** and had no production guard. Every production topology in this repository puts nginx in front of the API, and the compose file sets it to 1 — but nothing refused a deployment that did not |
| **Failure mode** | With zero, `client_ip` falls back to the socket peer, which is the proxy. Every request on the platform then shares one rate-limit bucket: the first twenty logins exhaust the per-IP budget and **every other player is refused**. It looks exactly like the limiter working |
| **Impact** | A silent, total lockout of new sign-ins, from a variable nobody set |
| **Action taken** | A production-like tier refuses to start with a count below one, and the message explains both wrong directions. Only "too low" is checkable from configuration — the real hop count is a fact about the deployment, stated in `deployment.md` §8.8 beside the nginx config that produces it |
| **Regression cover** | `tests/unit/test_settings.py::TestProductionProxyTrust`; removing the guard fails it |

### P2-10 — Three production images were on `:latest`

> **RESOLVED in the same task.**

| | |
| --- | --- |
| **Area** | Supply chain |
| **Status** | ~~FAIL~~ → **PASS** |
| **Evidence** | `certbot/certbot`, `minio/minio` and `minio/mc` were `:latest` while every other image in the file used a minor or release tag |
| **Failure mode** | `latest` is not a version: a rebuild silently changes what runs, a rollback cannot name what to return to, and a compromised upstream tag is pulled on the next restart — on the certificate client and the object store, which is where it would matter most |
| **Action taken** | Pinned to `v5.8.0` and the two current MinIO release tags. The API image stays `${ARENA64_TAG}`, which is the one tag that *should* move — it is the release being deployed |

### P3 findings

| ID | Finding | Owner |
| --- | --- | --- |
| P3-1 | ~~`apps/web/.env.example` does not document `VITE_PUBLIC_ORIGIN`~~ → **RESOLVED — A64-028.6.** Documented with what it writes into the output, why it is build-time, and why it must stay unset locally. The production value is set once from `ARENA64_DOMAIN`, and the image refuses to build with a development origin | A64-028.6 — **done** |
| P3-2 | ~~Runtime version ambiguity~~ → **RESOLVED — A64-028.3.** `apps/api/.python-version` pins **3.13**, which `uv` reads for the developer's virtualenv and CI's, and which the image already ran. The suite, ruff, mypy, pyright and import-linter all pass under it. 3.13 rather than 3.14 because the image runs it and every checker already targets it — upgrading a runtime because a newer one exists is not a reason | A64-028.3 — **done** |
| P3-3 | ~~`compose.yml` gives `RESEND_API_KEY` an empty default, implying it is optional~~ → **RESOLVED — A64-028.6.** The production compose requires it, and `EmailSettings` refuses a value that is not a Resend credential: `None` has defined behaviour and fails at boot, while a placeholder builds the real provider, reports the channel available and fails every message one at a time. Original finding: the empty default implied optional. It is not — `ConsoleEmailProvider` refuses to construct in a deployed tier, so the stack fails to boot | **A64-028.6** |
| P3-4 | ~~Host metrics, certificate expiry, email baseline~~ → **RESOLVED — A64-028.7** (certificate expiry closed in A64-028.6A). *Host:* `node_exporter` pinned, no published port, read-only mounts, defaults disabled and seven collectors enabled — seven rules including `DiskWillFillSoon`, which reads the slope rather than the level because a disk at 60% falling steadily is a ticket today and an outage on Sunday, and `HostMetricsMissing`, because every other rule in the group reads that exporter and a disabled alert is indistinguishable from a healthy one. *Email:* the threshold is **not** a percentage and says so — a figure separating "the provider is down" from "several addresses bounced" needs a baseline that does not exist, so the rule fires on any sustained failure, gated on the platform actually sending. Marked TRAFFIC-INSUFFICIENT in the rule itself. `EmailDeliveryStalled` covers what a rate cannot see: a delivery task that stopped produces no failures at all | A64-028.7 — **done** |

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
| PostgreSQL unavailable | Requests fail; readiness returns **503** and the balancer stops routing (A64-028.6); liveness stays 200, so no restart storm | **DEGRADED** |
| Redis unavailable | Rate limiting fails open **and says so** — `rate_limit_unavailable_total{failed_open}` and a page (A64-028.6); readiness returns 503; live match state in the `live` role is unavailable and rebuilds from the durable log | **DEGRADED** |
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
