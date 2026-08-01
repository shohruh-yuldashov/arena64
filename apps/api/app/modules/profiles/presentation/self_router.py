"""HTTP routes for your own profile — editing it (A64-012.3) and
controlling who sees it (A64-012.4).

Four endpoints, and **no business logic in any of them**. Each translates a
request into a service call and the result into a wire schema. Validation
is the domain's, the transaction is `users`', and URL composition is
`AvatarLinkBuilder`'s.

**No privacy logic either**, which is A64-012.4's architectural
requirement stated as a property of this file: "endpoints must not manually
hide fields". There is no `if not privacy.show_country` here and there
could not usefully be one — by the time a public profile reaches a route it
has already been redacted by `users`' mapper and composed by
`ProfileService`. The two endpoints below *set* the flags; nothing here
*applies* them.

## Why `/profile` and not `/profiles/{username}`

(The argument below is about editing, and it is the same argument that
makes `/profile/privacy` safe: the account is the token's, so "only the
owner may change their privacy settings" needs no check because no other
account is addressable.)

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

from fastapi import APIRouter, Depends, status

from app.api.exception_handlers import ErrorResponse
from app.api.responses import build_response
from app.core.responses import ApiResponse
from app.core.sentinels import UNSET
from app.modules.auth.presentation.dependencies import CurrentUser
from app.modules.avatars.presentation.dependencies import AvatarLinkBuilderDep
from app.modules.profiles.presentation.dependencies import (
    PrivacySettingsEditorDep,
    ProfileEditorDep,
)
from app.modules.profiles.presentation.rate_limits import PRIVACY_UPDATE_RATE_LIMIT
from app.modules.profiles.presentation.schemas import (
    MyProfileResponse,
    PrivacySettingsResponse,
    PrivacySettingsUpdateRequest,
    ProfileUpdateRequest,
)
from app.modules.users.public import PrivacyEdits, ProfileEdits

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


# --- privacy settings (A64-012.4) -------------------------------------------
#
# Two endpoints on `/profile/privacy`, kept out of `PATCH /profile` above on
# purpose. Merging them would look tidier and would be wrong in three ways:
# the write needs a rate limit the profile edit does not have, the two go
# through different published ports so that editing a biography does not
# confer the ability to publish an account's activity, and a settings screen
# loads visibility without touching a display name.

_PRIVACY_UNPROCESSABLE: _Responses = {
    422: {
        "description": (
            "The body carried a field this endpoint does not accept, or a flag was "
            "sent as `null`. `message` names which. Unknown fields are **rejected**, "
            "not ignored — a client that believed it had hidden something it had not "
            "would act as though the field were private."
        ),
        "model": ErrorResponse,
    }
}
_TOO_MANY_REQUESTS: _Responses = {
    429: {
        "description": (
            "Too many privacy updates from this address. `Retry-After` says how long to wait."
        ),
        "model": ErrorResponse,
    }
}


@my_profile_router.get(
    "/privacy",
    status_code=status.HTTP_200_OK,
    summary="Read your privacy settings",
    response_description="All five flags, as they currently stand.",
    responses={**_UNAUTHORIZED, **_NOT_FOUND},
)
async def get_my_privacy_settings(
    user: CurrentUser,
    privacy: PrivacySettingsEditorDep,
) -> ApiResponse[PrivacySettingsResponse]:
    """Returns the authenticated account's privacy settings.

    Always all five flags, never a subset. An account has an answer for
    every one of them — the columns are `NOT NULL` and the platform
    defaults apply to rows nobody has touched — so a client renders a
    settings screen from this response alone and never has to guess.

    Deliberately **not** rate limited: it is one indexed read of a row you
    have already authenticated as, it changes nothing, and a settings
    screen loads it on every visit. See `rate_limits.py`.

    Scoped to you by construction. There is no path segment or parameter
    that could name another account, which is why there is no ownership
    check — and why no endpoint anywhere returns *somebody else's* flags.
    A stranger's profile shows a `null` where a hidden field would be and
    says nothing about why.
    """
    return build_response(PrivacySettingsResponse.of(await privacy.get_privacy_settings(user.id)))


@my_profile_router.patch(
    "/privacy",
    status_code=status.HTTP_200_OK,
    summary="Update your privacy settings",
    response_description="All five flags as they now stand.",
    responses={**_UNAUTHORIZED, **_NOT_FOUND, **_PRIVACY_UNPROCESSABLE, **_TOO_MANY_REQUESTS},
    dependencies=[Depends(PRIVACY_UPDATE_RATE_LIMIT)],
)
async def update_my_privacy_settings(
    payload: PrivacySettingsUpdateRequest,
    user: CurrentUser,
    privacy: PrivacySettingsEditorDep,
) -> ApiResponse[PrivacySettingsResponse]:
    """Applies a partial update to the authenticated account's privacy
    settings.

    **A real PATCH.** Send only the flags you are changing; the rest are
    left exactly as they were. `{"show_country": false}` does not reset the
    other four to their defaults — which matters most for `show_last_seen`,
    whose default is *off*, so a reset would publish something the player
    had deliberately kept private.

    **Only your own settings.** The account comes from your access token;
    there is no way to name a different one.

    **Unknown fields are rejected**, and a flag sent as `null` is rejected
    too. A boolean control has two states, so `null` has no meaning here
    that would not have to be invented — and the most likely invention,
    "reset to default", is the one thing this endpoint must not do
    implicitly.

    ## What each flag governs

    | Flag | Effect on your public profile | Default |
    | --- | --- | --- |
    | `show_country` | `country` is `null` when off | `true` |
    | `show_last_seen` | `last_seen` is `null` when off | **`false`** |
    | `show_statistics` | `statistics` is `null` when off — never zeroes | `true` |
    | `show_online_status` | live presence, once it exists | `true` |
    | `show_activity` | recent activity, once it exists | `true` |

    A hidden field is `null` and nothing else: no placeholder, no marker,
    and nothing that distinguishes it from a field you simply never filled
    in. That is deliberate — reporting *that* something is hidden answers
    the question hiding it was meant to decline.

    **Ratings are always public** and no flag covers them. A rating is what
    pairing is computed from and what leaderboards publish; `show_statistics`
    covers the record of games behind it, not the number itself.

    Rate limited per network address. See the `429` response.
    """
    # `model_fields_set` is what makes this a real PATCH — the same
    # mechanism `PATCH /profile` uses above. Reading the attribute values
    # alone cannot distinguish `false` from "not sent", and on a privacy
    # endpoint that confusion writes `false` over a flag the caller never
    # mentioned.
    #
    # The `is not None` half is belt and braces: the schema has already
    # rejected an explicit null, so a key in `sent` carries a real boolean
    # and this branch is unreachable. It stays because `PrivacyEdits` has
    # no `None` in its unions — a validator removed or reordered later
    # would otherwise put a `None` where the type says a `bool` is, which
    # the type checker catches here and would not catch at all if this were
    # written as a `cast`.
    sent = payload.model_fields_set
    edits = PrivacyEdits(
        show_country=(
            payload.show_country
            if "show_country" in sent and payload.show_country is not None
            else UNSET
        ),
        show_last_seen=(
            payload.show_last_seen
            if "show_last_seen" in sent and payload.show_last_seen is not None
            else UNSET
        ),
        show_statistics=(
            payload.show_statistics
            if "show_statistics" in sent and payload.show_statistics is not None
            else UNSET
        ),
        show_online_status=(
            payload.show_online_status
            if "show_online_status" in sent and payload.show_online_status is not None
            else UNSET
        ),
        show_activity=(
            payload.show_activity
            if "show_activity" in sent and payload.show_activity is not None
            else UNSET
        ),
    )

    settings = await privacy.update_privacy_settings(user.id, edits)

    # **Field names only — never values, old or new.** A64-012.4 requires
    # the updated fields to be logged and forbids logging previous values,
    # and the same reading applies here as on `profile_updated`: log no
    # values at all. Which parts of their account a person has chosen to
    # hide is itself sensitive — a log line saying `show_last_seen: false`
    # records a privacy decision in a system with broader read access and
    # different retention than the database (services.md §8.5). The field
    # list answers what an audit asks — *what changed, for whom, when* —
    # and the row holds the answers.
    logger.info(
        "privacy_updated",
        extra={"user_id": str(user.id), "updated_fields": sorted(sent)},
    )

    return build_response(PrivacySettingsResponse.of(settings))
