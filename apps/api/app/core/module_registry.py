"""The module registry — dependency-injection.md DI-04.

Each future module exposes a registration hook; the composition root
iterates this registry and invokes each, rather than a central file
enumerating every module's bindings by hand.

**Empty at this stage.** No module exists yet — this bootstrap's scope is
infrastructure only (see the task's "Do NOT implement" list). The mechanism
is in place so that adding the first module never requires editing shared
code (services.md §11.1's extensibility test: a new module touches nothing
outside its own directory).
"""

from collections.abc import Iterable, Iterator
from typing import Protocol

from fastapi import APIRouter


class Module(Protocol):
    """What a module supplies at registration time.

    `configure()` is where a module binds its own repository and service
    implementations for the active profile (dependency-injection.md §1.5).
    `router`, if the module has HTTP surface, is mounted under its own
    prefix by the composition root (app/app_factory.py) — never assembled
    centrally by hand.
    """

    name: str

    def configure(self) -> None: ...

    @property
    def router(self) -> APIRouter | None: ...


class ModuleRegistry:
    """Holds every registered module. One instance, built once at the
    composition root (dependency-injection.md §1.1)."""

    def __init__(self) -> None:
        self._modules: list[Module] = []

    def register(self, module: Module) -> None:
        self._modules.append(module)

    def configure_all(self) -> None:
        for module in self._modules:
            module.configure()

    def routers(self) -> Iterable[tuple[str, APIRouter]]:
        for module in self._modules:
            if module.router is not None:
                yield module.name, module.router

    def __iter__(self) -> Iterator[Module]:
        return iter(self._modules)

    def __len__(self) -> int:
        return len(self._modules)
