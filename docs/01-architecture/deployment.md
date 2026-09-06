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

## 8. The edge is nginx — A64-028.6A

> §7 describes the production topology A64-028.6 built on Caddy. This
> section replaces its edge. Everything else in §7 stands.

### 8.1 Why

**An architecture decision, not a fault found in Caddy.** Arena64 targets
nginx as its production edge; the Caddy configuration was a correct
implementation of the same policy and is retained only in
`infrastructure/staging/`, which is explicitly not a production definition.

Migrating a proxy is a chance to lose behaviour silently, so nothing was
assumed to have survived translation: every invariant below was re-proved
through nginx over real HTTP, and two defects were found that way.

### 8.2 The migration matrix

| Behaviour | Caddy | Nginx | Migration risk | Cover |
| --- | --- | --- | --- | --- |
| HTTP→HTTPS | automatic | explicit `return 301`, ACME path excluded | a redirect loop, or an unrenewable certificate | `TestTheHttpListener` |
| TLS issuance | built in | `certbot` service | **the largest** — nginx renews nothing by itself | §8.5 |
| TLS policy | opinionated defaults | `TLSv1.2 TLSv1.3`, OpenSSL's own cipher defaults | a hand-written cipher list that ages badly | `TestTlsPolicy` |
| Security headers | `header` inherits | `add_header` **does not** | a location that sets one header loses them all | `TestHeaderInheritance` |
| `Server` header | removable | `server_tokens off` only | the name remains; the version does not | stated |
| SPA routes | enumerated matchers | enumerated `location` blocks | a route added to the router and not the edge | `TestEveryRouteIsServed` |
| True 404 | `error` + `handle_errors` | `try_files … =404` + `error_page` | a catch-all making every URL a page | `TestEveryRouteIsServed` |
| `X-Robots-Tag` | `header @private` | per-location `add_header` | a private route indexed, or the landing page hidden | `TestPrivateRoutesAreNotIndexable` |
| API upstream | `reverse_proxy api:8000` | `resolver` + variable `proxy_pass` | **stale or fatal name resolution** | §8.3 |
| WebSocket | transparent | explicit `Upgrade`/`Connection` | a handshake that returns 200 and a dead socket | `TestTheRealtimePath` |
| Forwarded headers | `header_up {remote_host}` | `proxy_set_header … $remote_addr` | an appending proxy trusting a forged prefix | `TestForwardedHeaderTrust` |
| Compression | `encode zstd gzip` | `gzip` only | zstd and Brotli need modules the official image lacks | stated |
| Metrics isolation | `error 404` | `return 404` | the exporter reachable from the internet | `TestTheOperatorSurfaceIsNotPublic` |
| HTTP/2 | automatic | `http2 on` | the pre-1.25 `listen … http2` syntax | proved by negotiation |
| HTTP/3 | automatic | `listen 443 quic` + `Alt-Svc` + UDP mapping | advertising a protocol nothing is listening for | `TestHttp3` |

### 8.3 Upstream resolution — the decision that was reversed by evidence

The obvious form was written first:

```nginx
upstream arena64_api { server api-1:8000; server api-2:8000; }
```

and rejected, because nginx resolves those names at **configuration parse
time** and treats a failure as fatal:

```
nginx: [emerg] host not found in upstream "api-1:8000"
```

That is not a startup-ordering inconvenience. It means an edge that **will
not start while a backend is down** — one crashed API container takes the
whole site offline, including static pages that need no backend — and a
`reload` that fails for the same reason, silently leaving a renewed
certificate unapplied.

What replaced it is a `resolver` and a variable `proxy_pass`, resolved per
request. Both replicas carry the network alias `api`, so Docker's DNS
returns every running container and round-robins.

**The trade, measured.** Twenty requests per state, one replica of two:

| State | Answered |
| --- | ---: |
| Both replicas up | 20/20 |
| Container stopped, probed immediately | 19/20 |
| After the resolver TTL (5 s) | 20/20 |
| Application killed, container still in DNS | 8/8 |

