# Admin

| Field | Value |
| --- | --- |
| **Status** | **Foundation shipped** — A64-024.1 (authorization, boundary, scaffold) and A64-024.2 (sign-in, routing, session isolation). Every operational surface is deferred; see §8. |
| **Owner** | _Unassigned_ |
| **Related ADRs** | `architecture.md` AD-04 (separate application) |
| **Related docs** | `docs/01-architecture/database.md` §10.4, `docs/01-architecture/domain-model.md` §13 |
| **Code** | `apps/api/app/modules/admin/`, `apps/api/app/operator/admin.py`, `apps/admin/` |

---

## 1. What A64-024.1 established

Arena64 had **no administrator at all** before this task, and
`app/operator/__init__.py` said so in as many words: *"`users.User` —
`is_active`, `is_verified` and nothing else; `auth.TokenClaims` — no scope;
anywhere — no role, no permission, no policy primitive."* It also named the
resolution: *"When the Administration epic ships a role, these commands become
the thing its routes call."*

This is that role, plus the boundary that enforces it and the smallest console
that proves the boundary works.

## 2. The authorization model

**Administrative authority is a row in `admin.role_assignment`**, exactly as
`database.md` §10.4 specified before anything needed it.

| Column | Meaning |
| --- | --- |
| `account_id` | DM-06's opaque `player_id`. No foreign key — DB-03 forbids cross-schema references |
| `role` | `AdminRole`, a native enum. One member (`admin`) today |
| `granted_by` | The administrator who conferred it. **Null only for a deployment's first grant** |
| `granted_at` / `revoked_at` | Live exactly while `revoked_at IS NULL` |

**Why not a boolean on `users.User`.** A flag answers "is this an admin" and
nothing else. A grant answers who conferred it, when, whether it is still live,
and what happened when it ended — the four questions a platform with
administrators actually has after an incident. `users` is also the wrong owner:
DM-06's rule is that a module keyed by `player_id` owns its own facts, and
administrative authority is `admin`'s fact about an account.

**Why revocation is a timestamp.** Deleting the row would make a demotion
indistinguishable from a grant that never happened. It also makes the live
check a partial index and gives §13.4's future `audit_entry` something to
reference.

**One live grant per account and role**, enforced by a partial unique index —
so two operators granting concurrently produce an integrity error rather than
two rows that disagree (BE-06).

### 2.1 Deliberately not a permission engine

There is no capability set, no policy evaluation and no inheritance. Nothing on
this platform has asked a question a role name cannot answer, and §1.7 forbids
building for the one that has not arrived. A second role (`moderator`) is one
enum member and one row per grant.

## 3. The server-authoritative boundary

`CurrentAdmin` is the **only** admin guard, and it is a router-level dependency
rather than a per-handler annotation — so a route added by a later A64-024.x
task is administrative by existing rather than by its author remembering.

    CurrentUser       401 — no credential, or one that does not verify
    active account    403 — the account is disabled
    live role         403 — authenticated, enabled, not an administrator

**Order matters.** `CurrentUser` first, so an anonymous caller gets `401` and
never `403` — the two mean different things, and "forbidden" to an anonymous
caller says the endpoint exists for somebody. Account state before role,
because a grant outlives a disabled account and checking the role first would
admit one on the strength of a row that survived the account's ability to sign
in.

**Both `403`s are identical** — same status, same message, same body. A caller
cannot tell "not an administrator" from "account disabled", and neither answer
reveals that the other case exists.

**Email verification is not required.** An administrator is created by an
operator command against a known account, not by self-service signup, so the
address is established out of band. Requiring it would protect nothing and would
lock out the first administrator of a fresh deployment.

## 4. Token staleness — the demotion question

**There is no role claim in the access token, deliberately.**

`auth.TokenClaims` carries `sub`, `jti`, `type`, `iat`, `exp`, `iss` and `aud`.
A64-024.1 added nothing. A role baked into a token is authority that outlives
its own revocation until the token expires, and the window is unobservable from
the server.

So the guard reads `admin.role_assignment` on **every** admin request. A
revoked administrator is refused on their **next request**, not when their
token happens to expire. The cost is one indexed lookup on a surface a handful
of people use.

There is no bounded-risk note to write here, because there is no bounded risk:
the staleness window is zero.

