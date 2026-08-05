# Feature Specification — Frontend Foundation

| Field | Value |
| --- | --- |
| **Spec ID** | `SPEC-FRONTEND` |
| **Status** | Approved through A64-020.4 — foundation, authentication, profile and social |
| **Owner** | _Unassigned_ |
| **Created** | 2026-08-05 |
| **Last updated** | 2026-08-05 — A64-020.4, social UI |
| **Related ADRs** | [`ADR-002`](../docs/07-decisions/ADR-002-frontend-spa.md) |
| **Related specs** | [`rating.md`](./rating.md), [`leaderboard.md`](./leaderboard.md), [`tournament.md`](./tournament.md) |
| **Related** | `docs/01-architecture/architecture.md` §5, `docs/04-frontend/` |

---

## 1. Summary

`apps/web` is a **React 19 single-page application built with Vite**, served as static
assets and talking to `apps/api` over HTTP and (later) one WebSocket.

A64-020.1 builds the foundation and **no business UI**: no sign-in, no profile, no game,
no tournament screen. What it delivers is the architecture every later phase builds on —
layers with an enforced dependency direction, the provider graph, routing, the API layer,
the theme, error and loading infrastructure, and the test harness.

## 2. Stack

| Concern | Choice |
| --- | --- |
| Framework | React 19 |
| Language | TypeScript, `strict` plus `noUncheckedIndexedAccess`, `noUnusedLocals`, `noUnusedParameters` |
| Build | Vite 8 |
| Routing | TanStack Router (code-based) |
| Server state | TanStack Query |
| Global client state | React Context — **only** |
| Component state | `useState` |
| Forms | React Hook Form + Zod |
| HTTP | Axios |
| Styling | Tailwind CSS v4 (CSS-first) |
| Components | shadcn/ui over Radix primitives, Lucide icons |
| i18n | A typed dictionary context — `shared/i18n`, uz/ru/en (§12.7) |
| Unit tests | Vitest + Testing Library + MSW |
| End-to-end | Playwright |

**No Redux, MobX or Zustand.** Server state is TanStack Query's; the only genuinely global
client state today is the theme, which is one value that changes on a click. A store would
add a dependency and a subscription mechanism to solve a problem this app does not have.

### 2.1 Why Vite and not Next.js — a replacement, not a new project

`apps/web` was a Next.js App Router application before A64-020.1. It was converted **in
place**: one project, one `package.json`, git history preserved through `git mv` where a
file survived. There is no second frontend and no nested application.

The consequences are recorded honestly in [`ADR-002`](../docs/07-decisions/ADR-002-frontend-spa.md)
and in §13 below — the two that mattered were token storage and i18n, and A64-020.2
resolved the first and half of the second.

## 3. Layers

```
shared  <-  entities  <-  features  <-  widgets  <-  pages  <-  app
```

A layer may import **strictly lower** layers and nothing else. This is enforced by
`import/no-restricted-paths` in `apps/web/eslint.config.mjs`, generated from one `LAYERS`
array, and it fails `npm run lint`. It is the frontend's counterpart to `apps/api`'s 27
`import-linter` contracts, and it exists for the same reason: a dependency direction that
is only written down is a dependency direction that drifts.

| Layer | Holds | Today |
| --- | --- | --- |
| `shared/` | Framework-level building blocks with no business meaning: `api/`, `config/`, `lib/`, `theme/`, `ui/`, `test/` | Full |
| `entities/` | Business nouns — a player, a tournament — with their shapes and queries | `session/`, `user/`, `profile/`, `relationship/` — aliases over generated types |
| `features/` | One user-facing capability, self-contained | `auth/`, `profile/`, `social/`, `avatar/`, `preferences/`, `privacy/`, `form-demo/` |
| `widgets/` | Composite blocks a page arranges | `app-shell/`, `auth-shell/`, `settings-shell/`, `session-menu/`, `profile-header/`, `rating-cards/`, `statistics-panel/`, `tournament-history/`, `player-row/`, `social-nav/`, `theme-toggle/` |
| `pages/` | One route's screen | `home/`, five auth pages, `profile/`, `public-profile/`, four `settings-*`, four social pages, `not-found/`, `unexpected-error/` |
| `app/` | Composition: providers, router, entry, global styles | Full |

**This is FSD-shaped, not FSD.** There are no slice/segment conventions, no public-API
`index.ts` per slice, and no cross-import rules beyond the layer direction above. The
direction is the part that is load-bearing; the ceremony is not.

## 4. Routing

TanStack Router, **code-based** (`src/app/router/routes.tsx`), not file-based.

| Route | Component | Guard |
| --- | --- | --- |
| `/` | `pages/home` | none |
| `/login` | `pages/login` | `RequireAnonymous` |
| `/register` | `pages/register` | `RequireAnonymous` |
| `/verify-email?token=` | `pages/verify-email` | none — see below |
| `/forgot-password` | `pages/forgot-password` | none |
| `/reset-password?token=` | `pages/reset-password` | none — see below |
| `/profile` | `pages/profile` | **`RequireAuth`** |
| `/players/$username` | `pages/public-profile` | none — public |
| `/settings/profile` | `pages/settings-profile` | `RequireAuth` |
| `/settings/preferences` | `pages/settings-preferences` | `RequireAuth` |
| `/settings/privacy` | `pages/settings-privacy` | `RequireAuth` |
| `/settings/sessions` | `pages/settings-sessions` | `RequireAuth` |
| `/friends` | `pages/friends` | `RequireAuth` |
| `/friends/requests` | `pages/friend-requests` | `RequireAuth` |
| `/friends/blocked` | `pages/blocked` | `RequireAuth` |
| `/search` | `pages/search` | `RequireAuth` |
| `/play` | `pages/play` | `RequireAuth` |
| `/games/$matchId` | `pages/game-ready` | `RequireAuth` |
| *anything unmatched* | `pages/not-found` | The root route's `notFoundComponent` |

The three link-landing pages are **deliberately unguarded**. A signed-in
player can legitimately be verifying a new address or following a reset link
requested from another device; bouncing them home would strand a one-time
token they cannot easily re-request.

Both guards redirect from an **effect**, never from `<Navigate>`. A
`<Navigate to="/login" search={{ next }} />` navigates from an effect whose
dependency is the options object, and `{ next }` is a new object on every
render — so it re-navigates, re-renders, and locks the tab. That is not
hypothetical: A64-020.3's first draft did exactly this and the profile test
exhausted the worker's heap before a single assertion ran. A guard mid-redirect
renders `null`, because showing the protected page for one frame is a flash
of somebody else's screen.

**There is no literal `/404` route.** `notFoundComponent` catches unmatched paths at any
depth, and the address bar keeps the URL that was wrong — a redirect would discard it
along with the user's ability to see their own typo.

No authentication, profile, game or tournament route exists. Each belongs to the phase
that ships the screen behind it.

**Lazy by default.** Every page is `lazyRouteComponent(() => import(...))`, so route-level
code splitting works at one route and still works at fifty. The root route carries a
`pendingComponent` with a 100 ms delay, so a fast chunk never flashes a spinner.

`createAppRouter(history?)` is a **factory**. A router owns a history, and a shared
instance would make one test's navigation another's starting URL.

## 5. Providers

```
App
 └─ AppProviders
      └─ ErrorBoundary            outermost — a boundary cannot catch from above itself
           └─ ThemeProvider       the error page must be themed too
                └─ I18nProvider   ...and translated
                     └─ QueryClientProvider
                          └─ SessionProvider
                               └─ RouterProvider
                                    └─ AppShell   (root route component)
                                         └─ Page
```

