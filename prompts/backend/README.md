# Backend Prompts

## Purpose

Reusable prompts for backend work on Arena64: API surfaces, service and repository layers, dependency wiring, background jobs, and realtime handlers.

## When to Use This Directory

Reach for a prompt here whenever the task falls into this category, rather than writing a
one-off instruction. Consistent prompts produce consistent output and make results
reviewable across sessions.

## Intended Prompts

_None authored yet._ Planned coverage:

- `Scaffold a service following `docs/03-backend/services.md``
- `Implement a repository against an agreed spec`
- `Add an endpoint from an approved `specs/` entry`
- `Write integration tests for a backend module`

## Conventions

- One prompt per file; lowercase hyphenated filenames (e.g. `add-endpoint.md`).
- Every prompt states: **Context**, **Task**, **Constraints**, **Expected Output**.
- Prompts reference documentation by path instead of restating it, so they stay accurate.
- Prompts must never instruct an agent to bypass the rules in `docs/02-development/CLAUDE.md`.
- Keep prompts free of secrets, credentials, and personal data.

## Reference Documents

- `docs/03-backend/`
- `docs/02-development/coding-standards.md`
- `specs/`

## TODO

- [ ] Author the planned prompts listed above
- [ ] Add a short index table once more than a few prompts exist
