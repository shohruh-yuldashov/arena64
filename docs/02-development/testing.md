# Testing Strategy

> **Status:** Draft — placeholder
> **Owner:** _Unassigned_
> **Last reviewed:** _Not yet reviewed_

## Purpose

Defines the test pyramid, coverage expectations, and quality gates for every change.

## Scope

Test categories, tooling, fixtures, and CI gating. Excludes individual test cases.

## Test Pyramid

_TBD._

## Unit Tests

_TBD._

## Integration Tests

_TBD._

## End-to-End Tests

_TBD._

## Realtime / WebSocket Testing

_TBD._

## Fixtures & Test Data

_TBD._

## Coverage Thresholds

_TBD._

## CI Quality Gates

> Filled in by A64-013.8 for the gates that **exist and run today**. The rest
> of this document is still a placeholder; this section is not, and a gate
> listed here has been verified to fail on a real violation.

Every one of these runs from `apps/api/`, and a red result blocks a merge
(CLAUDE.md §5.10 — "green before merge").

| Gate | Command | Fails on |
| --- | --- | --- |
| Formatting | `ruff format --check app tests` | A file that would be reformatted |
| Lint | `ruff check app tests` | Any configured rule |
| Types | `mypy app` | Strict-mode error, including an unused `type: ignore` |
| **Architecture** | `lint-imports` | Any contract in `apps/api/.importlinter` |
| Tests | `pytest` | A failing or erroring test |
| Migrations | `alembic upgrade head` then `alembic downgrade -1` | An irreversible or non-applying revision |

### The architecture gate — A64-013.8

`import-linter` reads the real import graph and checks fifteen contracts
(thirteen at A64-013.8; two more added by A64-014.1). They encode rules that
were already written down and, until this task, were enforced only by review:

- **`app.platform` imports no bounded context.** The outbox belongs to the
  platform (database.md §232), and the moment it imports `friends` for "just
  one type", every future producer depends on `friends` transitively.
- **A module is reachable only through its `public/` package** — one contract
  per owner (BE-03, architecture.md R-1). The *source* modules are each
  module's `domain`, `application` and `infrastructure` layers;
  `presentation/dependencies` is the composition root and is deliberately
  outside the rule, because assembling other modules' concrete classes is
  what a composition root is for.
- **Dependencies point inward** — one `layers` contract per module.
- **Domain layers import no framework** — no SQLAlchemy, FastAPI, Starlette
  or Redis reachable from an aggregate (architecture.md §8).
- **The rules kernel imports nothing but the shared kernel** — A64-014.1.
  AD-13 gives `engine` "no I/O, no clock, no randomness, no logging, no
  framework, no database, no configuration", and the contract forbids each
  by name, `logging`, `random` and `datetime` included. Everything that
  makes an engine trustworthy — perft counts, apply/undo property tests,
  differential testing against the TypeScript engine — needs purity, and
  the way purity is lost is a reporting requirement satisfied with a query
  inside a rules function, which no test would turn red.
- **Only `game`, `replay` and `fairplay` may import `engine`** — R-2.
  None of the three exists yet, so the contract names every module that
  does and forbids all of them; each new module joins that list unless it
  is one of the three.

Three imports are exempted, each with the argument recorded beside it in the
config rather than merely silenced. Adding a fourth without one defeats the
file.

**Why an import graph and not a review checklist.** Both were in place before
this task; only one of them found the three violations A64-013.8 fixed —
including a cache port that had the application layer building Redis keys,
which had passed review twice.

## TODO

- [ ] Assign a document owner
- [ ] Draft the sections above
- [ ] Link related decision records in `docs/07-decisions/`
- [ ] Review and promote status from Draft to Approved