| Provider | Defined in | Why it sits where it does |
| --- | --- | --- |
| `ErrorBoundary` | `shared/ui/error-boundary.tsx` | Inside the router it could not catch a router failure, which would render as a blank document |
| `ThemeProvider` | `shared/theme/theme-context.tsx` | Below the boundary so a theme throw is caught; above the fallback so the error page is themed |
| `I18nProvider` | `shared/i18n` | Below the theme, above everything that renders text — the error page included |
| `QueryClientProvider` | `@tanstack/react-query` | **Above** `SessionProvider`, which calls `useQueryClient` to clear the cache on sign-out |
| `SessionProvider` | `features/auth/model` | Below the cache it clears, above everything that reads the session |
| `RouterProvider` | `@tanstack/react-router` | Inside every cross-cutting concern it depends on |

`ThemeProvider` lives in `shared/` rather than `app/` because a **widget** consumes its
hook, and a widget may not import `app/`.

**No dead providers.** `src/app/App.test.tsx` asserts this twice: structurally, that every
provider is named by the composition root, and functionally, that a probe rendered as an
ordinary child can consume each context — because naming a provider is not the same as
being nested under it.

## 6. API layer

`src/shared/api/`, and nothing above it may reach past its `index.ts` into the Axios
instance.

| Module | Responsibility |
| --- | --- |
| `client.ts` | The one Axios instance: base URL, 15 s timeout, correlation-id interceptor, and the two auth extension points |
| `request.ts` | `request`/`api.*` — unwraps `{data, meta}`, normalises every failure |
| `errors.ts` | `ApiError` and `normalizeError` — four failure kinds, one type |
| `types.ts` | Envelope, error body, pagination, `ErrorCode` |
| `query-client.ts` | The cache policy and the global error handler |
| `query-keys.ts` | The hierarchical key factory |
| `generated/schema.d.ts` | **Generated** from the backend's OpenAPI document |

### 6.1 Failure taxonomy

Every thrown value leaves `request` as an `ApiError` with a `kind`:

| `kind` | Means | `isRetryable` |
| --- | --- | --- |
| `http` | The API answered with a status and usually a coded body | `5xx` and `429` only |
| `network` | Nothing answered — offline, DNS, CORS, timeout | Yes |
| `canceled` | The caller aborted it. Not a fault, never reported | No |
| `unknown` | Not a request failure at all. A defect in this app | No |

`cause` is preserved on every path.

### 6.2 Authentication — an extension point, not an implementation

`withAuthorization(source)` and `onUnauthorized(handler)` are the seams `shared/api` exposes.
A64-020.2 does **not** use them: authentication needs the token *and* a single-flight
refresh together, so `features/auth` installs its own interceptor pair over the same client
(§12.4). The seams remain for a consumer that needs only one half.

**Nothing in this app reads or writes `localStorage` or `sessionStorage` for a credential** —
§12.1.

### 6.3 Generated types, and the one hand-written exception

Endpoint payloads are **generated** — `npm run openapi:generate`, output committed to
`shared/api/generated/schema.d.ts`. No hand-written DTO may duplicate one.

The envelope, the error body and the two pagination shapes are hand-written, because
FastAPI describes `ApiResponse[T]` per-endpoint and the error body comes from an exception
handler, so neither has a stable generated name. `ErrorCode` mirrors
`app/core/error_codes.py`. Four small types; the rule for anything larger is: generate it.

## 7. Query policy

Every default is argued from what Arena64's data does. A query whose volatility differs
overrides these at the call site.

| Option | Value | Why |
| --- | --- | --- |
| `staleTime` | 30 s | Zero — the library default — refetches on every mount, so navigating back re-requests a ladder that cannot have moved. Arena64's reads change on the scale of a finished game. Truly live surfaces do not poll at all; they arrive over the WebSocket (AD-11) |
| `gcTime` | 15 min | Must exceed `staleTime` comfortably, or a back-navigation finds nothing and renders a spinner instead of revalidating stale content. Roughly a session's attention span |
| `retry` | typed predicate, max 2 | A 404, 422 or 401 fails identically however many times it is sent; retrying turns one visible failure into three (CLAUDE.md §9.10). Network faults and `5xx`/`429` only, exponential backoff capped at 30 s |
| `refetchOnWindowFocus` | `true` | A player alt-tabs mid-tournament; a bracket minutes old on return is worse than a request nobody noticed. `staleTime` already prevents a refetch storm |
| `refetchOnReconnect` | `true` | Coming back online is the moment the cache is most likely wrong |
| `mutations.retry` | `false` | This app cannot know which endpoints are idempotent. Retrying a blind `POST` is how a player enters a tournament twice |

**Global error handler.** `QueryCache`/`MutationCache` `onError` route every failure to
`shared/lib/report-error.ts`. It **observes and never swallows** — a `useQuery` still
returns its error. Cancelled requests are not reported.

`createQueryClient()` is a factory for the same reason the router is.

## 8. Theme

Light / dark / system, persisted, as a React Context.

**Written in two places, deliberately.** The inline script in `index.html` sets the `.dark`
class and `color-scheme` before the first paint — the only way to avoid a visible flash of
the wrong theme on reload. `shared/theme/theme-context.tsx` keeps the DOM in step
afterwards and owns the user's choice.

The two agree on the storage key (`theme`) and the class (`dark`), and
`src/shared/theme/theme.test.tsx` asserts that they still do — a drift there presents as
"it forgets my setting sometimes" and nothing functional would catch it.

`system` tracks `prefers-color-scheme` **live**, so a scheduled night shift takes effect
without a reload. Storage failure (private mode) degrades to a session-only preference,
never to a failed click.

## 9. Error, loading and forms

| Concern | Where |
| --- | --- |
| Error boundary | `shared/ui/error-boundary.tsx` — the only class component, because React exposes no hook equivalent |
| Unexpected-error page | `pages/unexpected-error` — user-safe message plus `reset`; the detail went to `reportError` |
| 404 page | `pages/not-found` |
| Spinner | `shared/ui/spinner.tsx` — `role="status"` with a label, or it is silence |
| Skeleton | `shared/ui/skeleton.tsx` |
| Suspense boundary | The root route's `pendingComponent` |
| Forms | `features/form-demo` — one schema is both the rule and the inferred type |

A boundary catches render-time throws only; it never sees event handlers or rejected
promises. That is why `shared/api` throws typed errors a caller handles rather than
relying on a boundary as the asynchronous safety net.

## 10. Testing and accessibility

**Deliberately few and deliberately architectural**: the failures worth catching are wiring
failures, not rendering ones. Each phase adds at most eight.

| Test | Asserts |
| --- | --- |
| `app/App.test.tsx` ×3 | The lazy route renders inside the shell's landmarks; an unknown path renders 404 with a way back; **every provider is reachable** — structurally and functionally |
| `app/providers/providers.test.tsx` | The boundary reports *and* renders *and* recovers, and leaks nothing internal |
| `shared/theme/theme.test.tsx` | The chosen mode reaches the DOM and `localStorage`, under the key the pre-paint script reads |
| `shared/api/api.test.tsx` ×2 | The envelope is unwrapped through a real query and the documented policy is in force; all four failure kinds normalise, with the right retryability |
| `tests/e2e/shell.spec.ts` | The built app boots, ships more than one chunk, is keyboard-reachable via the skip link, and 404s at the wrong path |
| `features/auth/auth.test.tsx` ×5 | A64-020.2 — bootstrap's three outcomes, single-flight refresh, the login form, sign-out, and `next` validation |
| `tests/e2e/auth.spec.ts` | A64-020.2 — a real browser keeps a real `HttpOnly` cookie across a reload and loses it on sign-out |
| `features/profile/profile.test.tsx` ×7 | A64-020.3 — `RequireAuth` on a real route, the self profile's three fixed requests, privacy omission and not-found, dirty-state editing with accessibility assertions, avatar size refusal, a privacy round trip, and cursor paging with no per-row fetch |
| `tests/e2e/profile.spec.ts` | A64-020.3 — an edit reaches the database and appears on the **public** page, which is a different endpoint with no shared cache |

