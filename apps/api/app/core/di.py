"""The composition root's container — dependency-injection.md §1.1, DI-01.

A minimal, explicit, framework-agnostic registry of process-lifetime
singletons. FastAPI's `Depends` (app/api/deps.py) bridges into this
container at the routing layer; it never *is* the container (DI-01) — the
same registrations must be reachable from a future Celery task or
clock-loop adjudication with no HTTP request in sight.

The concrete DI container library is an open question
(dependency-injection.md §1.1 footnote — "pending an ADR"). This hand-rolled
version implements exactly the one provider kind this bootstrap needs and
is deliberately swappable once that ADR lands; nothing outside this module
and app/api/deps.py knows which implementation is in use.

Scoped providers — a unit of work per request, per command, or per task
(dependency-injection.md §1.4) — are not held here. They are opened and
closed within a single request/command/task and are constructed directly by
app/api/deps.py, not by a container that outlives them.
"""

from typing import TypeVar

T = TypeVar("T")


class Container:
    """Process-lifetime singleton registry."""

    def __init__(self) -> None:
        self._singletons: dict[type, object] = {}

    def register_singleton(self, interface: type[T], instance: T) -> None:
        self._singletons[interface] = instance

    def resolve(self, interface: type[T]) -> T:
        try:
            return self._singletons[interface]  # type: ignore[return-value]
        except KeyError as exc:
            raise LookupError(
                f"{interface.__name__} was never registered with the container"
            ) from exc
