# Arena64

**Arena64** is an online multiplayer checkers platform built for competitive play —
realtime matches, skill-based matchmaking, ratings, leaderboards, and spectating.

> **Project status:** Foundation. The repository structure, engineering standards, and
> specification process are in place; application implementation has not begun.
> Feature behaviour is specified in [`specs/`](./specs/) before it is built.

---

## Overview

| | |
| --- | --- |
| **Domain** | Competitive online checkers |
| **Shape** | Monorepo — multiple applications over shared packages |
| **Core capabilities** | Realtime gameplay, matchmaking, rating, social, spectating, moderation |
| **Current phase** | Foundation and specification |

Product direction is defined in [`docs/00-overview/vision.md`](./docs/00-overview/vision.md);
delivery sequencing is tracked in [`docs/00-overview/roadmap.md`](./docs/00-overview/roadmap.md).

---

## Folder Structure

```text
Arena64/
├── apps/                 # Deployable applications
│   ├── api/              # Backend service — HTTP + realtime
│   ├── web/              # Player-facing web client
│   └── admin/            # Administration and moderation client
├── packages/             # Shared, versioned internal packages
│   ├── shared/           # Cross-cutting utilities and domain helpers
│   ├── types/            # Shared contracts and type definitions
│   ├── ui/               # Shared UI component library
│   └── config/           # Shared configuration and tooling presets
├── docs/                 # Long-lived documentation (see index below)
├── specs/                # One specification per product feature
├── prompts/              # Version-controlled prompt library for AI agents
├── templates/            # Reusable document templates
├── infrastructure/       # Infrastructure and deployment definitions
├── docker/               # Container definitions and compose files
├── scripts/              # Developer and operational scripts
└── .github/              # CI workflows and repository configuration
```

Placement rules for new code and documents are defined in
[`docs/02-development/folder-structure.md`](./docs/02-development/folder-structure.md).

---

## Tech Stack

> These are the **intended** platform choices for the monorepo layout above. Each is
> pending confirmation in an Architecture Decision Record before implementation begins —
> see [`docs/07-decisions/`](./docs/07-decisions/README.md).

| Layer | Intended choice | Decision record |
| --- | --- | --- |
| Backend runtime | Python + FastAPI | _Pending ADR_ |
| Frontend | TypeScript + Next.js (React) | _Pending ADR_ |
| Realtime transport | WebSocket | _Pending ADR_ |
| Primary datastore | Relational (SQL) | _Pending ADR_ |
| Cache / ephemeral state | In-memory key–value store | _Pending ADR_ |
| Containerisation | Docker | _Pending ADR_ |
| CI | GitHub Actions | _Pending ADR_ |

Nothing is installed yet; no dependency has been committed to.

---

## Documentation Index

### 00 — Overview

| Document | Purpose |
| --- | --- |
| [Vision](./docs/00-overview/vision.md) | What Arena64 is and who it serves |
| [Roadmap](./docs/00-overview/roadmap.md) | Milestones and delivery sequence |

### 01 — Architecture

| Document | Purpose |
| --- | --- |
| [Architecture Overview](./docs/01-architecture/architecture.md) | Components and boundaries |
| [System Design](./docs/01-architecture/system-design.md) | End-to-end flows and failure modes |
| [Database](./docs/01-architecture/database.md) | Persistence strategy and data model |
| [WebSocket](./docs/01-architecture/websocket.md) | Realtime connection and message design |
| [Events](./docs/01-architecture/events.md) | Domain event catalogue and delivery |
| [Caching](./docs/01-architecture/caching.md) | Cache layers and invalidation |
| [Security](./docs/01-architecture/security.md) | Threat model and controls |

### 02 — Development

| Document | Purpose |
| --- | --- |
| [CLAUDE.md](./docs/02-development/CLAUDE.md) | **Binding engineering instruction manual** |
| [Coding Standards](./docs/02-development/coding-standards.md) | Language-level conventions |
| [Git Workflow](./docs/02-development/git-workflow.md) | Branching, commits, releases |
| [Testing](./docs/02-development/testing.md) | Test strategy and quality gates |
| [Folder Structure](./docs/02-development/folder-structure.md) | Repository layout rules |

### 03 — Backend

| Document | Purpose |
| --- | --- |
| [API](./docs/03-backend/api.md) | HTTP conventions and endpoint index |
| [Services](./docs/03-backend/services.md) | Service layer responsibilities |
| [Repositories](./docs/03-backend/repositories.md) | Data access abstraction |
| [Dependency Injection](./docs/03-backend/dependency-injection.md) | Wiring and lifetimes |

### 04 — Frontend

| Document | Purpose |
| --- | --- |
| [Design System](./docs/04-frontend/design-system.md) | Tokens and component contracts |
| [Routing](./docs/04-frontend/routing.md) | Route map and guards |
| [State Management](./docs/04-frontend/state-management.md) | Client, server, realtime state |

### 07 — Decisions

| Document | Purpose |
| --- | --- |
| [Decision Records](./docs/07-decisions/README.md) | ADR process, lifecycle, and index |

### Supporting Directories

| Directory | Purpose |
| --- | --- |
| [`specs/`](./specs/README.md) | Per-feature behaviour and contracts |
| [`prompts/`](./prompts/README.md) | Curated prompts for AI-assisted development |
| [`templates/`](./templates/) | Skeletons for specs, ADRs, PRs, bugs, and module READMEs |

---

## Development Workflow

The default path from idea to merged change:

1. **Specify.** Write or update the feature spec in [`specs/`](./specs/README.md), starting
   from [`templates/feature-spec.md`](./templates/feature-spec.md). Get it to **Approved**
   before implementing.
2. **Decide.** If the work requires a significant or hard-to-reverse technical choice,
   record it as an ADR from
   [`templates/architecture-decision.md`](./templates/architecture-decision.md).
3. **Branch.** Create a branch per
   [`docs/02-development/git-workflow.md`](./docs/02-development/git-workflow.md). Never
   commit to the default branch.
4. **Implement.** Follow
   [`docs/02-development/CLAUDE.md`](./docs/02-development/CLAUDE.md) and
   [`coding-standards.md`](./docs/02-development/coding-standards.md). Keep one concern per
   change.
5. **Test.** Add the tests required by
   [`docs/02-development/testing.md`](./docs/02-development/testing.md) — new behaviour
   gets a test, every fix gets a regression test.
6. **Document.** Update the affected documents *in the same change*. Stale documentation
   makes a pull request incomplete.
7. **Review.** Open a pull request using
   [`templates/pull-request.md`](./templates/pull-request.md), linking the spec, issue, or
   ADR it implements.
8. **Merge.** Lint, type checks, and the full test suite must pass, with review approval.

**Reporting a defect?** Use [`templates/bug-report.md`](./templates/bug-report.md).
**Adding a module?** Give it a README based on
[`templates/module-readme.md`](./templates/module-readme.md).

---

## Working With AI Agents

[`docs/02-development/CLAUDE.md`](./docs/02-development/CLAUDE.md) is the binding
instruction manual for every Claude Code session on this repository. It outranks any
prompt in [`prompts/`](./prompts/README.md). Read it before making changes.

---

## Getting Started

No toolchain is installed yet. Setup instructions will be added here once the stack
decisions in [`docs/07-decisions/`](./docs/07-decisions/README.md) are accepted and the
first application scaffolds land.

## License

_Not yet determined._