### 10.1 E2E accounts are seeded, because authentication is rate-limited

Two limits bound the suite, and both are production behaviour that must stay on:

    register   3 per IP per hour
    login      5 per IP per 15 minutes

A64-020.3 registered a fresh account per run and became unrunnable on the fourth. A social
suite needs two accounts and would have hit the register cap on its second run and the
login cap on its first.

So `tests/e2e/global-setup.ts` seeds three fixed accounts **once**, before any worker
starts, and saves each one's **browser session** (`test-results/.auth/*.json`, git-ignored
— they carry a refresh cookie). Specs load them as `storageState` and never sign in.

| Account | Cost per run |
| --- | --- |
| `e2e_social_alice`, `e2e_social_bob` | **0** — their sessions survive, probed and rotated in setup |
| `e2e_profile_owner` | **1 login** — that spec asserts "sign out everywhere", which revokes its own session by design |
| `auth.spec.ts` | **1 registration** — registration is its subject |

So a run costs one login and one registration, and the binding limit is three registrations
per hour: roughly **three full runs an hour** from one IP. Not zero, and stated rather than
discovered.

**The accounts accumulate state.** A friendship from one run is still there on the next, so
`resetRelationship` returns the pair to strangers through the same endpoints a player uses
— never a truncation — and the profile spec edits a value that is unique per run rather
than a fixed one that would leave the form clean.

What was deliberately not done: disabling the rate limit (a suite that only passes without
it never exercises it), clearing Redis from a spec (a frontend test reaching into backend
infrastructure is a hole, not a fixture), and skipping silently when seeding fails.

**MSW intercepts the network, never a mocked module** — stubbing `shared/api` would prove
a component calls a function; intercepting the request proves the whole graph.
`onUnhandledRequest: "error"`, so a request no handler expected is a loud failure rather
than a silent trip to the real network.

Accessibility, at foundation level: landmark elements in the shell, a skip link as the
first focusable element, `focus-visible` rings (never `focus`), `aria-invalid` +
`aria-describedby` on rejected fields, `aria-pressed` on the theme toggles, `role="status"`
on the spinner, `role="alert"` on the error page. Radix's dialog behaviour — focus trap,
focus return, `Escape`, `aria-modal` — is **not overridden**.

## 11. Deployment contract — one origin

**The page and the API must share an origin.** Not a preference: the refresh
token lives in an `HttpOnly`, `SameSite=Lax` cookie, and a cross-origin API
would either never receive it or would need `SameSite=None`, which is
precisely the CSRF exposure the cookie exists to close.

```
browser  ──/api/*──>  reverse proxy  ──>  FastAPI
         ──/*─────>  static assets (this app's build output)
```

| Environment | How the origin is shared |
| --- | --- |
| Development | The Vite dev server proxies `/api` **and** `/ws` to `ARENA64_API_TARGET` (default `http://localhost:8000`) — `vite.config.ts` |
| E2E | `vite preview` uses the **same** proxy config; `preview.proxy` does not inherit `server.proxy`, so both are declared |
| Production | A reverse proxy (nginx, Caddy, a CDN rule) must route `/api` to FastAPI **and `/ws` with WebSocket upgrade**. **This is a deployment requirement, not a default** |

**`/ws` is a second rule, not a sub-path of `/api`** — A64-020.5A §20. The
gateway is mounted at the application root and is unversioned
(`app/app_factory.py`), so an `/api` rule does not reach it. Two properties
must hold and neither is a default:

  - `ws: true`, or the proxy forwards the HTTP request and drops the
    `Upgrade` handshake — which presents as a socket that connects and
    closes immediately with nothing in any log to act on
  - **same origin**, for the reason `/api` is: the socket carries the same
    cookies and the same `Origin` the API already checks

The proxy is configured ahead of the client that uses it. Authentication is
not a proxy concern: the gateway takes a one-time ticket from
`POST /auth/ws-ticket` as a query parameter, which A64-020.5B wires.

`VITE_API_URL` defaults to the relative `/api/v1` and **no code names a
host**. An absolute URL is accepted for the one case that needs it —
pointing a build at a separate API — but the browser session will not work
there, because the cookie will not cross origins.

`changeOrigin: false` on the proxy, deliberately: the backend's CSRF check
reads `Origin`, and rewriting it would make every development request claim
to come from the API's own host, exercising a check production applies
differently.

## 12. Authentication — A64-020.2

### 12.1 Where each credential lives

| Credential | Where | Lifetime | Readable by script |
| --- | --- | --- | --- |
| Access token | **JavaScript memory only** — a closure in `session-store.ts` | ~15 min | Yes, by design — it is cheap to lose |
| Refresh token | **`HttpOnly` cookie**, `Path=/api/v1/auth/browser`, `SameSite=Lax`, `Secure` outside local | 30 days | **No** |

**Nothing is written to `localStorage` or `sessionStorage`.** A reload
therefore starts with no token, which is correct: the app asks the cookie
for a new one rather than trusting something it found in storage.

This closes OQ-1. The approved F-1 design assumed a Next.js Route Handler
to set the cookie; `apps/api` sets it instead, on a browser-specific
surface, and the same-origin contract in §11 is what makes that equivalent.

### 12.2 The backend surface

`POST /api/v1/auth/browser/{register,login,refresh,logout,logout-all}` —
`SPEC-AUTH`. The JSON endpoints are unchanged and remain the contract for
non-browser clients. No browser response body carries a refresh token.

### 12.3 Session bootstrap

On load: call `refresh`. Three outcomes, and the third is the one usually
got wrong.

| Result | State | Why |
| --- | --- | --- |
| `200` | `authenticated` | Token in memory, user from the same response |
| `401` | `anonymous` | There was no session. A fact |
| network / `5xx` | **`unavailable`** | The server was not reached, so nothing was established. Rendering "signed out" here discards a live session over one failed request |

Guarded by a ref so React Strict Mode's double effect cannot fire two
refreshes — which, because the backend rotates on every use, would revoke
the session.

### 12.4 Single-flight refresh

The backend rotates the refresh token on every use and revokes the whole
chain when a superseded one is presented. A page issuing five parallel
requests that each `401` would, naively, fire five refreshes and sign the
user out of their own app.

So exactly one refresh runs: `inFlight` holds its promise, every concurrent
`401` awaits it, and each original request retries **once** (`_retried` on
the config). The refresh call itself is excluded from interception, or it
would refresh in order to refresh.

### 12.5 CSRF

Two layers, because one is a promise somebody else makes.

| Layer | What it stops | Where |
| --- | --- | --- |
| `SameSite=Lax` | The browser attaching the cookie to a cross-site `POST` | `BrowserSessionSettings` |
| `Origin`/`Referer` allowlist | The same, for clients that do not enforce SameSite | `auth/presentation/browser_csrf.py` |

All state-changing browser auth endpoints are `POST`. The origin is rebuilt
from parsed parts, so `https://arena64.uz.evil.com` cannot pass a check a
prefix match would allow. The allowlist is empty in `local`/`test` — the
proxy makes the app same-origin — and `Settings` **refuses to start** a
deployed tier without one.

Bearer-authenticated API calls need none of this: an attacker's page cannot
read our memory, so a forged request simply arrives unauthenticated.

### 12.6 Multi-tab

