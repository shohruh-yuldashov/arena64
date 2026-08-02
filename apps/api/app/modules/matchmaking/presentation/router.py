"""HTTP routes for the matchmaking queue and the acceptance handshake —
A64-014.1 and A64-015.4.

Six endpoints and **no business logic in any of them**. Each translates a
request into a service call and the result into a wire schema. QT-1 is the
index's, the ticket state machine is `QueueTicket`'s, the presence rule is
`QueueService`'s, the atomic claim is the repository's, and the acceptance
lifecycle is `game`'s — reached through `game.public` and nothing else.

## Why acceptance lives under `/matchmaking`

The match is `game`'s aggregate, and these three routes are on the queue's
prefix rather than a `/matches` one. That is a product judgement stated
here so it is not mistaken for a layering accident: a player who has been
paired and has not answered is, as far as anybody using the product is
concerned, **still being matched**. The handshake is the last step of
matchmaking, not the first step of a game — there is no game until both
sides say yes.

`game` gains routes of its own when there is something to play.

## Every endpoint is authenticated, and the actor is never a parameter

There is no `player_id` in any path, query or body. The queueing account
comes from the access token's `sub` on all three routes, which is the same
design `/friends` and `/profile` use and the same reason: an ownership rule
is strongest when the alternative *cannot be expressed*.

A caller can name a pool. It cannot name who is joining, leaving or being
read — so there is no ownership check in this file, because the thing one
would guard against is not addressable.

## Errors need no handling here

Every failure is a typed exception on the platform hierarchy, and
`app/api/exception_handlers.py` maps them by MRO walk. There is not one
`try`/`except` in this file:

    AlreadyQueued          -> 409  conflict
    QueueCooldownActive    -> 409  queue_cooldown_active, + Retry-After
    QueueNotPermitted      -> 422  validation_error
    NotQueued              -> 404  not_found
    MatchNotFound          -> 404  not_found
    MatchNotPending        -> 409  conflict
    AcceptanceWindowClosed -> 409  conflict
    MissingToken           -> 401  authentication_required
    TooManyRequests        -> 429  rate_limited

## Why joining returns the pool's depth

Both write endpoints and the read compose a `QueueTicketResponse`, which
carries `waiting`. That is a second query per call, and it is deliberate:
the first thing a client renders after joining is "searching…", and the only
honest thing to put beside it is how many other people are searching. The
alternative is a client polling `GET /matchmaking/queue/me` immediately
after `POST` to learn a number the `POST` already had in scope.

Two indexed reads against a partial index, on a request a human made. It is
not an N+1 — there is no per-item lookup anywhere in this file, and the
count does not grow with the response.
"""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, status

from app.api.openapi import Responses, error_response
from app.api.responses import build_response
from app.core.responses import ApiResponse
from app.modules.auth.presentation.dependencies import CurrentUser
from app.modules.game.public import MatchNotFound, PendingMatchView
from app.modules.matchmaking.domain.exceptions import NotQueued
from app.modules.matchmaking.domain.queue_pool import QueuePool
from app.modules.matchmaking.presentation.dependencies import (
    MatchAcceptanceDep,
    OpponentDirectoryDep,
    QueueServiceDep,
)
from app.modules.matchmaking.presentation.rate_limits import (
    enforce_acceptance_limit,
    enforce_queue_limit,
)
from app.modules.matchmaking.presentation.schemas import (
    JoinQueueRequest,
    PendingMatchResponse,
    QueueTicketResponse,
)
from app.modules.users.public import PublicProfileReader

logger = logging.getLogger(__name__)

matchmaking_router = APIRouter(prefix="/matchmaking", tags=["matchmaking"])

_UNAUTHORIZED: Responses = error_response(
    401, "No access token was presented, or it was invalid or expired."
)
_ALREADY_QUEUED: Responses = error_response(
    409,
    (
        "You already hold a live queue ticket, **or** you declined a match recently "
        "and are in a short cooldown. The `code` distinguishes them: `conflict` means "
        "leave the queue you are in; `queue_cooldown_active` means wait, and "
        "`Retry-After` says how long.\n\n"
        "One ticket per player, **across all pools** — leave the queue before joining "
        "a different one."
    ),
)
_NOT_QUEUED: Responses = error_response(
    404,
    (
        "You are not currently in a queue. Returned identically whether you never "
        "joined, left, were matched, or your ticket expired."
    ),
)
_UNPROCESSABLE: Responses = error_response(
    422,
    ("The body failed validation, or you are recorded as signed out. `message` says which."),
)
_TOO_MANY_REQUESTS: Responses = error_response(
    429,
    (
        "Too many queue actions from this account. Counted **per user**, not per "
        "network address, so a shared connection is never somebody else's problem. "
        "`Retry-After` says how long to wait."
    ),
)
_NO_PENDING_MATCH: Responses = error_response(
    404,
    (
        "No match is waiting for your answer. Returned identically whether you "
        "were never paired, already answered, or the offer expired."
    ),
)
_UNKNOWN_MATCH: Responses = error_response(
    404,
    (
        "No such match, **or** it is not yours. The two are deliberately "
        "indistinguishable — a different status for somebody else's match would "
        "make live match identifiers enumerable."
    ),
)
_MATCH_ANSWERED: Responses = error_response(
    409,
    (
        "That match is no longer awaiting your answer — your opponent declined, "
        "the window closed, or you both already accepted. Read "
        "`GET /matchmaking/matches/pending` to see where things stand."
    ),
)


