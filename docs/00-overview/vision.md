# Product Vision

> **Status:** Draft — derived from shipped behaviour, not from a prior product brief
> **Owner:** _Unassigned_
> **Last reviewed:** 2026-09-03

## Purpose

Describes what Arena64 is, who it serves, and the outcome it aims to achieve as a
competitive online checkers platform.

## Scope

Product positioning, target audience, principles, differentiators, and success criteria.
Excludes implementation detail — that is `docs/01-architecture/` and `specs/`.

## How This Document Was Written

This file was a placeholder for the whole of the build. Sixteen epics shipped without it,
so `docs/01-architecture/architecture.md` §1 derived its assumptions from `specs/` and the
root `README.md` instead — and said so.

What follows is therefore **reconstructed from what was actually built and specified**,
with the evidence cited. Every statement is either observable in the repository or
recorded in a spec or ADR. Where a genuine product decision was never made, §9 says so
rather than inventing one. This document does not create new direction; it makes the
direction already encoded in 421 commits legible, so the next decision can be checked
against something.

---

## Problem Statement

Online checkers is served almost entirely by casual apps: a board, an opponent, a result
that evaporates. What is missing is the structure that makes competition mean something —
a rating that persists, results that cannot be corrupted or disputed, rules enforced by
the server rather than trusted from the client, and tournaments a player can enter and
finish.

Arena64 exists for the player who wants their record to count. That commitment is the
platform's load-bearing constraint, stated in `architecture.md` A-4: *"ratings are
competitive and permanent — a corrupted result is unacceptable"*, and it is what dictated
a modular monolith with one transaction per completed match instead of a service topology
with a saga in the middle of the move loop (AD-01).

## Target Audience

| # | Audience | Evidence |
| --- | --- | --- |
| T-1 | **Competitive checkers players** who want a permanent, per-format ladder | `specs/rating.md` — one Glicko-2 rating per `(variant, speed class)`, one adjustment per rated match, enforced at the database |
| T-2 | **Russian-rules (shashki) players**, the platform's single variant | `ProductVariant.RUSSIAN_8X8` is the only playable rule set; the engine's geometry table anticipates 10×10 without offering it |
| T-3 | **Uzbek, Russian and English speakers**, in that order of primacy | Three complete locales in `apps/web/src/shared/i18n/locales/`; Uzbek is not an afterthought translation |
| T-4 | **Phone-first players** | The redesign is measured at 360 / 768 / 1280 px, and the client installs as a PWA ([ADR-003](../07-decisions/ADR-003-pwa-service-worker.md)) |
| T-5 | **Friends playing each other directly**, not only ladder opponents | `specs/friend-challenges.md` — a full challenge lifecycle beside the queue |
| T-6 | **Operators** who must moderate and answer for the platform | `apps/admin` is a separate application on a separate origin, with an audit entry per action |

Not addressed: complete beginners needing tuition, engine/bot practice, and puzzle
training. Nothing has been built for any of them.

## Product Principles

These are the rules the built product has actually followed. Each one has been paid for at
least once.

| # | Principle | What it cost, where |
| --- | --- | --- |
| P-1 | **A result is permanent.** One rating adjustment per rated match, explainable from stored data alone, without re-running the algorithm | Incremental Glicko-2 and a uniqueness constraint at the database — [ADR-001](../07-decisions/ADR-001-glicko2-incremental.md), `specs/rating.md` G-2, G-3 |
| P-2 | **The server owns the rules.** The client never decides legality, and the gateway validates every move against the engine | `app/modules/engine` is a pure kernel with a versioned corpus; `specs/game-engine/traceability.md` |
| P-3 | **Latency is the product.** A player who waits to see their own move perceives the platform as broken | The whole of AD-01's reasoning; live position and clocks in Redis, not in Postgres |
| P-4 | **Exactly once, or not at all.** A completed match writes result, final move and the rating trigger in one transaction | Transactional outbox in `app/platform`, which may import no module |
| P-5 | **Safety by construction, not moderation after the fact.** There is no free-text channel between opponents to police | [ADR-004](../07-decisions/ADR-004-quick-messages-not-free-text-chat.md) — server-owned quick messages, and `specs/chat.md` deliberately deferred |
| P-6 | **Privacy is a contract, not a setting that decorates.** What a visitor may see is decided server-side per field | `app/modules/users/domain/privacy.py`, `visibility.py`; `specs/profile.md` privacy rules |
| P-7 | **Never invent data to fill a surface.** Where the UI wants a field the API does not offer, the design changes — not the contract | `specs/product-experience.md` §2, §12.8; and §14, where seat ratings became a *match* fact rather than a profile read |
| P-8 | **Every epic closes with an audit** that records what shipped, what was deferred, and why | Six `specs/*/audit.md` documents, plus seven epics closed by an audit task recorded in the spec itself |
| P-9 | **Boundaries are enforced, not requested.** A module is reachable only through its `public/` package | `apps/api/.importlinter` — `lint-imports` fails the build, and each exception carries its argument |