The last row is `proxy_next_upstream` reaching the second address DNS
returned. What is lost with `max_fails` is one request in twenty during the
few seconds between a container stopping and the resolver noticing — and a
deploy drains and waits before stopping, so that window is off the deploy
path. The case neither approach covers is a container that **accepts a
connection and then hangs**.

### 8.4 The image

`nginx:1.29-alpine`, configuration **baked in**. A mounted config is a file
on the host that nothing versions and nothing validates; baking it makes the
tag that is deployed *be* the routing policy, and `nginx -t` runs at build
time against a throwaway certificate — a syntax error fails the build rather
than a container start.

The build also asserts the binary has `--with-http_v3_module`,
`--with-http_v2_module` and `--with-http_ssl_module`. An image without the
first would start happily and serve no HTTP/3 at all.

**Root.** The master process stays root and drops to `nginx` for workers,
which is the stock image's model and what lets it bind 80 and 443. Running
the master unprivileged means a capability grant or a high port and a
host-level redirect — trading a well-understood privilege separation for a
less-well-understood one. What root here can reach: no database credential,
no application secret, and a certificate volume mounted read-only.

### 8.5 Certificates

`certbot`, webroot challenge, two containers:

- **`certbot-init`** runs once and exits. It writes a self-signed stopgap
  first, because nginx will not start without the files its
  `ssl_certificate` names and the challenge is served *by nginx* — a
  deadlock the stopgap breaks. A browser in that window sees a certificate
  warning, which is the correct signal for a deployment that is not
  finished.
- **`certbot`** renews twice a day with jitter. A failed renewal leaves the
  existing certificate untouched and does not stop the loop.

**Nginx reloads itself on a six-hour timer.** The obvious
`--deploy-hook "nginx -s reload"` cannot work across containers, and giving
the renewal job the Docker socket would let a certificate task start any
container on the host. A renewal is live within six hours of being written
and the certificate is valid for thirty days at that point.

**The renewal is not trusted to report itself.** A job that exits zero and
writes nothing produces no failure log and an expiring certificate, so the
signal is the certificate on disk: the worker mounts
`/etc/letsencrypt` read-only and publishes
`arena64_certificate_expiry_timestamp_seconds`. Three alert rules read it —
expiring, expired, and absent.

### 8.6 HTTP/2 and HTTP/3

Both enabled. HTTP/2 on TCP 443 is the baseline; HTTP/3 is QUIC on **UDP**
443, which needs its own port mapping — without it `Alt-Svc` advertises a
port nothing is listening on and every client silently stays on HTTP/2.

Proven: `curl` negotiates HTTP/2 on TCP; an `aioquic` client negotiates ALPN
`h3` on QUIC v1 and receives HTTP 200 with the same 11 188-byte page.

**The fallback is the absence of a step, not a path that has to work.**
`Alt-Svc` is an advertisement: a client that cannot reach UDP 443 never
upgrades. Demonstrated by running the same image with the UDP port
unpublished — the HTTP/3 client gets a connection error, and HTTP/2 over TCP
serves the page unchanged.

### 8.7 Logging

The default `combined` format logs `$request`, the raw request line
**including the query string** — where a password reset carries `?token=` and
an email verification carries `?code=`. Those are precisely what
`app/common/redaction.py` keeps out of the application's own logs, and
logging them at the edge would put them back.

The format logs `$uri`, the normalised path with the query string removed.
No header is logged, so `Authorization` and `Cookie` cannot appear.

### 8.8 Firewall contract

The host must allow inbound:

| Port | Protocol | Why |
| --- | --- | --- |
| 22 | TCP | operator access, per the deployment's own SSH policy |
| 80 | TCP | HTTP→HTTPS redirect **and the ACME challenge** — closing it breaks renewal while leaving the site working |
| 443 | TCP | HTTP/1.1 and HTTP/2 |
| 443 | **UDP** | HTTP/3. Optional: closing it degrades to HTTP/2 with no other effect |

Everything else must be unreachable from outside the host: PostgreSQL 5432,
Redis 6379, the API's 8000, Prometheus, Grafana, and the operator surface
(`/metrics`, `/health/drain`) — which the edge refuses in addition to the
bearer token the application requires.

