# Admin

| Field | Value |
| --- | --- |
| **Status** | A64-024.1 (authorization), A64-024.2 (sign-in, routing), A64-024.2H (deployment origins), A64-024.3 (**Users**), A64-024.4 (**Matches**) and A64-024.5 (**Tournaments**) — all read-only. Remaining surfaces deferred; see §8. |
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

### 6.2 Production origins and session ownership — A64-024.2H

| Application | Canonical origin |
| --- | --- |
| `apps/web` | `https://arena64.gg` |
| `apps/admin` | `https://admin.arena64.gg` |

**The console is never `arena64.gg/admin`.** AD-04 makes it a separate
application, and a path on the player origin would put it in the player's
cookie jar, in the player's bundle and behind the player's session.

#### Each front end is same-origin with its own `/api`

This is the decision that makes isolation real, and it was reached by
rejecting the obvious alternative.

```
browser ──/api/*──> arena64.gg        ──reverse proxy──> FastAPI
browser ──/api/*──> admin.arena64.gg  ──reverse proxy──> FastAPI
```

`specs/frontend.md` §11 already required this for the player client — *"the
page and the API must share an origin… a cross-origin API would either never
receive [the cookie] or would need `SameSite=None`, which is precisely the
CSRF exposure the cookie exists to close."* A64-024.2H extends the same
contract to the console rather than making an exception for it.

**A shared `api.arena64.gg` for browser sessions was considered and
rejected.** The refresh cookie is host-only and belongs to **whichever host
answered the login**. Had both front ends posted to one API host, that host
would own one cookie, both apps would send it, and the player session and
the administrator's would be the same credential — with CORS and
`Allow-Credentials` needed to reach it. That is not session isolation, and
calling it one would have been the dishonest outcome A64-024.2H existed to
avoid.

`api.arena64.gg` may still exist for **token-bearing, non-browser clients**
(a mobile app authenticating with `Authorization`). It is not the canonical
endpoint of the browser refresh-session contract.

#### Cookie ownership, stated exactly

| Property | Value | Consequence |
| --- | --- | --- |
| `Domain` | **absent** | Host-only. Never `.arena64.gg` — that single attribute would merge the two sessions |
| `Path` | `/api/v1/auth/browser` | Sent only to the auth endpoints; `/admin/me` uses a bearer token instead |
| `SameSite` | `Lax` | |
| `Secure` | true in every deployed tier, not configurable off | |
| `HttpOnly` | true | The console cannot read it, and neither can an injected script |
| Owner | The **front-end host that answered the login** | `arena64.gg` and `admin.arena64.gg` hold different cookies of the same name |

The cookie **name** is the same in both; the **host** differs, and that is
what separates them. No second cookie namespace was introduced — host
isolation already achieves it, and a parallel admin cookie would have been
complexity added to match wording rather than to close a gap.

#### The invariants a deployment must satisfy

| # | Requirement |
| --- | --- |
| SI-1 | `apps/admin` is served from a **distinct host** from `apps/web` |
| SI-2 | **No cookie in this system sets a `Domain` attribute** |
| SI-3 | `apps/admin` is deployed separately and shares no session storage |
| SI-4 | Each front-end origin routes `/api/*` to FastAPI itself; browser sessions never cross to a separate API host |
| SI-5 | `BROWSER_SESSION_TRUSTED_ORIGINS` lists **both browser page origins** and nothing else |

**SI-5 is the one that fails loudly.** `enforce_trusted_origin` refuses a
cookie-authenticated call whose `Origin` is not listed, so a console
deployed without being added fails at login with `403`. That is the correct
direction to fail, and worth knowing before the first administrator tries.

    BROWSER_SESSION_TRUSTED_ORIGINS=["https://arena64.gg","https://admin.arena64.gg"]

Note these are **page** origins. Listing `https://api.arena64.gg` would allow
nothing, because no browser ever claims to be an API host.

#### CORS

**None, deliberately.** The backend registers no CORS middleware and needs
none: every browser request is same-origin through its own front end's
reverse proxy. Adding permissive CORS to reach a shared API host is exactly
the change SI-4 exists to prevent.

### 6.3 Local development

`app.localhost:5173` for the player client, `admin.localhost:5174` for the
console. Both are in each app's `allowedHosts`, and `*.localhost` resolves to
the loopback address in every current browser with no `/etc/hosts` entry.

**Bare `localhost:5173` and `localhost:5174` share a cookie**, because
cookies ignore the port — the two are one host to the cookie jar. That is a
development artefact which proves nothing about production and hides the
isolation the console depends on, so work that touches sessions should use
the hostnames above. Nothing about the cookie is relaxed to make either
convenient.

### 6.3a Network boundary — a recommendation, not a requirement

**Not required now.** The console already has a separate application, a
separate origin, server-side role authorization checked on every protected
navigation, a host-only session, and a login that refuses untrusted origins.
Nothing in this repository's security documentation requires network-level
restriction on top of that, and inventing a VPN dependency would add an
operational failure mode without closing a demonstrated gap.

**Recommended for production hardening**: an IP allowlist or a VPN in front
of `admin.arena64.gg`. It converts a stolen administrator credential from
sufficient into merely necessary. That belongs to a production-hardening
epic together with CSP and other security headers, which are reverse-proxy
concerns this task deliberately does not build.

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

## 6.8 User Management — A64-024.3

**Read-only, and that is the decision rather than an omission.**

### Why no mutations

§9 of A64-024.3 requires a security-sensitive admin mutation to produce an
audit entry. `admin.audit_entry` is specified in `database.md` §10.4 and
**is not built** — A64-024.1 created `role_assignment` alone. An unaudited
deactivation is exactly what §8 says to stop before, so this phase reads and
A64-024.8 unlocks the writes.

No deactivate button, no role grant control, no password action, no
deletion. §7 is explicit that a Users page must not grow a
privilege-escalation button merely because it exists; roles are granted by
`python -m app.operator.admin` (§5).

