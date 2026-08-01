# Security Architecture

> **Status:** Draft — placeholder
> **Owner:** _Unassigned_
> **Last reviewed:** _Not yet reviewed_

## Purpose

Defines the platform threat model and the controls that protect accounts, matches, and player data.

## Scope

Authentication, authorization, transport and storage protection, abuse prevention, and incident response.

## Threat Model

_TBD._

## Authentication

Two factors of the same credential, in two tasks.

| Step | Mechanism | Where |
| --- | --- | --- |
| Prove identity | Email + password, verified with Argon2id | `auth.AuthenticationService` (A64-011.2) |
| Carry identity | Signed JWT bearer token, `Authorization: Bearer` | `auth.AccessTokenService` / `TokenValidator` (A64-011.3) |

Sign-in returns a single generic failure whether the address is unknown
or the password is wrong, and spends an Argon2 verification against a
throwaway hash in the unknown-address case so the two are
indistinguishable in elapsed time as well as in response. Account state
(`inactive`, `locked`) is disclosed only *after* verification succeeds,
so it is never an enumeration oracle.

Password material is never stored, compared, logged, or exported in
recoverable form (AC-3). Hashes are re-derived at current parameters on
successful sign-in, so raising Argon2's cost never requires a reset.

## Authorization Model

_TBD._ Authentication answers "who are you"; nothing on the platform yet
answers "may you". `AuthenticatedUser` carries no roles, scopes or
permissions, deliberately — a placeholder claim that nothing enforces
reads as a control that exists.

## Session & Token Handling

### Token shape

One token type today: `access`. Claims are `sub`, `jti`, `iat`, `exp`,
`iss`, `aud`, `type` — and nothing else. **No personal data ever enters a
payload**: a JWT payload is base64, not encryption, and tokens reach
`localStorage`, proxy logs, screenshots and bug reports. A handle is also
mutable (domain-model.md §7.2), so a copy inside a credential is a copy
that can be wrong.

`type` is a private claim (RFC 7519 §4.3), deliberately not spelled `typ`
— that is a registered *header* parameter, and putting a security
decision in a field some JWT tooling rewrites is how it stops being one.

### Verification

Every one of these is checked on every decode, with no argument that
disables any of them:

signature (against every active key) · `exp` · `iss` · `aud` · presence
of all seven claims · the parseability of each · the expected `type`.

The accepted algorithm list comes from configuration, never from the
token's own header — the check that makes `alg: none` and
algorithm-confusion unreachable rather than merely unlikely. Only HMAC
algorithms are configurable, so a symmetric secret can never be verified
as an asymmetric public key.

Every rejection returns one status, one code and one message. The server
knows which check failed and says so at DEBUG, where a caller cannot read
it — a finer-grained response is a step-by-step oracle for shaping a
forgery. The single exception is expiry, which gets its own code because
a client must act differently on it (refresh, not re-authenticate) and
can read `exp` from the token it already holds anyway.

### Lifetime, and what it costs

Access tokens live **15 minutes**, bounded in configuration to one hour.

A stateless token is valid because it verifies, not because a row says
so, and therefore cannot be revoked between issue and expiry. SE-1 (a
password change revokes every other session) and SE-3 (suspension revokes
all sessions immediately) cannot be honoured by the signature — the
lifetime *is* the window in which they are wrong. Fifteen minutes is that
window set deliberately; raising it is a security decision, not a tuning
one.

`jti` is minted on every token and read by nothing yet. It is present so
that A64-011.4 can revoke an individual token without rotating a key and
signing everyone out — a claim cannot be added retroactively to
credentials already in circulation.

### Key rotation

`JWT_SECRET_KEY` signs; it **and** `JWT_PREVIOUS_SECRET_KEYS` verify.

Rotation is: publish the new key, move the old one to the previous list,
drop it after one token lifetime. Nobody is signed out. This is §2.4's
two-key requirement — written there for the WebSocket ticket key and
applying identically here, for the reason that argument makes: rotation
that signs every user out at once becomes an incident, and therefore
never happens.

