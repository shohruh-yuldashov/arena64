"""The FastAPI `Depends` bridge for this module — dependency-injection.md
DI-01: `Depends` is used *only* at the routing layer, to hand a route an
already-resolved service. It is not the container.

That distinction is why this file exists here rather than the router
constructing a `UserService` inline: the same service must be resolvable
by a future Celery task or admin tool that has no HTTP request and no
`Depends` at all. Those callers will construct the identical object graph
through `app.core.di.Container`; this module is only the HTTP-shaped half
of that bridge, and nothing in `application/` or `domain/` knows it exists.

The graph assembled here, per request:

    AsyncSession        opened by `app.api.deps.get_db_session` (one per
                        request — DI-02's rule that a session is scoped to
                        a command, never to a connection)
      -> SqlAlchemyUserRepository   the port's adapter
      -> SessionUnitOfWork          the transaction boundary over that
                                    same session, so the service can
                                    commit without touching SQLAlchemy
      -> UserService                the use cases
"""

from typing import Annotated

from fastapi import Depends

from app.api.deps import ClockDep, DbSessionDep, get_clock
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.users.application.ports import UserRepository
from app.modules.users.application.services import UserService
from app.modules.users.infrastructure.repositories import SqlAlchemyUserRepository

# `get_clock` and `ClockDep` moved to `app.api.deps` in A64-011.9 — "now"
# is a platform concern, not this module's, and `auth` was importing them
# from here, which meant reaching into another module's private
# presentation package (R-1). Re-exported under the original names so this
# module's own routes and every test that overrides `get_clock` are
# unaffected by where it lives.
__all__ = ["ClockDep", "UserRepositoryDep", "UserServiceDep", "get_clock"]


def get_user_repository(session: DbSessionDep) -> UserRepository:
    return SqlAlchemyUserRepository(session)


UserRepositoryDep = Annotated[UserRepository, Depends(get_user_repository)]


def get_user_service(
    session: DbSessionDep,
    users: UserRepositoryDep,
    clock: ClockDep,
) -> UserService:
    # The unit of work wraps the *same* session the repository holds —
    # otherwise the service would commit a transaction the repository
    # never wrote to, and the write would be silently lost on request
    # teardown.
    return UserService(
        users=users,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


UserServiceDep = Annotated[UserService, Depends(get_user_service)]