### API

    GET /api/v1/admin/users            list and search
    GET /api/v1/admin/users/{user_id}  one account

Both name `CurrentAdmin`, so the guard is visible in each signature.
`Cache-Control: no-store` on both.

| Concern | Behaviour |
| --- | --- |
| Search | **Prefix** on username *or* email. Both are covered by unique btree indexes; substring on email cannot use one and would be a sequential scan per keystroke |
| Filters | `is_active`, `is_verified`. An **admin-role filter is deliberately absent** — role lives in another schema, so filtering it means a cross-schema join (DB-03 forbids) or post-filtering that breaks the keyset. It is displayed instead |
| Pagination | Keyset on `(created_at, id)` — `ix_user__created_at_id` exists for it, and the `id` tiebreak is what makes the ordering total. Default 25, max 50 |
| Total count | **None.** An operator needs "are there more"; a count is a sequential scan per page |
| Query shape | One page = **two queries**: the accounts, then one whole-set read of administrators. Never per row |

### Fields exposed, and why

`id`, `username`, `display_name`, `email`, `is_active`, `is_verified`,
`created_at`, `is_admin`, and on the detail `admin_role_granted_at`.

`email` is deliberate — an operator's starting point is a support request,
and omitting it would push them to `psql`, which is a worse place for this
data to be read.

Absent, and unreachable through the port beneath: password hash, refresh and
access tokens, OTP material, sessions, provider responses. `_to_admin_record`
maps field by field rather than by reflection, so a new column on `UserModel`
does not silently widen this.

**Rating summaries are not included.** §6 admits them "if cheap" and they are
not: `RatingReader` batches on `(player, key)` pairs, so a detail page would
first have to enumerate every `variant × speed_class` the product offers —
product knowledge this router has no business holding.

### Console

`/users` replaces the placeholder; `/users/$userId` is a real route rather
than a modal. Search and filters live in the URL, so a filtered view is a
link an operator can send and the back button works. Search is debounced and
every superseded request is **aborted** — without that a slow first response
can land after a fast second and repaint stale rows.

A table above the breakpoint and the same rows as cards below it. Nothing is
hidden at either width.

---

## 6.9 Match Management — A64-024.4

**Read-only, for the same reason Users is.** `admin.audit_entry` is unbuilt,
so there is no `POST`, `PUT`, `PATCH` or `DELETE` on the match routes.
Force-finish, cancel, result editing, rollback and rating adjustment all wait
for A64-024.8 — a mutation that ends somebody's rated game without a record
is the one this platform must not ship first. The console offers no disabled
"coming soon" controls either: a greyed button is a promise.

### API

    GET /api/v1/admin/matches            list and filter
    GET /api/v1/admin/matches/{match_id} one match

Both name `CurrentAdmin`; both send `Cache-Control: no-store`.

`game.public.AdministrativeMatchDirectory` is a **separate port** from
`MatchHistoryReader`: that one is scoped to a single player and to finished
matches because a profile renders it, while an operator starts from a status
or a match id and often cares about the game still in progress.

| Concern | Behaviour |
| --- | --- |
| Filters | `status`, `rated`, `variant`, `origin`, `participant_id` — every one a typed enum or boolean mapping to a column |
| Participant *name* search | **Absent.** Names live in `users`; filtering by one means a cross-schema join (DB-03 forbids). Finding the id is the Users console's job, and `participant_id` is the port's form of the question |
| Pagination | Keyset on `(created_at, id)` — the match table's **primary key**, so the ordering is total and the keyset is an index seek. Default 25, max 50 |
| Query shape | **Two queries per page**: the matches, then one batch resolving every participant name |

### Fields exposed

Match: id, status, variant, rated, origin, ply count, outcome, winner,
termination reason, speed class, `created_at`, `settled_at`, `ended_at`, and
the time control on the detail.

**There is no `started_at`** — the schema has none, only `clock_turn_started_at`,
which is a clock field. `settled_at` (the acceptance handshake instant) is
published instead rather than inventing a lifecycle name for a clock.

Participants: **player id, username, display name, side — and nothing else**.
No email, no IP, no device, no session, no token. A match page shows who
played; anything more about the person is `/users/{id}`, which has its own
guard and its own decision.

Absent and unreachable through the port: the board, the move log, queue
ticket ids, clock deadlines, draw-offer bookkeeping.

### Replay

**Not integrated.** `MatchReplayReader` applies every ply through the engine,
so folding it into the detail would replay a game on every open — turning a
cheap read expensive. §10 asks for replay only where the architecture
supports it naturally, and reusing `apps/web`'s board component is ruled out
by AD-04. Deferred with the seam named: the published replay port already
exists when a phase has a reason.

---

## 6.10 Tournament Management — A64-024.5

**Read-only.** No `POST`, `PUT`, `PATCH` or `DELETE`. A tournament mutation
is the most consequential unaudited write this platform could offer:
publishing a round or advancing a player moves brackets, and brackets move
ratings. Everything waits for `admin.audit_entry` (A64-024.8).

### API

    GET /api/v1/admin/tournaments
    GET /api/v1/admin/tournaments/{tournament_id}

**One detail response rather than four endpoints.** A tournament is bounded
by its `capacity`, so entrants, rounds, pairings and standings are all
O(capacity) and cost a fixed number of statements together — splitting them
would make the console issue four round trips for one page.

| Concern | Behaviour |
| --- | --- |
| Filters | `status`, `format`, `variant`, `rated` — typed enums and a boolean |
| Name search | **Absent.** `tournament.name` carries no index; a substring match would be a sequential scan. Deferred rather than added expensively |
| Entrant filter | **Absent.** Registrations are another table; filtering by one breaks the keyset |
| Pagination | Keyset on `(created_at, id)`, default 25, max 50 |
| Query shape | List: 2 statements. Detail: 6 — one per collection plus one batch resolving every player named |

### The bracket

`pairing` is keyed `(tournament_id, round_number, slot)` and the tree is
arithmetic: the parent is `(round_number + 1, slot // 2)`, even slots feeding
the light seat. `domain.bracket_plan` states it.