## 5. First-admin bootstrap

    python -m app.operator.admin grant --email someone@example.com --yes

`app/operator/__init__.py` established the **process** as a real boundary —
"whoever can run a command on the host is already trusted with the database" —
and this uses it rather than inventing a mechanism.

| Property | How |
| --- | --- |
| No public promotion path | No endpoint, no signup parameter, no query parameter, no role selector, no email-based automatic promotion |
| No default admin | The command grants authority to an account that already signed up normally. It creates nothing and hardcodes no credential |
| Hard to invoke accidentally | `--yes` is required; without it the command prints what it would do and exits `0` |
| Names its target | `--email`, and the confirmation line prints the resolved username |
| Single-use | `bootstrap` refuses once the role has **any** live holder, so the unattributed path closes behind itself |

After the first, grants are attributed: `--by` names an administrator, and the
command verifies that account actually holds `ADMIN` before recording it —
otherwise `granted_by` would be a field nobody can trust.

**Revocation refuses to remove the last administrator.** Granting requires an
administrator and `bootstrap` refuses while one exists, so the combination
would otherwise let one command lock a deployment out of its own admin surface
permanently.

## 6. The console — AD-04 honoured

`apps/admin` is a **separate application**: its own origin, its own deploy, its
own `package.json`, and no dependency on `apps/web`.

### 6.1 Sign-in

**Ordinary Arena64 credentials.** There is no admin username, no admin
password and no admin registration — an administrator is a normal account
holding a live role, so the console posts to the *same* endpoints the player
client uses.

    POST /auth/browser/login     credentials -> host-only refresh cookie
                                 + an access token in the body
    POST /auth/browser/refresh   the cookie -> a fresh access token
    GET  /admin/me               Authorization: Bearer <access token>
    POST /auth/browser/logout    revokes the session, clears the cookie

**The access token lives in memory only** — a closure variable, never
`localStorage`, never `sessionStorage`, never a cookie this app writes. The
refresh half is in an `HttpOnly` cookie the app cannot read at all.

**Signing in is not being authorized.** A successful login stores a token and
navigates; it renders nothing privileged. Whether the account may administer
anything is `/admin/me`'s answer, asked by the route guard afterwards — so a
valid non-administrator gets a session and then a refusal.

> **A64-024.1 defect, fixed here.** Its client called `/admin/me` with
> `credentials: "include"` and nothing else. That could never have worked: the
> refresh cookie is scoped to `path=/api/v1/auth/browser`, so it is not sent to
> `/api/v1/admin/me`, and `CurrentUser` authenticates from an `Authorization`
> header. The console would have returned `401` to everybody.

### 6.2 Session separation — the invariant

**Separation is a property of the origin, not a flag.** The refresh cookie is
`HttpOnly`, `SameSite=Lax` and carries **no `Domain` attribute**, so it is
host-only: a cookie set for one host is never sent to another.

The invariant, stated so a deployment can be checked against it:

| # | Requirement |
| --- | --- |
| SI-1 | `apps/admin` is served from a **distinct host** from `apps/web` |
| SI-2 | No cookie in this system sets a `Domain` attribute — host-only, always |
| SI-3 | `apps/admin` is deployed separately, and shares no session storage |
| SI-4 | The admin origin is listed in `BROWSER_SESSION_TRUSTED_ORIGINS`, and the player origin is not sufficient for it |

**SI-4 is the one that is easy to miss.** `enforce_trusted_origin` refuses a
cookie-authenticated call whose `Origin` is not on the list, and the list is
required in production. An admin console deployed to a new host without being
added to it fails at login with `403`, not with a security hole — the failure
direction is correct, and it is worth knowing in advance.

Hostnames are **not** chosen here: no repository document names one, and
inventing production DNS in a spec is how a guess becomes a fact. What is
fixed is SI-1 … SI-4; which hosts satisfy them is the remaining infrastructure
decision (OQ-2).

### 6.3 Local development — the honest caveat

`apps/web` runs on `localhost:5173` and `apps/admin` on `localhost:5174`.
**Cookies ignore the port**, so on those two URLs the browser treats them as
one host and the two apps *do* share the refresh cookie.

That is a development artefact and it **proves nothing about production**. It
also does not weaken production: the isolation there comes from SI-1, which
distinct ports do not satisfy.

