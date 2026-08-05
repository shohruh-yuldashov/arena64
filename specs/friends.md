# Friends

> **Status:** Placeholder for the backend contract — the **viewer relationship** and the **client** are specified below and in [`frontend.md`](./frontend.md) §14
> **Owner:** _Unassigned_
> **Last updated:** 2026-08-05 — A64-020.4, viewer relationship state and social UI
> **Related:** [`frontend.md`](./frontend.md) §14, `templates/feature-spec.md`

## Description

Friend requests, friend lists, blocking, and presence visibility between players.

## TODO

- [ ] Define goals and non-goals
- [ ] Define user stories and acceptance criteria
- [ ] Define domain model and state transitions
- [ ] Define API surface (see `templates/api-spec.md`)
- [ ] Define events, permissions, and rate limits
- [ ] Define test scenarios and rollout plan

---

## The viewer relationship — A64-020.4

`ProfileResponse.relationship` is published on every surface that returns a profile:
search, public profile, friends, requests and blocks.

```
none | outgoing_request | incoming_request | friend | blocked
```

Always **from the authenticated viewer toward the returned player**.

### Absent is not `none`

| Case | Value | Why |
| --- | --- | --- |
| Anonymous reader | `null` | There is no viewer to have a relationship with |
| The reader's own profile | `null` | Nobody is their own friend, and no social action applies |
| Signed in, no relationship | `none` | What an "Add friend" control renders from |

One value covering both would push the decision of which is which into every consumer.

### `blocked` is one-directional

It means **the viewer blocked the returned player**. There is no member for
blocked-by-target and there must not be one: a player who could tell they had been blocked
would have exactly the information BL-1 withholds. A blocked player simply does not find
the blocker — search excludes them in both directions — and cannot distinguish that from
any other absence.

That asymmetry is why `RelationshipState` is a separate enum from `ViewerRelationship`,
whose `BLOCKED` is deliberately **symmetric** because the *visibility* consequence runs
both ways. Merging them would force the symmetric form into a published field.

### Precedence, and why it is not the invariant

```
BLOCKED  >  FRIEND  >  INCOMING_REQUEST  >  OUTGOING_REQUEST  >  NONE
```

A tie-break of last resort. The schema already prevents every conflicting pair: one live
friendship per pair and one pending request per ordered pair are partial unique indexes,
and `BlockingService` ends a friendship and declines pending requests in the same
transaction as the block. The precedence exists so the resolution is total and
deterministic if a repair script ever leaves two rows behind.

### Cost

One statement for a whole page — a `UNION ALL` over the three relations, tagged and
projected to the other player's id. Measured on search: **7 statements at `limit=1`, `20`
and `50`** (the endpoint's maximum). Flat.

The four list endpoints pay **nothing**: a friends list is `friend` by construction, a
blocked list is `blocked`, and the two request lists are `incoming_request` and
`outgoing_request`, so each states it and skips the query. Stating it is also the only way
to be right on the request lists — the direction *is* the state.

### Architecture

`profiles` declares its own `RelationshipStateProvider` port and never names `friends`
outside `infrastructure` and the composition root, which is the rule
`ViewerRelationshipProvider` already established: the fallback must not depend on the
module it replaces. `NoRelationshipStates` reports `none` for everything when `friends` is
switched off — removing actions rather than offering ones that would fail, and never
fabricating a block.

All 27 import-linter contracts hold.

## The client

[`frontend.md`](./frontend.md) §14. The three decisions worth finding from this file:

| Decision | Where |
| --- | --- |
| One state maps to one action set, so contradictory buttons are **unrepresentable** | §14.1 |
| The deprecated `show_*` booleans are **not consumed** — they lose the friends-only case | §14.5 |
| Presence updates on an HTTP read, not over a socket; deferred to the Game phase | §14.5, OQ-10 |
