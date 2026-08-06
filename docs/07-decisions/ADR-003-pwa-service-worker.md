# ADR-003 — Arena64 owns its service worker; Workbox is not adopted

| Field | Value |
| --- | --- |
| **Status** | Accepted |
| **Date** | 2026-08-06 |
| **Deciders** | Shohruh |
| **Consulted** | — |
| **Supersedes** | — |
| **Superseded by** | — |
| **Related** | `specs/frontend.md` §20, `docs/07-decisions/ADR-002-frontend-spa.md` |

---

## Context

A64-020.9 makes `apps/web` an installable Progressive Web App: a manifest, icons, an
application-shell precache, an update prompt, an install experience, and an offline
fallback. Everything but the manifest needs a service worker, and a service worker is
long-lived code with unusual failure modes — it survives page loads, it can serve stale
bytes for weeks, and a mistake in it is invisible until somebody's cache is already wrong.

Three constraints shaped the choice, and all three are recorded rather than assumed:

1. **The cache policy is deliberately narrow.** `specs/frontend.md` §20 caches the shell,
   the hashed build assets, and nothing else. No API response is cached — not an
   authenticated read, not a "safe" public one. The Cache API is shared by every session on
   a device, and this codebase cannot today prove per-user isolation inside it, so the
   conservative option in the phase brief was taken in full.
2. **The build must know the manifest.** Precaching means knowing which files Rollup
   emitted, which is a build-time question with a build-time answer.
3. **`apps/web` runs Vite 8** (ADR-002), which as of this decision is newer than most
   published PWA tooling.

## Decision

> We will author Arena64's service worker in this repository — `apps/web/pwa/` — and
> generate its precache manifest with a first-party Vite plugin, rather than adopting
> `vite-plugin-pwa` and Workbox, because the caching behaviour this product needs is
> smaller than the configuration surface a library would add, and because the
> security-relevant half of it must be a directly testable pure function.

There is exactly **one** service worker. A future phase that adds push handling
(A64-021 Notifications) extends this worker; it does not register a second one.

## Options Considered

### Option 1 — First-party worker and Vite plugin *(chosen)*

**Summary:** `pwa/cache-policy.ts` decides what may be cached, `pwa/service-worker.ts` does
the I/O, `pwa/vite-plugin.ts` computes the precache manifest and version from the real
bundle and compiles the worker in an isolated second build pass.

| Pros | Cons |
| --- | --- |
| No new runtime or build dependency, and no transitive tree to audit | ~250 lines of service-worker code this team maintains |
| The cache policy is a pure function unit-tested directly — no worker global to fake | Lifecycle correctness is ours to get right |
| Every caching decision is readable in one file a reviewer can hold in their head | No community-tested handling of exotic cases we have not met yet |
| Nothing to keep compatible with the next Vite major | |

### Option 2 — `vite-plugin-pwa` with `generateSW`

**Summary:** Declare `workbox` options and let the plugin generate the worker.

| Pros | Cons |
| --- | --- |
| Battle-tested lifecycle and expiration handling | The generated worker is an artefact nobody reads, which is the wrong property for the file that decides what is stored on a player's device |
| Community answers for unusual browsers | Expressing "handle nothing except the shell and `/assets`" is a deny-list bolted onto a tool designed around allow-lists |
| | Adds `workbox-build` and `workbox-window` for a manifest generator |

### Option 3 — `vite-plugin-pwa` with `injectManifest`

**Summary:** Keep an authored worker, let the plugin inject the precache manifest into it.

| Pros | Cons |
| --- | --- |
| Authored, readable worker | The handlers are still written by hand — the dependency buys only the manifest, which is the part that is ~40 lines |
| Manifest generation is somebody else's problem | Two dependencies and a Workbox version to track for that 40 lines |

### Option 4 — Do nothing

**Summary:** Ship the manifest and icons; register no worker.

