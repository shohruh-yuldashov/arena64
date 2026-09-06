# Deployment

> **Status:** Draft — describes the staging tier, which exists; production does not
> **Owner:** _Unassigned_
> **Last reviewed:** 2026-09-04

## Purpose

How Arena64 runs outside a developer's machine: what is built, what it is
configured with, and where the deployed shape deviates from
[`architecture.md`](./architecture.md).

## Scope

The staging tier. Production is not defined and nothing here should be read
as describing it — §6 lists what a production definition must decide that
this one deliberately does not.

---

## 1. What exists

| | |
| --- | --- |
| **Artifact** | One image, `apps/api/Dockerfile` — Python 3.13, `uv sync --frozen`, non-root, healthchecked |
| **Topology** | One host, `docker compose` — [`infrastructure/staging/compose.yml`](../../infrastructure/staging/compose.yml) |
| **Edge** | Caddy, which obtains and renews TLS itself |
| **Data** | PostgreSQL 17 and Redis 8 as containers on named volumes |
| **Objects** | MinIO, S3-compatible, one public bucket |
| **Client** | Not deployed — see §6 |

Bring it up:

```bash
cd infrastructure/staging
cp staging.env.example staging.env   # then fill it in
docker compose --env-file staging.env up -d
```

`staging.env` is gitignored and every value in it is the operator's.
`staging.env.example` names each one and says what it is for.

### Where the image comes from

CI publishes it. The `publish` job in `.github/workflows/ci.yml` builds
`apps/api` and pushes to `ghcr.io/<owner>/arena64-api` on a push to `main`,
and it `needs` the three test jobs — so **an image exists only for a commit
that passed every gate**, and a tag in the registry is one somebody can
deploy without asking what state it was in.

Two tags per build, and both earn their place. `latest` is what the compose
file follows by default, so a host can pull without being told a version.
The commit SHA is what makes a deployment reproducible: `latest` means
"whatever was newest", and a bug report needs to name the build it came
from. Pin `ARENA64_TAG` to a SHA when reproducing one.

**The package is private until somebody makes it public.** A GHCR package
inherits nothing from the repository's visibility, so the first pull from a
staging host fails with `denied` until either the package is set to public
in GitHub → Packages, or the host signs in with a token that can read it.
Stated here because the failure is a permission error on a machine at the
end of a deploy, not on the machine that built anything.

### Upgrading

```bash
cd infrastructure/staging
docker compose --env-file staging.env pull
docker compose --env-file staging.env up -d
```

`pull` fetches the new image; `up -d` recreates only what changed, runs the
migration job first and waits for it. **Every live game is dropped** — one
process holds the WebSocket connections and the schedulers alike, which is
§2's AD-02 deviation showing up as an operational fact rather than a
paragraph.

---

## 2. Three deviations from `architecture.md`, each deliberate

These are recorded rather than left to be discovered, per `CLAUDE.md` §3.11.

### AD-02 — three runtime profiles, one process

`architecture.md` AD-02 describes `api`, `gateway` and `worker` as separate
entrypoints over one codebase, with different scaling and different restart
tolerance. **Only one entrypoint exists.** `app_factory` mounts the gateway
router into the same application and starts every background scheduler in
its own lifespan, so the deployed shape is one process doing all three.

The consequence AD-02 itself predicts is live here: the gateway's restart
tolerance is *low* — a restart drops live matches — and it shares a process
with schedulers that are restart-tolerant, so **every deploy drops live
games.** On staging that is acceptable and visible. It is the first thing a
production definition has to fix.

It also bounds scaling: a second replica of this process would run a second
copy of the outbox relay, the pairing sweep and the clock adjudicator.
Several claim work with `SKIP LOCKED` and would be safe; that has never been
tested, so staging runs **one replica**.

### AD-03 — five Redis instances, one instance

AD-03 gives each Redis role its own instance in a deployed tier, sized and
configured for its own workload — `cache` evicts by design, `limits` must
not. Staging runs one instance with five logical databases, which is the
`local` shape.

