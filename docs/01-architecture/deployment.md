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

**Production shares this deviation, and A64-030.2 recorded it here rather
than leaving it implied.** The production host is the same one host, so
`infrastructure/production/compose.yml` also runs one Redis with five
logical databases, and one instance has one instance-wide
`maxmemory-policy`. What production adds is a **bound** — `maxmemory` was
unset, which is unbounded, on a machine with 7.75 GiB — and a deliberate
choice of policy: `noeviction`, so pressure fails a write loudly rather than
evicting a rate-limit counter silently. §9.4 carries the reasoning.

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
| Host metrics | `node-exporter` | 9100 | no | — | — | — | 1 | signal |
| Metrics and alerts | `prometheus` | 9090 | no | **durable volume** | `/-/healthy` | — | 1 | signal |

Only the edge publishes ports. Everything else is reachable on the compose
network and nowhere else. The edge is `nginx` rather than `caddy` — §8
replaced it, and this table is A64-028.6's record of the shape rather than
the current service list, which is the compose file.

`prometheus` was added by A64-030.2. `infrastructure/observability/` had
held a finished Prometheus configuration and twenty-nine alert rules since
A64-028.7, and no compose file ran it: every rule was unarmed, including the
two that fire on a backup that has never succeeded.

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

  **It exits zero even when issuance fails**, and that is load-bearing.
  Nginx waits on it with `condition: service_completed_successfully`, so a
  non-zero exit meant nginx never started, so nothing answered the
  challenge, so issuance could never succeed — the deadlock the stopgap
  exists to break, restored by an exit code. On a clean host that left
  eleven of fifteen services in `created` with nothing on 80 or 443
  (A64-029).
- **`certbot`** renews twice a day with jitter, and **completes a first
  issuance that `certbot-init` could not**. While
  `live/<domain>/.self-signed` is on disk it retries issuance every five
  minutes instead of renewing; `certbot renew` alone cannot help, being a
  no-op when there is no certbot lineage to renew. A failed renewal leaves
  the existing certificate untouched and does not stop the loop.

  Nothing is hidden by the retry: the marker persists until a real
  certificate replaces it, every attempt logs, and the stopgap expires in
  three days — well inside the expiry alert.

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



### 8.10 The names the edge answers — A64-030.2

Three, and the certificate carries all three:

| Name | Serves | Why |
| --- | --- | --- |
| `${ARENA64_DOMAIN}` | the product | The canonical origin. `PUBLIC_APP_URL`, the cookie's trusted origin, the media base and the client's canonical link all derive from it |
| `admin.${ARENA64_DOMAIN}` | the admin console | §7.6 — a separate origin so a cookie scoped to one is not sent to the other |
| `www.${ARENA64_DOMAIN}` | **a 301 to the apex, and nothing else** | below |

**Why `www` exists here at all.** It has a public A record pointing at the
production host, and before A64-030.2 the edge answered to no such name.
That was not a 404: the port-80 default server redirects to `https://$host`,
so a visitor reached `https://www.…` and met a **certificate name
mismatch**. `headers-common.conf` is what made that unrecoverable rather
than merely ugly — it sends `Strict-Transport-Security … includeSubDomains;
preload`, so once a browser has seen the apex once, `www` is pinned to HTTPS
for two years and the certificate error has **no click-through**.

An `includeSubDomains` policy is a promise that every subdomain terminates
TLS properly. A resolving `www` without a certificate breaks that promise,
which is why this was a defect in the edge rather than a missing feature.

**Why it redirects and never serves the application.**
`specs/frontend.md` §11 makes one origin a security contract rather than a
preference: the refresh token is a host-only `SameSite=Lax` cookie and
`BROWSER_SESSION_TRUSTED_ORIGINS` names the apex alone. An SPA served on
`www` would look like it worked, hold a cookie the apex does not recognise,
and have every state-changing request refused by `browser_csrf.py` — a
session that reports itself as signed in and is not. So `www` declares no
`root`, no `try_files`, no `proxy_pass` and no `location`; a test asserts
each of those absences.

