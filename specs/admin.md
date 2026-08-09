# Admin

| Field | Value |
| --- | --- |
| **Status** | **Foundation shipped** — A64-024.1 (authorization, boundary, console scaffold). Every operational surface is deferred; see §8. |
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

**Session separation is a property of the origin, not a flag.** `apps/web`'s
refresh cookie is `HttpOnly`, `SameSite=Lax` and carries no `Domain`
attribute — so it is host-only. Serving the console from its own origin gives
it its own cookie jar entry by construction.

| Requirement | How |
| --- | --- |
| No privileged flash | The gate starts in `checking` and has no optimistic branch. There is no cached decision and no stored role anywhere in the app |
| Direct navigation and refresh behave alike | Mounting the app *is* the check — there is no other path |
| Authorization is server-authoritative | One `GET /api/v1/admin/me`; the shell renders from its `200` and from nothing else |
| Hiding is not the boundary | Every admin API independently enforces `CurrentAdmin`. A player who reached the bundle and edited its state would still be refused by every request the shell makes |
| Unbuilt sections | Disabled buttons carrying a localized "not built yet", never links |
| Localization | `uz`, `ru`, `en` — the platform's three, in the console's own message tree |

**No admin entry point was added to `apps/web`.** AD-04 keeps the two apps
separate, and a link in the player client would be the beginning of the
coupling it forbids.

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

Routing in `apps/admin` is a `useState` over one reachable section: a router
earns its place when there is a second destination and a URL worth sharing.

## 9. Open questions

| # | Question | Blocked on |
| --- | --- | --- |
| OQ-1 | Does a narrower `moderator` role exist, and what does it exclude? | The first surface that distinguishes them |
| OQ-2 | Where does `apps/admin` deploy, and behind what network boundary? | Deployment work outside this epic |
| OQ-3 | Does the console need its own sign-in, or does it keep redirecting to the player client's? | A64-024.2 — today it links to `/login` on its own origin |
