# ADR-002 — `apps/web` is a Vite single-page application, not Next.js

| Field | Value |
| --- | --- |
| **Status** | Accepted |
| **Date** | 2026-08-05 |
| **Deciders** | Shohruh |
| **Consulted** | — |
| **Supersedes** | — |
| **Superseded by** | — |
| **Related** | `specs/frontend.md`, `docs/01-architecture/architecture.md` §5 |

---

## Context

`apps/web` was scaffolded as a **Next.js 15 App Router** application: `next-intl` with
three locales (uz, ru, en), `next-themes`, Zustand, a `fetch`-based client, route groups,
and `error.tsx` / `not-found.tsx` conventions. `architecture.md` recorded it as *"apps/web
— Next.js player client"*. It had 48 source files and **no tests**.

A64-020.1 specified the production foundation with a different stack: React 19 on **Vite**,
**TanStack Router**, Axios, React Context for global state, and an explicit prohibition on
Redux, MobX and Zustand. It also required that the existing frontend be reused in place —
no second project, no nested application, no duplicate `package.json`.

Vite and Next.js cannot coexist in one application: TanStack Router replaces App Router,
and Vite replaces the Next build. The two constraints — *this stack* and *that project* —
resolve to exactly one action: convert `apps/web` in place.

Two facts made the choice consequential rather than cosmetic:

1. Approved decision **F-1** keeps the access token in memory and the refresh token in an
   `HttpOnly` cookie set by a Next.js **Route Handler**. A Vite SPA has no server of its own.
2. `next-intl` is Next-only. The three locale files are product content, not scaffolding.

## Decision

> We will build `apps/web` as a **Vite-built React SPA** with TanStack Router, converting
> the existing Next.js application in place rather than starting a second project — because
> Arena64's player client is an authenticated, realtime, session-driven application whose
> pages are not public documents, which is the workload an SPA fits and the one server-side
> rendering pays for without benefit.

Concretely: one `apps/web`, one `package.json`, files carried over with `git mv` where they
survived. No Redux, MobX or Zustand — server state is TanStack Query's, global client state
is React Context.

## Options Considered

### Option 1 — Convert `apps/web` to Vite + TanStack Router *(chosen)*

**Summary:** Replace the Next.js build, routing, theme and store with the specified stack,
inside the existing project.

| Pros | Cons |
| --- | --- |
| One frontend, one dependency tree, one CI pipeline | F-1's cookie mechanism has no host and must be revisited |
| Build and dev server are markedly faster; no server runtime to operate | `next-intl` is discarded; i18n must be re-chosen |
| Router, cache and state are ordinary libraries with no framework coupling | ~10 Next-specific files deleted |
| The whole client is static assets — deployable to any CDN, no Node process | `architecture.md` §5 needs correcting |

### Option 2 — Keep Next.js, apply everything else from the brief

**Summary:** Retain App Router and `next-intl`; adopt the layer rules, Axios layer, test
harness, error infrastructure and OpenAPI generation.

| Pros | Cons |
| --- | --- |
| F-1 stays implementable exactly as approved | Contradicts the stack the phase specifies |
| i18n keeps working | Server components buy little for screens that are all authenticated |
| No files discarded | A Node server to operate for an app that renders nothing publicly |

### Option 3 — Do nothing

Leave `apps/web` as the untested Next.js scaffold and build features on it.

| Pros | Cons |
| --- | --- |
| No migration cost | Zero tests; no layer rule; no error, loading or API infrastructure |
| | The foundation phase produces nothing, and every later phase pays for it |

## Rationale

The deciding criterion is **what the client actually is**. Arena64's player client is a
signed-in, realtime, WebSocket-driven application: a lobby, a board, a bracket. None of
those is a public document, so none of them benefits from server rendering or from the
crawlability that justifies it. Next.js was answering a question this product does not ask,
while adding a server runtime that has to be deployed, scaled and operated.

Against that, the two costs are real but bounded and both are *decisions deferred rather
than problems created*: F-1's cookie can be set by `apps/api` instead of by a Route Handler
(a CORS and `SameSite` decision, not a redesign), and the locale files are preserved
byte-for-byte pending an i18n library choice. Neither is resolved by this ADR, and
`specs/frontend.md` §11 records both as open questions rather than letting a default be
discovered in code.

Option 3 was rejected outright: the status quo has no tests, no enforced architecture and
no error handling, and every later phase would inherit that.

## Consequences

### Positive

- One deployable artefact: static assets, no Node process in production.
- Dependency direction is **enforced** by `import/no-restricted-paths`, failing `npm run lint`
  — the frontend counterpart to `apps/api`'s 27 `import-linter` contracts.
- Routing, caching and state are libraries, replaceable independently of a framework.
- API payload types are generated from the backend's OpenAPI document, so no hand-written
  DTO can drift.

### Negative

- **F-1 is not implementable as written.** No Route Handler exists to set the `HttpOnly`
  refresh cookie. A64-020.2 must resolve this before any credential is stored; until then
  nothing in the app reads or writes a token. (`specs/frontend.md` OQ-1.)
- **i18n is unwired.** `next-intl` is gone; `src/shared/i18n/locales/{uz,ru,en}.json` are
  preserved untouched and no screen is translated. (OQ-2.)
- No server-side rendering, so no crawlable public pages. Should Arena64 ever want indexable
  tournament or profile pages, that is a separate surface — not a reason to re-add a server
  runtime to the player client.

### Neutral

- Route-level code splitting is configured from the first route rather than provided by a
  framework convention.
- The theme is a React Context plus a pre-paint inline script, in place of `next-themes`.

## Impact

| Area | Impact |
| --- | --- |
| Architecture | `architecture.md` §5 corrected: `apps/web` is a Vite SPA. `apps/admin` is untouched by this ADR |
| Data model | None |
| Security | No credential is stored anywhere in the client. Token storage is deferred to A64-020.2 under OQ-1; `localStorage`/`sessionStorage` remain forbidden for tokens |
| Operations | No Node runtime for the web client; static hosting plus the API's CORS configuration |
| Developer workflow | `npm run dev / build / lint / typecheck / test / test:e2e`; `npm run openapi:generate` regenerates API types |

## Compliance & Enforcement

| Rule | Enforced by |
| --- | --- |
| Layer dependency direction | `import/no-restricted-paths` zones in `apps/web/eslint.config.mjs` — fails `npm run lint` |
| No dead providers | `src/app/App.test.tsx` — structural and functional assertions |
| No hand-written DTOs | Review, plus `shared/api/generated/README.md`; the generated file is committed |
| No token in web storage | Review; `shared/api/client.ts` documents the seam and registers nothing |
| TypeScript strictness | `npm run typecheck` inside `npm run build` |

## Follow-Up Actions

- [ ] Resolve OQ-1 (token storage without Route Handlers) — A64-020.2, before any auth UI.
- [ ] Resolve OQ-2 (i18n library and locale routing) — before the first translated screen.
- [ ] Decide whether `apps/admin` follows this stack — outside A64-020 (F-6).

## Revisit Criteria

- A product requirement appears for **publicly crawlable** pages (indexed tournament
  results, shareable profiles) that meta tags and prerendering cannot satisfy.
- The route tree outgrows a single reviewable file, making file-based routing the better
  trade (OQ-3).
- First-load bundle size becomes a measured problem that route splitting cannot solve.