## Differentiators

| # | Differentiator | Status |
| --- | --- | --- |
| D-1 | Permanent Glicko-2 rating per format, with a full auditable adjustment history | Built |
| D-2 | Server-authoritative rules kernel with a versioned rule corpus and `perft` verification | Built |
| D-3 | No free-text chat, by decision — quick messages only, mutable and blockable | Built |
| D-4 | Single-elimination tournaments with seeding, brackets, rounds and standings | Built |
| D-5 | Installable, offline-aware PWA with Web Push | Built |
| D-6 | Trilingual product (uz / ru / en) as a first-class property, not a localisation phase | Built |
| D-7 | A real operator console — moderation, audit trail, notification operations | Built |
| D-8 | Spectating with player/spectator channel separation | Gateway support exists; **no player-facing surface** |
| D-9 | Leaderboard as a competitive destination | API and read model exist; **no player-facing surface** |

D-8 and D-9 are the honest edge of the product: the platform can do both, and a player
cannot yet reach either.

## Success Metrics

**None of these have been agreed.** They are proposed here because a vision without
measurable intent cannot be reviewed, and are the first thing an owner should accept or
replace. `architecture.md` A-3 already commits the architecture to a scale target —
~500k registered, ~50k peak concurrent connections, ~20k concurrent matches — so the
sizing question is settled even though the product question is not.

| # | Proposed metric | Why this one |
| --- | --- | --- |
| M-1 | Share of finished matches that are **rated and uncontested** | Directly measures P-1, the platform's core promise |
| M-2 | Median time from joining the queue to move one | Measures the lobby and pairing experience, the funnel's narrowest point |
| M-3 | Share of matches that end in a **result rather than an abandonment** | Abandonment is the competitive product's real churn |
| M-4 | Repeat play within seven days | Whether a permanent ladder actually motivates return |
| M-5 | Move round-trip latency at p95 | P-3 stated as a budget instead of an intention (`CLAUDE.md` §10.10) |

## Non-Goals

Each was deliberately declined, and by whom.

| # | Not building | Decided in |
| --- | --- | --- |
| N-1 | Free-text chat between players | [ADR-004](../07-decisions/ADR-004-quick-messages-not-free-text-chat.md) |
| N-2 | Variants other than Russian 8×8 — the engine's geometry table anticipates them, the product does not offer them | `app/modules/game/domain/variants.py` |
| N-3 | Correspondence time controls — the `SpeedClass` member exists, the catalogue has no row | `app/modules/reference/domain/time_control.py` |
| N-4 | Tournament formats beyond single elimination | `specs/tournament.md` — "Approved for v0.x — Single Elimination only" |
| N-5 | Rating seasons, resets and rewards | `specs/rating.md` N-2 — only the nullable column exists |
| N-6 | Fair-play / cheat detection | `specs/rating.md` N-3 — only `is_frozen` exists |
| N-7 | Multi-region active-active deployment | `architecture.md` A-6 |
| N-8 | Native mobile applications | The PWA is the mobile answer — [ADR-003](../07-decisions/ADR-003-pwa-service-worker.md) |
| N-9 | Monetisation of any kind | Nothing in `specs/` mentions payment, subscription or advertising |
| N-10 | Engine opponents, puzzles, or tuition | No spec, no module |

## Open Questions for the Owner

These are product decisions that were never made. They are listed rather than guessed.

| # | Question | Blocked work |
| --- | --- | --- |
| OQ-1 | Are the metrics in §Success Metrics the right ones, and what are their targets? | Any performance budget assertable in CI (`CLAUDE.md` §10.10) |
| OQ-2 | Does the leaderboard become a player-facing destination, and when? | D-9; the API is already there |
| OQ-3 | Is spectating a v1 product surface or a later one? `architecture.md` A-5 calls it first-class and load-bearing for the channel split | D-8; if the answer is "later", A-5 should be restated |
| OQ-4 | Is there a public, anonymous marketing surface, or is `/` always the player's home? | Closed for the epic in `specs/product-experience.md` OQ-1 — *"a public marketing page is a separate surface for a separate audience"* — but never decided as a product question |
| OQ-5 | Who owns this document, and every other `_Unassigned_` document? | `CLAUDE.md` §4.2 |

## Related Documents

- `docs/00-overview/roadmap.md` — what shipped, in what order, and what is next
- `docs/01-architecture/architecture.md` — the assumptions this vision was reconstructed against
- `specs/product-experience.md` — the player-facing experience and the current redesign
- `docs/07-decisions/` — the four accepted decision records cited above