**The API publishes the coordinates, not the edges.** Shipping a second
description of the tree would be a description that can disagree with the
domain's; publishing coordinates means a console cannot draw a bracket the
backend does not have. `AdminPairingView` deliberately has no `parent_id`,
`next_slot` or `feeds_into` field.

**The console renders it round by round, stating in text where each node
feeds** — derived from the same arithmetic. There are no connector lines,
and that is the decision rather than a shortcut: a line is a claim about
structure, and a claim drawn in CSS can be wrong where the data is not. It
is also the accessible representation, and it is what makes a large bracket
usable at 360px. Graphical connectors are A64-025's polish over data that
already supports them.

### Standings

Read from `standing`, **never recomputed**. The module owns that authority
and a console deriving placements from matches would be a second source of
truth for who won.

### Entrant privacy

Player id, username, display name, registration status, seed and timestamps.
**No email, no profile, no block state, no registration token.** The console
links to `/users/{id}` for anything the person's own page owns, and bracket
nodes link to `/matches/{id}`.

---

## 6.11 Audit Log — A64-024.8

**Read-only, permanently.** Not "read-only until a later phase" like §6.8
through §6.10: entries are written by the privileged service performing the
action, and there is no endpoint that accepts one. A `POST /admin/audit`
would let anything holding an admin session write history — including
history of things that never happened — which is the one failure an audit
trail cannot survive.

This is the phase that **unblocks** the mutations §6.8–§6.10 deferred.

### Storage

`admin.audit_entry`, exactly as `database.md` §10.4 specifies it, plus
`outcome`. Created by `b2d5f8a41c70`, verified up → down → up.

| Concern | Behaviour |
| --- | --- |
| Append-only | A trigger raises `restrict_violation` on `UPDATE`, `DELETE` **and** `TRUNCATE` |
| Why a trigger | The guarantee must survive a repository bug, a migration, and an operator with `psql`. A rule only the application keeps is a rule the application can forget |
| Why `TRUNCATE` separately | It fires no row trigger, so a row-level guard alone leaves the single statement that empties the whole trail unguarded |
| Where it is declared | On the model as `after_create` DDL **and** in the migration — so it exists in every database the table exists in, including the ones the contract suite builds with `create_all` |
| Indexes | `(created_at, id)` for the keyset; `(actor_id, created_at)`, `(action, created_at)`, `(subject_type, subject_ref, created_at)` for the three filters |
| Check | `ck_audit_entry__actor_matches_type` — `actor_id` is present exactly when `actor_type` is `administrator` |
| Foreign keys | **None.** `users` is another schema and DB-03 forbids the reference; the trail must outlive the accounts it names |

### The actor, and the bootstrap answer

`actor_type` distinguishes an `administrator` (an account the guard
resolved) from an `operator` (a process, `actor_id` NULL). A deployment's
first grant is made from a shell before any administrator exists, and
recording a fabricated account there would be the one lie an audit trail
cannot afford — a reader could not tell it from a real grant by that person.
What authorised the action is the process boundary, which is a stronger
control than anything the trail could record about it.

**The actor never comes from a client payload.** `AuditRecorder` cannot see
a request; it takes an account id its caller resolved.

### What an entry may and may not carry

`before` and `after` are typed slices written by the use case that knows
what changed — `{"role": "admin"}`, never `request.json()`.

**Forbidden, and enforced at the writing end** because a response model
cannot redact what was already stored: passwords and hashes, access and
refresh tokens, OTP material, session secrets and identifiers, raw
`Authorization` headers, cookies, arbitrary request bodies, raw provider
responses, whole user objects, and email addresses. There is no
`record(**anything)` — every writer is a named method with named fields.

`correlation_id` is present and is a *request* identifier, not a person's.

### Atomicity

The recorder joins the caller's transaction and never commits:

    async with unit_of_work:
        revoked = await assignments.revoke(...)
        await audit.record_administrator(...)

so the mutation and its entry commit together or roll back together. An
action with no entry and an entry with no action are equally useless to
somebody reconstructing what happened; atomicity makes both impossible
rather than unlikely. A refused action — `LastAdministrator`, `SelfGrant`,
`AlreadyGranted` — raises before anything is written and leaves no entry.

### Actions recorded today

| Action | Written by | Actor |
| --- | --- | --- |
| `admin.role.grant` | `AdminRoleService.grant` | the granting administrator |
| `admin.role.grant` | `AdminRoleService.bootstrap` | operator, no account |
| `admin.role.revoke` | `AdminRoleService.revoke` | the administrator named by `--by`, or operator |

`revoke` therefore takes `revoked_by` — required, and nullable only as the
explicit claim "an operator process". `python -m app.operator.admin revoke`
gained `--by`, verified to hold `ADMIN` for the same reason `grant`'s is.

`AuditOutcome.FAILED` exists and is deliberately unwritten in this phase: it
is the seam for auditing refused attempts, which is a policy decision
moderation will make rather than one to guess at now.

### API

    GET /api/v1/admin/audit

| Concern | Behaviour |
| --- | --- |
| Filters | `action`, `actor_id`, `subject_type` + `subject_ref` — every one index-backed |
| `subject_ref` alone | **`400`.** The index leads with `subject_type`, and a filter that quietly did nothing would show an operator the whole trail while they believed they were reading one account's history |
| Free-text search | **Absent.** The JSON columns vary in shape by action; a search over them would be unindexable and the first thing to become slow |
| Pagination | Keyset on `(created_at, id)`, default 25, max 50, no total count |
| Query shape | 2 statements per page — the page, then **one** batch resolving every account it names, actors and subjects together |
| Caching | `Cache-Control: no-store` |

### Console

`/audit` replaces its placeholder. Table above the breakpoint, cards below,
`Load more` on the cursor — the same shape as §6.8–§6.10.

