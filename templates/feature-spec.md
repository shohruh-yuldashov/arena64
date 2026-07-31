# Feature Specification — <Feature Name>

| Field | Value |
| --- | --- |
| **Spec ID** | `SPEC-000` |
| **Status** | Draft \| Review \| Approved \| Implemented \| Deprecated |
| **Owner** | <name> |
| **Created** | YYYY-MM-DD |
| **Last updated** | YYYY-MM-DD |
| **Related ADRs** | `docs/07-decisions/ADR-000-<slug>.md` |
| **Related specs** | `specs/<other>.md` |

---

## 1. Summary

One paragraph describing what this feature is, in language a non-engineer can follow.

## 2. Motivation

Why this feature exists. What problem it solves, and what happens if it is not built.

## 3. Goals

- <Measurable outcome this feature must achieve>

## 4. Non-Goals

- <Explicitly out of scope, to prevent scope creep>

## 5. User Stories

| ID | As a… | I want… | So that… |
| --- | --- | --- | --- |
| US-1 | | | |

## 6. Acceptance Criteria

Written so they can be turned directly into tests.

- [ ] **AC-1** — Given <precondition>, when <action>, then <observable result>.
- [ ] **AC-2** — …

## 7. Domain Model

Entities introduced or modified, their key attributes, and their relationships.
Describe the model conceptually; schema detail belongs in the migration and in
`docs/01-architecture/database.md`.

## 8. State Transitions

| From | Event | To | Guard / Condition |
| --- | --- | --- | --- |
| | | | |

## 9. API Surface

Summarise the endpoints this feature adds or changes. Full contracts go in a
document based on `templates/api-spec.md`.

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| | | | |

## 10. Realtime & Events

| Event | Direction | Payload summary | Consumers |
| --- | --- | --- | --- |
| | | | |

## 11. Permissions & Visibility

Who may perform each action, and what data each role can observe.

## 12. Validation Rules

Field-level and cross-field constraints, with the error each violation produces.

## 13. Failure Modes

| Scenario | Expected behaviour | User-facing result |
| --- | --- | --- |
| | | |

## 14. Performance & Limits

Expected volume, latency budget, rate limits, and pagination defaults.

## 15. Security & Privacy Considerations

Sensitive data handled, retention expectations, and abuse vectors with mitigations.

## 16. Observability

Metrics, logs, and traces this feature must emit to be operable in production.

## 17. Test Plan

| Level | Coverage |
| --- | --- |
| Unit | |
| Integration | |
| End-to-end | |

## 18. Rollout Plan

Feature flag, migration ordering, backfill needs, and rollback procedure.

## 19. Open Questions

- [ ] <Unresolved question and who must answer it>

## 20. TODO

- [ ] Complete all sections above
- [ ] Obtain sign-off and move status to Approved