The redirect is a 301 carrying `$request_uri`, so a shared deep link keeps
its path and query. It sends no `X-Robots-Tag`: a crawler consolidates a 301
onto its target, and `noindex` on the redirect can suppress the target
instead.

**The alternative, and why it was not chosen.** `www` could instead be
removed from DNS, which is equally consistent with the one-origin contract
and is one fewer name to renew. It was rejected because the DNS record is
not this repository's to remove, and because the failure mode while the
record exists is the worst kind — a hard TLS interstitial on the most
commonly typed variant of the domain, during a public beta. Supporting the
name is safe whether or not the record survives; removing the record is safe
only once it is actually gone.

**The coupling this creates.** ACME validates every name in an order, and one
name that cannot be validated fails the whole order. If `www` stops
resolving to this host, the apex and `admin.` stop renewing with it.
Removing `www` is therefore one change with three edits: the DNS record,
`certbot/issue.sh`, and `nginx/templates/30-www.conf.template`.

### 8.11 Two things the base image and the runtime got to decide — A64-030.2

Both were found by the application-tier preflight, on the built image rather
than in the configuration this repository writes — which is exactly why
neither was caught by a test that reads these files.

**E-1 — the stock server block survived.** `nginx:alpine` ships
`/etc/nginx/conf.d/default.conf` (`listen 80; server_name localhost;` over
`/usr/share/nginx/html`), `COPY conf.d/` added this repository's files beside
it without removing anything, and `nginx.conf` includes `conf.d/*.conf`
wholesale. Measured on the built image: `GET /` with `Host: localhost`
returned **200 "Welcome to nginx!"** while every other Host correctly got a
301. One request header opted out of the HTTP→HTTPS policy.

The image now deletes it **and asserts the directory contains exactly what
this repository put there**, so a future base image shipping a new default
fails the build rather than appearing in a response header on a production
host.

**E-2 — the edge could not reach its configured capacity.** `nginx.conf`
asks for 4096 connections per worker; containers on this host inherit a soft
`nofile` of 1024, and nginx said so at every start:

```
[warn] 4096 worker_connections exceed open file resource limit: 1024
```

A proxied request holds two descriptors — client and upstream — so the real
ceiling was about five hundred concurrent requests per worker on the only
process facing the internet, and reaching it fails quietly: accepts stall
and clients hang. The `nginx` service now declares
`ulimits.nofile` **65535 soft and hard**, inside this host's 524288 hard
ceiling. Raised rather than capped, because 4096 is the number the edge was
sized for and 1024 was a runtime default that happened to be smaller. Only
the edge declares it; nothing else here multiplies descriptors per
connection the way a proxy does.

### 8.12 Who owns `/etc/letsencrypt` — A64-030.2

The first real production issuance validated all three names, finalised the
ACME order, **received a certificate from Let's Encrypt**, and then threw it
away:

```
certbot.errors.CertStorageError: live directory exists for arena64.gg
```

`RenewableCert.new_lineage` refuses to create a lineage when `live/<name>`
already exists and is non-empty. The bootstrap wrote its self-signed stopgap
straight into that directory, so the file that existed to let nginx start
was also what made issuance impossible — permanently, because the stopgap is
only removed on a success that could never happen.

**The boundary, now explicit:**

| Path | Owner |
| --- | --- |
| `/etc/letsencrypt/live`, `archive`, `renewal` | **Certbot.** Arena64 creates nothing here, ever |
| `/etc/letsencrypt/arena64` | **Arena64.** Certbot never reads or writes it |

Under Arena64's half:

```
arena64/stopgap/<domain>/     the self-signed pair, real files
arena64/current/<domain>      a symlink -> stopgap/<domain>  or  ../../live/<domain>
arena64/quarantine/<stamp>/   whatever recovery moved aside
```

**nginx reads `arena64/current/<domain>` and nothing else.** Its
configuration never changes; only what one symlink resolves to does. That is
what lets the edge start before a certificate exists and switch to the real
one without a rewrite — and nginx mounts `/etc/letsencrypt` read-only, so it
could not have flipped anything itself.