A deployed tier refuses to start on the development signing key. Unlike a
wrong database URL, that misconfiguration fails nowhere at runtime — the
service starts, serves traffic, and anyone with a copy of the repository
can mint a token for any account.

### WebSocket connections do not use access tokens

AD-09 stands: sockets authenticate with a short-lived, single-use ticket,
not with the bearer token. Browsers cannot set headers on a WebSocket
handshake, which would put a fifteen-minute credential in a query string
and therefore in load balancer logs, proxy logs and browser history. The
JWT infrastructure supports that flow by being able to mint a *different
token type* with a different lifetime and audience — which is what the
`type` claim and the `TokenProvider`/service split exist for.

## Transport & Storage Protection

_TBD._

## Anti-Cheat & Abuse Prevention

Anti-cheat is `fairplay`'s (AD-05) and is not yet built. What exists today
is **abuse prevention on the authentication surface** (A64-011.8).

### What is limited, and per what

| Endpoint | Limit | Counted per |
| --- | --- | --- |
| `POST /auth/login` | 5 / 15 min | IP |
| `POST /auth/login` | 10 / hour | email |
| `POST /auth/register` | 3 / hour | IP |
| `POST /auth/password/forgot` | 3 / hour | email |
| `POST /auth/email/resend` | 3 / hour | email |
| `POST /auth/refresh` | 30 / min | IP |
| `POST /auth/password/reset` | 10 / hour | IP |

Every figure is configurable (`RATE_LIMIT_*`), so tightening one during an
incident is a restart rather than a release. The reset-password limit is
the one figure A64-011.8 did not specify; it is chosen against what that
endpoint actually risks, which is Argon2id CPU rather than token guessing.

**Why login carries two rules.** Per-IP and per-email answer different
attacks and neither is sufficient. Per-IP bounds one host guessing many
passwords for one account, and is evaded by a botnet — a thousand hosts
each receive their own five attempts. Per-email bounds the whole platform
guessing at one account however many hosts it comes from, and is evaded by
credential stuffing, which tries one password against a million accounts
and never trips a per-account limit. Login is where both attacks meet.

The two are evaluated in one atomic operation that consumes from both
buckets or from neither, so a request refused by one rule has not spent
the caller's allowance under the other.

### Algorithm

A **sliding window log** in Redis: one sorted set per (rule, subject),
scored by arrival, pruned and evaluated by a single Lua script.

Atomicity is the property that matters. A read-then-write limiter lets
every request in a concurrent burst observe the same "not full yet" and
proceed, so the overshoot scales with the attack — the limiter fails
precisely under the conditions it exists for. One script makes
prune-count-decide-write indivisible.

The window slides rather than resetting in blocks, which denies the
boundary burst a fixed window permits: ten password guesses in two seconds
against a limit of five.

`services.md §4.1` labels this step "Redis token bucket". The deviation is
deliberate and argued in `app/core/rate_limiting.py`: the limits here are
counts over windows rather than refill rates, and a bucket cannot produce
an honest `X-RateLimit-Reset` or an exact `Retry-After`.

### Storage

Counters live on their own Redis role (`REDIS_LIMITS_URL`), which is the
fifth instance rather than a reuse of `cache`. AD-03's argument applied
rather than amended: a cache is configured to evict, and a rate-limit
counter evicted under memory pressure is a limit that silently stops
applying during a traffic spike — which is when an authentication endpoint
is either genuinely busy or under attack.

Keys are `rl:v1:<rule>:<sha256(subject)[:32]>`, and the subject is hashed
because a Redis keyspace has neither the access control nor the retention
policy of the database (§14.1) — `KEYS`, `MONITOR` and an RDB dump all
expose key names. This is obfuscation, not encryption: an address is
guessable, so the digest confirms membership to anyone holding a candidate
list. What it buys is that a leaked key dump is not itself a list of
registered addresses.

Every key carries a TTL of its window, reset on each write. Nothing needs
sweeping.

### Behaviour on the wire

