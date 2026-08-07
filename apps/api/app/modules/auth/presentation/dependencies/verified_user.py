"""`VerifiedUser` — the guard on actions that need a confirmed address.
A64-021.5H §14, §16.

One dependency, added to the routes that classify as *verified required*.
Not a check repeated in handler bodies: §14 is explicit, and the reason is
the failure mode rather than the tidiness — a rule written thirty times is a
rule that is written twenty-nine times the day somebody adds a route.

## Why it reads the database

`AuthenticatedUser` is built entirely from a JWT and its own docstring says
what follows: *"these facts were true when the token was issued… anything
whose correctness depends on the account's current state must read that
state."*

Verification is exactly that state, and putting `email_verified` in the
token would break the flow it exists to serve. An access token lives fifteen
minutes; a person who verifies at minute two would keep being refused until
minute fifteen, on a screen that has just told them they are verified. The
alternative — reissuing tokens on verification — is a second mechanism for
one boolean.

So it is one primary-key read per guarded request. That is affordable
because of *what* is guarded: writes. Nothing on a read path carries this,
and §16's matrix is what keeps that true.

## What it deliberately does not guard

The verification flow itself, session lifecycle, and reads. A guard that
covered `POST /auth/email/verify-code` would make verification require
verification, and one that covered `GET /auth/me` would leave a client
unable to discover *why* it was being refused.
"""

from typing import Annotated

from fastapi import Depends

from app.modules.auth.domain.exceptions import EmailVerificationRequired
from app.modules.auth.presentation.dependencies.current_user import CurrentUser
from app.modules.auth.public import AuthenticatedUser
from app.modules.users.presentation.dependencies import UserServiceDep
from app.modules.users.public import UserProfileService


async def require_verified_email(user: CurrentUser, users: UserServiceDep) -> AuthenticatedUser:
    """Raises `EmailVerificationRequired` unless the address is confirmed.

    Returns the same `AuthenticatedUser` the route would otherwise receive,
    so a handler can depend on this **instead of** `CurrentUser` rather than
    in addition to it — one parameter, not two, and no way to hold the
    identity without having passed the check.

    `403`, not `401`. The caller is authenticated and re-authenticating
    would change nothing; the fix is `/verify-email`, and the stable code is
    what tells a client to go there rather than to a sign-in form.
    """
    # `UserProfileService` is `users`' published reader — the same adapter
    # `auth`'s own services hold. Narrowing here rather than taking a wider
    # dependency means this guard can read an account and can change
    # nothing about one.
    profile = await UserProfileService(users).get_profile(user.id)
    if not profile.is_verified:
        raise EmailVerificationRequired("this action requires a verified email address")
    return user


#: The annotation a guarded route names in place of `CurrentUser`.
#:
#:     async def send_request(user: VerifiedUser, ...):   # 403 while unverified
#:     async def read_profile(user: CurrentUser, ...):    # allowed either way
#:
#: Reading like `CurrentUser` is deliberate: the difference between a guarded
#: and an unguarded route should be one word in a signature, because that is
#: what makes §16's matrix reviewable in a diff.
VerifiedUser = Annotated[AuthenticatedUser, Depends(require_verified_email)]


__all__ = ["VerifiedUser", "require_verified_email"]
