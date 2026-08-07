"""Friend challenges over HTTP — A64-022.2 §2, §3.

Six routes: send one, read one, list what you have received, list what you
have sent, decline, cancel.

## No `/accept`, and that is a boundary rather than an omission

`domain-model.md` §10.3 requires acceptance to create the match in the same
transaction that consumes the challenge. A64-022.3 owns both halves, and an
endpoint here that moved the status without creating a game would be a
challenge claiming a match nobody can play. `ChallengeService` has no
`accept` either — see the aggregate.

## Every read is scoped to a party

There is no route that takes a player id, and no route that answers about a
challenge the caller is not part of. A challenge between two other people is
**not found**, not forbidden: an identifier that answered differently would
be an existence oracle, and the id is a UUID somebody could otherwise probe
for.

`ChallengeForbidden` is reserved for the two people who genuinely are
parties and used the wrong verb — a challenger trying to decline, a recipient
trying to cancel.

## Why the other party's profile is composed here

The list endpoints show who challenged you, which is a *profile* question:
whether a country is visible, whether presence may be shown, whether an
avatar exists. `profiles`' batch directory answers all of it under the same
privacy rules `GET /profiles/{username}` follows, and this module
deliberately re-derives none of them.

One batch call per page, never one per row — see `_render_page`.
"""

from collections.abc import Callable, Sequence
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.openapi import error_response
from app.api.responses import build_response
from app.core.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.core.pagination import CursorPage, CursorPageInfo
from app.core.responses import ApiResponse
from app.modules.auth.presentation.dependencies import CurrentUser, VerifiedUser
from app.modules.avatars.presentation.dependencies import AvatarLinkBuilderDep
from app.modules.matchmaking.domain.challenge import Challenge
from app.modules.matchmaking.presentation.challenge_rate_limits import (
    enforce_challenge_create_limit,
    enforce_challenge_respond_limit,
)
from app.modules.matchmaking.presentation.dependencies import ChallengeServiceDep
from app.modules.matchmaking.presentation.schemas.challenge import (
    ChallengeResponse,
    CreateChallengeRequest,
)
from app.modules.profiles.presentation.dependencies import ProfileDirectoryDep
from app.modules.profiles.presentation.schemas import ProfileResponse

challenges_router = APIRouter(prefix="/challenges", tags=["challenges"])

_UNAUTHORIZED = error_response(401, "No or invalid access token")
_FORBIDDEN = error_response(403, "You are a party to this challenge but not the right one")
_NOT_FOUND = error_response(404, "No such challenge of yours")
_CONFLICT = error_response(409, "A live challenge already exists, or this one was answered")
_UNPROCESSABLE = error_response(422, "The request or the challenge's state refuses this")


@challenges_router.post(
    "",
    dependencies=[Depends(enforce_challenge_create_limit)],
    response_model=ApiResponse[ChallengeResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Challenge a friend to a game",
    responses={**_UNAUTHORIZED, **_CONFLICT, **_UNPROCESSABLE},
)
async def create_challenge(
    user: VerifiedUser,
    challenges: ChallengeServiceDep,
    directory: ProfileDirectoryDep,
    avatar_links: AvatarLinkBuilderDep,
    body: CreateChallengeRequest,
) -> ApiResponse[ChallengeResponse]:
    """Invites one friend to play, at settings you choose.

    **You are the challenger**, from the session. There is no field for it
    and the schema forbids unknown ones, so a client cannot send an
    invitation on somebody else's behalf even by trying.

    `VerifiedUser`: challenging somebody reaches another player, and every
    outward-facing write has required a confirmed address since A64-021.5H.

    Refused when you are not friends, when either of you has blocked the
    other — the **same** answer for both, so a block is not disclosed — when
    the clock is not currently offered, or when a live challenge already
    exists between you, in either direction.

    Expires twenty-four hours from now. The clock is the server's.
    """
    challenge = await challenges.create(
        user.id,
        recipient_id=body.recipient_id,
        time_control_id=body.time_control_id,
        variant=body.variant,
        rated=body.rated,
    )
    return build_response(
        await _render(
            challenge,
            other=challenge.recipient_id,
            viewer_id=user.id,
            directory=directory,
            links=avatar_links,
        )
    )


@challenges_router.get(
    "/incoming",
    response_model=ApiResponse[CursorPage[ChallengeResponse]],
    status_code=status.HTTP_200_OK,
    summary="List challenges you have received",
    responses={**_UNAUTHORIZED, **_UNPROCESSABLE},
)
async def list_incoming(
    user: CurrentUser,
    challenges: ChallengeServiceDep,
    directory: ProfileDirectoryDep,
    avatar_links: AvatarLinkBuilderDep,
    limit: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, description="Challenges per page.")
    ] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[
        str | None,
        Query(description="Opaque cursor from a previous page. Pass it back unchanged."),
    ] = None,
) -> ApiResponse[CursorPage[ChallengeResponse]]:
    """Challenges **sent to you** that are still answerable, newest first.

    `player` on each row is the *challenger* — the person deciding whether to
    play is the one reading this, so the profile shown is the other party's.

    **Live only.** A challenge that was answered, cancelled or has passed its
    twenty-four hours leaves this list silently and is not deleted: the row
    is the record that an invitation happened. There is deliberately no
    history endpoint — that is a product decision nobody has taken, and an
    unbounded one added quietly would be a list nobody designed.

    Expiry is applied in the **query**, not to a fetched page, so `limit`
    means what it says: a page of twenty is twenty live challenges or the end
    of the list, never twenty rows of which some are stale.

    **Keyset pagination, never offset.** Follow `page.next_cursor` until it
    is `null`. There is no total — counting costs as much as the page and is
    not what somebody triaging invitations needs.
    """
    page, next_cursor = await challenges.incoming(user.id, limit=limit, cursor=cursor)
    return build_response(
        await _render_page(
            page,
            other=lambda challenge: challenge.challenger_id,
            next_cursor=next_cursor,
            viewer_id=user.id,
            directory=directory,
            links=avatar_links,
        )
    )