The compose file publishes ports for `nginx` **only**; every other service is
reachable on the compose network. That is the enforcement, and the firewall
is the second boundary.

### 8.9 What the edge costs

Forty requests over a reused connection, on a developer's laptop:

| Path | p50 | p95 |
| --- | ---: | ---: |
| Direct to the API, plain HTTP | 1.12 ms | 1.40 ms |
| Through nginx, HTTPS + HTTP/2 | 4.15 ms | 6.74 ms |
| Through nginx, HTTPS + HTTP/1.1 | 3.94 ms | 5.30 ms |

About three milliseconds, of which an unmeasured part is a `socat`
forwarder the test harness inserts between nginx and each uvicorn and
production does not have. **A regression check, not a capacity
measurement** — see `docs/05-operations/performance.md` §1 for why nothing
measured on this machine is a production number.



---

## 9. Gates — A64-028.7

> The epic's closing audit separates two things that are easy to conflate:
> **code readiness**, which this repository can establish, and **live
> deployment readiness**, which needs infrastructure that does not exist
> yet. A gate below is never a missing implementation. Each one is a thing
> the code is ready for and cannot prove alone.

### 9.1 Live deployment gates

| Gate | What is ready | What is missing | Proven when |
| --- | --- | --- | --- |
| **Public certificate** | ACME issuance, renewal, expiry metric and three alerts; a failed renewal leaves the existing certificate byte-identical | `arena64.gg` pointing at a host with port 80 open | `certbot-init` completes and `arena64_certificate_expiry_timestamp_seconds` reads a Let's Encrypt certificate |
| **Off-host backup** | Encryption, SigV4 upload, a separate off-host timestamp and two alerts; proven against a MinIO **on this laptop** | a bucket at a real provider, and credentials for it | the object appears in the remote bucket and the off-host gauge exists |
| **External monitoring** | nothing in this repository, deliberately | an off-host uptime check on `https://arena64.gg/` and on certificate expiry | see [monitoring the monitoring](./runbooks.md#monitoring-the-monitoring) |
| **Resend production credential** | fail-fast on a missing or placeholder key; delivery metrics and two alerts | a real key, and a sending domain with SPF, DKIM and DMARC | a verification email arrives during the first-boot smoke test |

### 9.2 Host-sizing gates

None of these is a defect. Each is a number that cannot be chosen honestly
before the machine exists, and inventing one would be the fake tuning
A64-028.5A's performance document refuses.

| Setting | Depends on | Today |
| --- | --- | --- |
| API replica count | cores, and measured request load | 2, which is what the edge and the deploy procedure are written against |
| Container memory and CPU limits | total RAM, and how it is shared | **unset** — a wrong limit turns a busy hour into an OOM kill, and `MemoryPressure` alerts on the host instead |
| PostgreSQL `shared_buffers`, `work_mem` | RAM | server defaults. The **connection budget is computed and safe**: 2 API × 15 + worker 15 = 45 steady, 60 during a deploy, against `max_connections` 100 with 3 reserved |
| Redis `maxmemory` and eviction | RAM, and the live-state working set | unset. Redis holds a cache of a replay (`data-reliability.md` §3); an eviction costs a rebuild, not data |
| Prometheus retention | disk | set at deploy. `DiskWillFillSoon` is what catches getting it wrong |
| nginx `worker_processes` | cores | `auto`, which reads the cgroup limit rather than the host's core count |

**After the server is provisioned**, work through §9.2 in order, then re-run
A64-028.5A's load matrix on it — every number in
`docs/05-operations/performance.md` was measured on a laptop that was also
the client, and none of it transfers.


---

## Related Documents

- [`architecture.md`](./architecture.md) — AD-02 and AD-03, which §2 deviates from
- [`docs/03-backend/dependency-injection.md`](../03-backend/dependency-injection.md) — the configuration layering §3 relies on
- [`infrastructure/staging/compose.yml`](../../infrastructure/staging/compose.yml) — the definition itself
- [`production-hardening.md`](./production-hardening.md) — the production risk register §6 feeds
