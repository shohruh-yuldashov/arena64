# Product Roadmap

> **Status:** Draft — current as of commit `43cffd3`, 2026-09-03
> **Owner:** _Unassigned_
> **Last reviewed:** 2026-09-03

## Purpose

Tracks the milestones delivered and the sequence in which remaining capabilities are
delivered.

## Scope

Milestone definitions, ordering, and exit criteria. Excludes task-level tracking, which
lives in the branch and pull-request names this table cites.

## How This Document Was Written

This file was a placeholder while sixteen epics shipped through it. The history below is
**reconstructed from the repository** — merge commits, branch names, and the audit
document that closed each epic — not from a plan that existed beforehand. It is recorded
so that the sequence is auditable and the remaining work has somewhere to be ordered.

Work is identified as `A64-<epic>.<task>`. One branch and one pull request per task, one
audit per epic.

---

## Milestone Overview

Build window: **2026-07-31 → 2026-08-10**, 421 commits, 111 merged pull requests.

| Epic | Capability | PRs | Closed by | Evidence |
| --- | --- | --- | --- | --- |
| `A64-001…008` | Repository foundation — engineering manual, templates, spec and ADR process, folder rules | — (direct commits, `task_001…009`) | — | `docs/02-development/CLAUDE.md`, `templates/` |
| `A64-009` | Authentication foundation | #1 | — | `app/modules/auth` |
| `A64-010` | User module — identity, value objects, validators | #2 | — | `app/modules/users` |
| `A64-011` | Authentication: registration, sign-in, JWT, refresh tokens, email verification, password reset | #3–#10 | `A64-011.9` audit | `specs/authentication.md` |
| `A64-012` | Profile: public profile, avatars, editing, privacy, preferences, statistics, presence | #11–#18 | `A64-012.8` audit | `specs/profile.md` |
| `A64-013` | Social: search, friend requests, lists, management, blocking, presence, notifications | #19–#26 | `A64-013.8` audit | `specs/friends.md`; `.importlinter` was written here |
| `A64-014` | Game engine: move generation, validation, multi-capture, kings, lifecycle, draws, serialization | #27–#36 | `A64-014.10` audit | `specs/game-engine/audit.md`, `traceability.md`, rule corpus v1/v2 |
| `A64-015` | Matchmaking: queue domain, management, pairing, persistence, recovery | #37–#42 | `A64-015.6` audit | `specs/matchmaking/audit.md` |
| `A64-016` | Live game: WebSocket gateway, rooms, move submission, durable move log | #43–#47 | `A64-016.8` audit | `specs/live-game/audit.md`, `websocket.md` (**Approved**) |
| `A64-017` | Rating: Glicko-2 applied incrementally, leaderboard reads | #48 | `A64-017.6` audit | `specs/rating/audit.md`, [ADR-001](../07-decisions/ADR-001-glicko2-incremental.md) |
| `A64-018` | Game history and replay | **none of its own — landed inside #49** | `A64-018.4` audit | `specs/replay/audit.md` |
| `A64-019` | Tournaments: seeding, brackets, rounds, standings, entry points | #49–#50 | `A64-019.7` audit | `specs/tournament/audit.md` |
| `A64-020` | Frontend: foundation, auth UI, profile, social, lobby, live game, controls, replay, history, tournaments, PWA | #51–#65 | — | `specs/frontend.md`, [ADR-002](../07-decisions/ADR-002-frontend-spa.md), [ADR-003](../07-decisions/ADR-003-pwa-service-worker.md) |
| `A64-021` | Notifications: in-app, realtime, preferences, event coverage, email (Resend), Web Push | #66–#75 | `A64-021.7` audit | `specs/notifications.md` |
| `A64-022` | Friend challenges: foundation, API, match integration, realtime, UI, hardening | #76–#83 | `A64-022.7` audit | `specs/friend-challenges.md` — **production-ready** |
| `A64-023` | Quick messages — the answer to chat | #86–#89 | `A64-023.4` audit | `specs/quick-messages.md` — **complete**, [ADR-004](../07-decisions/ADR-004-quick-messages-not-free-text-chat.md) |
| `A64-024` | Operator console: authorization, users, matches, tournaments, audit log, moderation, notification operations, dashboard | #90–#103 | `A64-024.10` audit | `specs/admin.md` |
| — | Full-suite sweep — repaired suites that had been failing unread | #104 | — | `5118f08` |
| `A64-025` | Product-experience redesign | #105– | **in progress** | `specs/product-experience.md` |

