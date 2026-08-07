# Testing Strategy

> **Status:** Draft — placeholder
> **Owner:** _Unassigned_
> **Last reviewed:** _Not yet reviewed_

## Purpose

Defines the test pyramid, coverage expectations, and quality gates for every change.

## Rate limits in the suites — A64-021.6

The limiter is **on** in every environment, including `test` and `ci`. A
suite that only passes with it disabled never exercises the thing that
ships, and the first regression in it is found in production.

What changes is the ceiling. `ENVIRONMENT=test` (and `ci`) applies a ×100
multiplier to every declared limit — see
`docs/01-architecture/security.md` for the mechanism and why production is
untouched. That is sized so a suite whose whole traffic comes from one
address can be run repeatedly without anybody first learning to clear
buckets.

Two things follow for anybody writing a test:

**Do not raise a limit to make a test pass.** If a suite exhausts even the
scaled budget, it is making far more requests than a person would and that
is the finding. `python -m app.operator.rate_limits show` prints what is
actually in force.

**A test that asserts a limit *fires* must override the settings**, not
send a hundred times as many requests. `RateLimit` holds a resolver rather
than concrete rules precisely so `dependency_overrides` can lower a limit
for one test — see its docstring.


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

### The architecture gate — A64-013.8, extended by A64-014.1

`import-linter` reads the real import graph and checks every contract defined in
`apps/api/.importlinter`. The contract set has grown as new modules were added; the rules below
were already written down and, before automated enforcement, were checked only by review:

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

**A64-014.1 added three contracts and one source.** `matchmaking` joins the
privacy and layering rules like every other module, and gains one of its own:

- **`no module depends on matchmaking`** — nothing outside it may import any
  part of it, including its `public/` package. R-6 forbids cycles "including
  through events" and import-linter has no general cycle detector; this is
  the strongest statement the graph *can* check, and it happens to make a
  cycle through the new module unconstructible. It is written **before**
  `matchmaking` has any consumers, which is the difference between a rule
  that fails on the pull request introducing a violation and one discovered
  in an audit. It is expected to be *relaxed* by the first real consumer —
  a visible, argued diff — rather than deleted.
- **`app.platform.tasks` is framework-free**, alongside `app.platform.events`
  and the module domains. A `TaskRequest` must be constructible in a unit
  test with no framework and encodable by a broker that knows none; the
  moment it could hold a SQLAlchemy object, AD-17's Celery migration would be
  blocked by a payload nobody can serialise — and the failure would surface
  on the day of the migration rather than on the change that caused it.

The gate now also runs as a test (`tests/unit/test_import_contracts.py`),
which shells out to the same `lint-imports` command. A gate that lives only
in a pipeline definition is one a contributor discovers after pushing; one
that lives only in the suite is one a pipeline can forget to run.

**Why an import graph and not a review checklist.** Both were in place before
this task; only one of them found the three violations A64-013.8 fixed —
including a cache port that had the application layer building Redis keys,
which had passed review twice.

## Game Engine verification — A64-014.9

The engine has a verification layer above its per-rule suites, specified in
`specs/game-engine.md` §9. Four things are worth knowing from here:

| Check | What it buys |
| --- | --- |
| **Perft** | The only **external** oracle in the repository. English 8x8 node counts are checked against the long-published English/American checkers series; everything else in the suite asks the engine whether it agrees with itself |
| **Corpus audit** | The corpus is audited as *files* — every entry round-trips through production serialization, so a corpus and a stored game cannot drift into two encodings |
| **Replay consistency** | Every corpus replay is walked prefix by prefix, so a replay is verified to reconstruct a game's *history* rather than its final board |
| **Performance sanity** | Blow-up detectors an order or two above the observed numbers. Not budgets — see §9.5 for why, and for the measured figures against CP-1 |

Two limits are recorded rather than hidden. There is **no published Russian
8x8 perft table** available, so the Russian numbers past depth 4 are a
characterization baseline rather than a verification. And **differential
testing is deferred until the TypeScript engine exists** (AD-14): until then
the corpus proves conformance to a contract, not agreement between two
implementations, and a bug both would share is exactly what the second one is
for.

`ENGINE_PERFT_DEEP` opts into the depth-6 runs, which cost about four seconds
per variant and are skipped by default.

## TODO

- [ ] Assign a document owner
- [ ] Draft the sections above
- [ ] Link related decision records in `docs/07-decisions/`
- [ ] Review and promote status from Draft to Approved
