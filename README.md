# Arena64

**Arena64** is an online multiplayer checkers platform built for competitive play —
realtime matches, skill-based matchmaking, permanent ratings, tournaments, and a separate
operator console.

> **Project status:** Implemented and running locally. Sixteen delivery epics
> (`A64-009` … `A64-024`) are closed, each with its own audit; the player-experience
> redesign (`A64-025`) is in progress. There is **no deployed environment and no CI
> pipeline yet** — see [Known gaps](#known-gaps).
>
> Feature behaviour is specified in [`specs/`](./specs/) before it is built, and every
> closed epic ends with an audit document recording what was actually shipped.

---

## Overview

| | |
| --- | --- |
| **Domain** | Competitive online checkers |
| **Shape** | Monorepo — three applications over one backend |
| **Backend** | Modular monolith, one artifact, three runtime profiles (`api`, `gateway`, `worker`) |
| **Current phase** | `A64-025` — player-experience redesign over the built product |

Product direction is in [`docs/00-overview/vision.md`](./docs/00-overview/vision.md);
delivery history and sequencing in
[`docs/00-overview/roadmap.md`](./docs/00-overview/roadmap.md).

### Size, as of commit `43cffd3` (2026-09-03)

| Area | Files | Lines |
| --- | --- | --- |
| `apps/api/app` — backend | 676 `.py` | ~128 500 |
| `apps/api/tests` — backend tests | 243 `.py` | ~79 400 |
| `apps/web/src` — player client | 246 `.ts`/`.tsx` | ~44 000 |
| `apps/web/tests` — Playwright end-to-end | 19 (16 specs) | ~3 350 |
| `apps/admin/src` — operator console | 41 `.ts`/`.tsx` | ~7 100 |

421 commits, 54 Alembic migrations, 25 player routes, three locales (`uz`, `ru`, `en`).

---

## What Is Built

### Backend modules — `apps/api/app/modules/`

Each module owns its `domain/`, `application/`, `infrastructure/`, `presentation/` and
`public/` packages. Cross-module access goes through `public/` only, and that rule is
enforced by `lint-imports`, not by convention — see
[`apps/api/.importlinter`](./apps/api/.importlinter).

| Module | Owns |
| --- | --- |
| `users` | Identity, usernames, privacy fields, presence, preferences |
| `auth` | Registration, sign-in, JWT, refresh tokens, sessions, email verification, password reset |
| `profiles` | Public and private profile reads, profile editing, search |
| `avatars` | Avatar upload, signature validation, EXIF handling, WebP renditions |
| `friends` | Friend requests, friend lists, blocking |
| `engine` | The checkers rules kernel — move generation, validation, multi-capture, kings, draw rules, serialization |
| `game` | Match lifecycle, clocks, live state, durable move log, results, history, replay |
| `matchmaking` | Queue tickets, pairing, reservations, decline cooldowns, friend challenges |
| `rating` | Glicko-2 applied incrementally per match ([ADR-001](./docs/07-decisions/ADR-001-glicko2-incremental.md)), leaderboard reads |
| `tournament` | Single-elimination tournaments — seeding, brackets, rounds, standings |
| `statistics` | Aggregated player and match statistics, match-history projections |
| `notifications` | In-app, email (Resend) and Web Push delivery with per-player preferences |
| `reference` | Time controls and other server-owned reference data |
| `admin` | Operator authorization, audit trail, moderation sanctions |

Cross-cutting packages: `app/gateway` (WebSocket connections, game rooms, move
submission, quick messages), `app/platform` (transactional outbox, events, email, metrics
— may not import any module), `app/core`, `app/config`, `app/database`, `app/operator`.

### Player client — `apps/web`

25 routes: authentication and recovery, own and public profiles, four settings surfaces
plus sessions, friends/requests/blocked, challenges, player search, the `/play` lobby,
the live game room, replay, match history, tournaments, and the notification centre.
Installable as a PWA ([ADR-003](./docs/07-decisions/ADR-003-pwa-service-worker.md)).

### Operator console — `apps/admin`

A separate application on a separate origin: users, matches, tournaments (with the four
tournament commands), the audit log, moderation, notification operations, and a dashboard
that answers each module's own fact in a fixed number of reads.

---

## Folder Structure

```text
Arena64/
├── apps/
│   ├── api/              # Backend — FastAPI, one artifact, three runtime profiles
│   ├── web/              # Player-facing web client — React + Vite, PWA
│   └── admin/            # Operator console — separate app, separate origin
├── docs/                 # Long-lived documentation (index below)
├── specs/                # One specification per product feature, plus per-epic audits
├── prompts/              # Prompt library for AI-assisted development (unpopulated)
├── templates/            # Document skeletons — specs, ADRs, PRs, bug reports, READMEs
└── docker/               # Local development infrastructure — Postgres 17, Redis 8
```

There is **no `packages/` directory**: nothing has yet been needed by two consumers at
once, and a shared package earns its place on the third real use, not the first
prediction (`CLAUDE.md` §3.5, §2.7). `infrastructure/`, `scripts/` and `.github/` do not
exist either — see [Known gaps](#known-gaps).

Placement rules for new code and documents are in
[`docs/02-development/folder-structure.md`](./docs/02-development/folder-structure.md).

---

## Tech Stack

Versions are pinned in [`apps/api/pyproject.toml`](./apps/api/pyproject.toml),
[`apps/web/package.json`](./apps/web/package.json) and
[`apps/admin/package.json`](./apps/admin/package.json), where each non-obvious choice
carries the reasoning that selected it.

| Layer | In use | Governed by |
| --- | --- | --- |
| Backend runtime | Python 3.13, FastAPI, Uvicorn | `architecture.md` AD-01, AD-02 — no ADR yet |
| Persistence | PostgreSQL 17, SQLAlchemy 2 (asyncio) + asyncpg, Alembic | `docs/01-architecture/database.md` (Draft) |
| Realtime | WebSocket gateway profile; Redis pub/sub and streams | `docs/01-architecture/websocket.md` (Approved) |
| Cache / ephemeral | Redis 8 — five role databases (`live`, `bus`, `broker`, `cache`, `limits`) | `architecture.md` AD-03, `caching.md` |
| Background work | Outbox + worker profile | `architecture.md` AD-02 |
| Auth | Argon2 (`argon2-cffi`), JWT (`PyJWT`) | `specs/authentication.md` |
| Email / push | Resend over `httpx`; Web Push via `cryptography` (RFC 8291/8292) | `specs/notifications.md` |
| Player client | TypeScript 5.9, React 19, Vite 8, TanStack Router + Query, Tailwind 4, Radix, Zod | [ADR-002](./docs/07-decisions/ADR-002-frontend-spa.md), [ADR-003](./docs/07-decisions/ADR-003-pwa-service-worker.md) |
| Backend tests | pytest — `unit/`, `integration/`, `contract/`, `e2e/`, plus an engine corpus and `perft` | `docs/02-development/testing.md` (Draft) |
| Frontend tests | Vitest, Testing Library, MSW, Playwright | same |
| Quality gates | `ruff`, `mypy --strict`, `pyright`, `lint-imports`; `eslint`, `prettier`, `tsc --noEmit` | `apps/api/pyproject.toml`, `apps/api/.importlinter` |
| Local infrastructure | Docker Compose — Postgres and Redis only | `docker/docker-compose.yml` |
| CI | **None configured** | [Known gaps](#known-gaps) |

The stack choices above are **in force but not all ratified**: only four ADRs exist, and
the backend platform decisions still live as `AD-nn` entries inside
[`docs/01-architecture/architecture.md`](./docs/01-architecture/architecture.md) awaiting
promotion.

---

## Getting Started

Requires Docker, Python 3.13 with [`uv`](https://docs.astral.sh/uv/), and Node.js ≥ 20.

### 1. Local infrastructure

```bash
docker compose -f docker/docker-compose.yml up -d
```

Postgres listens on host port **55432** (not 5432 — the reasoning is in the compose
file's header) and Redis on 6379. Both are overridable with `POSTGRES_PORT` and
`REDIS_PORT`.

### 2. Backend — `apps/api`

```bash
cd apps/api
uv sync
cp .env.example .env.local          # only `local` reads a file; every other tier uses the environment
uv run alembic upgrade head
uv run uvicorn main:app --reload    # http://localhost:8000, OpenAPI at /openapi.json
```

Configuration layers as `code defaults → .env.local → process environment → secret
manager`. `ENVIRONMENT` selects the tier (`local`, `test`, `ci`, `staging`,
`production`) and defaults to `local`. A file named `.env` is **not** read; startup logs
say which file was opened and whether it existed.

### 3. Player client — `apps/web`

```bash
cd apps/web
npm install
npm run openapi:generate            # regenerate typed API contracts from a running backend
npm run dev
```

### 4. Operator console — `apps/admin`

```bash
cd apps/admin
npm install
npm run dev
```

---

## Quality Gates

Run before opening a pull request. Nothing runs these automatically yet.

```bash
# apps/api
uv run ruff check . && uv run ruff format --check .
uv run mypy .
uv run pyright
lint-imports                        # architecture contracts from .importlinter
uv run pytest                       # requires the Docker services for integration/contract suites

# apps/web and apps/admin
npm run lint && npm run typecheck && npm run format:check
npm run test
npm run test:e2e                    # apps/web only; needs the backend and services running
```

---

## Development Workflow

1. **Specify.** Write or update the feature spec in [`specs/`](./specs/README.md), from
   [`templates/feature-spec.md`](./templates/feature-spec.md). Reach **Approved** before
   implementing.
2. **Decide.** Record significant or hard-to-reverse choices as an ADR from
   [`templates/architecture-decision.md`](./templates/architecture-decision.md).
3. **Branch.** One branch per task, named for it — `feature/A64-0NN.N-slug`. Never commit
   to the default branch.
4. **Implement.** Follow
   [`docs/02-development/CLAUDE.md`](./docs/02-development/CLAUDE.md). One concern per
   change.
5. **Test.** New behaviour gets a test; every fix gets a regression test.
6. **Document.** Update the affected documents *in the same change*.
7. **Review.** Open a pull request using
   [`templates/pull-request.md`](./templates/pull-request.md), linking the spec or ADR it
   implements.
8. **Close the epic with an audit.** Every epic here ends with an audit document that
   records what shipped, what was deferred, and why — see `specs/*/audit.md`.

**Reporting a defect?** Use [`templates/bug-report.md`](./templates/bug-report.md).

---

## Documentation Index

Statuses below are the real ones from each document's own header. A placeholder is listed
as a placeholder.

### 00 — Overview

| Document | Purpose | Status |
| --- | --- | --- |
| [Vision](./docs/00-overview/vision.md) | What Arena64 is and who it serves | Draft — derived from shipped behaviour |
| [Roadmap](./docs/00-overview/roadmap.md) | Delivery history and sequencing | Draft — current |

### 01 — Architecture

| Document | Purpose | Status |
| --- | --- | --- |
| [Architecture Overview](./docs/01-architecture/architecture.md) | Components, boundaries, `AD-nn` decisions | Draft — proposed for review |
| [System Design](./docs/01-architecture/system-design.md) | Runtime flows and failure modes | Draft — proposed for review |
| [Domain Model](./docs/01-architecture/domain-model.md) | Aggregates, identifiers, open questions | Draft — proposed for review |
| [Database](./docs/01-architecture/database.md) | Persistence strategy and data model | Draft — proposed for review |
| [WebSocket](./docs/01-architecture/websocket.md) | Realtime connection and message design | **Approved** |
| [Caching](./docs/01-architecture/caching.md) | Keyspaces, owners, TTLs | **Approved** for the keyspaces that exist |
| [Events](./docs/01-architecture/events.md) | Domain event catalogue | Placeholder |
| [Security](./docs/01-architecture/security.md) | Threat model and controls | Placeholder |

### 02 — Development

| Document | Purpose | Status |
| --- | --- | --- |
| [CLAUDE.md](./docs/02-development/CLAUDE.md) | **Binding engineering instruction manual** | In force |
| [Testing](./docs/02-development/testing.md) | Test strategy and quality gates | Draft — rate-limit rules written, pyramid TBD |
| [Coding Standards](./docs/02-development/coding-standards.md) | Language-level conventions | Placeholder |
| [Git Workflow](./docs/02-development/git-workflow.md) | Branching, commits, releases | Placeholder |
| [Folder Structure](./docs/02-development/folder-structure.md) | Repository layout rules | Placeholder |

### 03 — Backend

| Document | Purpose | Status |
| --- | --- | --- |
| [Services](./docs/03-backend/services.md) | Service layer responsibilities | Draft — proposed for review |
| [Repositories](./docs/03-backend/repositories.md) | Data access abstraction | Draft — proposed for review |
| [Dependency Injection](./docs/03-backend/dependency-injection.md) | Wiring, lifetimes, configuration layering | Draft — proposed for review |
| [API](./docs/03-backend/api.md) | HTTP conventions and endpoint index | Placeholder |

### 04 — Frontend

| Document | Purpose | Status |
| --- | --- | --- |
| [Design System](./docs/04-frontend/design-system.md) | Tokens and component contracts | Placeholder — the built system is described in `specs/product-experience.md` §10 |
| [Routing](./docs/04-frontend/routing.md) | Route map and guards | Placeholder — see `specs/frontend.md` |
| [State Management](./docs/04-frontend/state-management.md) | Client, server, realtime state | Placeholder — see `specs/frontend.md` |

### 07 — Decisions

| Record | Status |
| --- | --- |
| [ADR-001 — Glicko-2 applied incrementally](./docs/07-decisions/ADR-001-glicko2-incremental.md) | Accepted |
| [ADR-002 — Frontend is a single-page application](./docs/07-decisions/ADR-002-frontend-spa.md) | Accepted |
| [ADR-003 — PWA with a service worker](./docs/07-decisions/ADR-003-pwa-service-worker.md) | Accepted |
| [ADR-004 — Quick messages, not free-text chat](./docs/07-decisions/ADR-004-quick-messages-not-free-text-chat.md) | Accepted |

The process is in [`docs/07-decisions/README.md`](./docs/07-decisions/README.md).

### Supporting Directories

| Directory | Purpose |
| --- | --- |
| [`specs/`](./specs/README.md) | Per-feature behaviour and contracts, plus per-epic audits |
| [`templates/`](./templates/) | Skeletons for specs, ADRs, PRs, bugs, and module READMEs |
| [`prompts/`](./prompts/README.md) | Intended prompt library — structure only, no prompts authored |

---

## Known Gaps

Recorded here rather than left to be discovered. Each is a real deviation from what this
repository's own rules require.

| # | Gap | Rule it contradicts |
| --- | --- | --- |
| G-1 | **No CI.** No `.github/`; every gate above is run by hand | `CLAUDE.md` §5.10 — "lint, type checks, and the full test suite pass" before merge |
| G-2 | **No deployment definition.** No `infrastructure/`, no application container; only local Compose | `architecture.md` AD-02 names three runtime profiles that nothing yet deploys |
| G-3 | **Placeholder process docs.** `coding-standards.md`, `git-workflow.md`, `folder-structure.md` are placeholders that `CLAUDE.md` cites as authoritative | `CLAUDE.md` §4.2 — every document declares status and owner |
| G-4 | **No document owners.** Every `Owner` field reads `_Unassigned_` | `CLAUDE.md` §4.2 — "unowned documents rot" |
| G-5 | **Stack decisions unratified.** Backend platform choices live as `AD-nn` notes, not ADRs | `CLAUDE.md` §3.10 |
| G-6 | **Empty prompt library.** `prompts/` holds six READMEs and no prompt | `prompts/README.md` — prompts belong in source control |
| G-7 | **No shared packages.** `apps/web` and `apps/admin` share no code, including the API client | Acceptable today under `CLAUDE.md` §3.5; revisit on the third duplication |

## License

_Not yet determined._
