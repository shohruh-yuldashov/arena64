"""The `users` bounded context — public identity, independent of `auth`.

Owns *who a player appears to be*: the handle they are known by, how they
want the platform presented to them (language, timezone), and whether the
account is usable. It does not own *proof of identity* — no sign-in, no
token, no password verification. `auth` (A64-011) will own that, and this
module is deliberately buildable and testable without it.

## Layout, and how it reconciles with services.md §2.1

services.md §2.1 / BE-03 mandate a five-package module shape whose
transport layer is named `interface/`. This task (A64-010) specifies a
four-layer shape naming that layer `presentation/`, plus `repositories/`,
`services/`, `schemas/`, `exceptions/` and `dependencies/`. Per CLAUDE.md's
precedence rule ("where it conflicts with an explicit user instruction, the
user wins — but say which rule is being set aside and why"), the task's
naming is what is built here:

    users/
        domain/                  entities, value objects, invariants
            exceptions/          this module's typed failures
        application/             use cases and the ports they need
            services/            UserService — one transaction per use case
        infrastructure/          adapters realising the ports
            repositories/        the SQLAlchemy UserRepository
        presentation/            transport bindings  (services.md: `interface/`)
            schemas/             Pydantic wire models
            dependencies/        the FastAPI `Depends` bridge (DI-01)
        public/                  the ONLY package other modules may import (BE-03)

Two deliberate choices inside that:

**The five named sub-packages are nested in the layer that owns them**,
not laid out flat beside the four layers. A flat `repositories/` sibling
would sit outside `infrastructure/`, which is the one thing the dependency
rules of architecture.md §8 exist to prevent — a repository implementation
is infrastructure, and its port is application. The task's list is
satisfied (every named package exists); the layering is not inverted to
achieve it.

**`public/` is kept** even though the task does not list it. BE-03 makes it
the single lintable import surface for every other module, and dropping it
would mean the first module on the platform is also the first violation of
the rule meant to keep modules separable. Nothing outside `users` imports
anything but `users.public`.

## What this module does not know about

- Passwords. `password_hash` is stored (see `infrastructure/models.py` for
  why that placement is itself a documented deviation) but never produced,
  verified, or compared here. Hashing is `auth`'s job.
- Sessions, tokens, permissions. There is no "current user" concept in this
  module; endpoints take an explicit id.
- Ratings, statistics, presence, friendships. Every one of those is another
  module's aggregate keyed by `player_id` (DM-06), never a column here.
"""