The flip is `mv -T`, not `mv -f`: the link points at a *directory*, and
`mv -f` follows it, moving the replacement **inside** rather than over it —
silently, exit zero. The first draft of this fix shipped that bug and
`tests/unit/test_acme_bootstrap.py::TestTheSymlinkFlipReallyFlips` is why it
will not ship twice.

**State is decided by evidence, not by a marker Arena64 wrote.**
`certbot/lineage.sh`'s `has_certbot_lineage` requires
`live/<domain>/fullchain.pem` to be a *symlink resolving into*
`archive/<domain>/` — Certbot's own layout, which nothing this repository
creates can imitate. The old loop keyed on `.self-signed`, a file we wrote
into the directory that was simultaneously breaking issuance.

#### First boot, and the two state machines

```
no lineage   ->  write stopgap, point current at it, attempt issuance
                 success -> point current at live/<domain>   (nginx reloads within 60s)
                 failure -> stay on stopgap, exit 0 so nginx is released, retry in 5 min
lineage      ->  certbot renew twice a day, re-assert the symlink, sleep 12h + jitter
```

`certbot-init` still exits 0 on failure — nginx waits on it with
`service_completed_successfully`, and a non-zero exit is the A64-029
deadlock. What changed is that a failure now leaves Certbot's namespace
untouched, so the retry can actually succeed.

nginx watches `arena64/current/<domain>` resolved through to the archive file
and reloads when it changes, so a first issuance is live within a minute
instead of up to six hours. A renewal changes `fullchainN.pem` ->
`fullchainN+1.pem`, which the same watch catches.

#### One-time recovery for a host that ran the old bootstrap

A host that already failed this way holds two pieces of state in Certbot's
namespace, and **both** must go:

- `live/<domain>/` with regular `.pem` files — blocks `new_lineage` outright;
- `renewal/<domain>.conf`, left behind empty by the failed attempt.
  `util.unique_lineage_name` creates it with `safe_open` *before* the
  live-directory guard runs and does not unlink it when the guard raises. On
  the next attempt it finds the name taken and returns `<domain>-0001.conf`,
  so Certbot creates a lineage called **`<domain>-0001`** — quietly, at a
  path the edge does not read.

Both behaviours are pinned by
`tests/unit/test_acme_bootstrap.py::TestCertbotStorageContract`, which calls
Certbot's own storage module inside the pinned image.

```bash
docker compose --env-file production.env run --rm   --entrypoint sh certbot /usr/local/bin/recover-legacy-stopgap.sh
```

It **quarantines rather than deletes**, moving both into
`arena64/quarantine/<timestamp>/`, and refuses to run when it sees a real
Certbot lineage, a `live/` directory without the legacy `.self-signed`
fingerprint, or a non-empty `archive/<domain>`. Running it twice is safe.

**Operators must never place files under `live/<cert-name>`.** That directory
is Certbot's lineage namespace; anything there makes the next issuance either
fail or silently rename itself.

#### Before retrying real issuance on the affected host

The failed attempt already spent one of Let's Encrypt's **five duplicate
certificates per week** for this exact name set. Prove the fix against
staging first — it has no such limit:

```bash
docker compose --env-file production.env run --rm --entrypoint certbot certbot   certonly --webroot --webroot-path /var/www/certbot --dry-run   --cert-name arena64.gg -d arena64.gg -d www.arena64.gg -d admin.arena64.gg
```

