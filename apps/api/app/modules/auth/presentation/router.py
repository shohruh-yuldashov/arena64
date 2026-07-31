"""HTTP routes for `auth`.

One endpoint: `POST /auth/register`. No login, no token, no session —
A64-011.1's explicit boundary, and none of them is stubbed here.

Errors need no handling in this file. Every failure below is a typed
exception on the platform hierarchy, and
`app/api/exception_handlers.py` maps them by MRO walk:

    WeakPassword            -> 422  weak_password
    InvalidUsername         -> 422  invalid_username
    InvalidEmail            -> 422  invalid_email
    UsernameAlreadyExists   -> 409  username_already_exists
    EmailAlreadyExists      -> 409  email_already_exists

Each carries a distinct code precisely so a sign-up form knows which
field to mark, which is the rule for earning one
(`app.core.error_codes.ErrorCode`).
"""

from fastapi import APIRouter, Response, status

from app.api.responses import build_response
from app.core.constants import API_PREFIX, API_V1_PREFIX
from app.core.responses import ApiResponse
from app.modules.auth.application.commands import RegisterUser
from app.modules.auth.presentation.dependencies import RegistrationServiceDep
from app.modules.auth.presentation.schemas import RegisterRequest
from app.modules.users.public import UserRead

auth_router = APIRouter(prefix="/auth", tags=["auth"])


@auth_router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    service: RegistrationServiceDep,
    response: Response,
) -> ApiResponse[UserRead]:
    """Creates an account.

    Returns `201` with the new user, wrapped in the platform envelope, and
    a `Location` header pointing at the canonical resource — the created
    account is readable at `GET /api/v1/users/{id}`, and saying so is what
    `201` is for.

    The response body carries no `password_hash`: `UserRead` has no such
    field, so this is a property of the type rather than of remembering to
    exclude it here. `is_verified` is `false` — nobody has proven they own
    the address yet, and doing so is a later task's flow.
    """
    created = await service.register(
        RegisterUser(
            username=payload.username,
            email=payload.email,
            password=payload.password,
            preferred_language=payload.preferred_language.value,
            timezone=payload.timezone,
            display_name=payload.display_name,
        )
    )

    # Both prefixes: `API_V1_PREFIX` alone is "/v1", and the mount point
    # adds "/api" on top of it (`app_factory`). Composing them here is
    # what makes the header a URL a client can actually follow rather
    # than a path that 404s.
    response.headers["Location"] = f"{API_PREFIX}{API_V1_PREFIX}/users/{created.id}"
    return build_response(created)
