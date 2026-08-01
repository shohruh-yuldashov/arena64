"""HTTP routes for the matchmaking queue — A64-014.1.

Three endpoints and **no business logic in any of them**. Each translates a
request into a service call and the result into a wire schema. QT-1 is the
index's, the state machine is `QueueTicket`'s, the presence rule is
`QueueService`'s, and the atomic claim is the repository's.

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

    AlreadyQueued        -> 409  conflict
    QueueNotPermitted     -> 422  validation_error
    NotQueued            -> 404  not_found
    MissingToken         -> 401  authentication_required
    TooManyRequests      -> 429  rate_limited

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

from fastapi import APIRouter, Depends, status

from app.api.openapi import Responses, error_response
from app.api.responses import build_response
from app.core.responses import ApiResponse
from app.modules.auth.presentation.dependencies import CurrentUser
from app.modules.matchmaking.domain.exceptions import NotQueued
from app.modules.matchmaking.domain.queue_pool import QueuePool
from app.modules.matchmaking.presentation.dependencies import QueueServiceDep
from app.modules.matchmaking.presentation.rate_limits import enforce_queue_limit
from app.modules.matchmaking.presentation.schemas import JoinQueueRequest, QueueTicketResponse

logger = logging.getLogger(__name__)

matchmaking_router = APIRouter(prefix="/matchmaking", tags=["matchmaking"])

_UNAUTHORIZED: Responses = error_response(
    401, "No access token was presented, or it was invalid or expired."
)
_ALREADY_QUEUED: Responses = error_response(
    409,
    (
        "You already hold a live queue ticket. One ticket per player, **across all "
        "pools** — leave the queue before joining a different one."
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

    **No match is created.** A64-014.1 builds the queue and nothing that
    consumes it: your ticket waits until you leave or it expires, and
    pairing arrives in a later task. That is the honest state of this
    endpoint and not a temporary defect — a client written against it needs
    no change when pairing lands, because the ticket is the thing pairing
    will consume.

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
