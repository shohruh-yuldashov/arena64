# Player Profile

> **Status:** Placeholder for the backend contract — the **client** is specified in [`frontend.md`](./frontend.md) §13 (A64-020.3)
> **Owner:** _Unassigned_
> **Last updated:** 2026-10-29 — A64-032, recent games and rating progress
> **Related:** [`frontend.md`](./frontend.md) §13, `templates/feature-spec.md`

## Description

Public and private player profile data, avatars, display identity, and profile visibility.

## TODO

- [ ] Define goals and non-goals
- [ ] Define user stories and acceptance criteria
- [ ] Define domain model and state transitions
- [ ] Define API surface (see `templates/api-spec.md`)
- [ ] Define events, permissions, and rate limits
- [ ] Define test scenarios and rollout plan

---

## The client — A64-020.3

The profile **API** was built across A64-012.1–012.8 and is not written up here. What
A64-020.3 added is the UI over it, and no backend change: every endpoint it needs already
existed, and the regenerated OpenAPI types came back byte-identical.

Specified in [`frontend.md`](./frontend.md) §13. The three decisions worth finding from this
file:

| Decision | Where |
| --- | --- |
| The self and public profiles are **separate caches**, so a privacy-filtered page can never read unfiltered data | §13.1 |
| The client **never predicts** privacy filtering — an omitted field is rendered as absent, never as `false` | §13.4 |
| A device list is **deferred**: `SessionService.list_user_sessions` has no HTTP endpoint, so `/settings/sessions` offers only "sign out everywhere" | §13.8, OQ-8 |
| Country is **chosen from the backend's own list**, named by `Intl`, and never inferred from the interface language (A64-031.F) | §13.11 |
| A profile lists the player's **recent games from their seat**, and **your own** profile adds the rating line; neither recomputes a statistic (A64-032) | §13.12 |