Two cross-cutting fixes were merged outside an epic: `A64-friend-challenge-create-404`
(#83) and `A64-rating-result-contract` (#84–#85).

### What the history shows about the process

- **Every epic but three ended in an audit** that lists what was deferred and why — six
  as their own `specs/*/audit.md`, seven as a closing task recorded inside the spec. The
  exceptions are `A64-009`, `A64-010` (foundations) and `A64-020` (frontend), the largest
  epic in the history at fifteen pull requests.
- **`A64-018` never got its own pull request.** Its work rode in on the tournament
  branch (#49) even though it has a spec and a closing audit. One concern per pull
  request (`CLAUDE.md` §5.3) was not held there.
- **`A64-024` merged out of numeric order** (#97 = `.8` before #98 = `.6`), and needed
  three follow-up fixes for router prefixes and uncommitted transactions — the cost of a
  wide epic on a surface with no CI to catch a mounting mistake.

## Current Milestone — `A64-025`, Product Experience

The redesign changes how the built product is presented; it adds no features and changes
no domain logic (`specs/product-experience.md` §2). The phase plan and its dependencies
live in that spec §5; this is its state.

| Task | Scope | State |
| --- | --- | --- |
| `.1` | Current-state UX audit — the findings P0-1…P3-6 | **Done** (#105) |
| `.2` | Design-system foundation — brand and semantic tokens, missing primitives, shared list/notice states | **Done** (#107) |
| `.3` | App shell, product home, navigation | **Done** (#106) |
| `.4` | Authentication UX | **Done** (#108) |
| `.5` / `.5a` | Lobby and matchmaking; visual polish | **Done** (#109, #110) |
| `.6` / `.6A` / `.6B` | Game room — seats, clocks, low-time state, seat ratings | **Done** (#111 and the current branch) |
| `.7` | Profile and social | Not started |
| `.8` | Tournament and bracket — edges from `BracketSlot.parent()`; closes OQ-4 | Not started |
| `.9` | Notifications | Not started |
| `.10` | Email design system — fixes P2-2, the English-only plain-text reset mail | Not started |
| `.11` | Responsive and mobile polish | Not started, depends on `.3`–`.9` |
| `.12` | Accessibility and motion — fixes P3-5 | Not started, depends on `.3`–`.9` |
| `.13` | Closing audit | Not started |

## Upcoming Milestones

Ordered by what the product cannot ship without. Numbers are reserved, not planned dates.

| # | Milestone | Why it is next | Blocks |
| --- | --- | --- | --- |
| U-1 | **Continuous integration** | Every gate in `README.md` is run by hand; `CLAUDE.md` §5.10 requires them green before merge, and #101 and #104 are what its absence already cost | Nothing — this is the cheapest correction available |
| U-2 | **Deployment definition** | `architecture.md` AD-02 names three runtime profiles and nothing deploys them. There is no staging or production environment | Everything below reaching a player |
| U-3 | **Leaderboard surface** | The API, read model and keyset pagination exist; no player can reach them (`vision.md` D-9) | The ladder being visible is most of what a permanent rating is for |
| U-4 | **Ratify the stack decisions** | Backend platform choices are `AD-nn` notes in `architecture.md`, not ADRs (`CLAUDE.md` §3.10) | Nothing, but it gets more expensive to write the longer it waits |
| U-5 | **Promote the placeholder process docs** | `coding-standards.md`, `git-workflow.md`, `folder-structure.md` are placeholders that `CLAUDE.md` cites as authoritative | Consistent review of anything they were meant to govern |
| U-6 | **Spectator surface, or restate A-5** | `architecture.md` A-5 makes spectator fan-out load-bearing for the channel split; no surface exists. One of the two must move | `vision.md` OQ-3 |
| U-7 | **Settings, statistics and leaderboard specs** | Their features are built; their specs are still placeholders, so behaviour lives only in code | `CLAUDE.md` §4.1 |
| U-8 | **Performance budgets** | `CLAUDE.md` §10.10 asks for stated budgets asserted in CI; none are stated | Depends on U-1 |

## Deferred / Backlog

Deliberate declines, each with the record that decided it. Reopening any of them means
amending that record, not quietly building it.

| Item | Decided in | What would reopen it |
| --- | --- | --- |
| Free-text chat | [ADR-004](../07-decisions/ADR-004-quick-messages-not-free-text-chat.md) | A moderation capability that makes it safe |
| Rating seasons, resets, rewards | `specs/rating.md` N-2 | A product decision that the ladder should reset |
| Fair-play / cheat detection | `specs/rating.md` N-3 | Evidence of engine assistance at scale |
| Rating periods (batch, non-incremental) | `specs/rating.md` N-1, ADR-001 | A measured cost problem with incremental updates |
| Tournament formats beyond single elimination | `specs/tournament.md` | Demand, and a spec |
| Correspondence time controls | `time_control.py` | A row and a `SpeedClass` member — "not a redesign" |
| Variants beyond Russian 8×8 | `game/domain/variants.py` | A product decision; the engine geometry is ready |
| Admin broadcast messaging | `A64-024` hardening notes | An operator need that survives review |
| Storybook or a component gallery route | `specs/product-experience.md` §11 | Nothing — declined because `A64-025.3` removed exactly such a surface |
| Shared `packages/` for web and admin | `CLAUDE.md` §3.5 | A third real duplication |

## Release History

No release has been cut. There is no tag, no version stamp beyond `0.1.0` in the
manifests, and no deployed environment. Versions named in specs — `v0.5.0` for rating,
`v0.6.0` for replay — are **spec scope markers, not shipped releases**.

## Related Documents

- `docs/00-overview/vision.md` — what the product is for, and the open product questions
- `specs/README.md` — per-feature specifications and their real statuses
- `README.md` §Known Gaps — the same deviations, stated where a new contributor lands
- `docs/07-decisions/README.md` — how a decision above gets amended
