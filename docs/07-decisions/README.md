# Architecture Decision Records

This directory is the permanent log of significant technical decisions made on Arena64.

An Architecture Decision Record (ADR) captures a decision *and the context that produced
it*. Code shows what was built; an ADR explains why it was built that way, which is the
part that is otherwise lost the moment the people involved move on.

## When to Write an ADR

Write one when a decision is **significant and hard to reverse**:

- Choosing or replacing a framework, database, broker, or hosting model
- Establishing a boundary between applications, services, or packages
- Defining a cross-cutting mechanism: authentication, caching, eventing, error handling
- Adopting a rule that constrains all future code
- Accepting a known trade-off (consistency for latency, cost for reliability)
- Deliberately choosing *not* to do something that would otherwise be expected

Do **not** write one for reversible, local choices — a helper's name, a component's internal
structure, or a one-off library used in a single module.

## Rules

1. **One decision per record.** If a record needs the word "also", split it.
2. **Records are immutable once accepted.** Correct a decision by writing a new ADR that
   supersedes it; never rewrite history.
3. **Number sequentially and never reuse numbers**, including for rejected records — a
   rejected ADR is valuable evidence that an option was considered and declined.
4. **Write for a future reader** who lacks all present context.
5. **Always evaluate "do nothing"** as an explicit option.
6. **Link the record** from the specs and documents it constrains.

## Naming

```text
ADR-<zero-padded number>-<kebab-case-title>.md
```

Example: `ADR-001-choose-postgresql-as-primary-datastore.md`

## Lifecycle

```text
Proposed ──▶ Accepted ──▶ Superseded
    │                └──▶ Deprecated
    └──▶ Rejected
```

| Status | Meaning |
| --- | --- |
| **Proposed** | Written and under discussion; not yet binding |
| **Accepted** | Binding — code and reviews must comply |
| **Rejected** | Considered and declined; kept as a record of the reasoning |
| **Superseded** | Replaced by a later ADR, which must be linked |
| **Deprecated** | No longer applicable; not replaced |

## Process

1. Copy `templates/architecture-decision.md` to `ADR-<next>-<slug>.md`.
2. Open it as **Proposed** in a pull request and let discussion happen in review.
3. On sign-off, set the status to **Accepted**, add the date and deciders, and merge.
4. Add the record to the index below.

## Index

| ID | Title | Status | Date |
| --- | --- | --- | --- |
| _None yet_ | | | |

## TODO

- [ ] Record the foundational stack decisions (backend framework, datastore, realtime transport)
- [ ] Record the monorepo boundary rules between `apps/` and `packages/`
- [ ] Backfill ADRs for any decision already embedded in the architecture documents
