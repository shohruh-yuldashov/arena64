"""`CurrentAdmin` — the canonical admin guard. A64-024.1 §4.

The one place on this platform that decides whether a request may act
administratively. Every admin route names `CurrentAdmin` **instead of**
`CurrentUser`, which is what makes "is this route guarded" one word in a
signature rather than a snippet somebody has to remember to paste.

## The order, and why each step is where it is

    CurrentUser       401 — no credential, or one that does not verify
    active account    403 — the account is disabled
    live role         403 — authenticated, enabled, and not an administrator

`CurrentUser` first because an unauthenticated caller must get `401` and
never `403`: the two mean different things to a client, and telling an
anonymous caller "forbidden" would say that the endpoint exists for
somebody.

The **account state check before the role check** is deliberate. A disabled
account that still holds a grant must not act, and checking the role first
would let one through on the strength of a row that outlived the account's
ability to sign in. It also keeps the semantics identical to
`require_verified_email`, which is the platform's existing precedent for
"authenticated is not the same as permitted".

**Email verification is not required**, and that is a decision rather than
an omission: an administrator is created by an operator command against a
known account, not by self-service signup, so the address is already
established out of band. Requiring it would add a step that protects
nothing here and would lock out the first administrator of a fresh
deployment.

## Every admin request reads the database

There is no role claim in `auth.TokenClaims` and A64-024.1 deliberately did
not add one — see `AdminRoleService`. The consequence is the property that
matters: **a revoked administrator is refused on their next request**, not
when their access token happens to expire.

## The refusal says nothing

A non-administrator gets the same `403` whatever the reason, and the
response body names no role, no account and no grant. An endpoint that
explained *why* it refused would tell an ordinary player that
administrators exist, which accounts hold authority, and — by the shape of
the answer — whether a given account is one.
"""

from typing import Annotated

from fastapi import Depends

from app.core.exceptions import PermissionDeniedError
from app.modules.admin.application.services import AdminRoleService
from app.modules.admin.domain.roles import AdminRole
from app.modules.admin.presentation.dependencies.services import AdminRoleServiceDep
from app.modules.auth.presentation.dependencies.current_user import CurrentUser
from app.modules.auth.public import AuthenticatedUser
from app.modules.users.presentation.dependencies import UserServiceDep
from app.modules.users.public import UserProfileService


async def require_admin(
    user: CurrentUser,
    users: UserServiceDep,
    roles: AdminRoleServiceDep,
) -> AuthenticatedUser:
    """Raises unless the caller is an enabled account holding `ADMIN`.

    Returns the same `AuthenticatedUser` a route would otherwise receive,
    so a handler depends on this **instead of** `CurrentUser` — one
    parameter, and no way to hold the identity without having passed.
    """
    # `users`' published reader, the same one `require_verified_email`
    # holds. Narrowed on purpose: this guard can read an account and can
    # change nothing about one.
    profile = await UserProfileService(users).get_profile(user.id)
    if not profile.is_active:
        raise PermissionDeniedError("administrative access is not available for this account")

    if AdminRole.ADMIN not in await roles.roles_for(user.id):
        # The identical message and status a disabled account gets. A
        # caller cannot tell "you are not an administrator" from "your
        # account is disabled", and neither reveals that the other exists.
        raise PermissionDeniedError("administrative access is not available for this account")

    return user


#: The annotation an admin route names in place of `CurrentUser`.
#:
#:     async def me(admin: CurrentAdmin) -> ...:      # 401 / 403 / permitted
#:
#: Reading like `CurrentUser` and `VerifiedUser` is deliberate — the
#: difference between a guarded and an unguarded route should be one word,
#: because that is what makes it reviewable in a diff.
CurrentAdmin = Annotated[AuthenticatedUser, Depends(require_admin)]

__all__ = ["AdminRoleService", "AdminRoleServiceDep", "CurrentAdmin", "require_admin"]
