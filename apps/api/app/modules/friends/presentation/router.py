"""HTTP routes for friends — requests (A64-013.2) and the friend list
(A64-013.3).

Nine endpoints and **no business logic in any of them**. Each translates a
request into a service call and the result into a wire schema. The
transition rules and the ownership checks are `FriendRequest`'s, the
cross-row rules are `FriendRequestValidator`'s, uniqueness is the database's,
and composing the other party's profile is `ProfileDirectoryService`'s.

## Every endpoint is authenticated, and the actor is never a parameter

There is no `requester_id` in any path, query or body. The acting account
comes from the access token's `sub` on all six routes, which is the same
design `/profile` and `/profile/avatar` use and the same reason: an
ownership rule is strongest when the alternative *cannot be expressed*.

A caller can name a request id and a target player id. It cannot name who is
sending, accepting, declining or cancelling — so the ownership checks in the
aggregate are a second lock on a door that has no handle on the outside.

## Errors need no handling here

Every failure is a typed exception on the platform hierarchy, and
`app/api/exception_handlers.py` maps them by MRO walk. There is not one
`try`/`except` in this file:

    SelfFriendRequest              -> 422  validation_error
    InvalidFriendRequestCursor     -> 422  validation_error
    FriendRequestNotFound          -> 404  not_found
    DuplicateFriendRequest         -> 409  duplicate_friend_request
    OppositeFriendRequestPending   -> 409  opposite_friend_request_pending
    FriendRequestAlreadyResolved   -> 409  conflict
    NotRequestAddressee            -> 403  permission_denied
    NotRequestRequester            -> 403  permission_denied
    MissingToken                   -> 401  authentication_required
    TooManyRequests                -> 429  rate_limited

## Batch composition, never a loop

All three list handlers resolve the whole page's players in one call to
`ProfileDirectoryService.profiles_for`, which is a fixed number of round
trips regardless of page size. Both tasks require it by name, and the
service has no singular method to reach for — see `ProfileDirectoryService`
on why that absence is the design rather than an omission.

## The viewer is passed into composition, and on the friend list it matters

Every list handler passes `viewer_id=user.id`, so the composer resolves what
the caller is to each player rendered. On the friend list that resolution is
always `FRIEND` — which is what makes `VisibilityLevel.FRIENDS` visibly work
for the first time: a field a friend restricted to friends appears here and
is hidden from the same profile read by a stranger.

## Route ordering

`/friends/requests/...` is registered before `/friends/{player_id}`.
Starlette matches in registration order, and while the two happen to differ
in segment count today, `GET /friends/count` and a future
`GET /friends/{player_id}` would not — so the specific paths lead, and a
contract test asserts the resolution rather than trusting this note.
"""

import logging
from collections.abc import Callable, Sequence
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.openapi import Responses, error_response
from app.api.responses import build_response
from app.core.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.core.pagination import CursorPage, CursorPageInfo
from app.core.responses import ApiResponse
from app.modules.auth.presentation.dependencies import CurrentUser
from app.modules.avatars.presentation.dependencies import AvatarLinkBuilderDep
from app.modules.friends.domain.friend_request import FriendRequest
from app.modules.friends.presentation.dependencies import (
    FriendRequestServiceDep,
    FriendshipServiceDep,
)
from app.modules.friends.presentation.rate_limits import (
    enforce_friend_request_respond_limit,
    enforce_friend_request_send_limit,
)
from app.modules.friends.presentation.schemas import (
    FriendCountResponse,
    FriendRequestResponse,
    FriendResponse,
    FriendshipDetailsResponse,
    SendFriendRequestRequest,
)
from app.modules.profiles.presentation.dependencies import ProfileDirectoryDep
from app.modules.profiles.presentation.schemas import ProfileResponse
from app.modules.users.public import ViewerRelationship

logger = logging.getLogger(__name__)

friends_router = APIRouter(prefix="/friends", tags=["friends"])

