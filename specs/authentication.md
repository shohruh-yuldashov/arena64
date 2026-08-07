# Authentication

> **Status:** Placeholder for the JSON surface — the **browser session** (A64-020.2) and **email verification** (A64-021.5H) halves are specified below
> **Owner:** _Unassigned_
> **Last updated:** 2026-08-07 — A64-021.5H, email verification codes
> **Related:** [`frontend.md`](./frontend.md) §12, `templates/feature-spec.md`

## Description

Account registration, sign-in, session issuance, and credential recovery for Arena64 players.

## TODO — the JSON surface

The endpoints below were built across A64-011.1–011.9 and are not yet written up here.

- [ ] Define goals and non-goals
- [ ] Define user stories and acceptance criteria
- [ ] Define domain model and state transitions
- [ ] Define API surface (see `templates/api-spec.md`)
- [ ] Define events, permissions, and rate limits
- [ ] Define test scenarios and rollout plan

---

## Browser sessions — A64-020.2

### Two surfaces, one set of rules

| Surface | Refresh token travels in | For |
| --- | --- | --- |
| `POST /api/v1/auth/{register,login,refresh,logout,logout-all}` | The request/response **body** | Native and server-side clients that can store a credential safely |
| `POST /api/v1/auth/browser/{register,login,refresh,logout,logout-all}` | An **`HttpOnly` cookie** | Browsers, which cannot |

**Every browser handler calls the same application services** the JSON ones
call: the same authentication, the same rotation, the same reuse detection,
the same rate limits, the same verification mail. Nothing about credential
handling is reimplemented. The JSON contracts are unchanged.

A second surface rather than a flag, because these are genuinely different
contracts. A `?cookie=true` would be one endpoint whose response body
changes shape by query string — undocumentable, and a client that got the
flag wrong would receive a credential it could not store safely.

### What a browser receives

`BrowserSession`: `access_token`, `token_type`, `expires_in`, `user`.
**There is no `refresh_token` field and there will not be one.** A cookie
beside a body field is a body field; if the page can read the credential,
the page can leak it.

`register` on the browser surface **signs the caller in**, unlike its JSON
counterpart which deliberately issues nothing. That is a product decision
about a browser: somebody who just completed a sign-up form should be in
the app, not at a login form. Verification gating is unchanged — the
account is unverified either way.

### The cookie

| Attribute | Value | Why |
| --- | --- | --- |
| Name | `arena64_refresh` | Configurable — `BROWSER_SESSION_COOKIE_NAME` |
| `HttpOnly` | always | The entire security property |
| `Path` | `/api/v1/auth/browser` | The cookie is absent from every other API call, so a request an attacker can cause but not read cannot act on the session — and it is not sprayed across proxy logs |
| `SameSite` | `lax` | `strict` would drop it on a navigation *into* the app from a mail client, so a user arriving from their inbox would appear signed out. `none` is the exposure being closed |
| `Secure` | `False` in `local`/`test`, `True` everywhere else | Resolved by `BrowserSessionSettings.secure_for`; there is no way to turn it off in a deployed tier |
| `Max-Age` | `SESSION_REFRESH_TOKEN_TTL_DAYS` | Mirrors the session's absolute expiry, so cookie and row stop being useful together |

`Path` and `SameSite` must match between `set_cookie` and `delete_cookie`,
or the browser treats the deletion as a *different* cookie and leaves the
live one in the jar — a sign-out that appears to work and leaves a
thirty-day credential behind. One `RefreshCookie` value object owns
`read`/`write`/`clear` so that cannot be got wrong.

**Never logged:** the access token, the refresh token, the cookie value.
No token value appears in any error response.

### CSRF

Two layers, because one is a promise made by the browser.

1. **`SameSite=Lax`** stops a cross-site `POST` from carrying the cookie in
   every current browser. Necessary, not sufficient — a client that does not
   implement it simply sends the cookie.