For work that depends on the separation being real, use distinct local
hostnames — every modern browser resolves `*.localhost` to the loopback
address with no `/etc/hosts` entry:

    apps/web     http://app.localhost:5173
    apps/admin   http://admin.localhost:5174

Both proxy `/api` to the API, so each app remains same-origin with its own
API path and each receives its own host-only cookie. No cookie attribute is
relaxed to make development convenient.

### 6.4 Routes

`@tanstack/react-router` — the router `apps/web` already uses (ADR-002), so
there is one routing vocabulary in this repository. It replaces A64-024.1's
`useState` navigation, which could not survive a refresh and had no URL.

| Route | Access | Content |
| --- | --- | --- |
| `/login` | public | The sign-in form |
| `/` | protected | Dashboard |
| `/users`, `/matches`, `/tournaments`, `/moderation`, `/notifications`, `/audit` | protected | Localized "not implemented yet" |
| anything else | — | An intentional not-found page |

**Authorization lives in one place.** `ProtectedLayout` is the parent route of
every admin page, so a section added later is protected *by being a child*
rather than by its author remembering a guard.

Five states are handled explicitly, and the shell is reached from exactly one:
`checking`, `unauthenticated`, `forbidden`, `unavailable`, `authorized`. There
is no optimistic branch and no cached role, so privileged chrome cannot flash
before the server answers.

### 6.5 Intended destination

An unauthenticated visitor to `/users` is sent to `/login?next=/users` and
returns there after signing in.

`safeRedirect` is an **allowlist of shapes**: a single leading `/`, no scheme,
not protocol-relative, not a loop back to the form. Everything else becomes the
dashboard. `//evil.example` is the case that matters — a browser reads it as an
absolute URL with the current scheme, so a "starts with `/`" check alone lets an
external host through.

### 6.6 Role revocation while the console is open

The guard re-asks `/admin/me` on **every protected navigation**. A role revoked
mid-session is therefore refused the next time the administrator moves within
the console — not after a reload.

That is A64-024.1's zero-staleness property observed from the client, and it is
why the role is in the database rather than in the token. Caching the answer in
client state would have thrown it away, which is what A64-024.2 found and
fixed: the layout stays mounted across sections, so without the route key in
the check's dependencies the revocation went unnoticed.

## 6.7 Migration

`a1c4e7b92f30` was exercised **up → down → up** in A64-024.2 against the
development database: the table and its enum are removed on downgrade and
recreated on upgrade. The `admin` schema itself is deliberately left in place —
`DROP SCHEMA` would take anything a later migration put beside the table.

---

## 7. The audit invariant — for A64-024.8

A64-024.8 builds `admin.audit_entry`. Until then, one rule holds:

> **Every security-sensitive admin mutation must be attributable to an
> authenticated admin actor.**

A64-024.1 keeps it without a framework. `granted_by` is a column, `grant`
refuses without one, `bootstrap` is a separate method so the unattributed path
cannot be reached by code that merely forgot, and both writes log at `INFO`
with the account id and the role. No fake audit events were invented to check a
box.

## 8. Deferred to later A64-024.x tasks

Dashboard statistics, user management and suspension, match management,
tournament management, moderation workflows, notification operations, the audit
log viewer, analytics, infrastructure monitoring, anti-cheat surfaces, support
tooling, and the final visual design. None is started.

Their **routes exist and are guarded**; only their content is deferred, so
adding a real section later changes a page body and not the boundary.

## 9. Open questions

| # | Question | Status |
| --- | --- | --- |
| OQ-1 | Does a narrower `moderator` role exist, and what does it exclude? | **Open** — waiting on the first surface that distinguishes them |
| OQ-2 | Where does `apps/admin` deploy, and behind what network boundary? | **Narrowed by A64-024.2.** The security invariants are fixed (§6.2, SI-1 … SI-4) and no longer depend on the answer. What remains is infrastructure: which hostnames satisfy SI-1, whether the console sits behind a VPN or an IP allowlist, and adding the chosen origin to `BROWSER_SESSION_TRUSTED_ORIGINS` |
| ~~OQ-3~~ | ~~Does the console need its own sign-in?~~ | **Closed by A64-024.2.** It has its own `/login`, posting ordinary credentials to the shared auth endpoints from its own origin — so the session is the console's without a second credential system |
