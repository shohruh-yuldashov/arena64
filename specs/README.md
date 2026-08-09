# Feature Specifications

This directory holds one specification per product feature of Arena64. A spec is the
single source of truth for **what** a feature does and **why** — it is written and agreed
before implementation begins.

## Rules

- One feature per file; keep filenames lowercase and hyphenated.
- Start every new spec from `templates/feature-spec.md`.
- A spec describes behaviour, contracts, and acceptance criteria — not implementation code.
- Cross-cutting design belongs in `docs/01-architecture/`, not here.
- Update the spec **before** changing behaviour, and link the pull request that implements it.

## Status Legend

| Status | Meaning |
| --- | --- |
| Placeholder | File exists, content not yet written |
| Draft | Under active authoring, not agreed |
| Review | Awaiting sign-off |
| Approved | Agreed and ready for implementation |
| Implemented | Shipped; spec reflects live behaviour |
| Deferred | Deliberately not built; the record explains why and what would reopen it |

## Index

| Spec | Description | Status |
| --- | --- | --- |
| [Authentication](./authentication.md) | Account registration, sign-in, session issuance, and credential recovery for Arena64 players. | Placeholder |
| [Player Profile](./profile.md) | Public and private player profile data, avatars, display identity, and profile visibility. | Placeholder |
| [Friends](./friends.md) | Friend requests, friend lists, blocking, and presence visibility between players. | Placeholder |
| [Quick Messages](./quick-messages.md) | Predefined, server-owned courtesies between the two players of a live match. No free text. | Implemented |
| [Chat](./chat.md) | Free-text messaging. **Deferred and superseded** by Quick Messages — see [ADR-004](../docs/07-decisions/ADR-004-quick-messages-not-free-text-chat.md). | Deferred |
| [Notifications](./notifications.md) | Delivery of in-app and push notifications for invites, turns, and social events. | Placeholder |
| [Game Engine](./game-engine.md) | Checkers rules enforcement, move validation, board state, and game termination conditions. | Placeholder |
| [Matchmaking](./matchmaking.md) | Queueing, opponent selection, match creation, and direct challenge flows. | Placeholder |
| [Rating](./rating.md) | Skill rating calculation, rating periods, provisional ratings, and rating decay. | Placeholder |
| [Leaderboard](./leaderboard.md) | Ranked player listings, leaderboard scopes, and refresh cadence. | Placeholder |
| [Statistics](./statistics.md) | Aggregated player and match statistics, history, and analytical breakdowns. | Placeholder |
| [Spectator](./spectator.md) | Live match observation, spectator joins, and delayed or restricted viewing. | Placeholder |
| [Admin](./admin.md) | Admin authorization, the server-authoritative boundary, and the separate console. | Foundation shipped |
| [Settings](./settings.md) | Per-player preferences covering gameplay, notifications, privacy, and appearance. | Placeholder |
| [Product Experience](./product-experience.md) | The player-facing experience: current-state UX audit, design principles, design-system direction, and the A64-025 redesign roadmap. | Draft — audit complete |

## TODO

- [ ] Prioritise specs against `docs/00-overview/roadmap.md`
- [ ] Promote the first milestone's specs from Placeholder to Draft