`BroadcastChannel("arena64-auth")`, carrying exactly one message:
`{ type: "logged_out" }`. **Never a token** — any script on the origin can
read a named channel, so a credential there has the security properties of
a global variable.

Signing out in one tab clears memory in all of them, runs each tab's
registered cleanups (`onSessionEnded` — the seam the WebSocket will use),
and removes the private query cache. `session_refreshed` is deliberately
not a message: the only useful thing to send with it is the new access
token, which is the one thing that cannot be sent.

### 12.7 Localisation

`shared/i18n` — a typed dictionary context over `locales/{uz,ru,en}.json`.
`TranslationKey` is derived from the Uzbek file and the other two are typed
against it, so a key present in one and missing from another is a **compile
error** rather than a string that renders as itself.

Backend failures map to keys by **error code**, never by message text —
`features/auth/model/error-messages.ts`. The table is bounded to the codes
an auth screen can produce; anything else falls through to `unexpected`.
`invalid_credentials` is one message for both "no such account" and "wrong
password", because the backend returns one code for both on purpose.

This partially closes OQ-2 — see the revised entry in §11.

### 12.8 Safe redirects

`?next=` is validated by `safeRedirect`, not sanitised. Rejected: absolute
URLs, protocol-relative `//host`, backslash forms, schemes, whitespace and
control characters, malformed percent-encoding, relative paths, and the
auth pages themselves. Anything not an in-app absolute path becomes `/`.

An unvalidated `next` is an open redirect, and an open redirect on a real
login page is among the most effective phishing primitives there is —
every visible signal up to the final hop is genuine.

## 13. Profile — A64-020.3

### 13.1 Two profile surfaces, never one cache

| Read | Returns | Cached under |
| --- | --- | --- |
| `GET /profile/me` | The signed-in account, **unfiltered**, with statistics and **without ratings** | `["profile","me"]` |
| `GET /profiles/{username}` | Anybody's account, **privacy-filtered**, with ratings | `["profile","public",username]` |

Disjoint prefixes, deliberately. Under one key a self-profile fetch would populate what a
public page later reads, and the public page would render fields the viewer is not entitled
to — a silent leak that only shows up when somebody views their own public profile after
their own settings.

### 13.2 Query keys and what each mutation invalidates

`features/profile/api/keys.ts`. Blanket invalidation is the easy answer and the wrong one:
it refetches a tournament history because somebody changed their bio.

| Mutation | Invalidates |
| --- | --- |
| `PATCH /profile` | `me` (written from the response), every public profile |
| Avatar upload / delete | `me`, every public profile |
| `PATCH /profile/privacy` | **public profiles only** — `/profile/me` is unfiltered and cannot change |
| `PATCH /profile/preferences` | its own key; nothing else reads it |

Nothing is optimistic. The server normalises what it stores, so an optimistic write would
show a value that is about to change.

### 13.3 Requests per page

| Page | Requests | Grows with |
| --- | --- | --- |
| `/profile` | 3 — `me`, `ratings/me`, first history page | nothing |
| `/players/$username` | 3 — profile, ratings, first history page | nothing |
| History "load more" | 1 per page | pages, never rows |

`MyProfileResponse` carries statistics but **not** ratings; `ProfileResponse` carries both.
That asymmetry is the API's, and one extra request is the honest response to it rather than
a fabricated shape. Three components read the one `me` query rather than fetching per
section.

### 13.4 Privacy — the server decides, the client renders

`country`, `is_online`, `last_seen` and `statistics` are **omitted** from a public profile
when hidden. Every one is rendered conditionally on its presence and none is defaulted:
filling in "Offline" for an absent `is_online` would publish a fact the owner withheld.

The controls read `last_seen`, `online_status` and `activity` — the three-valued fields.
`show_last_seen`, `show_online_status` and `show_activity` are **deprecated** and are each
`true` only when their counterpart is `everyone`, so reading them would collapse a
friends-only setting to "off". `activity` is stored and published nowhere, so no control
offers it.

**Ratings are never hidden**: they are what pairing is computed from, and privacy does not
cover them.

### 13.5 Avatar

`POST`/`DELETE /profile/avatar`, multipart. Client checks mirror the API's own limits —
5 MB, `image/jpeg|png|webp` — to fail in milliseconds instead of after a five-megabyte
upload. They are **not** the guarantee: `file.type` is the browser's guess and is trivially
forged, and the server sniffs the bytes.

The preview is an **object URL**, revoked when it is replaced or the upload settles. No
base64 in state. Each upload gets an `AbortController`. `avatar_version` from the response
is appended as a query parameter so a replaced image is actually seen — the URL is
otherwise identical and a cached browser keeps the old picture. Removal goes through a
Radix dialog: focus trap, focus return, `Escape`.

No object key or storage internal reaches the client — the API returns URLs.

### 13.6 Ratings and statistics

Ratings from `GET /ratings/me` and `GET /players/{id}/ratings` (A64-020.0A). Rendered:
rating, deviation, games played, provisional, variant, speed class. **`volatility` is not
published** and the API does not return it.

A category with zero games says "not rated yet" rather than presenting the starting value as
a measurement — and that value is the API's own, not a client-side 1500.

Statistics come from `StatisticsResponse` and **nothing is recomputed**. `win_rate` is the
backend's figure. The one presentation decision: a player with no games shows "no games yet"
instead of `0%`, which reads as having lost everything.

### 13.7 Tournament history

`GET /players/{id}/tournaments` — keyset, newest first, `useInfiniteQuery`, cursor sent back
unread. **One request per page, never per row**: the endpoint returns each summary in the
same statement (A64-020.0C), and a per-row fetch would put that N+1 back on the client side
of the boundary.

`final_rank` is `null` while a tournament is running — "in progress", not "unplaced". Ranks
are non-dense and are never renumbered.

### 13.8 Session management — bounded on purpose

`SessionService.list_user_sessions` exists in the backend and **no endpoint exposes it**. So
`/settings/sessions` offers "Sign out everywhere" and says plainly that a device list is not
available — rather than rendering an empty table that looks broken, or inventing a one-row
list that is always "this device". Publishing that endpoint is a backend change with its own
visibility questions; deferred as OQ-8.

`signOutEverywhere` clears the private query cache in the auth layer; this page does not
repeat it, because two places that clear the cache is one that can be forgotten.

### 13.9 Dates and numbers

`Intl.DateTimeFormat` and `Intl.NumberFormat`, in `shared/lib/format.ts`. A hand-built
`${day}.${month}.${year}` is wrong in at least one of three locales the moment it is
written. This is the honest half of OQ-2: the gap was never date formatting — `Intl` covers
it — but pluralisation, and nothing on these pages needs a plural.

### 13.10 Responsive

360 px baseline. Settings navigation is a scrolling row below `md` and a sidebar above it; a
vertical sidebar at 360 px would take a third of the width for four links. Profile sections
stack in one column and become a grid from `sm`. Interactive controls are `min-h-11` (44 px).
Long display names and bios use `break-words`, so nothing forces a horizontal scroll.

## 14. Social — A64-020.4

### 14.1 The relationship is the server's, and it is one value

`ProfileResponse.relationship` (SPEC-SOCIAL / A64-020.4 backend) carries one of
`none | outgoing_request | incoming_request | friend | blocked`, viewer-relative, on
**every** surface that returns a profile. Nothing is inferred client-side from several
booleans.

`entities/relationship` maps that one value to a list of actions, and that mapping is the
whole reason the impossible combinations §6 forbids cannot occur: with one closed input
there is no arrangement of props that yields "Add friend" beside "Accept".

