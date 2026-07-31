"""The bounded contexts of architecture.md §6 — empty by design.

This task (A64-006) builds infrastructure only; no module (`auth`, `users`,
`game`, and so on) is implemented here — see the task's "Do NOT implement"
list.

When the first module is added, it is a package under this one, and it
follows the five-package shape of services.md §2.1 and architecture.md §8:

    modules/<name>/
        domain/           entities, value objects, domain services, invariants
        application/       use cases, repository ports, unit-of-work usage
        infrastructure/    SQLAlchemy repositories, Redis adapters
        interface/         FastAPI routers, WebSocket handlers, schemas
        public/             the ONLY package other modules may import (BE-03)

It registers itself with `app.core.module_registry.ModuleRegistry` — the
registry the composition root (app/app_factory.py) already iterates — so
that adding a module never requires editing this file, `app_factory.py`, or
any other module (dependency-injection.md DI-04, services.md §11.1).
"""
