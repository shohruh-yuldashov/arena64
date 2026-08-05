# Feature Specification — Frontend Foundation

| Field | Value |
| --- | --- |
| **Spec ID** | `SPEC-FRONTEND` |
| **Status** | Approved through A64-020.3 — foundation, authentication and profile |
| **Owner** | _Unassigned_ |
| **Created** | 2026-08-05 |
| **Last updated** | 2026-08-05 — A64-020.3, profile UI |
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
| `entities/` | Business nouns — a player, a tournament — with their shapes and queries | `session/`, `user/`, `profile/` — aliases over generated types |
| `features/` | One user-facing capability, self-contained | `auth/`, `profile/`, `avatar/`, `preferences/`, `privacy/`, `form-demo/` |
| `widgets/` | Composite blocks a page arranges | `app-shell/`, `auth-shell/`, `settings-shell/`, `session-menu/`, `profile-header/`, `rating-cards/`, `statistics-panel/`, `tournament-history/`, `theme-toggle/` |
| `pages/` | One route's screen | `home/`, five auth pages, `profile/`, `public-profile/`, four `settings-*`, `not-found/`, `unexpected-error/` |
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

### 10.1 The e2e suite registers, so it is rate-limited

Two of the three Playwright specs register an account, and `POST /auth/register` allows
**three per IP per hour** (`RATE_LIMIT_REGISTER_IP_LIMIT`). So the suite can be run twice
in an hour from one machine and the third run fails with a translated
`Too many attempts` on the sign-up form — which is the rate limit working, not a defect.

It surfaced exactly that way during A64-020.3. Clearing `rl:v1:register_ip:*` from the
local limits Redis (database 4) resets it. Making the suite immune would mean either
seeding accounts out of band or exempting the test IP, and both trade a real behaviour for
a convenience; recorded rather than papered over.

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
| Development | The Vite dev server proxies `/api` to `ARENA64_API_TARGET` (default `http://localhost:8000`) — `vite.config.ts` |
| E2E | `vite preview` uses the **same** proxy config; `preview.proxy` does not inherit `server.proxy`, so both are declared |
| Production | A reverse proxy (nginx, Caddy, a CDN rule) must route `/api` to FastAPI. **This is a deployment requirement, not a default** |

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

## 14. Open questions

| # | Question | Blocked work |
| --- | --- | --- |
| ~~OQ-1~~ | **Closed by A64-020.2.** `apps/api` sets the `HttpOnly` cookie on a browser-specific surface and the app is same-origin behind a proxy (§11, §12.1) — so F-1's guarantee holds without a Route Handler | — |
| OQ-2 | **i18n, partially closed.** `shared/i18n` (§12.7) wires uz/ru/en with compile-checked keys. Still open: an ICU library for plurals and per-locale date/number formatting, lazy namespace loading, and whether the locale belongs in the URL | Plurals, formatted dates, a locale-scoped route |
| OQ-3 | **Route tree scale.** Code-based routing is still right at seven routes. If the tree outgrows one screen, file-based routing plus its generated tree may be the better trade | A large route surface |
| OQ-4 | **Bundle budget.** No `manualChunks` and no size assertion in CI. The numbers to enforce are a measurement nobody has taken yet (CLAUDE.md §10.10) | A budget gate |
| OQ-5 | **Automated a11y checks.** `@axe-core/playwright` is not wired; today's accessibility guarantees are hand-asserted | An a11y gate |
| OQ-6 | **Access-token expiry is not anticipated.** The client waits for a `401` and refreshes reactively rather than scheduling against `expires_in`, so the first request after ~15 minutes idle always costs a round trip | A latency budget on the first interaction |
| ~~OQ-7~~ | **Closed by A64-020.3.** `signOutEverywhere` is reachable from `/settings/sessions`, behind an explicit confirmation | — |
| OQ-8 | **No session list.** `SessionService.list_user_sessions` has no HTTP endpoint, so there is no device list to show — only "sign out everywhere". Publishing it needs a decision about what a session row may reveal (IP, user agent) | A device-management UI |
| OQ-9 | **The public profile has no friend or block actions.** Deliberate — those are A64-020.4's, and half of one would be worse than none | A64-020.4 Social UI |

## Related documents

- `docs/07-decisions/ADR-002-frontend-spa.md` — why Vite replaced Next.js
- `docs/04-frontend/routing.md`, `state-management.md`, `design-system.md`
- `apps/web/src/entities/README.md` — what belongs in that layer
- `apps/web/src/shared/api/generated/README.md` — regenerating the API types