| State | Actions |
| --- | --- |
| `none` | Add friend, Block |
| `outgoing_request` | Cancel request, Block |
| `incoming_request` | Accept, Decline, Block |
| `friend` | Remove friend, Block |
| `blocked` | Unblock — **and nothing else** |

`null`/`undefined` yields **no** actions. The API omits the field for an anonymous reader
and on the reader's own profile, and `none` is different: signed in, no relationship.

`features/social/ui/relationship-actions.tsx` is the single component that renders this and
performs every transition. Search rows, the friends list, both request lists and the public
profile all use it, so a transition is written once.

**`blocked` is one-directional.** It means the viewer blocked this player. Nothing published
anywhere says the reverse, and the enum has no member that could.

### 14.2 Routes

`/friends`, `/friends/requests`, `/friends/blocked`, `/search` — all four behind
`RequireAuth`, all four reachable from the header's "Friends" link. The two request
directions share one page: they are two views of one resource, and a person checking
requests wants both.

### 14.3 Query keys and the invalidation matrix

`features/social/api/keys.ts`.

| Mutation | Invalidates |
| --- | --- |
| send request | `search`, `outgoing`, `friends/count`, public profiles |
| cancel request | `search`, `outgoing`, `friends/count`, public profiles |
| accept | + `incoming`, `friends` |
| decline | + `incoming` |
| remove friend | + `friends` |
| **block** | `social` (everything) + public profiles |
| unblock | `blocked`, `search`, public profiles |

Blocking is the one mutation that earns a broad scope, because the write genuinely is
broad: it ends a friendship, cancels pending requests in both directions and removes the
target from search and every list. Naming five keys would be a list that goes stale the
first time the backend adds a consequence. It is still scoped — ratings, tournaments and
the session are untouched.

`search` is invalidated by prefix, because the client does not know which term produced the
cached page. Public profiles use `profileKeys.publicAll()` — the profile feature's own key,
imported rather than respelled.

### 14.4 Search

Debounced at 300 ms, so typing a term issues one request rather than one per keystroke, and
**no request at all** below the API's two-character floor. The query key is the *normalised*
term, so `" Ali "` and `"ali"` are one cache entry rather than three fetches of one page.
`gcTime` is two minutes: a cache keyed by arbitrary user input is unbounded by construction.

Superseded requests are genuinely **cancelled** — TanStack's `AbortSignal` is threaded into
Axios — rather than merely ignored.

The client filters nothing. Search already excludes the caller and everyone in either
direction of a block, server-side; a second implementation here is the copy that goes stale.

The result count is an `aria-live` region.

### 14.5 Presence in lists

`is_online` and `last_seen` only. The deprecated `show_online_status` and `show_last_seen`
booleans are **not consumed anywhere**: each is `true` only when its audience-valued
counterpart is `everyone`, so reading them would collapse a friends-only setting to "off".

An omitted field renders nothing — no "Offline" default, in either direction. Online state
is never computed from a timestamp, and there is no invented "recently online" category.
The dot is decoration; the word beside it carries the meaning.

**Presence updates only on an HTTP read.** There is no WebSocket in the frontend yet, so a
friend going offline is seen on the next refetch rather than live. Deferred deliberately —
building a second socket for this would pre-empt the Game phase's — and recorded as OQ-10.

### 14.6 Dense lists use `thumbnail_url`

Every row in search, friends, requests and blocked renders `thumbnail_url`; `avatar_url` is
left to the profile header. Asserted in the Playwright flow rather than in jsdom: Radix
mounts the `<img>` only once the image has *loaded*, and jsdom never loads one.

### 14.7 The public profile's action seam

`ProfileHeader`'s `children`, fed from the `relationship` field the **same response**
carries — no second request. Nothing renders on the viewer's own profile or for an
anonymous reader.

`requestId` is deliberately not available there, so accept and decline are offered on
`/friends/requests` where the id exists rather than guessed by a lookup.

The public-profile query **waits for the session to resolve**. Without that it races the
bootstrap, gets composed for an anonymous viewer, and caches a profile with no social
actions — a defect the social e2e caught.

### 14.8 Not in this phase

No chat, no notifications UI, no game invitations, no activity feed, no recommendations, no
moderation tools. Nothing here claims a block deletes messages or cancels games, because
the backend contract guarantees neither.

## 15. Game lobby — A64-020.5A

Two routes, one derived state, and a polling loop that is written down as
temporary. The lobby is where a player chooses a game and where a pairing is
answered; the board is A64-020.5B's.

### 15.1 Backend surface

| Endpoint | Used for |
| --- | --- |
| `GET /time-controls` | the catalogue — **added by this phase** (`feat(reference): expose active time controls`) |
| `POST /matchmaking/queue` | join, with `variant`, `queue_type`, `time_control_id`, `region` |
| `DELETE /matchmaking/queue` | cancel — `204` whether or not a ticket existed |
| `GET /matchmaking/queue/me` | the live ticket, or `404` |
| `GET /matchmaking/matches/pending` | the open offer, or `404`. Deliberately **not rate limited** |
| `POST /matchmaking/matches/{id}/accept` | accept |
| `POST /matchmaking/matches/{id}/decline` | decline — earns a cooldown |

Both `404`s are translated to `null` at the API boundary
(`features/matchmaking/api`). "You are not queued" is an ordinary answer,
not a failure, and treating it as one would make an idle lobby render a
retry screen.

### 15.2 The state model

One discriminated union, **derived** on every render from the two reads and
never stored (`entities/queue`, `features/matchmaking/model/lobby-state`).

    bootstrapping · idle · joining · queued · match_offer
    awaiting_opponent · accepting · declining · transitioning · unavailable

**A pending match always outranks a queue ticket.** This is the single most
important rule in the feature. Pairing consumes a ticket and creates a match
in two transactions — a cross-context call may not sit inside an open one
(`PairingService`) — so a client polling across the gap legitimately sees a
live ticket beside a real offer, or a `404` beside one. Ordering the union
so the offer wins means the second reading can never overwrite the first.
The offer is the state with a deadline attached; losing it costs a game.

Storing this would reintroduce exactly that race in the one place nothing
could observe it, which is why §8 forbids it and why `derive` is a pure
function with its own test.

### 15.3 Query keys and polling

| Key | Policy |
| --- | --- |
| `["reference", "time-controls"]` | `staleTime: Infinity` — a catalogue changes when an operator edits a row, not when a player acts |
| `["matchmaking", "queue", "me"]` | `staleTime: 0`; polls every 2 s **while a ticket exists** |
| `["matchmaking", "matches", "pending"]` | `staleTime: 0`; polls every 2 s while an offer is open **or** a ticket is live |

The pending read reports the player's **current** match — `pending_acceptance` **or** `active` (`specs/matchmaking.md` §10.8). The lobby branches on `status`: an open offer opens the dialog, an active one hands off to `/games/:matchId`. That is what lets the *first* of the two acceptors learn their game started, since the match activates on the other player's request and their own response still says `pending_acceptance`.

Each query decides its own interval from its own data; the offer query is
additionally told whether a ticket exists, because a queued player who
stopped asking would learn they had been paired only on refocus.

**Polling is temporary and is not pretending to be anything else.** The
backend's realtime seam is real and unwired: `PendingMatchSink` is satisfied
by `LoggingPendingMatchSink`, so a pairing reaches a log line and no socket,
and `Channel.MATCHMAKING` has no producer. The pending endpoint is
deliberately not rate limited for exactly this reason. When the gateway is
connected, `refetchInterval` becomes `false` and the socket invalidates
these two keys — the queries, the components and the state model do not
change.

Every mutation invalidates **both** reads, including on failure. Any write
here can be overtaken by the pairing scan, so the honest response to
finishing one is to stop believing the cache.