@challenges_router.get(
    "/outgoing",
    response_model=ApiResponse[CursorPage[ChallengeResponse]],
    status_code=status.HTTP_200_OK,
    summary="List challenges you have sent",
    responses={**_UNAUTHORIZED, **_UNPROCESSABLE},
)
async def list_outgoing(
    user: CurrentUser,
    challenges: ChallengeServiceDep,
    directory: ProfileDirectoryDep,
    avatar_links: AvatarLinkBuilderDep,
    limit: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, description="Challenges per page.")
    ] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[
        str | None,
        Query(description="Opaque cursor from a previous page. Pass it back unchanged."),
    ] = None,
) -> ApiResponse[CursorPage[ChallengeResponse]]:
    """Challenges **you have sent** that are still live. See `incoming`.

    `player` is the recipient here — the other party again.
    """
    page, next_cursor = await challenges.outgoing(user.id, limit=limit, cursor=cursor)
    return build_response(
        await _render_page(
            page,
            other=lambda challenge: challenge.recipient_id,
            next_cursor=next_cursor,
            viewer_id=user.id,
            directory=directory,
            links=avatar_links,
        )
    )


@challenges_router.get(
    "/{challenge_id}",
    response_model=ApiResponse[ChallengeResponse],
    status_code=status.HTTP_200_OK,
    summary="Read one challenge",
    responses={**_UNAUTHORIZED, **_NOT_FOUND},
)
async def get_challenge(
    user: CurrentUser,
    challenges: ChallengeServiceDep,
    directory: ProfileDirectoryDep,
    avatar_links: AvatarLinkBuilderDep,
    challenge_id: Annotated[UUID, Path(description="Which challenge to read.")],
) -> ApiResponse[ChallengeResponse]:
    """One challenge you are part of, in either direction.

    Registered **after** `/incoming` and `/outgoing` deliberately: FastAPI
    matches routes in order, so a path parameter declared first would swallow
    both of them and a client asking for its incoming list would get a
    `422` about a malformed UUID.

    A challenge that does not exist and one between two other people produce
    the same `404` — see this module's docstring.

    Terminal challenges are readable here, unlike in the lists: a client
    holding an id it was given deserves to learn the invitation was declined
    rather than that it vanished.
    """
    challenge = await challenges.get(challenge_id, by=user.id)
    other = (
        challenge.recipient_id if challenge.challenger_id == user.id else challenge.challenger_id
    )
    return build_response(
        await _render(
            challenge, other=other, viewer_id=user.id, directory=directory, links=avatar_links
        )
    )


