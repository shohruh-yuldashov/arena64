# Prompt Library

Curated, version-controlled prompts for working on Arena64 with AI coding agents.

Prompts live in source control for the same reason scripts do: they encode a repeatable
procedure, they are reviewed before use, and they improve over time instead of being
retyped from memory.

## Structure

| Directory | Scope |
| --- | --- |
| `backend/` | API, service, repository, and realtime backend work |
| `frontend/` | UI, routing, state management, and accessibility work |
| `architecture/` | System design, trade-off analysis, and decision records |
| `database/` | Data modelling, migrations, and query tuning |
| `review/` | Code, security, performance, and specification review |

## Rules

- Every prompt states its **Context**, **Task**, **Constraints**, and **Expected Output**.
- Prompts link to documentation rather than duplicating it.
- No prompt may override `docs/02-development/CLAUDE.md`; that document always wins.
- No secrets, credentials, or personal data in any prompt.

## TODO

- [ ] Author the initial prompt set per directory