_UNAUTHORIZED: Responses = error_response(
    401, "No access token was presented, or it was invalid or expired."
)
_FORBIDDEN: Responses = error_response(
    403,
    (
        "You are not the party entitled to this action — only the recipient may "
        "accept or decline, and only the sender may cancel. The message never names "
        "the other player."
    ),
)
_NOT_FOUND: Responses = error_response(404, "No friend request with that identifier.")
_CONFLICT: Responses = error_response(
    409,
    (
        "The request cannot be created or resolved in its current state. `code` says "
        "which: `duplicate_friend_request` (you already have one pending to that "
        "player), `opposite_friend_request_pending` (they have one pending to you — "
        "respond to it instead), or `conflict` (this request has already been "
        "resolved, possibly on another device)."
    ),
)
_UNPROCESSABLE: Responses = error_response(
    422, "The body or a query parameter failed validation. `message` names which."
)
_TOO_MANY_REQUESTS: Responses = error_response(
    429,
    (
        "Too many friend-request actions from this account. Counted **per user**, "
        "not per network address, so a shared connection is never somebody else's "
        "problem. `Retry-After` says how long to wait."
    ),
)


@friends_router.post(
    "/requests",
    status_code=status.HTTP_201_CREATED,
    summary="Send a friend request",
    response_description="The request as created, with the recipient's public profile.",
    responses={
        **_UNAUTHORIZED,
        **_CONFLICT,
        **_UNPROCESSABLE,
        **_TOO_MANY_REQUESTS,
    },
    dependencies=[Depends(enforce_friend_request_send_limit)],
)
async def send_friend_request(
    payload: SendFriendRequestRequest,
    user: CurrentUser,
    service: FriendRequestServiceDep,
    directory: ProfileDirectoryDep,
    avatar_links: AvatarLinkBuilderDep,
) -> ApiResponse[FriendRequestResponse]:
    """Sends a friend request from your account to `player_id`.

    `201`, because a request is a new resource with an identifier the
    response carries — and the identifier is what every other endpoint here
    takes.

    **The sender is your token, never a field.** There is no way to send a
    request as somebody else.

    ## What is refused, and why

    | Outcome | `code` | Rule |
    | --- | --- | --- |
    | `422` | `validation_error` | You addressed yourself |
    | `409` | `duplicate_friend_request` | You already have one pending to them (FR-1) |
    | `409` | `opposite_friend_request_pending` | They already have one pending to you |

    The second `409` is the one worth handling specially: the right response
    is to show the request you already have and offer to accept it. It is
    **not** accepted automatically — two people each sending a request is
    not the same event as one agreeing to the other's, and converting it
    would resolve a request nobody acted on.

    A request to a player who does not exist is not distinguished from one
    that does. Checking would mean a read whose *timing* answers "is there
    an account with this id", which on an endpoint taking an id is an
    existence oracle.
    """
    request = await service.send(requester_id=user.id, addressee_id=payload.player_id)

    return build_response(
        await _render(
            request,
            other=request.addressee_id,
            viewer_id=user.id,
            directory=directory,
            links=avatar_links,
        )
    )


@friends_router.get(
    "/requests/incoming",
    status_code=status.HTTP_200_OK,
    summary="List friend requests you have received",
    response_description="A page of pending incoming requests, newest first.",
    responses={**_UNAUTHORIZED, **_UNPROCESSABLE},
)
async def list_incoming(
    user: CurrentUser,
    service: FriendRequestServiceDep,
    directory: ProfileDirectoryDep,
    avatar_links: AvatarLinkBuilderDep,
    limit: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, description="Requests per page.")
    ] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[
        str | None,
        Query(description="Opaque cursor from a previous page. Pass it back unchanged."),
    ] = None,
) -> ApiResponse[CursorPage[FriendRequestResponse]]:
    """Returns the pending requests **sent to you**, newest first.

    `player` on each row is the *sender* — the person deciding whether to
    accept is the one reading this, so the profile shown is the other party's.

    Only `pending` requests appear. A declined or cancelled request leaves
    this list silently and is not deleted: the row is history, and FR-5's
    future decline cooldown reads it.

    **Keyset pagination, never offset.** Follow `page.next_cursor` until it
    is `null`. There is deliberately no total — counting costs as much as
    the page and is not what somebody triaging requests needs.
    """
    requests, next_cursor = await service.incoming(addressee_id=user.id, limit=limit, cursor=cursor)
    return build_response(
        await _render_page(
            requests,
            other=lambda request: request.requester_id,
            next_cursor=next_cursor,
            viewer_id=user.id,
            directory=directory,
            links=avatar_links,
        )
    )