2. **`Origin`/`Referer` allowlist** (`auth/presentation/browser_csrf.py`) is
   the half the server controls. Applied as a `Depends` to every
   cookie-authenticated endpoint, so a route added later cannot forget it.
   The origin is rebuilt from parsed parts, so `https://arena64.uz.evil.com`
   cannot pass a check a prefix match would allow. Refusals name no origin,
   trusted or presented.

All state-changing browser endpoints are `POST`. `BROWSER_SESSION_TRUSTED_ORIGINS`
is empty in `local`/`test` — the Vite proxy makes the app same-origin — and
`Settings` **refuses to start** a production-like tier with an empty list,
so "empty" can never silently mean "allow everything".

**Bearer-authenticated API calls are not covered and do not need to be.** An
attacker's page cannot read our memory, so a forged request arrives
unauthenticated.

### Idempotency and authentication of each endpoint

| Endpoint | Authenticated by | Idempotent |
| --- | --- | --- |
| `register`, `login` | credentials | no |
| `refresh` | the cookie | no — rotation is the point |
| `logout` | the cookie, best-effort | **yes** — a missing or unknown cookie is still `204`, and the cookie is cleared either way |
| `logout-all` | the **access token** | yes. Acts on the account rather than one device, so the credential that names an account is the right one — a player whose laptop was taken can sign out everywhere from their phone |

### No migration

A cookie is not a schema. Nothing in `alembic/` changed.

### Deferred

- A device/session list UI. `SessionService.list_user_sessions` exists and
  no endpoint exposes it; it belongs with Profile.
- Access-token revocation before expiry. Still the documented cost of a
  stateless token (`JWTSettings`), unchanged by this phase.

---

## Email verification — A64-021.5H

> **Status:** Implemented
> **Last updated:** 2026-08-07 — A64-021.5H, six-digit codes and verified-user gating

### What changed and why

Verification was a **link** in an email. It still works, and it is no longer
the primary path.

A link is a poor fit for how people actually register on this platform. They
sign up on a phone, open the mail app, and the link opens a *second* browser
— one with no session — so they verify in one place and are still signed out
in the other. It is also unusable when registration happens on a desktop and
the mailbox is on a phone: the token cannot be retyped.

Six digits can be read on one device and typed on another. That is the whole
argument.

### The policy

| Rule | Value | Enforced in |
| --- | --- | --- |
| Length | exactly 6 numeric digits | `auth/domain/otp.py` |
| Lifetime | 10 minutes | `OTP_TTL_MINUTES` |
| Wrong guesses | 5, then the challenge is dead | `attempt_count`, checked and incremented in the database |
| Resend cooldown | 60 seconds | `EmailVerificationService._require_cooldown_elapsed` |
| Live challenges per account | exactly one | `uq_email_verification_tokens__one_live_per_user`, a partial unique index `WHERE used_at IS NULL` |
| Issuing a new code | invalidates the previous one | `invalidate_active_for_user` before the insert |
| Successful verification | consumes the code and ends any live link | the same `used_at` column serves both |

The one-live-per-user rule is a **database** constraint rather than a service
check, so two concurrent resends cannot both insert. The service reads the
`ConflictError` the index raises rather than racing to prevent it.

### What is stored

Not the code. `otp_verifier()` computes:

    HMAC-SHA256(EMAIL_VERIFICATION_OTP_SECRET, "{challenge_id}:{user_id}:{code}")

and the digest goes in `token_hash`. Three properties follow, and each
answers a specific attack:

| Property | Attack it answers |
| --- | --- |
| **Keyed**, not a bare digest | Six digits is twenty bits. Unsalted SHA-256 over a million candidates is under a second on a laptop, so a stolen table would surrender every live code. Without the key the digest is inert |
| The **challenge id** is in the message | Two accounts holding the same six digits produce different digests, so the table cannot be searched for a known code |
| The **user id** is in the message | A digest lifted from one row cannot be replayed against another |

Comparison is `hmac.compare_digest` — constant time, because a byte-by-byte
comparison leaks the prefix and twenty bits does not survive that.

Codes are generated with `secrets.randbelow(10**6)`. Not `random`, not a
counter, not a timestamp, and not a slice of a UUID: every one of those is
predictable given enough samples, and an attacker can obtain samples by
registering.

