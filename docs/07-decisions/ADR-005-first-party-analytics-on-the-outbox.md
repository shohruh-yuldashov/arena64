# ADR-005 — First-party product analytics on the existing outbox

| Field             | Value                                                                                |
| ----------------- | ------------------------------------------------------------------------------------ |
| **Status**        | Accepted                                                                             |
| **Date**          | 2026-09-05                                                                           |
| **Deciders**      | Shohruh                                                                              |
| **Consulted**     | —                                                                                    |
| **Supersedes**    | —                                                                                    |
| **Superseded by** | —                                                                                    |
| **Related**       | `docs/01-architecture/analytics.md`, `specs/product-experience.md` §45, AD-16, DM-06 |

---

## Context

Arena64 has no product measurement. A64-026 shipped a public landing page, a
brand and public tournament discovery, and every question those raise — does
anybody register, do they play, do they come back — is currently
unanswerable.

What the platform **does** have, found by audit before any decision:

| Capability                          | State                                                                                                                        |
| ----------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Transactional outbox (AD-16)        | **Exists.** `platform.outbox`, written in the same transaction as the state change it describes                              |
| Stable per-event identity           | **Exists.** `outbox.id`, generated at the authoritative source                                                               |
| Consumer idempotency ledger         | **Exists.** `platform.processed_event(consumer, event_id)` — a composite primary key, one row per delivery                   |
| At-least-once delivery with backoff | **Exists.** Relay with `attempt_count`, `next_attempt_at`, `last_error`                                                      |
| Domain events                       | **33 exist** across `game`, `matchmaking`, `rating`, `tournament`, `friends`, `users`                                        |
| Canonical enums                     | **Exist.** `MatchOutcome`, `TerminationReason`, `SpeedClass`, `ProductVariant`, `TournamentStatus`                           |
| Server-measured queue wait          | **Exists.** `matchmaking.players_paired.waited_for_seconds`                                                                  |
| Operational metrics                 | **Exists and is separate.** `app/platform/metrics` — counters and observations, sunk to logs                                 |
| Audit trail                         | **Exists and is separate.** `admin.audit_entry`, append-only, closed action vocabulary                                       |
| Opaque cross-context identifier     | **Exists.** `PlayerId` (DM-06), chosen precisely because a handle is mutable and an email is erasable                        |
| A read role for analytics           | **Anticipated.** `arena64_readonly`, described in `database.md` §297 as serving "replicas, analytics, and the admin console" |
| Frontend analytics SDK              | **None.** No tracker, no consent infrastructure, no cookie banner                                                            |

Eight consumers already implement the outbox's `EventHandler` protocol. The
pattern is not theoretical here; it is how this platform already moves facts
between contexts.

Volume is small and known: Arena64 measures matches and tournaments, not
page impressions at web scale.

## Decision

> We will build product analytics **first-party**, as a ninth consumer of the
> existing outbox, storing events in Arena64's own PostgreSQL — and we will
> not adopt a third-party product-analytics provider.

Backend-authoritative events are **projections of domain events that already
exist**, not new instrumentation scattered through services. Frontend
behavioural events reach the same store through a narrow, validated,
server-authenticated collector whose allowlist cannot name a server event.

## Options Considered

### Option 1 — First-party on the existing outbox _(chosen)_

**Summary:** An `analytics` outbox consumer projects domain events into an
analytics store; a bounded collector endpoint accepts client behavioural
events.

| Pros                                                                                                 | Cons                                                           |
| ---------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| Deduplication, idempotency and retry are **already solved** — `event_id` and `processed_event` exist | Query and dashboard work is ours to build (A64-027.3 → .6)     |
| No personal data leaves the platform, so no processor relationship and no new consent surface        | No off-the-shelf funnel/cohort UI                              |
| No SDK in the bundle — A64-026.2 sized the whole public bundle deliberately                          | PostgreSQL will need aggregate tables if raw volume ever grows |
| Backend facts are projections of events that exist, so services gain no `analytics.track(...)` calls | Retention and partitioning are our operational burden          |
| The database architecture already names analytics as a reader (`arena64_readonly`)                   |                                                                |
| SQL answers every query in §48 — cohort retention, p95, funnels — with no export step                |                                                                |

### Option 2 — Third-party provider (PostHog, Mixpanel, Amplitude)

**Summary:** Ship an SDK, send events to a vendor, use their dashboards.

| Pros                                          | Cons                                                                                                               |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Funnels, cohorts and retention out of the box | Sends `PlayerId` and behaviour to a processor: a data-protection relationship this product does not currently have |
| No storage or query work                      | Introduces the consent question the platform has so far avoided entirely                                           |
| Mature identity merge                         | An SDK in a bundle A64-026 deliberately kept small, plus a third-party origin in the CSP                           |
|                                               | Vendor lock-in over the event history, which is the asset                                                          |
|                                               | Duplicates dedup, retry and delivery machinery the outbox already provides                                         |
|                                               | Self-hosting PostHog is a second database, a second deployment and a second thing to operate                       |

### Option 3 — Hybrid

**Summary:** Backend facts first-party, client behaviour to a vendor.

