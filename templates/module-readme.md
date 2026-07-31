# <Module Name>

> **Location:** `<path/to/module>`
> **Type:** application \| shared package \| service \| infrastructure
> **Owner:** <team or individual>
> **Stability:** experimental \| stable \| deprecated

## Purpose

What this module is responsible for, in one paragraph. A reader should be able to decide
from this section alone whether their change belongs here.

## Responsibilities

**This module owns:**

- <responsibility>

**This module does not own:**

- <responsibility that belongs elsewhere, with a pointer to where>

## Public Interface

What other modules may depend on. Anything not listed here is internal and may change
without notice.

| Export | Kind | Description |
| --- | --- | --- |
| | | |

## Dependencies

| Dependency | Reason |
| --- | --- |
| `<internal or external dependency>` | |

**Dependency rules:** which layers or modules this one may import, and which are forbidden.
See `docs/01-architecture/architecture.md`.

## Structure

```text
<path/to/module>/
├── <dir>/    # responsibility
└── <dir>/    # responsibility
```

## Configuration

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| | | | |

Never document actual secret values here — only their names and purpose.

## Usage

How a consumer integrates with this module, at the conceptual level.

## Local Development

```bash
# install, run, and watch commands
```

## Testing

```bash
# test commands
```

Coverage expectations and any module-specific testing notes.

## Observability

Metrics, logs, and traces this module emits, and the dashboards or alerts that consume them.

## Failure Modes

| Failure | Behaviour | Recovery |
| --- | --- | --- |
| | | |

## Conventions

Module-specific rules that extend — never contradict — `docs/02-development/coding-standards.md`.

## Related Documentation

- `docs/<relevant document>.md`
- `specs/<relevant spec>.md`

## TODO

- [ ] <known gap or planned work>