**The server sends facts; the console composes the sentence.** "Sanjar
granted the admin role to Aziza" is assembled from `action`, `actor` and
`subject` in the operator's own language (uz/ru/en). A server returning the
sentence would put the platform's languages in the API — the same decision
A64-023 made for quick messages.

| Case | Rendering |
| --- | --- |
| Operator action | The word "operator", never a name |
| Erased account | The id it recorded, `username` absent — the trail outlives what it describes |
| Known subject type | A link: `account` → `/users/{ref}`, `match` → `/matches/{ref}`, `tournament` → `/tournaments/{ref}` |
| Unknown subject type | Plain text. A link built from an unrecognised type is a route that does not exist, and a dead link in an incident review is worse than none |
| Unknown action | Its raw identifier. The trail is older than the console reading it |

---

## 6.12 Moderation & Safety — A64-024.6

**The first admin surface that writes.** Every phase before it was read-only
because `admin.audit_entry` was unbuilt (§7); §6.11 built it, and these two
mutations are what it unblocked.

### The model, and why it is not `is_active`

`domain-model.md` §6 draws the two transitions separately —

    Active --> Suspended:    sanction applied
    Active --> Deactivated:  player-initiated

— and states the ownership rule outright: *"`admin` may request suspension
through a published port; **it never writes account rows**."* So moderation
does not touch `users.User.is_active`. Overloading it would make "did they
leave or were they removed" unanswerable, and would let a player's own
reactivation silently undo a moderator's decision.

What ships instead is what §13.2 and §13.3 specified and `database.md` §10.4
already had columns for:

| Table | Role | Notes |
| --- | --- | --- |
| `admin.moderation_case` | The decision | Written once, never updated. §13.2: an editable moderation record cannot be trusted in an appeal |
| `admin.sanction` | The enforcement | `case_id` **NOT NULL** — §13.3's "a sanction names the case that authorised it" |

Two tables rather than one because DM-12 keeps them apart: the sanction is a
hot authorization input read at every credential boundary, and the case is a
document written for humans.

**A case is created closed, by the action itself.** An administrator acting
directly is a decision-maker, so the case names them, their category and
their reasoning. What is deliberately **not** built: reports, evidence, a
case inbox, assignment, review, appeals, automated moderation. The table
exists now so that when reports arrive they attach to it — rather than to a
`NOT NULL` column that would need backfilling with fabricated rows.

### Kinds

`SanctionKind` has **one member**, `suspended`, and that is the honest count.
§13.3 names four; only one has an enforcement seam today.

| Kind | Status | Why |
| --- | --- | --- |
| `suspended` | **Shipped** | Authentication is withheld; the seams exist |
| `muted` | Deferred | Would need a guard on the quick-message surface, which has none. A kind nothing enforces is a restriction the console reports and the player never experiences |
| `matchmaking_restricted` | Deferred | Same — no queue-entry guard exists yet |
| `banned` | Deferred | §6 ties a permanent ban to erasure, which is DM-13's obligation and not this task's |

An **indefinite** suspension is already expressible: `expires_at` is null.
Indefinite is not permanent — a restore ends it.

### Reason taxonomy

Machine-readable identifiers; the console localises them (uz/ru/en).

| Category | Justified by |
| --- | --- |
| `cheating` | `fairplay` integrity signals (§13.1); IS-1 forbids automatic sanctions, so a human deciding on that evidence is exactly this |
| `abuse` | The quick-message surface (ADR-004) and the block graph |
| `account_compromise` | `auth` already has lockout and password reset; withholding access to a stolen account is protective |
| `policy_violation` | The bounded catch-all |
| `other` | The honest escape hatch — `reasoning` is required on every case, so it is not a hole |

**`harassment` and `spam` are deliberately absent.** There is no free-text
channel to distinguish them on and quick messages are already rate-limited;
three categories no evidence can tell apart would be three filled in at
random.

`reasoning` is **required**, plain text, bounded at 500 characters, stored on
the case and **never shown to the restricted account**. It is admin-private
operational context. The console's field says so, and the bound exists
because an unbounded administrative textarea is where pasted logs and tokens
end up.

### Temporary and indefinite

Both. The client sends a **duration**, never an instant — an absolute expiry
from a browser is subject to the operator's device clock, and a skewed one
silently ends a restriction at the wrong time. The server computes
`expires_at` against its own clock. Durations are bounded at one year; a
longer restriction is a second deliberate action.

**No scheduler, and none is needed.** §13.3: *"expiry is by instant,
evaluated at read time, never by a job that 'removes' sanctions — because a
job that fails leaves players banned."* Effectiveness is:

    lifted_at IS NULL AND starts_at <= now AND (expires_at IS NULL OR expires_at > now)

An expired sanction stops applying with no job having run, and its row stays:
history, not a deletion.

### Enforcement, and the bounded window

| Boundary | Behaviour |
| --- | --- |
| `POST /auth/login` (and browser login) | Refused with `AccountRestricted` → **403**, after a successful password verification |
| `POST /auth/browser/refresh`, `POST /auth/refresh` | Refused, and the presented session is revoked |
| Applying a restriction | `revoke_all_sessions(reason=SUSPENSION)` — SE-3 — **in the same transaction** |
| Every other authenticated request | **No new query.** No sanction lookup was added to `CurrentUser`, to game commands, or to any gateway frame |

**This is not "immediate enforcement", and calling it that would be wrong.**
Stated precisely:

- For refresh and session credentials, enforcement is **immediate**: every
  live session is revoked in the restricting transaction, and a rotation
  already in flight is refused at the next attempt.
- For an **already-issued access token**, enforcement is bounded by the
  access-token TTL. A token minted seconds before the restriction stays
  syntactically valid until it expires.