| Pros                          | Cons                                                                                                       |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Vendor handles the noisy half | **Two identity models and two stores**, so no funnel can cross them — and every funnel in §37 crosses them |
| Less first-party client work  | Every cost of Option 2, for half the benefit                                                               |

### Option 4 — Reuse `app/platform/metrics`

**Summary:** Count product events with the existing operational recorder.

| Pros                    | Cons                                                                                                     |
| ----------------------- | -------------------------------------------------------------------------------------------------------- |
| Zero new infrastructure | **Counters have no actor.** Retention, cohorts and funnels are per-person questions and are unanswerable |
|                         | Its sink is log lines. Log retention is set for debugging, not for measurement                           |
|                         | It would conflate operational health with product behaviour, which §2 of the analytics document forbids  |

### Option 5 — Do nothing

Arena64 launches unable to say whether anyone registers, activates or
returns. Every subsequent product decision is made on intuition, and the
first three months of real traffic — the only cohort that will ever be the
first — is unmeasured and unrecoverable.

## Rationale

Four criteria decided it.

**The hard parts are already built.** Deduplication and idempotency are the
two problems that make analytics pipelines wrong, and both are solved here by
existing infrastructure rather than by a vendor: a stable `event_id` written
in the same transaction as the fact, and a `processed_event` ledger keyed by
`(consumer, event_id)`. Adopting a provider would mean paying for that
machinery twice.

**The events exist.** The backend half of the taxonomy is a projection of 33
domain events carrying canonical enums. This is the difference between
"instrument the product" and "read what the product already says", and it is
why no service acquires an `analytics.track(...)` call.

**Privacy is a design position, not a configuration.** First-party means no
personal data leaves the platform, which keeps the consent question narrow
and keeps the A64-026 bundle free of a third-party script. `PlayerId` already
exists as the opaque identifier that survives erasure (DM-06, AC-5).

**Volume does not justify the alternative.** Providers earn their cost on
clickstream volume and on query patterns SQL struggles with. Arena64 measures
matches. PostgreSQL answers every query in §48 of the analytics document.

## Consequences

### Positive

- Dedup, retry, ordering and transactional consistency are inherited, not written.
- No personal data leaves the platform; no processor, no SDK, no third-party origin.
- Events are joinable with product tables, so "matches per activated player in their first week" is one query.
- The taxonomy is enforceable in the type system, because it is our own.

### Negative

- Funnel, cohort and retention **queries** are ours to write (A64-027.3 → .5).
- A dashboard is ours to build (A64-027.6).
- Raw event growth becomes an operational concern; retention and partitioning must be planned, not assumed.
- No mature identity-merge implementation exists for free — the anonymous-to-authenticated stitch is ours.

### Neutral

- Analytics becomes the ninth outbox consumer, using the pattern eight others already use.
- The environment separation is the existing `Environment` enum, not a new concept.

## Impact

| Area               | Impact                                                                                                                               |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| Architecture       | One new module consuming existing events. **No new outbox**, no second event bus                                                     |
| Data model         | An analytics event store, proposed in A64-027.2. Nothing in this ADR                                                                 |
| Security           | One new authenticated, rate-limited, allowlisted collector endpoint for client events. Threat-modelled in the analytics document §41 |
| Operations         | Retention and partitioning to plan. No new deployment, no new datastore technology                                                   |
| Developer workflow | Adding a fact means adding a domain event, which is already how this platform works                                                  |

## Compliance & Enforcement

- The event registry is code, not prose: `app/platform/analytics/registry.py` names every event with its owner and trust level.
- `CLIENT_EMITTABLE` is derived from ownership, so a server-owned name is not emittable by construction rather than by review.
- Contract tests assert the PII denylist, name uniqueness, naming convention and the client/server split.
- `.importlinter` keeps `app.platform.analytics` free of `app.modules`, as it does for the outbox and the task dispatcher.

## Follow-Up Actions

- [ ] A64-027.2 — event store, outbox consumer, collector endpoint
- [ ] A64-027.3 — acquisition and activation funnels
- [ ] A64-027.4 — engagement and retention metrics
- [ ] A64-027.5 — matchmaking and game metrics
- [ ] A64-027.6 — dashboard and the epic's closing audit
- [ ] Resolve the four open decisions in `docs/01-architecture/analytics.md` §44 before A64-027.2 ships

## Revisit Criteria

Reopen this decision if any of the following becomes true:

- Raw event volume exceeds roughly **10 million rows a month**, at which point columnar storage stops being premature.
- A product question needs session replay, heatmaps or funnel exploration by non-engineers faster than queries can be written.
- The analytics store starts competing with the product database for resources that a read replica cannot absorb.
- A legal requirement makes first-party storage of behavioural data harder than a processor agreement, rather than easier.

## References

- `docs/01-architecture/analytics.md` — the taxonomy, the metrics and the threat model
- `docs/01-architecture/architecture.md` AD-16 — the transactional outbox
- `docs/01-architecture/domain-model.md` DM-06, AC-5 — `PlayerId`, and what survives erasure
- `docs/01-architecture/database.md` §297 — `arena64_readonly` as the analytics reader
