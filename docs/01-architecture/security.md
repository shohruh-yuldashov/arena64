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

_TBD._

## Secrets Management

_TBD._

## Incident Response

_TBD._

## TODO

- [ ] Assign a document owner
- [ ] Draft the remaining `_TBD._` sections — Threat Model, Authorization,
      Transport & Storage, Anti-Cheat, Secrets, Incident Response.
      Authentication and Session & Token Handling were written from
      A64-011.2 / A64-011.3 and describe shipped behaviour
- [ ] Revisit token lifetime once A64-011.4's refresh tokens exist —
      15 minutes is chosen against the *absence* of revocation
- [ ] Link related decision records in `docs/07-decisions/`
- [ ] Review and promote status from Draft to Approved