A staging tier that loses a rate-limit counter to cache pressure is a fair
warning and a cheap one. Five instances on one host would be five processes
pretending to be five machines, which teaches nothing the single instance
does not.

### Storage — MinIO rather than a managed bucket

`LocalStorageProvider` refuses to construct in a deployed tier, correctly:
objects would live on one node's disk, vanish on the next reschedule and be
invisible to a second replica, and all three failures are silent. Staging
therefore runs `S3StorageProvider` (A64-027.1) against MinIO in the same
compose file.

MinIO speaks the same protocol as S3, R2 and B2, so moving to a managed
bucket is four environment variables and no code.

---

## 3. Configuration

Every value is an environment variable; nothing is read from a file in a
deployed tier (`dependency-injection.md` §2.2). `Settings` refuses to start
when a deployed tier is missing one of these:

| Variable | Why it is required rather than defaulted |
| --- | --- |
| `POSTGRES_DSN` | The local default points at a developer's port |
| `REDIS_*_URL` (five) | Same |
| `PUBLIC_APP_URL` | Every email link is built from it; the default sends players to `localhost` |
| `JWT_SECRET_KEY` | The development key is in the source — anyone holding this repository could mint a token for any account |
| `EMAIL_VERIFICATION_OTP_SECRET` | Same |
| `BROWSER_SESSION_TRUSTED_ORIGINS` | The server-side half of the CSRF defence for the refresh cookie; empty leaves only the browser's SameSite guarantee |
| `STORAGE_S3_*` (four) | `STORAGE_PROVIDER=s3` without an endpoint, a bucket or credentials fails at startup rather than at the first upload |

**The first run of this stack failed on `BROWSER_SESSION_TRUSTED_ORIGINS`**,
which is the guard working exactly as designed and the reason the table
above exists: until a tier was actually built, that list lived only in a
validator.

---

## 4. Ordering

`migrate` runs `alembic upgrade head` and exits; `api` waits for it with
`service_completed_successfully`. A migration racing an application that has
already begun serving is how a request meets half a schema.

`minio-provision` creates the bucket and sets `anonymous download` on it,
then exits. Avatars are public objects — `core/storage.py` says so and
`get_public_url` composes an unsigned URL on that basis — so a bucket that is
not actually readable renders every avatar as broken.

---

## 5. What has been verified

Locally, against this compose file:

- the image boots and its healthcheck passes;
- migrations apply to an empty database;
- logs are structured JSON with correlation fields, which is the `staging`
  branch of `app.common.logging` doing its job;
- `S3StorageProvider`'s four operations round-trip against real MinIO, and
  the stored object is publicly readable with its `content-type` intact.

The last of those is now a contract test —
`tests/contract/test_s3_storage.py` — with MinIO as a CI service, so the
signature is checked against a real implementation on every pull request
rather than once by hand.

**Not verified:** anything requiring a public hostname. TLS issuance, the
WebSocket through Caddy, and email links all need a domain that resolves,
and this stack has only ever run on `localhost`.

---

## 6. What a production definition must decide, and this one does not

Each row below is now carried, with a severity and an owning task, in
[`production-hardening.md`](./production-hardening.md) — A64-028.1's risk
register. This table stays as the record of where each question was first
asked.


| # | Question | Why staging does not answer it |
| --- | --- | --- |
| P-1 | How to deploy without dropping live games | Needs AD-02's `gateway` split first |
| P-2 | Where secrets come from | Staging reads a file on the host; a real tier wants a secret manager (`dependency-injection.md` §2.4) |
| P-3 | Backups and restore | A named volume is not a backup, and an untested restore is not one either |
| P-4 | Where `apps/web` and `apps/admin` are served from | Both are static bundles; staging serves neither |
| P-5 | Managed data services | One host means one failure domain for the database, Redis and the object store at once |
| P-6 | Observability | `docs/00-overview/roadmap.md` U-6; there is no error tracking, no alerting and no dashboard |


---

## 7. Production — A64-028.6

> §1–§6 above describe the **staging** definition and the gaps it left.
> This section is the production one, and it closes P0-2, P0-3, P1-5, P1-6,
> P2-6 and P3-1. Everything here is in `infrastructure/production/`.