@friends_router.get(
    "/requests/outgoing",
    status_code=status.HTTP_200_OK,
    summary="List friend requests you have sent",
    response_description="A page of pending outgoing requests, newest first.",
    responses={**_UNAUTHORIZED, **_UNPROCESSABLE},
)
async def list_outgoing(
    user: CurrentUser,
    service: FriendRequestServiceDep,
    directory: ProfileDirectoryDep,
    avatar_links: AvatarLinkBuilderDep,
    limit: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, description="Requests per page.")
    ] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[
        str | None,
        Query(description="Opaque cursor from a previous page. Pass it back unchanged."),
    ] = None,
) -> ApiResponse[CursorPage[FriendRequestResponse]]:
    """Returns the pending requests **you have sent**, newest first.

    `player` on each row is the *recipient*.

    A request that the other player declines simply disappears from here,
    with no notification and no explanation — FR-3, and it is deliberate: a
    notified decline turns a refusal into a confrontation.
    """
    requests, next_cursor = await service.outgoing(requester_id=user.id, limit=limit, cursor=cursor)
    return build_response(
        await _render_page(
            requests,
            other=lambda request: request.addressee_id,
            next_cursor=next_cursor,
            viewer_id=user.id,
            directory=directory,
            links=avatar_links,
        )
    )


@friends_router.post(
    "/requests/{request_id}/accept",
    status_code=status.HTTP_200_OK,
    summary="Accept a friend request",
    response_description="The request in its resolved state.",
    responses={
        **_UNAUTHORIZED,
        **_FORBIDDEN,
        **_NOT_FOUND,
        **_CONFLICT,
        **_TOO_MANY_REQUESTS,
    },
    dependencies=[Depends(enforce_friend_request_respond_limit)],
)
async def accept_friend_request(
    request_id: Annotated[UUID, Path(description="The request identifier.")],
    user: CurrentUser,
    service: FriendRequestServiceDep,
    directory: ProfileDirectoryDep,
    avatar_links: AvatarLinkBuilderDep,
) -> ApiResponse[FriendRequestResponse]:
    """Accepts a request addressed to you.

    **Only the recipient.** The sender gets `403` — they may cancel, which
    is a different act leaving different history.

    `409` when the request is no longer pending, which includes the case
    worth knowing about: you accepted on one device and declined on
    another. The second one loses, and this is what it is told.

    **No friendship exists yet.** A64-013.2 records the resolution and
    stops; A64-013.3 creates the `Friendship` in this same transaction
    (FR-4). Accepting today is durable and correct — it simply has no friend
    list to appear in.
    """
    request = await service.accept(request_id=request_id, actor_id=user.id)

    return build_response(
        await _render(
            request,
            other=request.requester_id,
            viewer_id=user.id,
            directory=directory,
            links=avatar_links,
        )
    )


@friends_router.post(
    "/requests/{request_id}/decline",
    status_code=status.HTTP_200_OK,
    summary="Decline a friend request",
    response_description="The request in its resolved state.",
    responses={
        **_UNAUTHORIZED,
        **_FORBIDDEN,
        **_NOT_FOUND,
        **_CONFLICT,
        **_TOO_MANY_REQUESTS,
    },
    dependencies=[Depends(enforce_friend_request_respond_limit)],
)
async def decline_friend_request(
    request_id: Annotated[UUID, Path(description="The request identifier.")],
    user: CurrentUser,
    service: FriendRequestServiceDep,
    directory: ProfileDirectoryDep,
    avatar_links: AvatarLinkBuilderDep,
) -> ApiResponse[FriendRequestResponse]:
    """Declines a request addressed to you.

    **Silent to the sender** (FR-3): nothing notifies them, and the request
    simply leaves their outgoing list. A notified decline turns a refusal
    into a confrontation.

    The row is kept rather than deleted — it is a fact with a date, and
    FR-5's future decline cooldown reads it.

    **Only the recipient.** `403` for anybody else.
    """
    request = await service.decline(request_id=request_id, actor_id=user.id)

    return build_response(
        await _render(
            request,
            other=request.requester_id,
            viewer_id=user.id,
            directory=directory,
            links=avatar_links,
        )
    )