A real lineage is confirmed by `live/<domain>/fullchain.pem` being a symlink
into `archive/<domain>/` — not by `certbot certonly` exiting zero, which it
did on the day it discarded a real certificate.

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
| **Public certificate** | ACME issuance, renewal, expiry metric and three alerts; a failed renewal leaves the existing certificate byte-identical | `arena64.gg` pointing at a host with port 80 open | `live/<domain>/.self-signed` is gone and `arena64_certificate_expiry_timestamp_seconds` reads a Let's Encrypt certificate. **Not** `certbot-init` exiting zero — it exits zero on failure too, deliberately, so that nginx is released (§8.5) |
| **Off-host backup** | Encryption, SigV4 upload, a separate off-host timestamp and two alerts; proven against a MinIO **on this laptop** | a bucket at a real provider, and credentials for it | the object appears in the remote bucket and the off-host gauge exists |
| **External monitoring** | nothing in this repository, deliberately | an off-host uptime check on `https://arena64.gg/` and on certificate expiry | see [monitoring the monitoring](./runbooks.md#monitoring-the-monitoring) |
| **Resend production credential** | fail-fast on a missing or placeholder key; delivery metrics and two alerts | a real key, and a sending domain with SPF, DKIM and DMARC | a verification email arrives during the first-boot smoke test |

### 9.2 Host-sizing gates — CLOSED by A64-030.2

None of these was a defect. Each was a number that could not be chosen
honestly before the machine existed, and inventing one would have been the
fake tuning A64-028.5A's performance document refuses.

**The machine now exists** — netcup RS 1000 G12, Ubuntu 24.04.4, **4 vCPU,
7.75 GiB RAM, 4 GiB swap, 251 GiB ext4**, with fio measuring 4 KiB random
reads at ~61.9k IOPS (p99 1.16 ms) and writes at ~26.6k IOPS (p99 1.50 ms).
So every row below is now answered in `infrastructure/production/compose.yml`
rather than deferred, and §9.3 records the envelope.

| Setting | Depends on | Now |
| --- | --- | --- |
| API replica count | cores, and measured request load | **2**, unchanged — what the edge and the deploy procedure are written against |
| Container memory and CPU limits | total RAM, and how it is shared | **set** — §9.3. Ceilings total 5.25 GiB against 7.75 GiB, so a per-container limit acts before the kernel's OOM killer, which is not obliged to choose a good victim |
| PostgreSQL `shared_buffers`, `work_mem` | RAM | **set** — 256MB and 4MB, sized against the *container* limit rather than host RAM. §9.3 |
| PostgreSQL connection budget | replica count | **30 steady, 32 during a deploy**, against `max_connections` 100 with 3 reserved. Pools are 5 + 5 per process rather than the code default of 10 + 5: one single-threaded uvicorn process with a 5 s statement timeout cannot usefully hold fifteen backends, and each idle backend costs 5–10 MB |
| Redis `maxmemory` and eviction | RAM, and the live-state working set | **256mb, `noeviction`** — it was unset, which is unbounded. See §9.4 on why the policy is the loud one |
| Prometheus retention | disk | **15 days, and 4 GB** — bounded twice, because the size bound is what stops `DiskWillFillSoon` becoming an alert about the alerting |
| nginx `worker_processes` | cores | `auto`, which reads the cgroup limit rather than the host's core count — so the CPU limit below is what it sees |

**Still outstanding:** re-run A64-028.5A's load matrix on this host. Every
number in `docs/05-operations/performance.md` was measured on a laptop that
was also the client, and none of it transfers. The envelope below is a
conservative starting point derived from those measurements, not a
measurement of this machine.

### 9.3 The resource envelope — A64-030.2

| Service | `cpu_shares` | CPU limit | Memory reservation | Memory limit |
| --- | ---: | ---: | ---: | ---: |
| `nginx` | 256 | 1.0 | 64M | 128M |
| `api-1` | 768 | 1.5 | 512M | 832M |
| `api-2` | 768 | 1.5 | 512M | 832M |
| `worker` | 512 | 1.0 | 384M | 640M |
| `postgres` | 768 | 2.0 | 768M | 1280M |
| `redis` | 256 | 0.5 | 256M | 768M |
| `minio` | 154 | 0.5 | 192M | 384M |
| `prometheus` | 256 | 1.0 | 384M | 768M |
| `node-exporter` | 51 | 0.25 | 32M | 64M |
| `certbot` | 51 | — | 32M | 64M |
| `backup` | 256 | 1.0 | 64M | 384M |
| **Total** | **4 096** | — | **3 200M** | **6 144M** |

`migrate` carries a 512M ceiling and no reservation; the other one-shot
services carry neither. Reservations total 3.1 GiB, which is what the tier
is expected to occupy; ceilings total 6.0 GiB, and the gap is what absorbs a
spike without the kernel getting involved.

**Why `cpu_shares` and not `deploy.resources.reservations.cpus`.** Verified
on this host against Compose v5.5.1 by creating a container and reading
`HostConfig` back: `limits.memory`, `limits.cpus` and
`reservations.memory` are applied; **`reservations.cpus` is silently
ignored** — it is a Swarm scheduling hint with no meaning in the local
runtime. `cpu_shares` is the real cgroup control with that meaning: relative
weight under saturation, never a cap. The mapping is
`cpu_shares = round(reserved cores × 1024)`.

Every resident service carries one, including the small ones, and that is
not tidiness: an unset `cpu_shares` is the Docker default of **1024**, so a
service left out would outweigh PostgreSQL at 768 under exactly the
contention this exists to arbitrate.

**Swap is not application capacity.** `memswap_limit` is deliberately not
pinned to `mem_limit` — that converts a transient spike into an OOM kill of
PostgreSQL, which is worse than a second of paging. Keeping the host off
swap in normal operation is `vm.swappiness`, a host setting this repository
does not own; **10 is the recommendation** and it has not been applied.

### 9.4 PostgreSQL and Redis, as configured

PostgreSQL runs with a `command:` override rather than a mounted
`postgresql.conf`, for the reason the nginx image gives about baking its
configuration: a mounted file is a thing on the host that nothing versions
and nothing reviews.

```
shared_buffers=256MB          effective_cache_size=2GB
work_mem=4MB                  maintenance_work_mem=96MB
autovacuum_max_workers=2      max_connections=100
wal_buffers=16MB              max_wal_size=1GB / min_wal_size=128MB
checkpoint_completion_target=0.9
random_page_cost=1.1          effective_io_concurrency=200
log_min_duration_statement=500ms   log_checkpoints=on   timezone=UTC
```

`shared_buffers` is a quarter of the **container**, not of host RAM: a
quarter of 7.75 GiB inside a 1280M container is a process that is killed
before it fills its cache. The rest of the caching is the OS page cache,
which §9.3 leaves 1.5 GiB for and which `effective_cache_size` tells the
planner about without allocating a byte. `random_page_cost` and
`effective_io_concurrency` are the only two numbers derived from a
measurement on this host; the stock 4.0 describes a spinning disk.

**Durability is untouched.** `fsync`, `synchronous_commit` and
`full_page_writes` keep their defaults and are deliberately not listed, so
no later edit can turn one off by looking like the others.
`shared_preload_libraries=pg_stat_statements` is **not** enabled: the
library alone does not create the extension, and creating it needs an init
mechanism this deployment does not otherwise have. Carried as a follow-up.

Redis runs one instance with `appendonly yes`, `appendfsync everysec`,
`maxmemory 256mb`, `maxmemory-policy noeviction` and a `requirepass`.
`data-reliability.md` §3 asks for `allkeys-lru` on `cache` and `noeviction`
on `limits` and `live`; one instance has one instance-wide policy, so the
tie is broken towards the **loud** failure. With `noeviction`, memory
pressure fails writes and the rate limiter fails open *and increments
`rate_limit_unavailable_total{failed_open}`*, which is an alert. With
`volatile-lru` — nearly every key here carries a TTL — Redis would instead
silently evict `rl:` counters during exactly the spike the limiter exists
for, which `data-reliability.md` names as the unacceptable outcome.


---

## Related Documents

- [`architecture.md`](./architecture.md) — AD-02 and AD-03, which §2 deviates from
- [`docs/03-backend/dependency-injection.md`](../03-backend/dependency-injection.md) — the configuration layering §3 relies on
- [`infrastructure/staging/compose.yml`](../../infrastructure/staging/compose.yml) — the definition itself
- [`production-hardening.md`](./production-hardening.md) — the production risk register §6 feeds