@matchmaking_router.post(
    "/queue",
    status_code=status.HTTP_201_CREATED,
    summary="Join a matchmaking queue",
    response_description="The ticket as created, with the pool's current depth.",
    responses={
        **_UNAUTHORIZED,
        **_ALREADY_QUEUED,
        **_UNPROCESSABLE,
        **_TOO_MANY_REQUESTS,
    },
    dependencies=[Depends(enforce_queue_limit)],
)
async def join_queue(
    payload: JoinQueueRequest,
    user: CurrentUser,
    service: QueueServiceDep,
) -> ApiResponse[QueueTicketResponse]:
    """Enters your account into a matchmaking pool.

    `201`, because a ticket is a new resource with an identifier the
    response carries.

    **Your ticket may become a match.** Since A64-015.4 a background scan
    pairs waiting players and creates a match for them, so a ticket ends in
    one of four ways: you leave, it expires, or it is consumed by a pairing
    — at which point `GET /matchmaking/queue/me` answers `404` and
    `GET /matchmaking/matches/pending` has your offer.

    Since A64-015.5 that offer is **pushed** to a connected client rather
    than waited for; the polling endpoint is the reconnect fallback.

    ## Two `409`s, and they mean different things

    `conflict` — you already hold a live ticket. Leave it, or answer the
    match you have.

    `queue_cooldown_active` — you declined a match recently. Nothing to
    undo; `Retry-After` says how many seconds until you may queue again.
    Declining is the only thing that earns this: letting a match's window
    close in silence does not.

    ## One ticket, across every pool

    `409` if you already hold one, **including in a different pool**.
    Multi-queueing means being paired into two simultaneous matches, one of
    which must then be abandoned — which looks, to the opponent whose game
    vanished, exactly like a stolen win.

    ## What you are refused for, and what you are not

    | Outcome | Rule |
    | --- | --- |
    | `409` | You already hold a live ticket (QT-1) |
    | `422` | The platform has recorded you as signed out |

    The `422` fires only on a *recorded* sign-out. Presence that is simply
    unknown — you have not been observed recently, or the presence store is
    unavailable — is permitted, because those cases are indistinguishable
    by design and refusing on them would make a cache blip an outage of
    matchmaking.

    ## The rating is not yours to send

    `rating_snapshot` in the response is the rating your ticket was entered
    with, recorded by the platform and fixed for the ticket's life. A rating
    that changes while you wait does not move your place; re-queueing is
    what picks up a new one. There is deliberately no way to supply it.

    Every rating is provisional today — no game has been played on this
    platform and no rating system exists yet — so every ticket records the
    same starting value.
    """
    pool = QueuePool(variant=payload.variant, queue_type=payload.queue_type, region=payload.region)
    ticket = await service.join(player_id=user.id, pool=pool)
    snapshot = await service.snapshot(pool=ticket.pool)
    return build_response(QueueTicketResponse.of(ticket, snapshot))


@matchmaking_router.delete(
    "/queue",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Leave the matchmaking queue",
    response_description="You are no longer queued.",
    responses={**_UNAUTHORIZED, **_TOO_MANY_REQUESTS},
    dependencies=[Depends(enforce_queue_limit)],
)
async def leave_queue(user: CurrentUser, service: QueueServiceDep) -> None:
    """Withdraws your queue ticket.

    **Your own, always.** There is no path segment or body field naming a
    ticket, so another player's is not addressable — which is why this
    handler has no ownership check.

    **Idempotent.** Leaving a queue you are not in returns `204` and changes
    nothing; it does not `404`. A client retrying after a dropped response
    must not be told the resource is gone when its own first attempt is what
    removed it, and one answer for both cases keeps this from reporting your
    queue state back through a status code.

    Contrast `GET /matchmaking/queue/me`, which *does* `404`: a read of a
    resource that does not exist has no other honest answer, and a `GET` has
    no idempotency contract to honour.

    `204` with no body. Unlike a cancelled friend request — which is
    returned in its resolved state because a client shows it in a list — a
    withdrawn ticket simply stops existing as far as any client surface is
    concerned. The row is kept and marked `cancelled`, because how long
    somebody was prepared to wait is a fact worth having.
    """
    await service.leave(player_id=user.id)