@friends_router.delete(
    "/requests/{request_id}",
    status_code=status.HTTP_200_OK,
    summary="Cancel a friend request you sent",
    response_description="The request in its resolved state.",
    responses={
        **_UNAUTHORIZED,
        **_FORBIDDEN,
        **_NOT_FOUND,
        **_CONFLICT,
        **_TOO_MANY_REQUESTS,
    },
    dependencies=[Depends(enforce_friend_request_respond_limit)],
)
async def cancel_friend_request(
    request_id: Annotated[UUID, Path(description="The request identifier.")],
    user: CurrentUser,
    service: FriendRequestServiceDep,
    directory: ProfileDirectoryDep,
    avatar_links: AvatarLinkBuilderDep,
) -> ApiResponse[FriendRequestResponse]:
    """Withdraws a request you sent.

    **Only the sender.** The recipient gets `403` — they have `decline`,
    which reaches the same practical outcome and leaves different history.

    `200` with a body rather than `204`, and `DELETE` rather than a `POST
    /cancel`, which needs stating because the two pull in opposite
    directions. The verb describes what the caller is doing to *their
    request*; the body exists because the row is not removed. Nothing here
    is ever deleted — database.md §1221: "a row that ended is a fact with a
    date; the row is history, not debris" — so the response reports the
    resolved request rather than an empty success a client would have to
    interpret.
    """
    request = await service.cancel(request_id=request_id, actor_id=user.id)

    return build_response(
        await _render(
            request,
            other=request.addressee_id,
            viewer_id=user.id,
            directory=directory,
            links=avatar_links,
        )
    )


async def _render(
    request: FriendRequest,
    *,
    other: UUID,
    viewer_id: UUID,
    directory: ProfileDirectoryDep,
    links: AvatarLinkBuilderDep,
) -> FriendRequestResponse:
    """One request with the other party's composed profile.

    Goes through the *batch* directory with a one-element sequence rather
    than a singular lookup, because there is no singular lookup — see
    `ProfileDirectoryService`, which omits one deliberately so the N+1 is
    unreachable from any caller.

    Raises `KeyError` if the other party has been deactivated between the
    write and this read, which is a genuine 500 and the honest outcome: a
    request whose counterpart no longer exists has no rendering, and
    inventing a placeholder would publish that an account was withdrawn.
    The list path takes the opposite route — see `_render_page`.
    """
    profiles = await directory.profiles_for([other], viewer_id=viewer_id)
    profile = profiles[other]
    return FriendRequestResponse.of(
        request, ProfileResponse.of(profile, links.links_for(profile.identity.avatar))
    )


async def _render_page(
    requests: Sequence[FriendRequest],
    *,
    other: Callable[[FriendRequest], UUID],
    next_cursor: str | None,
    viewer_id: UUID,
    directory: ProfileDirectoryDep,
    links: AvatarLinkBuilderDep,
) -> CursorPage[FriendRequestResponse]:
    """A whole page, composed in **one** batch.

    `other` names which party to render, which is the only difference
    between the two list endpoints — the incoming list shows senders and the
    outgoing list shows recipients. A parameter rather than two copies of
    this function, because everything else about them is identical.

    **Rows whose counterpart is missing are dropped**, not rendered with a
    placeholder. A missing counterpart means a deactivated account, and the
    platform's rule since A64-012.1 is that a withdrawn account is invisible
    rather than marked — a row saying "deleted user" would publish exactly
    what that rule withholds.

    The consequence is stated rather than hidden: a page can come back
    shorter than `limit` while `has_more` is true. That is already true of
    any keyset page a filter touches, and a client that treats a short page
    as the end is broken regardless — which is why `has_more` exists and is
    the thing to follow.
    """
    player_ids = [other(request) for request in requests]
    profiles = await directory.profiles_for(player_ids, viewer_id=viewer_id)

    items = [
        FriendRequestResponse.of(
            request,
            ProfileResponse.of(
                profiles[player_id], links.links_for(profiles[player_id].identity.avatar)
            ),
        )
        for request, player_id in zip(requests, player_ids, strict=True)
        if player_id in profiles
    ]

    return CursorPage(
        items=items,
        page=CursorPageInfo(next_cursor=next_cursor, has_more=next_cursor is not None),
    )


