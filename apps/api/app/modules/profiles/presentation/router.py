"""HTTP routes for `profiles` — the public profile API.

One endpoint, and **no business logic in it**. The handler translates a
path parameter into a service call and the result into a wire schema.
Composition is `ProfileService`'s, visibility is `users`', and `win_rate`
is the domain's.

## Errors need no handling here

`ProfileNotFound` is a `NotFoundError` on the platform hierarchy, and
`app/api/exception_handlers.py` maps it by MRO walk. There is not one
`try`/`except` in this file:

    ProfileNotFound  -> 404  not_found
    InvalidUsername  -> 422  invalid_username

The second comes from the path parameter's validator and is the only place
this endpoint distinguishes anything: a name that is not a *possible*
username is a client error, while a name that is possible and unclaimed is
a 404. Neither reveals more than the other — see `ProfileNotFound` on why a
404 here is not the membership oracle it would be on a credential path.

## Unauthenticated, by design

No `CurrentUser`, no `RequireAuthentication`. A public profile is public:
requiring a token would break every link a player shares and every
server-rendered page AD-24 anticipates. The response carries nothing that
authentication would gate — no email, no account state, no activity.

That does make this the platform's most enumerable surface, and it is
deliberately **not** rate limited, because A64-012.1's scope does not
include it. The recommendations say so; it is one dependency away, since
`app.api.rate_limiting.RateLimit` is already the mechanism six `auth`
endpoints use.
"""

from typing import Annotated

from fastapi import APIRouter, Path, status

from app.api.openapi import Responses, error_response
from app.api.responses import build_response
from app.core.responses import ApiResponse
from app.modules.avatars.presentation.dependencies import AvatarLinkBuilderDep
from app.modules.profiles.presentation.dependencies import ProfileServiceDep
from app.modules.profiles.presentation.schemas import ProfileResponse
from app.modules.users.domain.validators import USERNAME_MAX_LENGTH, USERNAME_MIN_LENGTH

profiles_router = APIRouter(prefix="/profiles", tags=["profiles"])


_NOT_FOUND: Responses = error_response(
    404,
    (
        "No visible profile for that username. Returned identically "
        "whether the username was never registered or belongs to a "
        "deactivated account."
    ),
)
_UNPROCESSABLE: Responses = error_response(
    422,
    (
        "The username is not a possible handle — wrong length, or "
        "characters a username may not contain."
    ),
)


@profiles_router.get(
    "/{username}",
    status_code=status.HTTP_200_OK,
    summary="Read a player's public profile",
    response_description="The player's public profile.",
    responses={**_NOT_FOUND, **_UNPROCESSABLE},
)
async def get_profile(
    username: Annotated[
        str,
        Path(
            min_length=USERNAME_MIN_LENGTH,
            max_length=USERNAME_MAX_LENGTH,
            description="The player's handle. Matching is case-insensitive.",
            examples=["player_one"],
        ),
    ],
    service: ProfileServiceDep,
    avatar_links: AvatarLinkBuilderDep,
) -> ApiResponse[ProfileResponse]:
    """Returns the public profile of the player holding `username`.

    **Case-insensitive.** `/profiles/Alice`, `/profiles/alice` and
    `/profiles/ALICE` resolve to one account, because usernames are unique
    on their case-folded form (domain-model.md UP-1). The `username` in the
    response is the casing that player chose, which is what a client should
    render.

    **Public and unauthenticated.** No token is required and none changes
    the response. Nothing here is gated: the body carries no email, no
    account state and no activity data.

    `404` when there is no visible profile — whether the username was never
    registered or the account has been deactivated. The two are
    deliberately indistinguishable, so that this endpoint cannot be used to
    discover which handles belong to withdrawn accounts, which is what an
    impersonator would want before adopting one.

    ## Presence: `is_online` and `last_seen`

    Both are best-effort and both may be `null`, and a `null` deliberately
    means **nothing more than "nothing can be said"**. It covers a player
    who has hidden their presence, a player nobody has observed, a presence
    record that has expired, and presence being temporarily unavailable —
    all rendered identically, because reporting which applies would answer
    the question the privacy setting exists to decline.

    Read them independently. They are governed by two different settings
    with two different defaults, and the common case is a player who shows
    `is_online` and withholds `last_seen`: `show_last_seen` is the one
    privacy flag that is off out of the box, because a published "last seen
    03:14" is a sleep schedule while "online now" is momentary.

    Render `null` as *unknown*, never as *offline* — `is_online: false` is
    the value that means offline, and it is only available for a player seen
    disconnecting recently.

    **Both are `null` for every player today**, because presence is written
    by the realtime gateway and that does not exist yet. The fields are in
    the contract so clients gain no unexpected keys when it does.

    `bio` and `country` are `null` only when a player has not set them — or,
    for `country`, when they have chosen not to show it, which is
    indistinguishable here for the same reason presence is. Both are written
    through `PATCH /profile`.

    ## Ratings and statistics are real fields with placeholder values

    Every player currently reports the starting rating in each category,
    marked `is_provisional: true`, with every match count at zero. That is
    the truthful answer while no match has ever been played, and it is the
    same shape the rating system will return — so a client written today
    needs no change when real values arrive.

    **Read `is_provisional` before rendering a rating.** A provisional
    rating is a starting value rather than a measurement, and displaying
    one unmarked misleads both the viewer and any opponent deciding whether
    to accept a challenge (domain-model.md PR-6).
    """
    profile = await service.get_public_profile(username)

    # The avatar URL is composed here, at the edge, from the reference the
    # profile carries — see `ProfileResponse.of` on why the schema is
    # handed links rather than a provider.
    return build_response(
        ProfileResponse.of(profile, avatar_links.links_for(profile.identity.avatar))
    )
