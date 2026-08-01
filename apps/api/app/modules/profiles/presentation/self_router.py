"""HTTP routes for editing your own profile — A64-012.3.

Two endpoints, and **no business logic in either**. Each translates a
request into a service call and the result into a wire schema. Validation
is the domain's, the transaction is `users`', and URL composition is
`AvatarLinkBuilder`'s.

## Why `/profile` and not `/profiles/{username}`

Singular, and with no identifier in it — the same design as
`/profile/avatar` (A64-012.2), for the same reason.

A64-012.3 requires that "only the profile owner may edit". The strongest
way to enforce that is an endpoint on which the target *cannot be
expressed*: the account comes from the access token's `sub`, and there is
no path segment, query parameter or body field that could name a different
one. There is no ownership check in this file because there is nothing to
check — another player's profile is not addressable here.

`/profiles/{username}` stays what it is: the public, unauthenticated read
of anybody's profile (A64-012.1). One is "me", one is "them", and keeping
them on separate paths is what stops the second from ever growing a write
verb.

## Errors need no handling here

Every failure is a typed exception on the platform hierarchy, and
`app/api/exception_handlers.py` maps them by MRO walk. There is not one
`try`/`except` in this file:

    InvalidDisplayName  -> 422  validation_error
    InvalidBio          -> 422  validation_error
    InvalidCountryCode  -> 422  validation_error
    InvalidLanguage     -> 422  validation_error
    InvalidTimezone     -> 422  validation_error
    UserNotFound        -> 404  not_found
    MissingToken        -> 401  authentication_required

## What this endpoint deliberately cannot change

`username`, `email`, `is_active`, `is_verified` and the avatar. The first
two have their own flows for good reasons recorded on
`UpdateUserProfile`; the next two are state transitions rather than
values; the avatar is `/profile/avatar`'s, because changing it means
validating and re-encoding an image rather than storing a string.
"""

import logging
from typing import Any

from fastapi import APIRouter, status

from app.api.exception_handlers import ErrorResponse
from app.api.responses import build_response
from app.core.responses import ApiResponse
from app.core.sentinels import UNSET
from app.modules.auth.presentation.dependencies import CurrentUser
from app.modules.avatars.presentation.dependencies import AvatarLinkBuilderDep
from app.modules.profiles.presentation.dependencies import ProfileEditorDep
from app.modules.profiles.presentation.schemas import MyProfileResponse, ProfileUpdateRequest
from app.modules.users.public import ProfileEdits

logger = logging.getLogger(__name__)

my_profile_router = APIRouter(prefix="/profile", tags=["profile"])

#: FastAPI's own annotation for `responses=`. Spelled once rather than
#: inferred, for the reason `auth`'s router gives.
type _Responses = dict[int | str, dict[str, Any]]

_UNAUTHORIZED: _Responses = {
    401: {
        "description": "No access token was presented, or it was invalid or expired.",
        "model": ErrorResponse,
    }
}
_NOT_FOUND: _Responses = {
    404: {
        "description": "The account no longer exists — deleted while a valid token was in flight.",
        "model": ErrorResponse,
    }
}
_UNPROCESSABLE: _Responses = {
    422: {
        "description": (
            "A field failed validation, or the body carried a field this endpoint does "
            "not accept. `message` names which. Unknown fields are **rejected**, not "
            "ignored — a silently dropped `username` would look like a successful "
            "rename."
        ),
        "model": ErrorResponse,
    }
}


@my_profile_router.get(
    "/me",
    status_code=status.HTTP_200_OK,
    summary="Read your own profile",
    response_description="Your profile, including fields the public view omits.",
    responses={**_UNAUTHORIZED, **_NOT_FOUND},
)
async def get_my_profile(
    user: CurrentUser,
    editor: ProfileEditorDep,
    avatar_links: AvatarLinkBuilderDep,
) -> ApiResponse[MyProfileResponse]:
    """Returns the authenticated account's own profile.

    Differs from `GET /profiles/{username}` in two ways that matter. It
    includes **`timezone`**, which the public view withholds because
    publishing it narrows a player's physical location to anyone who asks;
    and it is scoped to the caller by construction, so it needs no
    username and cannot be pointed at anybody else.

    Differs from `GET /auth/me` in the other direction: that one answers
    *who am I* and carries the email and verification state; this one
    answers *what does my profile say* and carries the five editable
    fields. Together they are the two halves an account settings page
    needs.

    Returns exactly the shape `PATCH /profile` returns, so a client can
    populate an edit form and render the result of a save with one parser.
    """
    profile = await editor.get_own_profile(user.id)

    return build_response(MyProfileResponse.of(profile, avatar_links.links_for(profile.avatar)))