@matchmaking_router.get(
    "/queue/me",
    status_code=status.HTTP_200_OK,
    summary="Read your queue ticket",
    response_description="Your live ticket, with the pool's current depth.",
    responses={**_UNAUTHORIZED, **_NOT_QUEUED},
)
async def read_my_ticket(
    user: CurrentUser, service: QueueServiceDep
) -> ApiResponse[QueueTicketResponse]:
    """Returns your live queue ticket.

    **Your own, always** — the account comes from your access token and no
    parameter could name a different one. There is deliberately no endpoint
    that reads somebody else's ticket: who is queueing right now is exactly
    the information that would let a player wait for a favourable pool.

    `404` when you have no live ticket, covering "never joined", "left",
    "expired" and "matched" indistinguishably. Which of the four applies is
    not something this endpoint should answer, and a client's next move is
    the same for all of them.

    **A ticket past its deadline reads as absent** even in the moment before
    a worker records it as expired. `expires_at` is the rule; the sweep is
    bookkeeping, and a player must never be told they are queued because a
    background job is a few seconds behind — nor be blocked from re-queueing
    by one.

    **Not rate limited**, unlike the two writes. This is the endpoint a
    client polls while waiting, and throttling it would make a working queue
    look broken in exactly the situation it is working.

    `waiting` counts everyone in your pool, including you. It is a reading
    rather than a position — the queue is ordered by entry time for pairing,
    but nobody is "third in line", because the pool is scanned by rating
    rather than drained in order.
    """
    ticket = await service.active_ticket(player_id=user.id)
    if ticket is None:
        raise NotQueued("You are not currently in a matchmaking queue.")

    snapshot = await service.snapshot(pool=ticket.pool)
    return build_response(QueueTicketResponse.of(ticket, snapshot))


#: The path parameter both answers take. One definition, so the two routes
#: cannot describe the same identifier differently in the generated document.
MatchIdPath = Annotated[UUID, Path(description="The match you were offered.")]


@matchmaking_router.get(
    "/matches/pending",
    status_code=status.HTTP_200_OK,
    summary="Read the match awaiting your answer",
    response_description="Your pending match, with a preview of your opponent.",
    responses={**_UNAUTHORIZED, **_NO_PENDING_MATCH},
)
async def read_pending_match(
    user: CurrentUser,
    acceptance: MatchAcceptanceDep,
    players: OpponentDirectoryDep,
) -> ApiResponse[PendingMatchResponse]:
    """Returns the match you have been paired into and not yet answered.

    **Yours, always** — the account comes from your access token and no
    parameter could name a different one. There is deliberately no endpoint
    that reads somebody else's pending match: who is being paired with whom
    right now is exactly what would let a player wait for a favourable draw.

    **At most one, by construction.** You hold one live queue ticket, a
    ticket produces at most one match, and a pending match holds you until
    it settles — so this is singular without needing a rule of its own.

    `404` when there is none, covering "never paired", "already answered"
    and "the offer expired" indistinguishably. Which applies is not
    something this endpoint should answer, and your next move is the same
    for all three: rejoin the queue.

    ## Answer inside `acceptance_deadline`

    It is an instant rather than a countdown, so a slow response cannot make
    your timer wrong. An answer that arrives after it is refused, and the
    match is expired for both of you shortly afterwards by a background job
    — the deadline is the rule, and the job is only the bookkeeping.

    ## This is the fallback, not the primary delivery

    Since A64-015.5 a pending match is **pushed**: `game.match_created`
    reaches a realtime consumer through the transactional outbox, which
    re-reads the match, re-checks the block graph and hands it to the
    gateway. A connected client learns about its match without asking.

    This endpoint remains, and is deliberately not deprecated. It is what a
    client uses to **recover**:

    | Situation | Why polling is the answer |
    | --- | --- |
    | Reconnect | The push happened while the socket was down |
    | Cold start | A client that has just opened does not know what it missed |
    | Push disabled | `MATCHMAKING_REALTIME_DELIVERY_ENABLED=false` is a real deployment |
    | Doubt | The authoritative state is the database, and this reads it |

    A client that only polls still works correctly; it simply learns later.
    A client that only listens is correct until the first dropped
    connection, which is why this must not become optional.

    **Not rate limited**, unlike the two answers. This is the endpoint a
    client polls while deciding *and* the one it calls on every reconnect,
    and throttling it would make a working handshake look broken in exactly
    the situation it is working.
    """
    view = await acceptance.pending_match(user.id)
    if view is None:
        raise MatchNotFound("No match is waiting for your answer.")

    return build_response(await _render(view, players))


