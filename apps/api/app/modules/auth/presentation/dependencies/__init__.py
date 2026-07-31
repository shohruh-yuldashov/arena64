"""The FastAPI `Depends` bridge for `auth` — dependency-injection.md
DI-01: `Depends` is used only at the routing layer, to hand a route an
already-resolved service. It is not the container.

The graph assembled per request:

    AsyncSession                  one per request (`app.api.deps`)
      -> SqlAlchemyUserRepository
      -> SessionUnitOfWork        the transaction `users` will commit
      -> UserService              validation + uniqueness + transaction
      -> UserAccountService       adapts it to the published port
      -> UserCredentialService    adapts it to the *other* published port
    Argon2idPasswordHasher        process-lifetime singleton
    Clock                         injected, never read directly (AD-07)
      -> RegistrationService
      -> AuthenticationService

**Everything here is per-request except the hasher.** A64-011.1 built
that per-request too, having measured construction at **1 µs** against
the **~19,000 µs** of the hash it performs, and concluded — correctly, on
cost — that the cache was optimising nothing.

A64-011.2 shares it again for a reason cost does not reach: the hasher
memoises the dummy hash that makes an unknown-address sign-in take the
same time as a known one, and a per-request instance memoises nothing.
See `build_password_hasher`. The key is the three integer cost
parameters, not the `AuthSettings` model that broke the earlier attempt.

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
from app.modules.auth.application.services import (
<<<<<<< HEAD
    AccessTokenService,
    AuthenticationService,
    RegistrationService,
)
from app.modules.auth.infrastructure import JwtTokenProvider, build_password_hasher
from app.modules.auth.presentation.dependencies.current_user import (
    CurrentUser,
    OptionalCurrentUser,
    RequireAuthentication,
    TokenValidatorDep,
    get_current_user,
    get_current_user_optional,
    get_token_validator,
    require_authentication,
=======
    AuthenticationService,
    RefreshTokenService,
    RegistrationService,
    SessionService,
)
from app.modules.auth.infrastructure import (
    SqlAlchemySessionRepository,
    build_password_hasher,
>>>>>>> 56a5884 (task_011.4 completed)
)
from app.modules.users.application.services import UserService
from app.modules.users.application.services.user_account_service import UserAccountService
from app.modules.users.application.services.user_credential_service import UserCredentialService
from app.modules.users.infrastructure.repositories import SqlAlchemyUserRepository
from app.modules.users.presentation.dependencies import ClockDep
from app.modules.users.public import UserAccountCreator, UserCredentialStore


def get_password_hasher(settings: SettingsDep) -> PasswordHasher:
    return build_password_hasher(settings.auth)


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


def get_user_credential_store(session: DbSessionDep, clock: ClockDep) -> UserCredentialStore:
    """`users`' side of the login graph, behind its second published port.

    Assembled separately from `get_user_account_creator` rather than
    returning one object satisfying both: the two ports exist apart so
    that registering and reading password hashes are separately grantable
    capabilities, and a single factory handing back something with both
    would quietly undo that.

    The unit of work is here because the rehash-on-login write needs one.
    Nothing on the read path opens a transaction.
    """
    users = UserService(
        users=SqlAlchemyUserRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )
    return UserCredentialService(users)


UserCredentialStoreDep = Annotated[UserCredentialStore, Depends(get_user_credential_store)]


def get_authentication_service(
    credentials: UserCredentialStoreDep,
    password_hasher: PasswordHasherDep,
    clock: ClockDep,
) -> AuthenticationService:
    return AuthenticationService(
        credentials=credentials,
        password_hasher=password_hasher,
        clock=clock,
    )


AuthenticationServiceDep = Annotated[AuthenticationService, Depends(get_authentication_service)]


<<<<<<< HEAD
def get_access_token_service(
    settings: SettingsDep,
    clock: ClockDep,
) -> AccessTokenService:
    """Token issuance (A64-011.3).

    Not wired into any route: `POST /auth/login` still returns only the
    account, and A64-011.3's brief is infrastructure, not endpoints. It is
    assembled here so that A64-011.4 adds one line to the login handler
    rather than a dependency graph.
    """
    return AccessTokenService(
        tokens=JwtTokenProvider(settings.jwt, clock),
        settings=settings.jwt,
    )


AccessTokenServiceDep = Annotated[AccessTokenService, Depends(get_access_token_service)]
=======
def get_refresh_token_service(settings: SettingsDep) -> RefreshTokenService:
    """Stateless and cheap — a per-request instance costs one attribute
    assignment against the SHA-256 it exists to perform."""
    return RefreshTokenService(settings.session)


RefreshTokenServiceDep = Annotated[RefreshTokenService, Depends(get_refresh_token_service)]


def get_session_service(
    session: DbSessionDep,
    tokens: RefreshTokenServiceDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> SessionService:
    """Refresh sessions (A64-011.4).

    Not reachable from any route: this task's brief is explicit that no
    endpoints are exposed. It is assembled here so A64-011.5's refresh and
    logout endpoints add a handler rather than a dependency graph — and so
    that the graph itself is exercised now, while it is small.

    The unit of work wraps the *same* session the repository holds;
    otherwise the service would commit a transaction the repository never
    wrote to, and a sign-in would be silently lost on teardown.
    """
    return SessionService(
        sessions=SqlAlchemySessionRepository(session),
        tokens=tokens,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        settings=settings.session,
    )


SessionServiceDep = Annotated[SessionService, Depends(get_session_service)]
>>>>>>> 56a5884 (task_011.4 completed)


__all__ = [
    "AccessTokenServiceDep",
    "AuthenticationServiceDep",
    "Clock",
    "CurrentUser",
    "OptionalCurrentUser",
    "RequireAuthentication",
    "PasswordHasherDep",
    "RefreshTokenServiceDep",
    "RegistrationServiceDep",
<<<<<<< HEAD
    "TokenValidatorDep",
=======
    "SessionServiceDep",
>>>>>>> 56a5884 (task_011.4 completed)
    "UserAccountCreatorDep",
    "UserCredentialStoreDep",
    "get_access_token_service",
    "get_authentication_service",
    "get_current_user",
    "get_current_user_optional",
    "get_password_hasher",
    "get_refresh_token_service",
    "get_registration_service",
<<<<<<< HEAD
    "get_token_validator",
=======
    "get_session_service",
>>>>>>> 56a5884 (task_011.4 completed)
    "get_user_account_creator",
    "get_user_credential_store",
    "require_authentication",
]