### 7.1 Topology

| Component | Container | Port | Public | State | Liveness | Readiness | Scaling | Shutdown |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| Edge | `caddy` | 80, 443 | **yes — the only one** | certificates | — | — | 1 | signal |
| API | `api` | 8000 | no | stateless | `/health` | `/health/ready` | **N** | drain, then signal |
| Scheduled work | `worker` | 8000 | no | stateless | `/health` | `/health/ready` | **exactly 1** | signal, 60s grace |
| Schema | `migrate` | — | no | — | — | — | one-shot | exits |
| Web bundle | `web` | — | no | — | — | — | one-shot | exits |
| Admin bundle | `admin` | — | no | — | — | — | one-shot | exits |
| Database | `postgres` | 5432 | no | **durable** | `pg_isready` | — | 1 | signal |
| Cache and bus | `redis` | 6379 | no | rebuildable | `redis-cli ping` | — | 1 | signal |
| Object storage | `minio` | 9000 | no | **durable** | `mc ready` | — | 1 | signal |
| Backup | `backup` | — | no | **durable volume** | — | — | 1 | signal |

Only `caddy` publishes ports. Everything else is reachable on the compose
network and nowhere else.

### 7.2 The api/worker split, and why the flags are written out

A64-028.1's audit found the trap: every scheduled job is gated by a
per-process boolean that **defaults to `true`**, so N replicas of one image
run N copies of every sweep. The handlers are individually safe under
concurrency — they claim with `FOR UPDATE SKIP LOCKED` and count the races
they lose — but safe is not intended, and N pairing scanners a second is
load nobody asked for.

So both services set every flag explicitly. The shape is visible in the
compose file rather than inferred from defaults, and a future default change
cannot silently alter it.

**One task stays on everywhere: the gateway forwarder.** It drains *this
node's* cross-instance mailbox, so a node that does not run it receives no
frames from any other node. A64-028.4 established that this is one of the
two tasks a leader election would break.

**Exactly one worker is a deliberate single point of failure** for scheduled
work. Scheduled work is recoverable — the outbox retains its rows and a
restarted worker drains the backlog — and two workers would double every
sweep for no benefit the claim semantics do not already provide.

### 7.3 Startup order

```
config validation      Settings refuses a deployed tier with a local default,
                       a development signing key, an unguarded operator
                       surface or a placeholder Resend key. Refusing to start
                       is a rolled-back deploy; starting is an outage.
        ↓
dependencies           postgres and redis healthchecks
        ↓
migrations             `migrate` runs alembic to completion and exits.
                       A replica never migrates a database other replicas
                       are already using.
        ↓
application            api and worker start; readiness is 503 until both
                       PostgreSQL and Redis answer
        ↓
traffic                the edge routes to whatever readiness says yes to
```

### 7.4 A deploy, and what it costs a live game

```
POST /health/drain   →  readiness 503, liveness still 200
                     →  the balancer stops routing new work
SIGTERM              →  uvicorn closes sockets with 1012, lifespan tears down
                     →  the client reconnects to a surviving instance
                     →  game.resume replays from the durable move log
```

Draining is a **request, not a signal handler**, and A64-028.4 found out
why the obvious version cannot work: uvicorn closes every socket before the
lifespan hears about `SIGTERM`, so by the time application code could flip a
flag the connections are gone and the balancer has not been told anything.
`POST /health/drain` is the step before, which is a `preStop` hook wherever
one exists.

Liveness deliberately does **not** follow readiness down. An orchestrator
restarts what fails liveness, and restarting a draining instance is the
orchestrator undoing the deploy.

**Measured end to end**, two instances, one live game (A64-028.6 §29):

| | |
| --- | --- |
| Readiness before drain / after | 200 / **503** |
| Liveness while draining | **200** |
| Untouched instance | 200 throughout |
| Socket close code | **1012** service restart |
| Reconnect to the survivor | **265 ms** |
| `game.resume` | answered with a snapshot |
| Next legal move | **accepted** |
| Durable plies before / after | 2 / 3 |
| Duplicate plies | **0** |