`429` with the platform error envelope and code `rate_limited`, plus
`Retry-After` and `X-RateLimit-Limit` / `-Remaining` / `-Reset`. The three
`X-RateLimit-*` headers are also returned on **successful** responses, so
a client can pace itself rather than discovering its budget at the moment
it is refused.

The headers describe the *binding* rule and never name it. Naming the
dimension that refused a request is the one piece of information needed to
evade it — "per email" says rotate the address, "per IP" says rotate the
host — so the numbers are published and the shape of the defence is not.
For the same reason the message is one fixed string, identical whether or
not the address exists: a 429 that varied would reintroduce the account
enumeration oracle `/auth/password/forgot` is built to deny.

### Failure policy

**Fails open by default**, logging at ERROR. A Redis outage then degrades
abuse prevention rather than removing the ability to sign in, register or
recover a password.

Stated as the trade it is: failing closed would convert an outage of the
least critical dependency in the request path into a total authentication
outage (T-2). Rate limiting is also not the only control on these
endpoints — Argon2id still bounds guess throughput, `users.locked_until`
still exists, sign-in still returns one generic failure, and reset tokens
still carry 256 bits. Losing the limiter is losing defence in depth, not
losing the defence. `RATE_LIMIT_FAIL_OPEN=false` inverts the choice and
returns `503` (not `429` — the caller did nothing wrong).

Every check is bounded by `RATE_LIMIT_REDIS_TIMEOUT_MS`, because a Redis
that is *slow* is the common failure and an unbounded wait would take the
platform down while the limiter itself remained available.

### Caller identity

The per-IP rules are only as good as the address they count. With
`RATE_LIMIT_TRUSTED_PROXY_COUNT=0` (the default) the socket peer is used
and `X-Forwarded-For` is ignored entirely; with a count of N the address
is read N entries from the right of that header, so entries a caller
forged on the left are never read.

**Both wrong values are severe.** Trusting the header with no proxy in
front lets any client set its own rate-limit identity. Leaving the count
at zero behind a load balancer makes every request appear to come from the
balancer, so the platform shares one bucket and the first five sign-ins of
any fifteen minutes lock out everybody else.

### Logging

A blocked request logs at WARNING with the endpoint, method, caller IP,
rule name and retry delay. The *rate* of these is the platform's signal
that a credential-stuffing run is in progress.

The address is logged in full while the same value is hashed before it
becomes a Redis key, and the asymmetry is deliberate: responding to abuse
requires knowing which address to block, and the log pipeline has
controlled access and a retention policy. Email addresses are never
logged — only the name of the rule that fired, which identifies the
dimension without the value. Passwords and tokens never reach a subject, a
key, or a log line.

### Not covered

`POST /auth/logout`, `POST /auth/logout-all` and `GET /auth/me` require a
credential the caller already holds. `POST /auth/email/verify` is guarded
by its token's 256 bits rather than by a counter. None appear in
A64-011.8's scope; the verify endpoint is a reasonable addition.

There is no distributed-attack detection, no adaptive tightening, and no
block list — a limiter counts, it does not decide that a pattern is an
attack. Those belong with A64-011.9.

## Secrets Management

_TBD._

## Incident Response

_TBD._

## TODO

- [ ] Assign a document owner
- [ ] Draft the remaining `_TBD._` sections — Threat Model, Authorization,
      Transport & Storage, Secrets, Incident Response.
      Authentication and Session & Token Handling were written from
      A64-011.2 / A64-011.3, and Anti-Cheat & Abuse Prevention's
      abuse-prevention half from A64-011.8; all three describe shipped
      behaviour. The anti-cheat half remains `fairplay`'s (AD-05) and
      unwritten
- [ ] `services.md §4.1` still labels the rate-limiting step "Redis token
      bucket". The shipped implementation is a sliding window log, for
      reasons argued in `app/core/rate_limiting.py`. One word, in a
      document whose owner is unassigned — worth correcting with that
      owner rather than unilaterally
- [ ] Revisit token lifetime once A64-011.4's refresh tokens exist —
      15 minutes is chosen against the *absence* of revocation
- [ ] Link related decision records in `docs/07-decisions/`
- [ ] Review and promote status from Draft to Approved
