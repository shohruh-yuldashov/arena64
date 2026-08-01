"""HTTP routes for `users` — two read endpoints, both anonymous, both
returning the same deliberately thin shape.

## The A64-012.6 bug fix

`GET /users/{user_id}` returned `UserRead` to **anyone, unauthenticated**:
an email address, verification and activation state, biography, country,
language, timezone, both timestamps, and the raw avatar `object_key`. The
gap was recorded in this docstring from A64-010 ("safe only because nothing
is deployed") and every task since widened the shape rather than closing
it — A64-012.1 added `bio` and `country`, A64-012.2 swapped the avatar URL
for a storage key.

**Fixed by replacing the representation, not by deleting the route.** Both
endpoints now render `PublicUserResponse`: id, username, display name and
two avatar URLs. Nothing else.

Keeping the route matters. Every cross-context reference on this platform
is a `player_id` (DM-06), so a match card, a leaderboard row or a
moderation queue holds ids rather than usernames — and `GET /profiles/{username}`
cannot serve them. Deleting the only id-to-name lookup would have pushed
every future consumer toward denormalising usernames into their own tables,
which is a worse outcome than a thin public endpoint. See
`schemas/public_user.py` for why the shape is thinner than
`GET /profiles/{username}` rather than a second copy of it.

**`PATCH /users/{user_id}` was removed by A64-012.3** for the same class of
reason — see the comment at the foot of this file.

## The A64-012.8 fix — deactivated accounts left the roster

`GET /users` accepted `?is_active=false` and answered with **exactly the
list of withdrawn accounts**, to anyone, unauthenticated. With no parameter
at all it returned active and deactivated accounts mixed together.

That contradicted a rule the rest of this epic enforces at length.
`PublicProfileReader.find_public_profile` returns `None` for a deactivated
account, `ProfileService` turns that into a 404 identical to a username
nobody ever registered, and both carry paragraphs explaining why: *which
accounts are withdrawn* is itself a disclosure, because an impersonator
choosing whose handle to adopt wants precisely that list. One endpoint
published it as a query parameter.

**Fixed by removing the parameter, not by changing its default.** A default
of `is_active=True` would have left `?is_active=false` reachable, which is
the whole problem; and a filter nobody may set to the interesting value is
a parameter with one legal argument. `UserService.list_users` keeps all
three modes — a moderation console (AD-04) is the caller that legitimately
needs "show me the withdrawn accounts", and it will not reach it through
this router.

The behaviour change is deliberate and is the only one A64-012.8 makes: a
client paging the roster now sees fewer rows. A client still sending
`?is_active=false` gets a `200` and the active accounts — FastAPI ignores
query parameters a signature does not declare, so the parameter is silently
inert rather than rejected.

Silent is the right outcome *here* and would not be on a write. The caller
asked for something it may not have and receives something it may; there is
no state to corrupt and nothing it could believe happened that did not. The
opposite argument governs `PATCH /profile`, where an ignored field would
look like a successful edit — which is why every request schema on the
platform sets `extra="forbid"` and this read does not.

## What these still do not do

Neither requires authentication, and that is now a considered position
rather than an unclosed gap: what they return is a handle and a picture,
which is what a public chess platform publishes about its players by
design. `GET /users` remains an anonymous, keyset-paginated roster of
*active* players; whether a player directory should be enumerable at all is
a product question, and it stays flagged rather than decided here.

Every response goes through `build_response` (`app.api.responses`), so the
`{data, meta}` envelope and its correlation ids are identical to every
other endpoint on the platform (A64-008). Errors need no handling here at
all: this module's exceptions inherit the platform hierarchy, and
`app/api/exception_handlers.py` maps them by MRO walk.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from app.api.openapi import Responses, error_response
from app.api.responses import build_response
from app.core.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.core.pagination import CursorPageParams
from app.core.responses import ApiResponse
from app.modules.avatars.presentation.dependencies import AvatarLinkBuilderDep
from app.modules.users.application.mappers import to_user_summary
from app.modules.users.presentation.dependencies import UserServiceDep
from app.modules.users.presentation.schemas import PublicUserResponse, UserList

users_router = APIRouter(prefix="/users", tags=["users"])

#: A64-012.8. Both routes could fail this way and neither said so: a
#: malformed `user_id` and an out-of-range `limit` are both `422`, and a
#: status a client has not been told to expect is one a generated client has
#: no branch for.
_NOT_FOUND: Responses = error_response(404, "No account has that identifier.")
_UNPROCESSABLE: Responses = error_response(
    422,
    (
        "A path or query parameter was malformed — an identifier that is not a UUID, "
        "or a `limit` outside 1-100. `message` names which."
    ),
)


@users_router.get(
    "/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="Resolve a player id to a public identity",
    response_description="The player's handle and avatar. Nothing else.",
    responses={**_NOT_FOUND, **_UNPROCESSABLE},
)
async def get_user(
    user_id: UUID,
    service: UserServiceDep,
    avatar_links: AvatarLinkBuilderDep,
) -> ApiResponse[PublicUserResponse]:
    """Turns a `player_id` into a name and a picture.

    Exists because every cross-context reference on this platform is an
    opaque `player_id` (DM-06), so a match card or a leaderboard row holds
    an id and needs a handle to render — and `GET /profiles/{username}`
    takes the wrong key.

    **Carries no email, no account state and no storage key.** Until
    A64-012.6 it returned all three; see this module's docstring.

    For a player's full public profile — bio, country, ratings, statistics,
    and the privacy settings that govern them — use
    `GET /profiles/{username}`.

    `404` if no such user, raised as `UserNotFound` by the service and
    mapped by the platform handler, not here.
    """
    user = await service.get_user(user_id)
    summary = to_user_summary(user)
    return build_response(PublicUserResponse.of(summary, avatar_links.links_for(summary.avatar)))


@users_router.get(
    "",
    status_code=status.HTTP_200_OK,
    summary="List players",
    response_description="A page of public player identities, newest cursor last.",
    responses={**_UNPROCESSABLE},
)
async def list_users(
    service: UserServiceDep,
    avatar_links: AvatarLinkBuilderDep,
    cursor: Annotated[str | None, Query(description="Opaque cursor from a previous page.")] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_PAGE_SIZE, description="Rows per page."),
    ] = DEFAULT_PAGE_SIZE,
) -> ApiResponse[UserList]:
    """Keyset-paginated listing (RP-03) — the cursor is opaque and must be
    passed back unchanged, never constructed by a client.

    Returns the same `PublicUserResponse` as the single-user route above,
    which is what makes "what does this endpoint expose" one answer rather
    than two. Before A64-012.6 it returned `UserSummary` directly — no
    email, but the raw avatar `object_key` per row, which is a storage
    detail no client should see and no deployment should have to keep
    stable.

    **Active accounts only.** A deactivated account does not appear here, in
    any page, under any parameter — see the A64-012.8 note in this module's
    docstring for why the `is_active` filter that used to select them was
    removed rather than defaulted.
    """
    users, page = await service.list_users(
        CursorPageParams(cursor=cursor, limit=limit),
        # **Pinned, not defaulted.** `UserService.list_users` still accepts
        # `None` (every account) and `False` (only withdrawn ones), because
        # a moderation console under AD-04 genuinely needs both. This is the
        # public roster, and it gets exactly one of the three.
        is_active=True,
    )
    summaries = [to_user_summary(user) for user in users]
    return build_response(
        UserList(
            items=[
                PublicUserResponse.of(summary, avatar_links.links_for(summary.avatar))
                for summary in summaries
            ],
            page=page,
        )
    )


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