### 15.4 Recovery

There is no restore branch. A reload runs the same two queries a first visit
does, so a refreshed page reconstructs the chosen mode, the chosen clock,
the instant the player queued and any open offer without a line of code that
knows it is a reload. **No lobby state is in `localStorage`** and none is
broadcast between tabs: the backend allows one live ticket per player, so a
second tab's action is reconciled by the next poll rather than by a client
protocol.

### 15.5 What the lobby does not offer, and why

| Absent | Reason |
| --- | --- |
| Variant picker | `ProductVariant` has one member. A radio group with one option is a control that can only be left where it was |
| Region picker | AD-25 defers multi-region infrastructure. Every non-`global` value shrinks the pool and buys no latency back |
| Estimated wait | The backend supplies none. One computed here would be invented |
| Queue depth | `QueueTicketResponse.waiting` is a reading of one pool at one instant and is **not** a position in a line |
| Opponent avatar | `OpponentPreview` is three public fields. Rendering an avatar needs the privacy-gated composition `profiles` owns |
| Opponent rating | Not on the pending response |
| Custom clocks | The catalogue is authoritative; a submitted duration would be a player-authored pool |

### 15.6 The acceptance countdown

Computed from the deadline the server sent, against the local clock, and it
**decides nothing**: reaching zero refetches rather than concluding. A client
whose clock runs fast would otherwise lose a match it still had time to
accept.

Ticks are scheduled for the instant the displayed number next changes rather
than every 1000 ms, so error does not accumulate and a backgrounded tab
resumes on the right number. Announcements are made at 30, 20, 10 and 5
seconds only — an `aria-live` region containing a per-second counter is a
screen reader saying a number thirty times.

### 15.7 The handoff

Acceptance navigates to `/games/{match_id}`. This phase ships the route and
a page that names the match and nothing else: **no requests, no socket, no
board**. A64-020.5B replaces the component without touching the route, the
guard or the navigation that reaches it.

### 15.8 E2E state

The lobby suite has **its own two accounts** (`e2e_lobby_one`,
`e2e_lobby_two`), not the social suite's. Spec files run in parallel and
this one settles its accounts' matchmaking state; doing that to accounts
another suite is simultaneously friending would make both flaky for reasons
neither could see.

`resetLobby` clears what a previous run left — an open offer first, then the
ticket — through the endpoints a player uses. It **accepts** a stale offer
rather than declining it, because a decline earns the queue cooldown that
would then stop the spec from queueing. Nothing truncates a table, flushes
Redis, or disables a rate limit.

## 16. Live game — A64-020.5B

`/games/{match_id}` replaces §15.7's placeholder. The route, its guard and the
navigation that reaches it are unchanged.

### 16.1 One socket, owned above the route

`RealtimeContextProvider` is mounted in `app/providers`, not in the page, so the
socket outlives navigation — AD-11's one connection per client, multiplexed by
channel. The provider is split in two because `shared` may not import
`features`: `shared/realtime/context.tsx` holds the context and the hooks,
`app/providers/realtime-provider.tsx` supplies the ticket by calling the
matchmaking API.

A ticket is minted per connection attempt and **never stored**. It is a
single-use credential; keeping one would mean holding a redeemable secret for
as long as the tab is open, to save a request that only happens on reconnect.

### 16.2 Protocol mapping

The contract is hand-maintained in `shared/realtime/protocol.ts` and reviewed
against `apps/api/app/gateway/protocol.py`. There is no generator: the gateway
publishes no OpenAPI document, and a hand-written contract that is *known* to
be hand-written gets read.

| Direction | Frame | What the client does with it |
| --- | --- | --- |
| → | `room.join` | First mount. Correlated; the answer proves participation |
| → | `game.resume` | Every reconnect, and the `resyncing` recovery |
| → | `game.move.submit` | One in flight at a time (§16.5) |
| → | `room.leave` | Unmount |
| ← | `room.joined` | Participants and `both_connected` |
| ← | `game.snapshot` | **Replaces** the whole state; its sequence is the new baseline |
| ← | `game.events` | Ordered catch-up frames, applied in sequence |
| ← | `game.resumed` | Nothing was missed; keep what is held |
| ← | `game.resync_required` | Ask again from nothing — `resyncing` |
| ← | `game.move.accepted` | *Our* submission landed; clears `pending` |
| ← | `game.move.applied` | The fan-out that carries the state change |
| ← | `game.move.rejected` | Correlated refusal; the board reverts to the server's truth |

`accepted` and `applied` are deliberately not collapsed. The submitter receives
both, and only the correlation on `accepted` tells a client that the move it is
watching arrive is its own.

`parseFrame` returns `null` rather than throwing. A malformed frame is a
gateway defect, and a client that threw inside its socket handler would tear
down a live game over one bad byte.

### 16.3 The state machine

One owner: the reducer in `features/game/model/state.ts`. TanStack Query holds
nothing here — a live game is a stream with an authoritative sequence, and
`staleTime` has no meaning for it.

```
loading -> joining -> active <-> submitting_move
                        |  ^
                        v  |
                   reconnecting
                        |
                        v
                    resyncing -> active
                        
  any -> completed | unavailable | fatal
```

`sequence` never advances without a server frame. A frame at or below the held
sequence returns the identical state object — replay is free and re-renders
nothing. A frame that skips one is a gap, and a gap goes to `resyncing`.

### 16.4 The board

Squares are the server's algebraic strings (`"c3"`), never renumbered. A move is
submitted as a list of them, so a client with its own numbering would have to
convert back at exactly one place, and the day it forgot it would send a legal
move for the wrong squares.

The model is always in the engine's frame — `a1` is LIGHT's near-left corner.
**Orientation is rendering only**, one boolean in `board.tsx`, so a future
flip-the-board control touches nothing else. Playable squares are derived
(`(file + rank)` even), not listed, and asserted against the corpus's opening
position.

### 16.5 Authority, and what the client is allowed to decide

| Decision | Owner |
| --- | --- |
| Legality, capture continuation, promotion, turn, result, ply | Server |
| Which squares to *light up* before a round trip | Client kernel |
| Whether a clock has run out | Server |

The TypeScript kernel in `features/game/engine/moves.ts` exists for one reason:
a player must see legal destinations on click, not after a round trip. It is
validated against the **same conformance corpus** the Python engine is —
AD-14's "the corpus is the contract" — and passes 22/22. It is never the
authority. A disagreement between the kernel and the server is resolved by the
server, silently, because the server is right by definition.

One move in flight at a time. `submitting_move` makes the board
non-interactive, and the request registry refuses a second submission rather
than queueing it.

### 16.6 Reconnect and resume

Backoff is exponential with ±25% jitter, clamped to a ceiling **after** the
jitter is applied — an uncapped spread put the tail 25% past the ceiling the
policy documented.

The first mount sends `room.join`; every reconnect after that sends
`game.resume` with the sequence this client holds. The gateway answers with the
missed frames if the buffer can prove continuity, a snapshot if it cannot, or
`game.resync_required` if it can prove a gap — `websocket.md` §20.3.

A resume with **no** sequence means "I am holding nothing", and it is answered
with a snapshot whatever the server's sequence is. That includes the sequence-0
resume every game opens with; see the fix in this phase's commits.

### 16.7 Clock

`useClock` interpolates between authoritative frames and **adjudicates
nothing**. When the visual countdown reaches zero, nothing happens: no winner,
no state change. The flag arrives as a frame, or it did not happen.

Drift is corrected rather than accumulated. The payload carries absolute
instants (`deadline`, `server_time`), so the hook computes `offset = server_time
− received_at` on every authoritative update. A machine whose clock is a minute
fast shows the right countdown from the first frame.