# --- the friend list (A64-013.3) --------------------------------------------
#
# A second resource on the same router rather than a router of its own.
# `friends_router` is one bounded context's HTTP surface, and a
# `friendships_router` beside it would split one module's routes across two
# files that share every dependency and every error mapping.

_FRIENDSHIP_NOT_FOUND: Responses = error_response(
    404,
    (
        "You are not friends with that player. Returned identically whether you never "
        "were or the friendship has already ended — which of the two applies is not "
        "something an endpoint should answer."
    ),
)


@friends_router.get(
    "/count",
    status_code=status.HTTP_200_OK,
    summary="Count your friends",
    response_description="How many friends you currently have.",
    responses={**_UNAUTHORIZED},
)
async def count_friends(
    user: CurrentUser,
    service: FriendshipServiceDep,
) -> ApiResponse[FriendCountResponse]:
    """Returns your current number of friends.

    **Your own count, always.** There is no path segment or parameter that
    could name another player, so this needs no ownership check — another
    account's friend count is not addressable here.

    Counts **live** friendships only: one that ended is not included, in
    either direction.

    A real count rather than the length of a page, so it stays correct past
    the first one. It is deliberately **not cached** — `friends:v1:` is
    reserved for exactly this, and a count with no invalidation trigger goes
    wrong on the first removal.
    """
    return build_response(FriendCountResponse(total=await service.count_friends(player_id=user.id)))


@friends_router.get(
    "",
    status_code=status.HTTP_200_OK,
    summary="List your friends",
    response_description="A page of your friends, most recently added first.",
    responses={**_UNAUTHORIZED, **_UNPROCESSABLE},
)
async def list_friends(
    user: CurrentUser,
    service: FriendshipServiceDep,
    directory: ProfileDirectoryDep,
    avatar_links: AvatarLinkBuilderDep,
    limit: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, description="Friends per page.")
    ] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[
        str | None,
        Query(description="Opaque cursor from a previous page. Pass it back unchanged."),
    ] = None,
) -> ApiResponse[CursorPage[FriendResponse]]:
    """Returns your friends, most recently added first.

    **Your own list, always.** The account comes from your access token and
    no parameter could name a different one, so there is no ownership check
    here because another player's friend list is not addressable.

    Each `player` is the full public profile — and because you are a friend,
    **fields they restricted to friends are visible to you**. The same
    profile read by a stranger hides them. That is `VisibilityLevel.FRIENDS`
    working end to end, and it needs nothing from a client.

    **Keyset pagination, never offset.** Follow `page.next_cursor` until it
    is `null`; `page.has_more` says whether there is one. There is
    deliberately no total in the page — `GET /friends/count` is the endpoint
    for that, and computing it on every page would pay for a number most
    callers do not read.
    """
    friendships, next_cursor = await service.list_friends(
        player_id=user.id, limit=limit, cursor=cursor
    )

    player_ids = [friendship.other_than(user.id) for friendship in friendships]
    # One batch for the whole page — never `compose` in a loop, and a fixed
    # number of round trips regardless of page size.
    #
    # `known_relationship` is A64-013.4's saving: every player on this page
    # is a friend *by construction*, so resolving that from the social graph
    # would be asking `friend_ids_among` — now on every composition path —
    # to confirm what building the page already established.
    profiles = await directory.profiles_for(
        player_ids,
        viewer_id=user.id,
        known_relationship=ViewerRelationship.FRIEND,
    )

    items = [
        FriendResponse.of(
            friendship,
            ProfileResponse.of(
                profiles[player_id], avatar_links.links_for(profiles[player_id].identity.avatar)
            ),
        )
        for friendship, player_id in zip(friendships, player_ids, strict=True)
        if player_id in profiles
    ]

    return build_response(
        CursorPage(
            items=items,
            page=CursorPageInfo(next_cursor=next_cursor, has_more=next_cursor is not None),
        )
    )


