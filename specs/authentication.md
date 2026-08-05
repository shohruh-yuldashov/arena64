# Authentication

> **Status:** Placeholder for the JSON surface — the **browser session** half is specified below (A64-020.2)
> **Owner:** _Unassigned_
> **Last updated:** 2026-08-05 — A64-020.2, browser session endpoints
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
