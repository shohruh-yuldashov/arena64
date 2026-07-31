# Frontend Prompts

## Purpose

Reusable prompts for frontend work on Arena64: UI composition, routing, state management, realtime bindings, and accessibility passes.

## When to Use This Directory

Reach for a prompt here whenever the task falls into this category, rather than writing a
one-off instruction. Consistent prompts produce consistent output and make results
reviewable across sessions.

## Intended Prompts

_None authored yet._ Planned coverage:

- `Build a screen from an approved spec and the design system`
- `Wire a route with guards and loading boundaries`
- `Connect a view to realtime state`
- `Run an accessibility and responsiveness audit`

## Conventions

- One prompt per file; lowercase hyphenated filenames (e.g. `add-endpoint.md`).
- Every prompt states: **Context**, **Task**, **Constraints**, **Expected Output**.
- Prompts reference documentation by path instead of restating it, so they stay accurate.
- Prompts must never instruct an agent to bypass the rules in `docs/02-development/CLAUDE.md`.
- Keep prompts free of secrets, credentials, and personal data.

## Reference Documents

- `docs/04-frontend/`
- `docs/02-development/coding-standards.md`
- `specs/`

## TODO

- [ ] Author the planned prompts listed above
- [ ] Add a short index table once more than a few prompts exist