The secret is required outside `local` — startup refuses to serve a deployed
tier that still holds the development default (`_forbid_local_defaults_outside_local`).
Rotating it invalidates every code in flight, which is a sixty-second
inconvenience and the correct answer to a suspected disclosure.

### The code never appears anywhere but the message

No log line, no metric, no exception message, no API response, no test
snapshot. `build_verification_code_email` keeps it out of the subject and out
of every URL, because a subject line is shown on a lock screen and a URL ends
up in referrer headers and proxy logs.

The one exception is `ConsoleEmailProvider`, the **development** transport,
which prints the whole message including the code — that is what it is for,
and it is the only way the contract tests can obtain a code the way a person
does.

### Endpoints

| Endpoint | Auth | Rate limit | Notes |
| --- | --- | --- | --- |
| `POST /auth/email/verify-code` | session | none — see below | Body `{"code": "123456"}`. No address: the session says whose challenge it is |
| `POST /auth/email/resend-code` | session | `resend_code_ip`, 20/hour | `202`, or `409` with `Retry-After` inside the cooldown |
| `POST /auth/email/verify` | none | none | The link path, unchanged |
| `POST /auth/email/resend` | none | `resend_verification_email`, 3/hour | Issues a **link**. The anonymous escape hatch |

`verify-code` deliberately carries no rate limit. The five-attempt counter is
per challenge and lives in the database, and a sixth challenge costs a
60-second cooldown — so the guessing rate is capped at 5/minute/account from
any number of hosts. An IP rule would add no bound and would refuse a shared
connection.

`resend-code` is limited per **IP** rather than per email, unlike its
anonymous sibling, because the request carries no address to key on. Its own
per-user cooldown is not sufficient by itself: `register_ip` permits ten
accounts an hour from one host, and ten accounts each resending once a minute
is six hundred messages an hour — a sending-reputation problem for
`arena64.gg` before it is anything else.

### Failure modes

| Code | HTTP | Meaning |
| --- | --- | --- |
| `email_verification_code_invalid` | 422 | Wrong, malformed, or no live challenge. One code for all three — distinguishing them tells an attacker which guesses were structurally wrong |
| `email_verification_code_expired` | 422 | Past ten minutes. Distinct from invalid because retyping an expired code is pointless, and telling somebody to do it spends one of five attempts |
| `email_verification_attempts_exceeded` | 422 | Five wrong guesses. The remedy is a resend |
| `email_verification_resend_too_soon` | 409 + `Retry-After` | Inside the cooldown |
| `email_already_verified` | — | **Not an error on `verify-code`**: an already-verified account gets success. See below |
| `email_verification_required` | 403 | A gated endpoint, called by an unverified account |

A malformed code is rejected **before** an attempt is counted. Five attempts
is a security budget, and spending one on somebody's typo is a denial of
service against the person the limit exists to protect.

### Already verified is not an error

Submitting a code for an account that is already verified answers success.
The person may have verified in another tab, or by clicking an older link in
their mail app, and the second submission is a request to reach a state that
already holds. Answering `409` would strand somebody in front of a form that
refuses to let them past a condition that is already true.

### Verified-email gating

`VerifiedUser` (`auth/presentation/dependencies/verified_user.py`) replaces
`CurrentUser` on endpoints that act on the platform. It reads
`users.is_verified` from the **database**, never from a token claim:
`AuthenticatedUser` is built entirely from the JWT, so a claim would be stale
for the lifetime of an access token issued before verification — which is
precisely the token every newly-registered account is holding.

| Gated | Not gated | Why |
| --- | --- | --- |
| avatars: upload, delete | every read endpoint | Reads harm nobody, and a person who cannot see the platform cannot decide to finish verifying |
| friends: request, accept, decline, cancel, remove, block, unblock | `/auth/*` | Verification itself must be reachable, and so must signing out |
| matchmaking: join, leave, accept, decline | `/health`, `/ready` | Not a user surface |
| profiles: update profile, privacy, preferences | notification preference reads/writes | A person needs to be able to stop the mail |
| tournament: enter, withdraw | | |

