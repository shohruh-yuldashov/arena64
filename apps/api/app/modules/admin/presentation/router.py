"""The admin API — A64-024.1 §5.

**One endpoint**, and it exists to prove the boundary rather than to do
work. A64-024.2+ adds the surfaces; what this establishes is that an admin
route is one that names `CurrentAdmin`, and that nothing reaches it
otherwise.

## Why the whole router is guarded, not each handler

`dependencies=[Depends(require_admin)]` on the router means a handler added
later is guarded by existing, rather than by the author remembering. §4
forbids "duplicated role-check snippets across every endpoint", and the
strongest form of that is a route that cannot be added unguarded.

The handler still names `CurrentAdmin` because it needs the identity — but
if it did not, the router-level guard would still refuse.

## Never cached

`Cache-Control: no-store` on every response. A privileged answer sitting in
a shared proxy or a browser's back-forward cache is the leak §10 names, and
the admin surface is exactly where it would matter.
"""

from fastapi import APIRouter, Depends, Response

from app.modules.admin.presentation.dependencies import (
    AdminRoleServiceDep,
    CurrentAdmin,
    require_admin,
)
from app.modules.admin.presentation.schemas import AdminSessionResponse
from app.modules.users.presentation.dependencies import UserServiceDep
from app.modules.users.public import UserProfileService

admin_router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


@admin_router.get(
    "/me",
    response_model=AdminSessionResponse,
    summary="The signed-in administrator",
    responses={
        401: {"description": "No credential, or one that does not verify."},
        403: {"description": "Authenticated, and not an administrator."},
    },
)
async def read_admin_session(
    admin: CurrentAdmin,
    users: UserServiceDep,
    roles: AdminRoleServiceDep,
    response: Response,
) -> AdminSessionResponse:
    """Who this session administers as, and what it may do.

    The admin client calls this **before rendering anything privileged**:
    it is the server-authoritative answer that decides whether the shell
    appears at all. A client that guessed from local state would be
    guessing, which is what §6 forbids.
    """
    response.headers["Cache-Control"] = "no-store"

    profile = await UserProfileService(users).get_profile(admin.id)
    return AdminSessionResponse(
        id=str(admin.id),
        username=profile.username,
        display_name=profile.display_name,
        roles=sorted(await roles.roles_for(admin.id)),
    )


__all__ = ["admin_router"]
