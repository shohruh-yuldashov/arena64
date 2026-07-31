# Database Prompts

## Purpose

Reusable prompts for data modelling, migration authoring, index and query tuning, and data lifecycle review.

## When to Use This Directory

Reach for a prompt here whenever the task falls into this category, rather than writing a
one-off instruction. Consistent prompts produce consistent output and make results
reviewable across sessions.

## Intended Prompts

_None authored yet._ Planned coverage:

- `Model a new entity and its relationships`
- `Author a reversible migration`
- `Review and tune a slow query`
- `Audit indexes and retention policy`

## Conventions

- One prompt per file; lowercase hyphenated filenames (e.g. `add-endpoint.md`).
- Every prompt states: **Context**, **Task**, **Constraints**, **Expected Output**.
- Prompts reference documentation by path instead of restating it, so they stay accurate.
- Prompts must never instruct an agent to bypass the rules in `docs/02-development/CLAUDE.md`.
- Keep prompts free of secrets, credentials, and personal data.

## Reference Documents

- `docs/01-architecture/database.md`
- `docs/03-backend/repositories.md`

## TODO

- [ ] Author the planned prompts listed above
- [ ] Add a short index table once more than a few prompts exist