One 250 ms timer for both sides, not one per clock. The display floors to whole
seconds; four ticks a second is what makes the seconds change *on time* without
the display changing four times.

### 16.8 Accessibility

- `role="grid"` / `role="row"` / `role="gridcell"`, arrow keys plus Enter, and
  a roving tabindex — one stop for the board, not sixty-four.
- Every cell has a spoken label: `"c3, Light, man"`, `"d4, empty, legal move"`.
- Light squares are `aria-hidden` — a piece can never stand on one.
- The status line is `role="status"` (polite: a turn change should not interrupt),
  a rejection is `role="alert"` (assertive: it needs the player now).
- Clocks are labelled per side, so "0:49" is never read without whose it is.

### 16.9 Responsive

The board is a square that shrinks with the viewport; the panel moves from
beside it to below it. No horizontal scroll at any width, and the board never
depends on a hover to be playable — every interaction is a click or a key.

### 16.10 E2E

`tests/e2e/game.spec.ts` pairs two seeded accounts through the **real lobby**,
opens both boards, moves on one, asserts the other changed, and reloads to prove
the position came from `game.resume` rather than from anything the browser kept.
Nothing is mocked.

Three accounts, not two: QT-3 excludes a player's most recent opponent with no
time window, so a fixed pair is pairable exactly once. `1+0`, so a game the
suite leaves active flags in a minute instead of blocking its own accounts for
ten.

Sessions are written back through `saveState`, which merges the seeded-account
block Playwright's own `storageState` would drop.

### 16.11 Not in this phase

| Deferred | Why |
| --- | --- |
| Spectating | The gateway has the subscription keyspace (`gwspec:v1:`) but no viewer surface is specified; a spectator board is a different product decision about what a non-participant may see |
| Replay / move list | Needs the move log as a read model, which is a backend surface that does not exist |
| ~~Draw offers, resignation~~ | **Shipped by A64-020.5C** — §16.12 |
| Takebacks | Still no frames, no domain concept and no product decision |
| Orientation toggle, sound, premoves | One boolean, one asset, and a queue respectively — none of them requirements yet |

### 16.12 Game controls — A64-020.5C

Resign, offer a draw, accept and decline. `GameControls` is mounted in
`pages/game` beside `GamePanel` and sends through `useGameRoom.command`, so
every participant command goes through the one socket, the one request
registry and the one reducer.

**Nothing here decides anything.** The panel renders `GameState.draw`, which
is the server's answer resolved for this viewer. There is no ply arithmetic,
no eligibility rule and no result derivation in the feature — a client
recomputing the spam rule would be a second copy of
`game.domain.draw_agreement`, and the copy that disagreed would show a
button the server refuses.

#### Protocol to UI

| Frame | What the UI does |
| --- | --- |
| `game.draw.offered` | Recipient: the answer panel. Offerer: a durable "sent" line |
| `game.draw.declined` | Both: the offer disappears; board, clock, turn and ply unchanged |
| `game.completed` | Both: the terminal result, from the payload |
| `game.command.rejected` | The actor only: a mapped sentence beside the controls |

The actor receives its event **twice** — correlated to `request_id` and
again as the room fan-out — exactly as it does for a move. The reducer
applies the fan-out; the awaited promise exists to surface a *refusal*.

#### Reconnect

`game.snapshot` carries a `draw` object and it **replaces**: an offer the
server still holds reappears, one it does not is dropped, and the three
booleans are restored exactly. Nothing is read from `localStorage` or from
component memory. A spectator's snapshot omits the block entirely, which
reads as "no agreement" rather than as a parse failure.

#### One refresh the client does have to ask for

`game.move.applied` is a fan-out to participants **and** spectators, so it
cannot carry viewer-resolved draw permissions. That leaves a real gap, found
by running the two-browser flow: a player whose offer was declined sees the
button correctly disabled, the opponent moves, the server now says they may
ask again — and nothing tells them.

So a **restricted** client re-reads the snapshot once per ply. The condition
excludes everybody who is not blocked, and in an ordinary game it never
fires. It is deliberately not `resync()`, which would announce
"Resynchronising…" and freeze the board for a routine refresh.

#### Move-triggered expiration

The backend clears an offer when its **recipient** applies a legal move,
inside that move's transaction. The client mirrors it on
`game.move.applied` so the indicator disappears with the move rather than a
round trip later. It does **not** clear on submission, on
`game.move.accepted`, or on `game.move.rejected` — a refused move must leave
the offer standing.

#### Error mapping

Only codes `GatewayErrorCode` publishes, matched on the code and never on
the server's prose. `draw_offer_not_allowed_yet` reads "wait for your
opponent to move", not "slow down" — it is not a rate limit.

#### Visibility and limitations

- **Participant-only.** A spectator has no `draw` block and no side, so the
  whole panel is absent — not disabled, absent.
- **No withdrawal.** v0.x has no command for it. The offerer is told the
  opponent may answer *or move*, because a move is what ends it.
- Takebacks, rematch and chat remain out of scope.

#### E2E

`tests/e2e/game-controls.spec.ts` runs the whole negotiation across two real
browsers: offer, decline, a refused re-offer proven by a **reload** (so the
disabled state came from the snapshot, not memory), one opponent move, the
control returning, then a resignation both browsers read identically.

It is third in the Playwright project chain — `lobby` → `live-game` →
`game-controls` — because all three drive the lobby with the same three
accounts and refresh-token rotation makes concurrent use destructive.

### 16.13 Realtime matchmaking push — A64-020.5D

The lobby polled every two seconds because `LoggingPendingMatchSink` never
put a pairing on a socket. It does now.

#### The push is a wake-up signal

`matchmaking.match.offered` arrives on the shared socket and
`useMatchOfferPush` invalidates the two authoritative reads. **Nothing
trusts the payload**: the frame may be duplicated, late, or missed entirely,
so what it triggers is a read that decides whether the offer still exists,
whether the deadline is valid, whether this player may answer, and whether
the match has already started.

A client that rendered the payload would show an acceptance dialog for an
offer the opponent declined a second earlier — asserted in
`realtime-push.test.tsx`.

Duplicates are dropped by `match_id` against a bounded ring, and concurrent
distinct offers collapse into one refetch (single-flight). Three pushes are
one read.

#### Polling policy

| Situation | Interval | Why |
| --- | --- | --- |
| Queued, socket `ready` | 25 s | Backstop only — the push carries it |
| Queued, socket anything else | 2 s | No push can arrive |
| **Offer open**, any socket state | 2 s | Nothing pushes *activation* — see below |
| Idle | none | Nothing to wait for |

**Measured**: 0 matchmaking `GET`s in 20 seconds while queued with realtime
healthy, against ~20 before. Asserted in `realtime-push.spec.ts` rather than
described.

The 25-second backstop is sized against the **acceptance window** (30 s), not
against responsiveness: a push lost without closing the socket — the sink
found no connection mid-reconnect, a stream trimmed, a forwarder restarting
— is still recovered inside the window it matters in.

The open-offer exception is a real protocol gap, not a client choice:
`matchmaking.match.offered` fires on pairing and nothing fires on
activation, so the first acceptor would otherwise wait up to 25 seconds to
be taken into a game that had already started. Found by the E2E when the
interval first went slow.

#### Delivery mode

`deliveryMode(status)` derives four states from the connection status —
`realtime`, `reconnecting`, `fallback_polling`, `offline`. The waiting card
shows a line **only when degraded**; a "connected" banner during normal
operation is noise.

#### Scope limitation

The subscription is mounted by the lobby, not by the app. A player on
`/profile` when they are paired learns on their next visit to `/play` —
exactly as before, because the durable read is unchanged. Closing that needs
an app-level offer surface, which is notifications and is out of scope.