@challenges_router.post(
    "/{challenge_id}/decline",
    dependencies=[Depends(enforce_challenge_respond_limit)],
    response_model=ApiResponse[ChallengeResponse],
    status_code=status.HTTP_200_OK,
    summary="Decline a challenge you received",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND, **_CONFLICT, **_UNPROCESSABLE},
)
async def decline_challenge(
    user: VerifiedUser,
    challenges: ChallengeServiceDep,
    directory: ProfileDirectoryDep,
    avatar_links: AvatarLinkBuilderDep,
    challenge_id: Annotated[UUID, Path(description="Which challenge to decline.")],
) -> ApiResponse[ChallengeResponse]:
    """Says no. Only the recipient may.

    A challenger who tries gets `403`, not `404`: they are a party to it and
    know it exists, so hiding it would be a fiction rather than a protection.

    **Not idempotent.** A second decline is refused rather than reported as
    success, because the platform cannot tell a double-click from a client
    that missed the first answer — and quietly succeeding would let a
    duplicate emit a second event to everything downstream.

    Answering an expired challenge is refused too. Twenty-four hours is
    server-authoritative and a device's clock has no say in it.
    """
    challenge = await challenges.decline(challenge_id, by=user.id)
    return build_response(
        await _render(
            challenge,
            other=challenge.challenger_id,
            viewer_id=user.id,
            directory=directory,
            links=avatar_links,
        )
    )


@challenges_router.delete(
    "/{challenge_id}",
    dependencies=[Depends(enforce_challenge_respond_limit)],
    response_model=ApiResponse[ChallengeResponse],
    status_code=status.HTTP_200_OK,
    summary="Withdraw a challenge you sent",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND, **_CONFLICT},
)
async def cancel_challenge(
    user: VerifiedUser,
    challenges: ChallengeServiceDep,
    directory: ProfileDirectoryDep,
    avatar_links: AvatarLinkBuilderDep,
    challenge_id: Annotated[UUID, Path(description="Which challenge to withdraw.")],
) -> ApiResponse[ChallengeResponse]:
    """Withdraws it. Only the challenger may.

    `DELETE`, and it returns the challenge rather than `204`, because the row
    is not deleted — it becomes `cancelled` and stays as the record that an
    invitation happened. The verb describes what the *client* is doing to its
    own resource; the body says what actually became of it.

    **Permitted past expiry**, unlike declining. Cancelling an expired
    challenge is somebody tidying a list, and refusing it would leave a row
    they cannot clear until a sweep they cannot see runs.
    """
    challenge = await challenges.cancel(challenge_id, by=user.id)
    return build_response(
        await _render(
            challenge,
            other=challenge.recipient_id,
            viewer_id=user.id,
            directory=directory,
            links=avatar_links,
        )
    )


async def _render(
    challenge: Challenge,
    *,
    other: UUID,
    viewer_id: UUID,
    directory: ProfileDirectoryDep,
    links: AvatarLinkBuilderDep,
) -> ChallengeResponse:
    """One challenge with the other party's composed profile.

    Goes through the *batch* directory with a one-element sequence rather
    than a singular lookup, because there is no singular lookup —
    `ProfileDirectoryService` omits one deliberately so the N+1 is
    unreachable from any caller.
    """
    profiles = await directory.profiles_for([other], viewer_id=viewer_id)
    profile = profiles[other]
    return ChallengeResponse.of(
        challenge, ProfileResponse.of(profile, links.links_for(profile.identity.avatar))
    )


async def _render_page(
    challenges: Sequence[Challenge],
    *,
    other: Callable[[Challenge], UUID],
    next_cursor: str | None,
    viewer_id: UUID,
    directory: ProfileDirectoryDep,
    links: AvatarLinkBuilderDep,
) -> CursorPage[ChallengeResponse]:
    """A page of challenges, with **one** profile lookup for all of them.

    §6: a page of twenty costs one challenge query and one profile batch, not
    twenty-one queries. The ids are collected first and resolved together.

    A challenge whose counterpart has been deactivated between the write and
    this read is **omitted** rather than raising. The singular path takes the
    opposite route and 500s, and the difference is what each is for: a client
    that asked for one specific challenge deserves to know something is
    wrong, where a list that failed entirely because one row's counterpart
    withdrew would be a screen nobody can use.
    """
    others = [other(challenge) for challenge in challenges]
    profiles = await directory.profiles_for(others, viewer_id=viewer_id)

    items = [
        ChallengeResponse.of(
            challenge,
            ProfileResponse.of(
                profiles[counterpart], links.links_for(profiles[counterpart].identity.avatar)
            ),
        )
        for challenge, counterpart in zip(challenges, others, strict=True)
        if counterpart in profiles
    ]
    return CursorPage(
        items=items,
        page=CursorPageInfo(next_cursor=next_cursor, has_more=next_cursor is not None),
    )


__all__ = ["challenges_router"]