The alternative — a live sanction read on every authenticated request —
would put an indexed query on every game move and every gateway frame, which
§25 rules out and DM-12 does not ask for ("every sign-in, every message
send, every queue entry", not every request).

**Deferred and recorded as risk:** enforcement at the matchmaking-queue and
quick-message seams, which is where `muted` and `matchmaking_restricted`
will land.

### `is_active` and the deactivation gap

The audit for this phase found that `is_active=false` blocks **login only** —
no product surface and not the refresh path check it, so a deactivated
account could refresh indefinitely. That is a pre-existing gap in a
*different* concept and A64-024.6 did not merge the two semantics to close
it. It is recorded here as a known risk for a `users`/`auth` task; the
credential-boundary shape used for restrictions is the model for fixing it.

### Safety

| Rule | Behaviour |
| --- | --- |
| Self-restriction | **Refused** (`SelfSanction` → 422). §13.2 already forbids acting on a case involving oneself, and an administrator who can withhold their own access can lock out the operator |
| Last administrator | **Refused** (`ProtectedAdministrator` → 409). A suspended administrator cannot sign in, and unlike a role revocation there is no `bootstrap` to recover through |
| Target holds `admin` | Allowed **while another administrator exists**. The restriction does not revoke the role — a suspended administrator simply cannot authenticate, and their grant is still there when restored |
| Moderator role | **Not invented.** OQ-1 stays open; nothing here required a second role |
| Client-supplied actor | Impossible: the request models have no actor field and `extra="forbid"` |

### Idempotency and concurrency

| Case | Behaviour |
| --- | --- |
| Restrict an already-restricted account | **409**, not a silent success. A second `SUCCEEDED` audit row for a transition that did not happen would be worse than a missing one |
| Restore an unrestricted account | **409**, for the same reason |
| Two administrators restricting at once | `uq_sanction__live_kind` (partial unique on `lifted_at IS NULL`) resolves it to one row; the loser gets an integrity error, not a second live restriction |
| Restrict → restore → restrict | Allowed. The partial predicate frees the slot on lift, because that is ordinary history |

### Audit

Every successful mutation writes an `admin.audit_entry` **in the same
transaction** as the case, the sanction and the session revocation. All four
commit together or none does — asserted against a real database in
`tests/contract/test_admin_moderation.py`.

    admin.sanction.apply    before {restricted:false}
                            after  {restricted, kind, category, case_id,
                                    expires_at, sessions_revoked}
    admin.sanction.lift     before {restricted, kind, case_id, since}
                            after  {restricted:false, lifted_at}

**No `reasoning` in the trail**, deliberately: the entry records that a
decision was taken and *where it is written down* (`case_id`), not a second
copy of the prose in a table nobody may delete from. No `User` object, no
request body, no address.

### Failed-attempt audit policy — closing A64-024.8's open question

§6.11 shipped `AuditOutcome.FAILED` with no producers. The line is **who is
asking**:

| Category | Where it goes | Why |
| --- | --- | --- |
| Authenticated administrator refused by a domain safety rule (`self_restriction`, `last_administrator`, `already_restricted`, `not_restricted`) | **`FAILED` audit row** | Somebody trusted tried something the platform stopped — the fact an incident review needs. Bounded in volume: each requires a live admin session |
| Unauthenticated / non-admin / revoked-role request | **Application log only** | Attacker-controlled in volume; letting it append to an append-only table nobody may delete from is a denial-of-service disguised as diligence |
| Infrastructure failure | **Logs and metrics** | The transaction that would carry the entry is the one that failed |

The `FAILED` entry is written in its own transaction — correctly, because
there is no mutation for it to be atomic with. `refused` is a closed
identifier chosen server-side, never a message and never anything the
request supplied.

### API

    GET  /api/v1/admin/moderation?effective_only=&limit=&cursor=
    POST /api/v1/admin/users/{user_id}/restrict
    POST /api/v1/admin/users/{user_id}/restore

The mutations sit on the user's path because the target is an account and a
body field naming the subject would be one a caller could change
independently of the URL they were authorised against.

| Concern | Behaviour |
| --- | --- |
| Guard | `CurrentAdmin` on every route, asserted against the route table |
| Actor | The session's, never the payload's |
| Request models | Explicit, `extra="forbid"`, closed reason enum, bounded reasoning, bounded duration |
| Restore body | **None.** A restore ends the one live restriction; a second taxonomy for ending one would be read by nobody |
| Pagination | Keyset on `(created_at, id)`, default 25, max 50, no total count |
| Query shape | List: 3 statements — the page, one batch of cases, one batch of accounts |
| Caching | `Cache-Control: no-store` |
| Unknown account | `404` **before** anything is written — a moderation case about nobody is one nobody can review |

### Runtime cost

| Path | Added cost |
| --- | --- |
| Sign-in | +1 indexed read, after Argon2 verification — three orders of magnitude cheaper than the work already done, and never reached by a wrong password, so it is not an enumeration oracle |
| Refresh | +1 indexed read |
| Every other authenticated request | **0** |
| Game move, gateway frame, queue entry | **0** |

Backed by `ix_sanction__player_expiry` — `database.md` §12.6's design,
partial on `lifted_at IS NULL` because a partial index predicate must be
immutable and `now()` cannot appear in one.

### Console

`/moderation` replaces its placeholder: active restrictions, with an
"include history" toggle, links to each account, and the deciding
administrator. **No case queue** — Arena64 has no player reports, and an
inbox for a stream that does not exist would be empty by construction.

**The actions are on the account's page, not on the list.** An operator
should have read who somebody is before withholding their access; a control
on a list row is one applied to whichever row was under the cursor. No bulk
action exists.

Both actions confirm in a native `<dialog>` (`showModal`), which gives the
focus trap, `Escape` and page inertness from the browser rather than from a
dependency. The dialog names the target, states the consequence, and stays
open on refusal holding what was typed.

### Notification to the restricted account

**Deferred.** No product policy exists for it, and inventing one would mean
choosing what a restricted person is told — a product and legal decision
(`domain-model.md` Q-17 keeps it open). What they see today is the generic
403 message `AccountRestricted` carries. Nothing about the category, the
reasoning, the case or the deciding administrator reaches a client.

---

## 6.13 Notification Operations — A64-024.7

An operational view of the notification system, and **one** privileged
operation.

### What this is not, and cannot become

No composer, no broadcast, no campaign, no template, no segmentation, no
schedule, no recipient picker, no payload field, no arbitrary URL. There is
no endpoint on this platform that creates a notification from an
administrative request, and the console has no control that implies one.

That is not restraint for its own sake: a notification exists because a
**source event** happened, and its recipients are named by that event
(`specs/notifications.md`). An admin-composed notification would be a
durable row with no source event — unexplainable to the person who received
it and unattributable in the outbox.

### The authoritative model

| Record | Role |
| --- | --- |
| `notifications.notification` | The durable fact. Unique on `(recipient_id, source_event_id, type)` |
| `notifications.notification_push_delivery` | One row **per device**, PK `(notification_id, subscription_id)` |

The two are separate, and every property below follows from that: a retry
touches the delivery and can never produce a second notification.

### Delivery-state semantics — stated exactly

There is **no `DELIVERED` meaning the person saw it**, and the console never
says one. `domain.push_delivery` is explicit: `SENT` means a push service
accepted the request — "the device may be asleep, the browser closed, the
notification dismissed unread. Nothing downstream of this platform reports
back."

| Status | Meaning |
| --- | --- |
| `pending` | Owed. `next_attempt_at` says when |
| `sent` | A push service **accepted** the request. Rendered "accepted by the push service" |
| `skipped` | Deliberately not sent; the outcome says why |
| `failed` | Abandoned — a permanent rejection or the attempt limit |

Outcomes are the platform's own bounded labels, never a vendor's error text,
which is what makes them safe to render and safe on a metric.

### API

    GET  /api/v1/admin/notifications
    GET  /api/v1/admin/notifications/{notification_id}
    POST /api/v1/admin/notifications/{notification_id}/deliveries/{subscription_id}/retry

The mutation is spelled for the **delivery**. A route named
`/notifications/{id}/resend` would describe a capability this platform
deliberately does not have.

| Concern | Behaviour |
| --- | --- |
| Guard | `CurrentAdmin` on every route, asserted against the route table |
| Request body | **None on any route**, including the mutation — there is nothing to decide |
| Filters | `recipient_id`, `failed_push_only` — both index-backed |
| Pagination | Keyset on `(created_at, id)`, default 25, max 50, no total count |
| Query shape | List: 3 statements — the page, one delivery batch, one account batch |
| Caching | `Cache-Control: no-store` |

**Deliberately absent filters:** type, category and a time range. None has an
index on `notification`, so each would be a sequential scan that gets slower
every day. An operator's starting point is a person or a failure, and both
are offered. There is no free-text search and nothing to search — no rendered
text is stored, and the payload is typed JSON whose shape varies by type.

### Indexes added

Two, no columns and no data change:

| Index | Serves |
| --- | --- |
| `ix_notification__created_at_id` | The console's default page. `ix_notification__recipient_recent` leads with `recipient_id` and cannot order an unfiltered list |
| `ix_notification_push_delivery__failed` | `failed_push_only`. Partial on `failed` — the exact complement of `…__due`, which is partial on `pending` |

No `admin_retry` column and no new state: a retry is the existing row
returning to `pending`, which the delivery model already represents.

### The retry decision — the ten conditions

Retry ships, and only because every condition held. Each is structural
rather than checked:

| # | Condition | How it holds |
| --- | --- | --- |
| 1 | Targets an existing authoritative row | The port takes `(notification_id, subscription_id)` — the delivery's own PK |
| 2 | Recipient cannot change | `recipient_id` is on the row and is never written |
| 3 | Type/payload cannot change | The payload is derived from the notification at send time; nothing stores or accepts one |
| 4 | Target cannot change | It belongs to the notification, which is untouched |
| 5 | Bounded | See below |
| 6 | Permanent failures not retried | The `WHERE` admits one outcome |
| 7 | No second durable notification | The operation cannot reach the notification table |
| 8 | Duplicates prevented or acceptable | An exhausted delivery was never accepted by a push service, and the in-app row is untouched, so nothing duplicates in the inbox |
| 9 | Auditable | Same transaction as the update |
| 10 | Race-safe | A guarded `UPDATE`; terminal rows are invisible to the worker's claim |

### Exactly one outcome is retryable

`attempts_exhausted` — the push service was unreachable or erroring for the
whole retry curve. An outage, and the one case where one more attempt is the
right operator action.

| Refused | Why |
| --- | --- |
| `permanent_failure` | "Retrying is asking the same question and being told no again" |
| `subscription_gone` | The device is gone and that outcome revoked the subscription. Nowhere to send |
| `skipped_preference` | **The recipient muted this category.** A retry here would be an administrator overriding somebody's stated choice |
| every other `skipped_*` | Nothing failed |
| `pending` | Already owed; the worker has it |
| `sent` | Accepted. Nothing to retry |

The rule lives in the `UPDATE`'s `WHERE`, so a hand-made request gets the
same `409` the console's hidden button would have prevented.

### Bounded without a new counter

`attempt_count` is **not** reset. The worker's cap is applied *after* the
attempt it grants, so a re-armed row gets exactly **one** more real attempt
and returns to terminal by the existing mechanism.

And while it is `pending` it is no longer eligible — so a second retry is
refused until a worker has settled it. Repeated clicking is a conflict, not a
storm, and the conflict is the database's rather than a disabled button's.

### Preference interaction

**A retry cannot bypass a preference, and does not need a rule saying so.**
The worker reads the recipient's push preference at *send* time
(`specs/notifications.md` §14), so a re-armed row goes through the same check
as any other: a recipient who muted the category since the original attempt
gets `skipped_preference` rather than a push. The operation does not send
anything, so there is nothing for it to override.

### Concurrency

| Race | Resolution |
| --- | --- |
| Two administrators | The guarded `UPDATE` matches for one; the other changes nothing and gets `409` |
| Administrator vs worker | A terminal row is invisible to `claim_due`, so nothing can be in flight when the retry runs |
| Retry then worker | The claim spends the attempt the retry bought — asserted end to end against the real claim query |

### Audit

`notification.delivery.retry`, subject type `notification`, subject ref the
notification id. Written in the **same transaction** as the guarded update:
the re-armed delivery and its entry commit together or neither does.

Metadata is small and structured — the device id, the previous status, and
how many attempts were already spent. **No payload, no endpoint, no push
keys, no recipient profile.**

Refusals follow §6.12's policy unchanged and are not re-decided: an
authenticated administrator refused by the eligibility rule writes a `FAILED`
entry in its own transaction; anybody the guard rejected writes only a
security log.

### Sensitive-data boundary

Never exposed, and with no field to arrive in: the push endpoint, `p256dh`,
`auth`, any VAPID material, any provider response body, the notification
payload, the recipient's email. A device is described by three operational
timestamps — first seen, last seen, revoked — which answer "is this device
still real" and could not be replayed anywhere.

`source_event_id` **is** exposed, deliberately: "the event never fired" and
"the notification was never written" are different failures, and it is the
only field that tells them apart.

### Console

`/notifications` replaces its placeholder; `/notifications/{id}` shows every
device. Table above the breakpoint, cards below, `Load more` on the cursor.

The retry lives on the **detail**, beside the device it addresses, and
confirms in the same accessible `<dialog>` §6.12 introduced. The dialog names
the notification and the recipient and states the consequence — "the existing
delivery is queued once more; no new notification is created". There is no
payload field and no recipient field, because the API has neither.

Status is text, never colour alone. An unknown status or outcome renders as
its identifier rather than blank: the backend outlives the console reading
it.

### Observability

None added. The delivery worker's existing metrics already carry status and
outcome as bounded labels, and a per-notification or per-user counter would
be exactly the high-cardinality label those metrics were designed to avoid.
The retry is recorded where a privileged action belongs — the audit trail —
and in one `INFO` log line.

---

## 6.14 Dashboard & System Overview — A64-024.9

The console's home page: six facts, the things waiting for a person, and the
ten most recent privileged actions.

### What it is not, and the correction that matters

**Not a metrics system, and not a Grafana replacement — because there is no
Grafana to replace.** This repository has no Prometheus, StatsD or
OpenTelemetry collector; `specs/live-game/audit.md` §8 and
`specs/matchmaking/audit.md` §4 both state it plainly: *"Metrics are emitted
as structured log records."* They are consumed by whatever log pipeline a
deployment runs.

That makes the boundary sharper rather than softer. Recomputing a metric here
as a SQL aggregate would create a **second answer** to a question the log
pipeline already answers, and the two would disagree exactly when somebody
was relying on them. So this page reports facts the log stream does not: what
is true *right now* in the database, and what is waiting.

Also not built: charts, trends, percentages, date-range analytics, CPU or
memory, a logs viewer, an export.

### The facts, and why each one

| Card | Fact | Owner | Backed by |
| --- | --- | --- | --- |
| Accounts | Registrations in the last 24h and 7d | `users` | `ix_user__created_at_id` — a range scan bounded by the window |
| Matches | In play, awaiting acceptance | `game` | `ix_match__current_light`/`__current_dark`, partial on exactly those two statuses |
| Tournaments | Registration open, in progress | `tournament` | **No index** — see below |
| Attention | Restrictions in force | `admin` | `ix_sanction__player_expiry`, partial on `lifted_at IS NULL` |
| Attention | Push deliveries out of retries | `notifications` | `ix_notification_push_delivery__failed`, partial on `failed` |
| Activity | The ten most recent audit entries | `admin` | `ix_audit_entry__created_at_id` |

Every number is produced by the module that owns the data, through that
module's published administrative port. `admin` composes; it queries no other
module's tables.

### Deliberately omitted

| Tempting metric | Why not |
| --- | --- |
| **Users online** | Presence is one Redis key per player with a TTL (`presence:v1:<id>`) and no counter. Counting means a keyspace scan, and the published port deliberately offers per-player reads only. An inferred figure — active sessions, recent requests — would be a number an operator trusts and cannot check |
| **Total accounts** | An unbounded `COUNT(*)` that gets more expensive every day and answers nothing anybody acts on |
| Verified / unverified split | No index; a full scan for a figure with no action behind it |
| **Matches completed today** | The most interesting product signal and the most expensive: `game.match` is the platform's largest table and a partition candidate, and there is no index for it. Adding one to the hottest table so a dashboard can show a number is the trade `database.md` §12.2 exists to prevent |
| Health / readiness card | `/health/ready` pings PostgreSQL and **every** Redis pool. One operator opening a tab must not become a probe across every dependency; liveness belongs to the orchestrator that already asks |
| Trends, deltas, percentages | Nothing stores a prior period. A "+12%" composed from two numbers the platform never recorded together is a figure an operator acts on and cannot reproduce |

### Query budget

    accounts        1   two windows, one range scan with a FILTER
    matches         1   grouped, over a partial index
    tournaments     1   grouped
    restrictions    1   over a partial index
    push deliveries 1   over a partial index
    recent audit    1   bounded LIMIT 10
    actor names     1   one batch over users.public

**Seven reads, and the count does not move** — not with the account count,
not with the match history, not with the length of the audit trail. Asserted
by counting in `tests/unit/test_admin_dashboard.py`, including against ten
times the activity by ten times as many administrators.

The plans are asserted against a real database in
`tests/contract/test_admin_dashboard.py`, with `enable_seqscan = off` as a
penalty rather than a prohibition — a plan that still falls back to a scan is
one with no index behind it, whatever the row count.

### The one accepted scan

`tournaments.tournament` has no index on `status`, and none was added.

The table holds one row per tournament ever created, and tournaments are
created by operators rather than by traffic: it grows by a handful of rows a
day, not by thousands. The scan is measured in microseconds and stays that
way for years.

**The threshold is written down rather than left to be discovered:** at
roughly a hundred thousand rows the scan is no longer free, and the fix is
one partial index on the two live statuses — the same shape
`ix_match__current_*` already uses. Adding it now would be an index guessed
at rather than measured, which §11 of this task rules out. A contract test
asserts that this is the *only* scan, so a second one cannot arrive quietly.

**No migration was needed for A64-024.9.** Every other read rides an index
that already existed for its own reason.

### Time windows

A day and a week for registrations — "is it happening today" and "is today
unusual". Both are computed from the service's clock and passed into the
port, so the answer is reproducible in a test and explicable to an operator;
neither `now()` in SQL nor the browser's clock is involved.

The activity list has **no window**. It is the newest ten whenever they
happened, because a quiet week must not empty it — an operator glancing at
the page wants to know what the last thing that happened *was*.

### Attention items

Two, and both are states the product itself defines as actionable rather than
thresholds invented here:

- **Restrictions in force** — somebody currently unable to sign in (§6.12).
- **Push deliveries out of retries** — `attempts_exhausted`, the one push
  state A64-024.7 built an operator action for. `permanent_failure` and every
  `skipped_*` are deliberately **not** summed in: a combined "failures"
  figure would invite action on a number most of which needs none.

When both are zero the section still renders and says so. A section that
vanished would leave an operator unable to tell "nothing to do" from "that
part did not load".

### Freshness

Fetched on mount and on an explicit **Refresh**. No polling: a page that
reissued six aggregate reads every few seconds would cost more than
everything it reports on, and nothing here changes fast enough to warrant it.

The server sends `generated_at` and the page shows it, because a figure with
no age invites trust it has not earned.

**A failed refresh keeps the numbers already on screen** and says the refresh
failed. Replacing known-good data with zeros would invent an all-clear.

### Partial failure

The endpoint is **atomic**. All seven reads share the request's session; if
one fails the request fails and the console shows an error.

`0` on this page always means zero. It never means "that query did not
answer" — a typed partial-success shape would have to be rendered and
reasoned about everywhere, and the honest failure is cheaper than a page an
operator learns to distrust.

### Drill-down

Every figure is a link, and no card carries an action — §15. A retry beside a
failure count is one clicked without reading which failure it was.

| From | To |
| --- | --- |
| Matches | `/matches?status=active` |
| Tournaments | `/tournaments?status=in_progress` |
| Restrictions | `/moderation` |
| Push deliveries | `/notifications?failed=true` |
| Activity | `/audit` |

Each parameter is one the destination's own `validateSearch` declares. The
notifications link is asserted by navigating rather than by reading its
`href`: the router quotes a search value that would otherwise parse back as a
boolean, so the URL reads `failed=%22true%22` while the page receives the
string it declared.

### Privacy

The dashboard shows **less** than the pages it links to. No email, no
session, no token, no push endpoint, no notification payload — and no audit
`before`/`after` metadata, because a glance surface has no business carrying
what a reviewer opens `/audit` for. `CurrentAdmin` on the route,
`Cache-Control: no-store` on the response.

### Audit vocabulary

The activity list reuses the audit console's action labels rather than
duplicating them (`features/audit/vocabulary.ts`, extracted for this
purpose). Extracting it closed a gap: A64-024.6's and A64-024.7's actions had
never been added to the map and were rendering as raw identifiers on `/audit`
— readable, by the fallback that exists for exactly that reason, but not
localised. They are now.

---

## 7. The audit invariant

**Built by A64-024.8** — see §6.11. The rule it exists to keep:

> **Every security-sensitive admin mutation must be attributable to an
> authenticated admin actor.**

A64-024.1 kept it without a framework: `granted_by` is a column, `grant`
refuses without one, `bootstrap` is a separate method so the unattributed
path cannot be reached by code that merely forgot, and both writes log at
`INFO`. No fake audit events were invented to check a box.

A64-024.8 makes it a record rather than a convention. `admin.audit_entry`
is append-only in the database, the entry is written in the same
transaction as the mutation, and the actor is the identity the guard
resolved — so attribution cannot be forgotten, edited or supplied by the
party being audited.

## 8. Deferred to later A64-024.x tasks

Analytics, infrastructure monitoring, anti-cheat surfaces, support tooling,
and the final visual design. Reads ship in §6.8–§6.11; account restrictions
ship in §6.12; notification operations ship in §6.13; the operator overview
ships in §6.14.

Within notifications specifically, deliberately unsupported and not deferred:
administrative sending of any kind — broadcast, campaign, template,
segmentation, scheduled or one-off. A notification exists because a source
event named its recipients; an admin-composed one would have none.

Within moderation specifically, deferred and named: player reports and a
case queue, evidence, appeals, the `muted` and `matchmaking_restricted`
kinds and their enforcement seams, bans tied to erasure, bulk actions, and
IP or device restrictions.

Match and tournament mutations are no longer blocked on `admin.audit_entry`
— it exists, and §6.12 demonstrates the pattern. What each still needs is
its own decision about *what* to record, which is `AuditAction`'s to
extend.

Their **routes exist and are guarded**; only their content is deferred, so
adding a real section later changes a page body and not the boundary.

## 9. Open questions

| # | Question | Status |
| --- | --- | --- |
| OQ-1 | Does a narrower `moderator` role exist, and what does it exclude? | **Open** — waiting on the first surface that distinguishes them |
| ~~OQ-2~~ | ~~Where does `apps/admin` deploy?~~ | **Closed by A64-024.2H.** `https://admin.arena64.gg`, same-origin with its own `/api` reverse proxy, listed in `BROWSER_SESSION_TRUSTED_ORIGINS`, no shared `Domain` cookie, no CORS. Network allowlisting is recorded as a production-hardening recommendation rather than a requirement (§6.3a) |
| ~~OQ-3~~ | ~~Does the console need its own sign-in?~~ | **Closed by A64-024.2.** It has its own `/login`, posting ordinary credentials to the shared auth endpoints from its own origin — so the session is the console's without a second credential system |
