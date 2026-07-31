# Architecture Prompts

## Purpose

Reusable prompts for system-level reasoning: evaluating design options, drafting decision records, and assessing scalability or failure behaviour.

## When to Use This Directory

Reach for a prompt here whenever the task falls into this category, rather than writing a
one-off instruction. Consistent prompts produce consistent output and make results
reviewable across sessions.

## Intended Prompts

_None authored yet._ Planned coverage:

- `Compare design options and recommend one`
- `Draft an architecture decision record`
- `Analyse a critical path for failure modes`
- `Review a subsystem against the architecture rules`

## Conventions

- One prompt per file; lowercase hyphenated filenames (e.g. `add-endpoint.md`).
- Every prompt states: **Context**, **Task**, **Constraints**, **Expected Output**.
- Prompts reference documentation by path instead of restating it, so they stay accurate.
- Prompts must never instruct an agent to bypass the rules in `docs/02-development/CLAUDE.md`.
- Keep prompts free of secrets, credentials, and personal data.

## Reference Documents

- `docs/01-architecture/`
- `docs/07-decisions/`
- `templates/architecture-decision.md`

## TODO

- [ ] Author the planned prompts listed above
- [ ] Add a short index table once more than a few prompts exist