#### Participant draw state

`game.draw.state` replaces A64-020.5C's snapshot-per-ply workaround. The
frame is addressed per seat, carries the same shape the snapshot's `draw`
block does, and the reducer **replaces** the agreement without touching the
board, the clock, the turn or the move in flight — which is what makes it
safe whichever order it arrives in relative to the move that caused it.

The workaround is gone: `realtime-push.spec.ts` asserts that eligibility
returns after an opponent's move with **zero** HTTP requests in between.

#### `rated` on the snapshot

Added. The fact is already on `game.match.rated`, already published on
`PendingMatchView` and in every history row, and is the same value both
players agreed to when they queued — so nothing private crosses and no new
coupling appears. Spectator-safe, so it is in the base projection. The
resignation dialog now says which rather than "if this game is rated".

#### E2E server freshness

`reuseExistingServer: false`. A `vite preview` left running from an earlier
invocation served a two-hour-old bundle through several runs of A64-020.5C,
so specs asserted against a frontend that no longer existed and nothing
failed loudly. Option A of the two available, chosen because it has no
moving parts — a build-hash handshake needs three things to agree in order
to detect a problem that not reusing cannot have. The cost is one rebuild
per run (~250 ms). `strictPort` makes an occupied 4173 a clear bind error,
and nothing is killed automatically: a process this config did not start is
not its to stop.

### 16.14 The backend contract these controls consume

Specified in full in [`websocket.md`](../docs/01-architecture/websocket.md) §22. What a frontend
needs to know, in one place:

| Client sends | Server answers the actor | Server fans out |
| --- | --- | --- |
| `game.resign` | `game.completed` (correlated) | `game.completed` |
| `game.draw.offer` | `game.draw.offered` (correlated) | `game.draw.offered` |
| `game.draw.accept` | `game.completed` (correlated) | `game.completed` |
| `game.draw.decline` | `game.draw.declined` (correlated) | `game.draw.declined` |
| any of them, refused | `game.command.rejected` (correlated) | — |

**Send `match_id` and nothing else.** No side, no player id, no outcome. The server derives the
acting side from the socket; a payload that named one would be a client resigning for its
opponent, and the protocol has no field for it.

**The actor receives its event twice** — once correlated to `request_id`, once as the room
fan-out — exactly as it does for a move. Advance state from the fan-out and use the correlated
copy only to clear the in-flight request, and one code path handles both players.

**Reconnect is a snapshot read, not a reconstruction.** `game.snapshot` carries a `draw` object:

```
"draw": {
  "offer": { "offered_by": "light", "offered_at_ply": 7, "offered_at": "..." } | null,
  "may_offer": bool, "may_accept": bool, "may_decline": bool
}
```

The three booleans are already resolved for the requesting player. Render buttons from them
rather than deriving "I may accept only if the offer is not mine" client-side — a client that got
that backwards would show an accept button the server refuses. A resignation or an agreed draw
that happened while the client was away arrives as the snapshot's ordinary `result`.

**A pending offer disappears on its own** when the recipient moves. Do not treat
`game.move.applied` as leaving the offer untouched: clear it locally when the applied move is the
recipient's, and the next snapshot will agree.

**`draw_offer_not_allowed_yet` is not a rate limit.** It means the opponent has not moved since
this player's last offer was resolved, so the correct message is "wait for your opponent to move",
not "slow down". `may_offer` in the snapshot is the same answer in advance, which is what a
disabled button should read from.

**Spectators receive none of it.** `game.draw.offered` and `game.draw.declined` are
participant-only, and a spectator's snapshot has no `draw` key at all. A spectator UI must not
assume the field exists.

## 17. Open questions

| # | Question | Blocked work |
| --- | --- | --- |
| ~~OQ-1~~ | **Closed by A64-020.2.** `apps/api` sets the `HttpOnly` cookie on a browser-specific surface and the app is same-origin behind a proxy (§11, §12.1) — so F-1's guarantee holds without a Route Handler | — |
| OQ-2 | **i18n, partially closed.** `shared/i18n` (§12.7) wires uz/ru/en with compile-checked keys. Still open: an ICU library for plurals and per-locale date/number formatting, lazy namespace loading, and whether the locale belongs in the URL | Plurals, formatted dates, a locale-scoped route |
| OQ-3 | **Route tree scale.** At seventeen routes `routes.tsx` no longer fits on a screen, which is the threshold this question named. If the tree outgrows one screen, file-based routing plus its generated tree may be the better trade | A large route surface |
| OQ-4 | **Bundle budget.** No `manualChunks` and no size assertion in CI. The numbers to enforce are a measurement nobody has taken yet (CLAUDE.md §10.10) | A budget gate |
| OQ-5 | **Automated a11y checks.** `@axe-core/playwright` is not wired; today's accessibility guarantees are hand-asserted | An a11y gate |
| OQ-6 | **Access-token expiry is not anticipated.** The client waits for a `401` and refreshes reactively rather than scheduling against `expires_in`, so the first request after ~15 minutes idle always costs a round trip | A latency budget on the first interaction |
| ~~OQ-7~~ | **Closed by A64-020.3.** `signOutEverywhere` is reachable from `/settings/sessions`, behind an explicit confirmation | — |
| OQ-8 | **No session list.** `SessionService.list_user_sessions` has no HTTP endpoint, so there is no device list to show — only "sign out everywhere". Publishing it needs a decision about what a session row may reveal (IP, user agent) | A device-management UI |
| ~~OQ-9~~ | **Closed by A64-020.4.** The public profile renders a relationship-aware action set through `ProfileHeader`'s seam | — |
| ~~OQ-10~~ | **Closed by A64-020.5B, in part.** The frontend has a socket: `shared/realtime` owns it, `app/providers` mounts it above the route tree, and a live game runs entirely on frames (§16). What remains is narrower and no longer a frontend gap — **the lobby still polls**, because `LoggingPendingMatchSink` (§15.3) means no match offer is ever published to the gateway. Reopened as OQ-13 | — |
| ~~OQ-13~~ | **Closed by A64-020.5D.** `GatewayPendingMatchSink` publishes a pairing to the paired players' sockets through the existing fleet fan-out, and the lobby reconciles against the durable read. Friend presence is still polled — reopened as OQ-14, which is the narrower half | — |
| OQ-14 | **Friend presence is still an HTTP read.** The socket and the channel exist; what is missing is a presence event and a decision about who may be told what, which is `friends`' privacy question rather than a transport one | A presence channel |
| OQ-15 | **Match activation is not pushed.** `matchmaking.match.offered` fires on pairing; nothing fires when the opponent accepts, so an open offer keeps the two-second interval. Narrow — it lasts at most the acceptance window — and closed by a `matchmaking.match.activated` frame | A second matchmaking frame |
| OQ-12 | **The offer dialog cannot show an avatar or a rating.** `OpponentPreview` carries three public fields by design (§15.5). Publishing more needs the privacy-gated composition `profiles` owns, which is a backend decision rather than a frontend gap | A richer match card |
| OQ-11 | **A friend's `relationship` is fetched but structurally known.** The four list endpoints state it server-side and cost nothing, but a client-side `friends` cache could serve the public profile's state too — not done, because a stale action is worse than a request | A measured need |

## Related documents

- `docs/07-decisions/ADR-002-frontend-spa.md` — why Vite replaced Next.js
- `docs/04-frontend/routing.md`, `state-management.md`, `design-system.md`
- `apps/web/src/entities/README.md` — what belongs in that layer
- `apps/web/src/shared/api/generated/README.md` — regenerating the API types
