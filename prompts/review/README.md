# Review Prompts

## Purpose

Reusable prompts for quality gates: code review, security review, performance review, and specification review.

## When to Use This Directory

Reach for a prompt here whenever the task falls into this category, rather than writing a
one-off instruction. Consistent prompts produce consistent output and make results
reviewable across sessions.

## Intended Prompts

_None authored yet._ Planned coverage:

- `Review a change against the coding standards`
- `Run a security review of a diff`
- `Review a spec for completeness and testability`
- `Assess performance impact of a change`

## Conventions

- One prompt per file; lowercase hyphenated filenames (e.g. `add-endpoint.md`).
- Every prompt states: **Context**, **Task**, **Constraints**, **Expected Output**.
- Prompts reference documentation by path instead of restating it, so they stay accurate.
- Prompts must never instruct an agent to bypass the rules in `docs/02-development/CLAUDE.md`.
- Keep prompts free of secrets, credentials, and personal data.

## Reference Documents

- `docs/02-development/CLAUDE.md`
- `docs/02-development/testing.md`
- `docs/01-architecture/security.md`

## TODO

- [ ] Author the planned prompts listed above
- [ ] Add a short index table once more than a few prompts exist
