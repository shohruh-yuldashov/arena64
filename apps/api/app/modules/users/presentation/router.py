"""HTTP routes for `users`.

Three endpoints, per the task: fetch one, list, update one. No
registration, no login, no token — those are A64-011's, and none of them
is reachable from here.

**No authorisation, and that is a stated gap rather than an oversight.**
Every route below is open. `GET /users/{id}` returns an email address to
anyone who asks, and `PATCH /users/{id}` lets anyone edit anyone. That is
what "no authentication required yet" means for a module whose data is
personal, and it is safe only because nothing is deployed. A64-011 must
close it before this reaches an environment with real users — see the task
summary's recommendations for exactly which routes need which check.

Every response goes through `build_response` (`app.api.responses`), so the
`{data, meta}` envelope and its correlation ids are identical to every
other endpoint on the platform (A64-008). Errors need no handling here at
all: this module's exceptions inherit the platform hierarchy, and
`app/api/exception_handlers.py` maps them by MRO walk.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from app.api.responses import build_response
from app.core.pagination import CursorPageParams
from app.core.responses import ApiResponse
from app.modules.users.application.mappers import to_user_read, to_user_summary
from app.modules.users.presentation.dependencies import UserServiceDep
from app.modules.users.presentation.schemas import UserList, UserRead

users_router = APIRouter(prefix="/users", tags=["users"])


@users_router.get("/{user_id}", status_code=status.HTTP_200_OK)
async def get_user(user_id: UUID, service: UserServiceDep) -> ApiResponse[UserRead]:
    """Fetches one user. `404` if no such user — raised as `UserNotFound`
    by the service and mapped by the platform handler, not here."""
    user = await service.get_user(user_id)
    return build_response(to_user_read(user))


@users_router.get("", status_code=status.HTTP_200_OK)
async def list_users(
    service: UserServiceDep,
    cursor: Annotated[str | None, Query(description="Opaque cursor from a previous page.")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    is_active: Annotated[bool | None, Query(description="Filter by activation state.")] = None,
) -> ApiResponse[UserList]:
    """Keyset-paginated listing (RP-03) — the cursor is opaque and must be
    passed back unchanged, never constructed by a client.

    Returns `UserSummary`, not `UserRead`: a listing has no business
    handing out an email address per row.
    """
    users, page = await service.list_users(
        CursorPageParams(cursor=cursor, limit=limit), is_active=is_active
    )
    return build_response(UserList(items=[to_user_summary(user) for user in users], page=page))


# --- removed in A64-012.3: `PATCH /users/{user_id}` --------------------------
#
# A64-010 shipped an **unauthenticated** partial profile update keyed on a
# user id in the path. Anyone who knew a player's id — which is public, and
# which `GET /profiles/{username}` returns — could rewrite that player's
# display name, language and timezone.
#
# A64-012.3's requirement is that "only the profile owner may edit", and
# leaving this route would have made that claim false rather than merely
# incomplete: the new `PATCH /profile` would enforce ownership while this
# one sat beside it enforcing nothing. Shipping both is worse than shipping
# neither.
#
# `UserService.update_profile` is untouched and is what the new endpoint
# calls. What is gone is only the route and its request schema.
#
# The replacement is `PATCH /api/v1/profile` — authenticated, scoped to the
# token's own account, and unable to name a different one. An
# administrative "edit any player" capability is a different feature with a
# different authorisation story, and belongs with `apps/admin` (AD-04)
# rather than on the public API.
