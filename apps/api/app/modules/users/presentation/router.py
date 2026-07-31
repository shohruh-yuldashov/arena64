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
from app.core.sentinels import UNSET
from app.modules.users.application.commands import UpdateUserProfile
from app.modules.users.domain.entities import User
from app.modules.users.presentation.dependencies import UserServiceDep
from app.modules.users.presentation.schemas import UserList, UserRead, UserSummary, UserUpdate

users_router = APIRouter(prefix="/users", tags=["users"])


def _to_read(user: User) -> UserRead:
    """Maps the domain entity to its wire shape.

    Explicit rather than `UserRead.model_validate(user)` because the
    entity's `username`, `email` and `timezone` are value objects, not
    strings — an implicit conversion would either fail or silently
    serialise the wrapper. Being explicit also means adding a field to the
    entity never leaks it onto the API by accident, which for a model
    carrying a password hash is worth the extra lines.
    """
    return UserRead(
        id=user.id,
        username=user.username.value,
        email=user.email.value,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
        preferred_language=user.preferred_language,
        timezone=user.timezone.value,
        is_active=user.is_active,
        is_verified=user.is_verified,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


def _to_summary(user: User) -> UserSummary:
    return UserSummary(
        id=user.id,
        username=user.username.value,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
    )


@users_router.get("/{user_id}", status_code=status.HTTP_200_OK)
async def get_user(user_id: UUID, service: UserServiceDep) -> ApiResponse[UserRead]:
    """Fetches one user. `404` if no such user — raised as `UserNotFound`
    by the service and mapped by the platform handler, not here."""
    user = await service.get_user(user_id)
    return build_response(_to_read(user))


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
    return build_response(UserList(items=[_to_summary(user) for user in users], page=page))


@users_router.patch("/{user_id}", status_code=status.HTTP_200_OK)
async def update_user(
    user_id: UUID, payload: UserUpdate, service: UserServiceDep
) -> ApiResponse[UserRead]:
    """Partial profile update.

    `model_fields_set` is what makes this a real PATCH: it reports which
    keys the client actually sent, so an omitted field maps to `UNSET`
    (leave alone) while an explicit `null` maps to `None` (clear). Reading
    the attribute values alone cannot distinguish the two, and treating
    them the same is how a PATCH silently wipes fields the caller never
    mentioned.
    """
    sent = payload.model_fields_set
    command = UpdateUserProfile(
        display_name=payload.display_name if "display_name" in sent else UNSET,
        avatar_url=payload.avatar_url if "avatar_url" in sent else UNSET,
        # These two are non-nullable on the entity, so there is no "clear"
        # state to map — `UserUpdate` rejects an explicit null for them
        # before reaching here, leaving only present-with-value or absent.
        preferred_language=(
            payload.preferred_language if payload.preferred_language is not None else UNSET
        ),
        timezone=(payload.timezone if payload.timezone is not None else UNSET),
    )

    user = await service.update_profile(user_id, command)
    return build_response(_to_read(user))