The application is installable but has no offline shell, no update prompt, and no
foundation for A64-021's push handling — a `push` event is delivered *to a service
worker*, so Notifications would have to make this decision instead, under more pressure and
with a domain of its own to design. Declined.

## Rationale

Three criteria decided it.

**Where the risk actually is.** The expensive mistake a service worker can make is caching
one authenticated response, which publishes one player's data to whoever uses the device
next. Workbox does not prevent that mistake — the configuration author does. What reduces
it is making the decision a pure function with a table-driven test, which is what
`pwa/cache-policy.ts` is and what a generated worker cannot be.

**Size of the thing being bought.** Options 2 and 3 both leave us writing the handlers or
writing configuration equivalent to them. What the dependency genuinely provides is
precache-manifest generation from the bundle, which the plugin does in about forty lines
because Vite hands the bundle to a `generateBundle` hook.

**CLAUDE.md §2.6.** A dependency is a long-term liability, and one is not added for what an
existing dependency already does. Vite's plugin API is the existing dependency.

The counter-argument is real and is accepted below: lifecycle bugs in a worker are ours,
and Workbox has met browsers we have not.

## Consequences

### Positive

- Every byte the application stores on a device is decided by one readable file.
- The cache policy is unit-tested as data (`pwa/cache-policy.test.ts`) rather than through
  a faked `ServiceWorkerGlobalScope`.
- The build fails loudly if the worker is not a self-contained classic script, or if its
  precache manifest was not injected — two failures that are otherwise silent in production.
- No dependency to keep compatible with the next Vite major.

### Negative

- Service-worker lifecycle correctness is maintained here. The known gaps are named in
  `specs/frontend.md` §20: no background sync, no navigation preload, no periodic update
  check beyond `visibilitychange`.
- A browser quirk Workbox already handles will be met the hard way. One already was — a
  `Vary: Origin` response header makes every precache lookup miss unless `ignoreVary` is
  set, which cost a debugging session and is now documented at the line that fixes it.

### Neutral

- `apps/web/pwa/` sits beside `src/` rather than inside it. The worker has its own global
  scope and must never import React; keeping it outside the layered source tree makes that
  structural rather than a convention.

## Impact

| Area | Impact |
| --- | --- |
| Architecture | One service worker, built from `apps/web/pwa/`, registered from `src/main.tsx` |
| Data model | None |
| Security | The Cache API stores public build output only; no token, no API response, no `/ws` traffic — asserted in `pwa/cache-policy.test.ts` and `tests/e2e/pwa.spec.ts` |
| Operations | Production must serve `/sw.js` at the site root over HTTPS with the app; the worker's scope is `/` |
| Developer workflow | No worker in `npm run dev`; the PWA is exercised through `npm run build && npm run preview` |

## Compliance & Enforcement

- `pwa/vite-plugin.ts` fails the build when the emitted worker contains module syntax or an
  un-injected placeholder.
- `pwa/cache-policy.test.ts` asserts that authentication, the ws ticket, every `/api` path,
  every mutation and every third-party origin classify as untouched.
- `tests/e2e/pwa.spec.ts` asserts against real Cache Storage that no cached entry is outside
  the shell, and that every cache name carries the `arena64-` prefix.
- A second service worker, or a PWA library that registers one, contradicts this record and
  requires a superseding ADR.

## Follow-Up Actions

- [ ] A64-021 Notifications: add `push` and `notificationclick` handlers to **this** worker
      — owner Shohruh, with that phase.

## Revisit Criteria

Reopen this decision if any of the following becomes true:

- The cache policy grows past "shell plus hashed assets" — for example if a completed
  replay or a public tournament result is to be cached, which needs per-user isolation
  Workbox's plugins have already thought about.
- Background sync, periodic sync, or navigation preload is required.
- Maintaining the worker costs more than one debugging session per phase.

## References

- `apps/web/pwa/` — the worker, its policy, and its build plugin
- `specs/frontend.md` §20 — the PWA contract this implements
- A64-020.9 §4 — the phase brief's own test for adopting a PWA library
