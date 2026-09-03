# Feature Specifications

This directory holds one specification per product feature of Arena64. A spec is the
single source of truth for **what** a feature does and **why** — written and agreed before
implementation, and kept level with the behaviour that shipped.

## Rules

- One feature per file; keep filenames lowercase and hyphenated.
- Start every new spec from `templates/feature-spec.md`.
- A spec describes behaviour, contracts, and acceptance criteria — not implementation code.
- Cross-cutting design belongs in `docs/01-architecture/`, not here.
- Update the spec **before** changing behaviour, and link the pull request that implements it.
- **Close every epic with an audit.** A closing task writes `specs/<feature>/audit.md`
  recording what shipped, what was deferred, and the evidence for both. Six exist, and
  seven further epics were closed by an audit task recorded inside the spec itself.

## Status Legend

The first six values are the intended lifecycle. The last three are in active use because
several specs were written *alongside* an epic rather than before it, and they say
something the lifecycle cannot.

| Status | Meaning |
| --- | --- |
| Placeholder | File exists, content not yet written |
| Draft | Under active authoring, not agreed |
| Review | Awaiting sign-off |
| Approved | Agreed and ready for implementation |
| Implemented | Shipped; spec reflects live behaviour |
| Deferred | Deliberately not built; the record explains why and what would reopen it |
| **Partial** | Some sections specify shipped behaviour; named sections are still unwritten |
| **Production-ready / Complete** | Shipped and audited end to end |
| **Ready with documented limitations** | Shipped and audited, with named gaps recorded in the audit rather than fixed |

## Index

Statuses below are copied from each file's own header. Where a spec's header contradicts
the code, the row says so.

| Spec | Description | Status |
| --- | --- | --- |
| [Authentication](./authentication.md) | Registration, sign-in, session issuance, credential recovery | **Partial** — browser session (A64-020.2) and email verification (A64-021.5H) specified; the JSON surface is a placeholder |
| [Player Profile](./profile.md) | Public and private profile data, avatars, display identity, visibility | **Placeholder** for the backend contract; the client is `frontend.md` §13 |
| [Friends](./friends.md) | Friend requests, lists, blocking, presence visibility | **Placeholder** for the backend contract; viewer relationship and client specified in `frontend.md` §14 |
| [Friend Challenges](./friend-challenges.md) | Direct challenge lifecycle between friends | **Production-ready** — audited A64-022.7; consolidated contract in §28 |
| [Quick Messages](./quick-messages.md) | Predefined, server-owned courtesies between the two players. No free text | **Complete** — A64-023.1 … .4; scope matrix in §0 |
| [Chat](./chat.md) | Free-text messaging | **Deferred and superseded** by quick messages — [ADR-004](../docs/07-decisions/ADR-004-quick-messages-not-free-text-chat.md) |
| [Notifications](./notifications.md) | In-app, email and Web Push delivery, and per-player preferences | **Production-ready** — audited A64-021.7 |
| [Game Engine](./game-engine.md) | Rules enforcement, move validation, board state, termination | **Ready with documented limitations** — A64-014.10. Three draw thresholds remain undecided product rules (§7.7); differential testing awaits a TypeScript engine (§9.6) |
| [Matchmaking](./matchmaking.md) | Queueing, pairing, match creation and acceptance, recovery, retention | **Partial** — §1–§13 implemented through A64-015.6; challenges unspecified here (see `friend-challenges.md`) |
| [Rating](./rating.md) | Glicko-2 skill rating, provisional ratings, deviation inflation | **Approved** — audited A64-017.6, [ADR-001](../docs/07-decisions/ADR-001-glicko2-incremental.md) |
| [Leaderboard](./leaderboard.md) | Ranked listings, scopes, keyset pagination | **Approved** — API and read model built; **no player-facing surface exists** |
| [Statistics](./statistics.md) | Aggregated player and match statistics as a projection | **Implemented** — A64-020.5F |
| [Match History and Replay](./replay.md) | History listing and move-by-move replay | **Approved for v0.6.0** — audited A64-018.4 |
| [Tournament](./tournament.md) | Registration, seeding, brackets, rounds, standings | **Approved for v0.x — single elimination only**; audited A64-019.7 |
| [Spectator](./spectator.md) | Live match observation and eligibility | **Implemented** — A64-016.7. Eligibility is a *defaulted* product decision (§3), and there is **no player-facing surface** |
| [Settings](./settings.md) | Per-player gameplay, notification, privacy and appearance preferences | **Placeholder** — five settings surfaces ship in `apps/web` against no spec |
| [Admin](./admin.md) | Operator authorization, the server-authoritative boundary, the separate console | **Implemented and audited** — A64-024.1 … .10; six read surfaces, plus account restrictions and the four tournament commands. Remaining surfaces deferred, `admin.md` §8 |
| [Frontend Foundation](./frontend.md) | Stack, layers, routing, providers, per-phase client implementation | **Approved through A64-021.4** |
| [Product Experience](./product-experience.md) | Player-facing experience: UX audit, design principles, the A64-025 redesign | **Draft** — epic in progress; §5 holds the phase plan |

## Supporting Material

| Path | Contents |
| --- | --- |
| `game-engine/audit.md`, `game-engine/traceability.md` | Closing audit, and GE-1 … GE-101 rule by rule |
| `game-engine/corpus/v1/`, `v2/` | Versioned rule corpus — men, captures, rejections, kings, draws, replays, terminal positions |
| `live-game/audit.md` | Live Game epic closing audit — A64-016.8 |
| `matchmaking/audit.md` | Matchmaking epic closing audit — A64-015.6 |
| `rating/audit.md` | Rating epic closing audit — A64-017.6 |
| `replay/audit.md` | History & Replay epic closing audit — A64-018.4 |
| `tournament/audit.md` | Tournament epic closing audit — A64-019.7 |

## Where Specification Lags Implementation

Recorded rather than left to be discovered. Each row is behaviour that ships today and is
described only by code and tests — a direct deviation from `CLAUDE.md` §4.1.

| # | Built | Specified |
| --- | --- | --- |
| L-1 | Five settings surfaces (`/settings/{profile,preferences,privacy,notifications,sessions}`) | `settings.md` is a placeholder |
| L-2 | Profile backend — reads, editing, privacy field rules, avatars | `profile.md` is a placeholder for the backend contract |
| L-3 | Friends backend — requests, lists, blocking, presence | `friends.md` is a placeholder for the backend contract |
| L-4 | The authentication JSON API | `authentication.md` specifies only the browser session and email verification |
| L-5 | Challenge behaviour inside matchmaking | `matchmaking.md` defers it to `friend-challenges.md`; the boundary is not stated in either |
| L-6 | Three draw thresholds enforced by the engine | `game-engine.md` §7.7 records them as undecided product rules |

## TODO

- [ ] Assign an owner to every spec — sixteen of nineteen read `_Unassigned_`
- [ ] Close the six lags in the table above
- [ ] Decide whether the three engine draw thresholds are product rules or engine defaults