`stop_grace_period` is **30s** for `api` — a PROPOSED OPERATIONAL DEFAULT
covering requests already in flight, against A64-028.5A's measured p99 of
616 ms and 1.45 s maximum — and **60s** for `worker`, which must exceed the
outbox claim lease.

### 7.5 The edge

`infrastructure/production/Caddyfile`, validated by `caddy validate`.

| Concern | What it does |
| --- | --- |
| TLS | Caddy obtains and renews; HTTP redirects to HTTPS |
| HSTS | 2 years, subdomains, preload-eligible |
| CSP | `default-src 'self'`, two SHA-256 script hashes, no `unsafe-eval`, `frame-ancestors 'none'` |
| Other headers | `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`; `Server` removed |
| Indexing | `X-Robots-Tag: noindex` on every private path; **nothing on `/`** |
| Unknown routes | a real **404** with a branded page |
| Operator surface | `/metrics` and `/health/drain` answer 404 from outside |
| Caching | hashed assets immutable for a year; `index.html` and `sw.js` `no-cache`; API `no-store` |
| Admin | its own hostname, `noindex` wholesale |

**Why the SPA routes are enumerated.** `try_files {path} /index.html`
returns 200 for every URL on the internet, so `/gmaes/abc` is a page as far
as a crawler is concerned. The router's paths are listed instead, and fixed
children are listed rather than prefixed — `/settings/*` was a prefix in the
first draft and `/settings/nope` came back 200, which is the same defect one
level down.

**The stated residual.** `/games/*`, `/players/*` and `/tournaments/*` stay
prefixes because their next segment is a match id, a username and a
tournament id. Those cannot be enumerated at the edge, so
`/games/<nonsense>` is a 200 that the application resolves to its own
not-found view. `apps/api/tests/unit/test_edge_policy.py` keeps the lists in
step with the router.

### 7.6 The clients

Both build to a `scratch` image whose only content is `dist/`, copied into a
volume the edge serves. Not a second web server behind Caddy: that would be
a second place for cache headers, a second place to forget a security
header, and a second process to operate.

`VITE_PUBLIC_ORIGIN` is a **build argument with no default**, set from
`ARENA64_DOMAIN` so the origin is named once for the whole deployment. It
writes the canonical link, `og:url`, the absolute social image and the
sitemap, none of which can be decided at runtime. The image refuses to build
with it empty, non-`https`, or naming a development host — the SEO generator
accepts `http://localhost` deliberately, which is right for a preview and
wrong for an image.

Source maps are generated and **deleted from the image**. A public source
map hands an attacker the unminified application, and deleting them at the
deployment layer is the answer that does not touch the user-owned
`vite.config.ts`.

The admin console is on `admin.<domain>`. **Its security boundary is the
API's authorization, not its hostname** — a bundle is public the moment it
is served, and hiding a frontend route has never been a control. What a
separate origin buys is that a cookie scoped to one is not sent to the
other.

### 7.7 What a second host still needs

Nothing here spans machines. Before one exists:

- shared object storage rather than a MinIO container per host;
- backups off this host — **P2-8 is open**, and this deployment writes them
  to a volume beside the database, so a host loss takes both;
- a real load balancer in front of several edges, and a decision about where
  TLS terminates;
- `RATE_LIMIT_TRUSTED_PROXY_COUNT` raised to match the new hop count. It is
  **1** here, and it must agree with the Caddyfile: a limiter that trusts the
  wrong hop either rate-limits the proxy as one client or accepts a spoofed
  address from a real one.

---

## Related Documents

- [`architecture.md`](./architecture.md) — AD-02 and AD-03, which §2 deviates from
- [`docs/03-backend/dependency-injection.md`](../03-backend/dependency-injection.md) — the configuration layering §3 relies on
- [`infrastructure/staging/compose.yml`](../../infrastructure/staging/compose.yml) — the definition itself
- [`production-hardening.md`](./production-hardening.md) — the production risk register §6 feeds