@friends_router.get(
    "/{player_id}",
    status_code=status.HTTP_200_OK,
    summary="Inspect one friendship",
    response_description="The friendship, with the other player's profile.",
    responses={**_UNAUTHORIZED, **_FRIENDSHIP_NOT_FOUND},
)
async def get_friendship(
    player_id: Annotated[UUID, Path(description="The friend to inspect.")],
    user: CurrentUser,
    service: FriendshipServiceDep,
    directory: ProfileDirectoryDep,
    avatar_links: AvatarLinkBuilderDep,
) -> ApiResponse[FriendshipDetailsResponse]:
    """Returns your friendship with `player_id`.

    **Only your own.** The other party comes from the path and *you* come
    from the access token, so the only relationship inspectable here is one
    you are part of — there is no arrangement of parameters that could ask
    about two other people. That is why there is no authorization check in
    this handler: the alternative it would guard against is not expressible.

    `404` when you are not currently friends, covering "never were" and "it
    ended" indistinguishably. Whether two people were *ever* friends is not
    a question an inspection endpoint should answer.

    `player` is the full public profile, composed exactly as
    `GET /profiles/{username}` composes it — and because you are a friend,
    **fields restricted to friends are visible to you** here and hidden from
    the same profile read by a stranger.

    Registered **after** `/friends/count`, which is the same two-segment
    shape; Starlette matches in registration order, so the literal path
    leads. A contract test asserts the resolution.
    """
    metadata = await service.friendship_details(player_id=user.id, other_id=player_id)

    # One batch of one — the directory has no singular method, deliberately,
    # so the N+1 is unreachable from any caller. The relationship is stated
    # rather than resolved: this endpoint only returns at all when the two
    # are friends.
    profiles = await directory.profiles_for(
        [player_id],
        viewer_id=user.id,
        known_relationship=ViewerRelationship.FRIEND,
    )
    profile = profiles[player_id]

    return build_response(
        FriendshipDetailsResponse.of(
            metadata, ProfileResponse.of(profile, avatar_links.links_for(profile.identity.avatar))
        )
    )


@friends_router.delete(
    "/{player_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a friend",
    response_description="The friendship has ended.",
    responses={**_UNAUTHORIZED, **_TOO_MANY_REQUESTS},
    dependencies=[Depends(enforce_friend_request_respond_limit)],
)
async def remove_friend(
    player_id: Annotated[UUID, Path(description="The friend to remove.")],
    user: CurrentUser,
    service: FriendshipServiceDep,
) -> None:
    """Ends your friendship with `player_id`.

    **Unilateral and silent** (FS-2). You do not need their agreement, and
    they are not told — "requiring mutual agreement to stop being friends is
    not a feature anyone wants", and a notified removal turns a withdrawal
    into a confrontation.

    **Only your own friendships.** The other party comes from the path and
    *you* come from the token, so the only friendship addressable here is
    one you are part of. A player who is not in the pair gets `404`, not
    `403` — there is no friendship between them and the caller to speak of.

    **Idempotent** (A64-013.4). Removing somebody you are not friends with
    returns `204` and changes nothing — it does not `404`. A client
    retrying after a dropped response must not be told the resource is gone
    when its own first attempt is what removed it.

    It also stops the endpoint answering a question it should not. A `404`
    for "you are not friends" beside a `204` for "you were" is an oracle:
    anybody holding a player id could probe their own relationship state,
    and once blocking voids friendships could detect having been blocked by
    watching a removal turn into a `404`. One answer for both closes that.

    Contrast `GET /friends/{player_id}`, which *does* `404`: a read of a
    resource that does not exist has no other honest answer, and a `GET` has
    no idempotency contract to honour.

    `204` with no body, unlike `DELETE /friends/requests/{id}`, and the
    difference is real rather than inconsistent: cancelling a request
    returns the request in its resolved state because a client shows it in a
    list; a removed friendship simply leaves the list, and there is nothing
    left to render. The row is still kept — database.md §1221: a friendship
    that ended is a fact with a date, and it is what lets the two be friends
    again later.
    """
    await service.remove_friend(player_id=user.id, other_id=player_id)