@matchmaking_router.post(
    "/matches/{match_id}/accept",
    status_code=status.HTTP_200_OK,
    summary="Accept a match you have been offered",
    response_description="The match, including whether your opponent has answered.",
    responses={
        **_UNAUTHORIZED,
        **_UNKNOWN_MATCH,
        **_MATCH_ANSWERED,
        **_TOO_MANY_REQUESTS,
    },
    dependencies=[Depends(enforce_acceptance_limit)],
)
async def accept_match(
    user: CurrentUser,
    match_id: MatchIdPath,
    acceptance: MatchAcceptanceDep,
    players: OpponentDirectoryDep,
) -> ApiResponse[PendingMatchResponse]:
    """Says yes to a match you were paired into.

    **For yourself only.** There is no side or player field anywhere in the
    request — which seat you hold is derived from your access token — so
    accepting on your opponent's behalf is not something this API can
    express.

    The match becomes `active` when *both* of you have accepted, and not
    before. Until then the response reports `status: pending_acceptance`
    with `you_accepted: true`, which is the honest state: you have agreed
    and are waiting.

    **Idempotent.** Accepting twice returns the same match rather than a
    `409` — a client retrying after a dropped response asked for something
    that is already true, and telling it otherwise would make a network
    blip look like a lost game.

    `404` for a match that does not exist **and** for one that is not
    yours. `409` once the handshake is over — your opponent declined, or
    the window closed — and `409` for an answer that arrives after
    `acceptance_deadline`, whether or not a background job has recorded the
    expiry yet.
    """
    view = await acceptance.accept(player_id=user.id, match_id=match_id)
    return build_response(await _render(view, players))


@matchmaking_router.post(
    "/matches/{match_id}/decline",
    status_code=status.HTTP_200_OK,
    summary="Decline a match you have been offered",
    response_description="The match, now cancelled.",
    responses={
        **_UNAUTHORIZED,
        **_UNKNOWN_MATCH,
        **_MATCH_ANSWERED,
        **_TOO_MANY_REQUESTS,
    },
    dependencies=[Depends(enforce_acceptance_limit)],
)
async def decline_match(
    user: CurrentUser,
    match_id: MatchIdPath,
    acceptance: MatchAcceptanceDep,
    players: OpponentDirectoryDep,
) -> ApiResponse[PendingMatchResponse]:
    """Says no to a match you were paired into.

    **One decline ends it**, whatever your opponent did. The match is
    `cancelled` and no game is created.

    ## Neither of you goes back in the queue

    Your queue ticket was consumed the moment the match was created, and
    declining does not restore it — for you or for an opponent who had
    already accepted. Both of you must join the queue again.

    That is the platform's current behaviour and it is a **stated choice
    rather than a finished policy**: `specs/matchmaking.md` lists what a
    declined acceptance should do to both tickets as an open specification
    item, and re-queueing somebody automatically raises four product
    questions nobody has answered — whose place in line survives, whether
    the decliner waits, what rating snapshot the new ticket carries, and
    what happens if they have already re-queued by hand.

    `200` rather than `204`: the response carries the settled match, so a
    client can render "cancelled" without a second read.

    A second decline is a `409`, not a repeat — by then the match is
    already cancelled and there is nothing left to refuse. A client that
    needs to be safe against its own retry reads the match instead.
    """
    view = await acceptance.decline(player_id=user.id, match_id=match_id)
    return build_response(await _render(view, players))


async def _render(view: PendingMatchView, players: PublicProfileReader) -> PendingMatchResponse:
    """One pending match plus its opponent's public identity.

    **One batched lookup of one id**, which is worth saying plainly because
    §7 asks for no N+1 profile composition: a player has at most one pending
    match and a match has exactly one opponent, so there is no list here to
    loop over and no per-item read to accumulate. `find_public_profiles`
    takes a sequence because its other callers render pages; this one hands
    it a single element.

    A missing entry means the account was deactivated between the pairing
    and this read, and `None` is the same answer every other surface on this
    platform gives for a withdrawn account.
    """
    profiles = await players.find_public_profiles([view.opponent_player_id])
    return PendingMatchResponse.of(view, profiles.get(view.opponent_player_id))
