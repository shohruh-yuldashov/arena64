"""The FastAPI `Depends` bridge for `friends` — dependency-injection.md
DI-01: `Depends` is used only at the routing layer, to hand a route an
already-resolved service. It is not the container.

The graph assembled per request:

    AsyncSession                          one per request (`app.api.deps`)
      -> SqlAlchemyFriendRequestRepository
      -> SqlAlchemyFriendshipRepository
      -> FriendRequestValidator           shares the request repository
      -> SessionUnitOfWork
      -> FriendRequestService             holds both repositories
      -> FriendshipService                holds the friendship repository

**Two factories since A64-013.3**, one per service, and the split is the
capability rather than the graph — the argument `profiles`' four factories
make. `FriendRequestService` can accept a request and therefore *create* a
friendship; `FriendshipService` can list, count and end them and can do
nothing with requests at all.

## Both services share one session, and that is what makes FR-4 work

`SessionUnitOfWork(session)` is constructed per factory over the *same*
request-scoped `AsyncSession`, so a write issued by the friendship
repository inside `FriendRequestService.accept`'s unit of work is part of
that transaction. A second session would put the two writes in two
transactions and reintroduce exactly the split A64-013.3 forbids.

The validator is built here rather than inside the service, and shares the
request repository instance deliberately: both read the same relation in the
same request, and a second repository would mean a second identity map over
the same rows.
"""

from typing import Annotated

from fastapi import Depends

from app.api.deps import ClockDep, DbSessionDep
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.friends.application.services import FriendRequestService, FriendshipService
from app.modules.friends.application.validators import FriendRequestValidator
from app.modules.friends.infrastructure.repositories import (
    SqlAlchemyFriendRequestRepository,
    SqlAlchemyFriendshipRepository,
)


def get_friend_request_service(session: DbSessionDep, clock: ClockDep) -> FriendRequestService:
    """The friend-request use cases, assembled for this request.

    Everything is constructed here rather than injected from further out,
    which is the composition root's job (BR-6 forbids a *module* reaching
    for the container, not the root wiring a module together).

    The `Clock` is injected rather than read (AD-07): `created_at` and
    `responded_at` both come from it, and a test asserting on either must
    not have to sleep. It is also what makes the future expiry window
    testable without a real one elapsing.
    """
    requests = SqlAlchemyFriendRequestRepository(session)
    return FriendRequestService(
        requests=requests,
        # The **repository**, not `FriendshipService`. That service opens
        # transactions of its own, and calling it from inside `accept`'s
        # unit of work would produce the nested, two-transaction shape
        # A64-013.3 forbids. What acceptance needs is a write that joins the
        # caller's transaction, which is exactly what a repository is.
        friendships=SqlAlchemyFriendshipRepository(session),
        validator=FriendRequestValidator(requests),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


FriendRequestServiceDep = Annotated[FriendRequestService, Depends(get_friend_request_service)]


def get_friendship_service(session: DbSessionDep, clock: ClockDep) -> FriendshipService:
    """The friend-list use cases — A64-013.3.

    Separate from `get_friend_request_service` above even though both are
    built over the same session, for the reason every port pair on this
    platform is separate: what differs is the *capability*. This one can
    list, count and end friendships and cannot touch a request; that one can
    resolve a request and, as a consequence, create a friendship.

    A single factory returning something that did both would hand every
    route on this module the union of the two.
    """
    return FriendshipService(
        friendships=SqlAlchemyFriendshipRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


FriendshipServiceDep = Annotated[FriendshipService, Depends(get_friendship_service)]


__all__ = [
    "FriendRequestServiceDep",
    "FriendshipServiceDep",
    "get_friend_request_service",
    "get_friendship_service",
]
