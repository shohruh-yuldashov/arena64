"""The FastAPI `Depends` bridge for `friends` — dependency-injection.md
DI-01: `Depends` is used only at the routing layer, to hand a route an
already-resolved service. It is not the container.

The graph assembled per request:

    AsyncSession                          one per request (`app.api.deps`)
      -> SqlAlchemyFriendRequestRepository
      -> FriendRequestValidator           shares the repository
      -> SessionUnitOfWork
      -> FriendRequestService

**One factory, unlike `profiles`' four.** That module publishes several
narrow ports because different routes need different capabilities from
`users`; here every route is a friend-request use case served by one
service, so splitting would produce four factories over one object graph
with nothing distinguishing them.

The validator is built here rather than inside the service, and shares the
service's repository instance deliberately: both read the same relation in
the same request, and a second repository would mean a second identity map
over the same rows.
"""

from typing import Annotated

from fastapi import Depends

from app.api.deps import ClockDep, DbSessionDep
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.friends.application.services import FriendRequestService
from app.modules.friends.application.validators import FriendRequestValidator
from app.modules.friends.infrastructure.repositories import SqlAlchemyFriendRequestRepository


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
        validator=FriendRequestValidator(requests),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


FriendRequestServiceDep = Annotated[FriendRequestService, Depends(get_friend_request_service)]


__all__ = ["FriendRequestServiceDep", "get_friend_request_service"]
