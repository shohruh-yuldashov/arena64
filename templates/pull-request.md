# <Type>: <Concise imperative title>

> Type is one of: `feat`, `fix`, `refactor`, `perf`, `docs`, `test`, `build`, `ci`, `chore`.

## Summary

What this pull request changes, and why, in two or three sentences. Written for a reviewer
who has not read the linked issue.

## Related

| | |
| --- | --- |
| **Issue / Ticket** | Closes #000 |
| **Spec** | `specs/<feature>.md` |
| **ADR** | `docs/07-decisions/ADR-000-<slug>.md` |

## Changes

- <change, one bullet per meaningful unit of work>

## Motivation & Approach

Why this approach was chosen, and what alternatives were rejected. Call out anything a
reviewer would otherwise have to reverse-engineer from the diff.

## Type of Change

- [ ] Bug fix (non-breaking)
- [ ] New feature (non-breaking)
- [ ] Breaking change
- [ ] Refactor (no behaviour change)
- [ ] Performance improvement
- [ ] Documentation only
- [ ] Infrastructure / tooling

## Breaking Changes

State the break, who it affects, and the migration path. Write "None" if not applicable.

## Database Changes

- [ ] No schema change
- [ ] Migration included and reversible
- [ ] Backfill required — describe below
- [ ] Deploy ordering constraint — describe below

## API Changes

- [ ] No API change
- [ ] Additive only
- [ ] Breaking — version bumped and documented

## Testing

How this was verified. Include commands run and their results.

- [ ] Unit tests added or updated
- [ ] Integration tests added or updated
- [ ] End-to-end tests added or updated
- [ ] Manually verified — steps below

**Manual verification steps:**

1. <step>

## Screenshots / Recordings

Required for user-facing changes. Include before and after.

## Performance Impact

Expected effect on latency, memory, query count, or bundle size. State "None expected"
with a brief justification if unchanged.

## Security Considerations

New inputs, permissions, or data exposure introduced, and how each is controlled.

## Rollout & Rollback

Feature flag, staged rollout plan, and how to revert safely.

## Observability

Metrics, logs, or alerts added or updated so this change is diagnosable in production.

## Reviewer Notes

Where to start reading, which parts need the closest attention, and any known
follow-up work deliberately left out of scope.

## Author Checklist

- [ ] Follows `docs/02-development/coding-standards.md` and `docs/02-development/CLAUDE.md`
- [ ] Commit messages follow `docs/02-development/git-workflow.md`
- [ ] Documentation and specs updated
- [ ] No secrets, credentials, or debug output committed
- [ ] Lint, type checks, and the full test suite pass locally
- [ ] Self-reviewed the complete diff