The line is **outward-facing writes**. Everything in the gated column either
reaches another player (friend requests, chat by way of a game, tournament
entry) or attaches content to an identity the platform has not confirmed.

### The frontend flow

Registration signs the browser in and navigates to `/verify-email` rather
than to the app — the session exists, so sending somebody to a home page
whose every action answers `403` is a worse experience than the one screen
that has something to do.

`RequireVerifiedEmail` wraps every protected route beneath `RequireAuth` and
redirects an unverified session to `/verify-email`, carrying the attempted
path as `next`. `/verify-email` itself carries **no** guard, because the link
half must work for a visitor who has never signed in on that browser.

`/verify-email` branches on the URL:

| URL | Screen | Session |
| --- | --- | --- |
| `?token=…` | exchanges the token, unchanged | not required |
| no token | the six-digit form | required |

Nothing on the screen is authoritative. Verified state comes from the
session's `user`, code validity from the server, and the cooldown from
`Retry-After` — so a reload rebuilds all three and loses only a half-typed
field, and a second tab that verifies is noticed by the first on its next
navigation. A client cannot mark itself verified: `verify-code` returns the
server's own `UserRead`, and that is what the session stores.

The input is one field, not six boxes: `inputMode="numeric"` for a keypad,
`autoComplete="one-time-code"` because on iOS it is the only way the code is
offered from the message, and `maxLength` so typing a seventh digit is
dropped rather than submitted. Non-digits are stripped as the person types,
which handles the paste that carried a space out of a mail client. There is
no automatic submit on the sixth digit — a paste that arrives one character
at a time would fire it early, and a mistyped last digit would spend an
attempt before it could be corrected.

The address is masked (`n•••••@example.com`). Enough to recognise which
mailbox to open, not enough to publish on a shared screen.

### Links are deprecated, not removed

`POST /auth/email/verify` and `POST /auth/email/resend` still work, and
`/verify-email?token=` still exchanges them. Links already sitting in inboxes
when this shipped must not break, and the anonymous resend is the only
recovery available to somebody with no session.

Both credentials converge on one row and one `is_verified` flag, so a code
that succeeds also ends any live link and vice versa. Removal belongs in a
later phase, once the oldest outstanding link has expired.

### Operator command

    python -m app.operator.accounts verify --email someone@example.com

Idempotent, and it exists because "I never got the email" is the most common
account problem any platform has and the answer cannot be "register again".
It deliberately cannot *read* a code — what is stored is a keyed verifier, and
a support tool that reads a live credential aloud is a phishing script with a
company logo on it.

A process profile rather than an `/api/v1/admin` route, and it matters more
here than usual: an endpoint that marks an address verified is an endpoint
that removes email verification from the platform.

### Testing

| Layer | File | Covers |
| --- | --- | --- |
| Contract | `tests/contract/test_otp_verification.py` | The real API against real PostgreSQL: issue, verify, expiry, attempt exhaustion, cooldown, reissue-invalidates-previous, already-verified, gating |
| Unit | `tests/unit/test_email_verification_service.py` | Orchestration against fakes |
| Frontend | `src/features/auth/verify-email.test.tsx` | The real router: the guard's redirect, submit-and-apply, and distinct rejections |
| E2E | `tests/e2e/verify-email.spec.ts` | Registration lands on verification, a product route waits, verifying opens it |

The E2E journey marks the account verified through the operator command
rather than by typing a code. Reading a real inbox from an automated test is
forbidden, and so is exposing a code through a browser-reachable endpoint for
testing — so the digits themselves are covered where they can be, in the
contract test that reads the code out of the delivered message.

### Migration

`d1f6a83c04e2` adds two columns to `auth.email_verification_tokens`:
`kind` (`'link'` | `'otp'`, defaulting to `'link'`) and `attempt_count`
(defaulting to `0`). Both carry server defaults, so rows written before this
migration are valid links and remain redeemable.