@my_profile_router.patch(
    "",
    status_code=status.HTTP_200_OK,
    summary="Update your own profile",
    response_description="The profile as it now stands, with normalised values.",
    responses={**_UNAUTHORIZED, **_NOT_FOUND, **_UNPROCESSABLE},
)
async def update_my_profile(
    payload: ProfileUpdateRequest,
    user: CurrentUser,
    editor: ProfileEditorDep,
    avatar_links: AvatarLinkBuilderDep,
) -> ApiResponse[MyProfileResponse]:
    """Applies a partial update to the authenticated account's profile.

    **A real PATCH.** Send only what changes; omitted fields are left
    alone. The three optional fields — `display_name`, `bio`, `country` —
    distinguish *omitted* from *explicitly null*: omitting leaves the value
    as it is, sending `null` clears it. `preferred_language` and `timezone`
    always have a value, so an explicit `null` for either is a `422` rather
    than a silent no-op.

    **Only your own profile.** The account comes from your access token;
    there is no way to name a different one.

    **Unknown fields are rejected.** A body carrying `username`,
    `is_verified` or `avatar_object_key` returns `422` naming the field.
    None of them is settable here, and failing loudly is what stops a
    client from believing a rename succeeded.

    ## Validation

    | Field | Rule |
    | --- | --- |
    | `display_name` | 3-50 characters, any script, trimmed |
    | `bio` | at most 500 characters, plain text |
    | `country` | an assigned ISO 3166-1 alpha-2 code |
    | `preferred_language` | `en`, `ru`, or `uz` |
    | `timezone` | an IANA name this system knows |

    `display_name` and `bio` additionally reject control and
    bidirectional characters — a right-to-left override in a name rendered
    beside every match reverses the text around it, which is a display
    spoofing primitive rather than a typo.

    **Nothing is written if any field is rejected.** Values are validated
    as they are converted, before the account row is touched, so a request
    with a good bio and a bad country changes neither.

    Returns the full profile with **normalised** values — a padded display
    name comes back trimmed, `uz` comes back `UZ` — so a client renders
    what was actually stored rather than what it sent.
    """
    # `model_fields_set` is what makes this a real PATCH: it reports which
    # keys the client actually sent, so an omitted field maps to `UNSET`
    # (leave alone) while an explicit `null` maps to `None` (clear).
    # Reading the attribute values alone cannot tell the two apart, and
    # treating them the same is how a PATCH silently wipes fields the
    # caller never mentioned.
    sent = payload.model_fields_set
    edits = ProfileEdits(
        display_name=payload.display_name if "display_name" in sent else UNSET,
        bio=payload.bio if "bio" in sent else UNSET,
        country=payload.country if "country" in sent else UNSET,
        # These two reject an explicit null at the schema, so reaching here
        # with the key present means a real value.
        preferred_language=(
            payload.preferred_language
            if "preferred_language" in sent and payload.preferred_language is not None
            else UNSET
        ),
        timezone=(
            payload.timezone if "timezone" in sent and payload.timezone is not None else UNSET
        ),
    )

    profile = await editor.update_own_profile(user.id, edits)

    # **Field names only — never values, old or new.** A64-012.3 requires
    # the updated fields to be logged and forbids logging previous values
    # of sensitive fields, and the safe reading of that is to log no
    # values at all: a display name and a biography are personal data
    # (§14.1), a country is location data, and none of them is worth
    # putting in a system with broader read access and different retention
    # than the database (services.md §8.5). The field list answers what an
    # audit actually asks — *what changed, for whom, when* — and the
    # database holds the values.
    logger.info(
        "profile_updated",
        extra={"user_id": str(user.id), "updated_fields": sorted(sent)},
    )

    return build_response(MyProfileResponse.of(profile, avatar_links.links_for(profile.avatar)))
