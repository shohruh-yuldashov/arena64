"""The FastAPI `Depends` bridge for `auth` — dependency-injection.md
DI-01: `Depends` is used only at the routing layer, to hand a route an
already-resolved service. It is not the container.

The graph assembled per request:

    AsyncSession                  one per request (`app.api.deps`)
      -> SqlAlchemyUserRepository
      -> SessionUnitOfWork        the transaction `users` will commit
      -> UserService              validation + uniqueness + transaction
      -> UserAccountService       adapts it to the published port
    Argon2idPasswordHasher        process-lifetime singleton
      -> RegistrationService

**Everything here is per-request, including the hasher.** An earlier
version cached `Argon2idPasswordHasher` on the settings object, on the
theory that a process-lifetime singleton was worth having. Measured, its
construction costs **1 µs** against the **~19,000 µs** of the hash it
performs — 0.005% of the operation. The cache was optimising nothing and
cost a `lru_cache` keyed on a Pydantic model, which is not reliably
hashable. Per-request construction is simpler and indistinguishable.

The repository and unit of work must be per-request for a reason that is
not about cost at all: they hold the request's session
(dependency-injection.md §1.3 — "Never singleton: anything holding a
session... A singleton service holding a session is the classic
production-only bug").

`auth` builds `users`' internals here rather than importing a factory
from `users`, which looks like a boundary violation and is not: this is
the *composition root's* job, and a composition root is the one place
permitted to know how to construct things (BR-6 forbids a *module*
reaching for the container, not the root wiring modules together). The
`RegistrationService` itself sees only `users.public.UserAccountCreator`.
"""

from typing import Annotated

from fastapi import Depends

from app.api.deps import DbSessionDep, SettingsDep
from app.core.clock import Clock
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.auth.application.ports import PasswordHasher
from app.modules.auth.application.services import RegistrationService
from app.modules.auth.infrastructure import Argon2idPasswordHasher
from app.modules.users.application.services import UserService
from app.modules.users.application.services.user_account_service import UserAccountService
from app.modules.users.infrastructure.repositories import SqlAlchemyUserRepository
from app.modules.users.presentation.dependencies import ClockDep
from app.modules.users.public import UserAccountCreator


def get_password_hasher(settings: SettingsDep) -> PasswordHasher:
    return Argon2idPasswordHasher(settings.auth)


PasswordHasherDep = Annotated[PasswordHasher, Depends(get_password_hasher)]


def get_user_account_creator(session: DbSessionDep, clock: ClockDep) -> UserAccountCreator:
    """Assembles `users`' side of the graph behind its published port.

    The unit of work wraps the *same* session the repository holds —
    otherwise the service would commit a transaction the repository never
    wrote to, and the registration would be silently lost on teardown.
    """
    users = UserService(
        users=SqlAlchemyUserRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )
    return UserAccountService(users)


UserAccountCreatorDep = Annotated[UserAccountCreator, Depends(get_user_account_creator)]


def get_registration_service(
    accounts: UserAccountCreatorDep,
    password_hasher: PasswordHasherDep,
) -> RegistrationService:
    return RegistrationService(accounts=accounts, password_hasher=password_hasher)


RegistrationServiceDep = Annotated[RegistrationService, Depends(get_registration_service)]


__all__ = [
    "Clock",
    "PasswordHasherDep",
    "RegistrationServiceDep",
    "UserAccountCreatorDep",
    "get_password_hasher",
    "get_registration_service",
    "get_user_account_creator",
]
