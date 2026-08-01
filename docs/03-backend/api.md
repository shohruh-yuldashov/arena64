# API Reference

> **Status:** Draft — placeholder
> **Owner:** _Unassigned_
> **Last reviewed:** _Not yet reviewed_

## Purpose

Canonical index of HTTP API surfaces, conventions, and versioning rules.

## Scope

Cross-cutting API conventions and an index of per-feature specifications in `specs/`.

## Conventions

_TBD._

## Versioning

_TBD._

## Authentication

Bearer tokens. `Authorization: Bearer <access token>`, where the access token comes from
`POST /auth/login` or `POST /auth/refresh`.

Two credentials, and they are not interchangeable:

| | Access token | Refresh token |
| --- | --- | --- |
| Shape | Signed JWT, stateless | Opaque random value, stored hashed |
| Lifetime | 15 minutes (`JWT_ACCESS_TOKEN_TTL_SECONDS`) | 30 days absolute, 14 days idle |
| Sent as | `Authorization` header | Request body of `POST /auth/refresh` only |
| Revocable | **No** — see below | Yes, and revoked on reuse |

**An access token cannot be revoked before it expires.** It is valid because it verifies,
not because a row says so, so a password change or a suspension takes effect on the
*refresh* path and leaves at most fifteen minutes in which an already-issued access token
still works. That window is the documented cost of a stateless credential and the reason
the lifetime is short; closing it needs a `jti` denylist, which is outstanding (see
`security.md`).

A refresh token is rotated on every use. Presenting one that has already been rotated is
treated as theft and revokes the entire session chain — see database.md §14.3.

`security.md` holds the full model; the generated OpenAPI at `/docs` is authoritative for
request and response shapes.

## Pagination

_TBD._

## Error Format

One shape, for every error, from every endpoint:

```json
{
  "code": "invalid_credentials",
  "message": "Invalid email or password.",
  "request_id": "0f9c…",
  "correlation_id": "3b21…"
}
```

Deliberately **not** wrapped in the `{"data": …, "meta": …}` envelope successful responses
use. Nesting an error under `data` would invite a client to decide whether a call
succeeded by inspecting the body instead of the status code, which is the one signal that
is never ambiguous.

- `code` is a stable, machine-readable member of `app.core.error_codes.ErrorCode`. Clients
  branch on it; they never parse `message`. The enum is **additive only** — removing or
  renaming a member breaks already-deployed clients.
- `message` is safe to display but is written for a developer, not an end user. It never
  contains a stack trace, SQL, an internal identifier, or the value that caused the error.
- A code is added only when a client must *behave* differently and the status plus the
  endpoint are not enough to tell the cases apart. That rule is why `409` from registration
  distinguishes `username_already_exists` from `email_already_exists`, and why every
  authentication failure — unknown address, wrong password — shares `invalid_credentials`.

FastAPI's native `{"detail": …}` shape is never returned; request-validation failures are
translated into the envelope above, with each error's `input` dropped so a rejected value
never lands in a log or a browser console.

## Rate Limiting

Applied to the six unauthenticated authentication endpoints. `security.md` holds the
limits, the algorithm and the failure policy; what a *client* needs is only this:

| Header | On | Meaning |
| --- | --- | --- |
| `X-RateLimit-Limit` | 2xx and 429 | Requests permitted in the window |
| `X-RateLimit-Remaining` | 2xx and 429 | Requests left before refusal |
| `X-RateLimit-Reset` | 2xx and 429 | Seconds until the allowance changes |
| `Retry-After` | 429 only | Seconds to wait — **delta-seconds, not a date** |

The three `X-RateLimit-*` headers are returned on **successful** responses too, so a client
can pace itself rather than discovering its budget at the moment it is refused.

They describe the single most constraining limit and deliberately do not name it. An
endpoint may count per IP, per email address, or both; publishing which one refused a
request is the one piece of information needed to evade it.

A refusal is `429` with code `rate_limited`, and the body is identical whatever was
counted — including for addresses that do not exist, so that the limiter cannot become the
account-enumeration oracle the endpoints themselves are built to deny.

## Endpoint Index

**The generated OpenAPI document is the index.** `/docs` (Swagger UI), `/redoc`, and
`/openapi.json` are produced from the routes themselves, carry a summary, a description,
examples, response models and error models for every operation, and cannot drift from the
code.

A hand-maintained list here would be a second source of truth for something already
generated, and the one that drifts is always the copy. A64-011.9's audit spent much of its
effort removing exactly that class of drift from the `auth` module's docstrings; adding a
fresh instance of it to this file would be an odd way to conclude.

What belongs in this document is what OpenAPI *cannot* express: the conventions above, and
the reasoning behind them. Per-feature request and response detail lives in `specs/`.

## TODO

- [ ] Assign a document owner
- [ ] Draft the remaining `_TBD._` sections — Conventions, Versioning, Pagination.
      Authentication, Error Format, Rate Limiting and Endpoint Index were written from
      A64-011.9's audit and describe shipped behaviour
- [ ] Link related decision records in `docs/07-decisions/`
- [ ] Review and promote status from Draft to Approved
