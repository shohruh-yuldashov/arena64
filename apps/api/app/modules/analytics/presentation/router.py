"""The behavioural event collector — analytics.md §24, §37, §43.

One endpoint, and the smallest one that can exist:

    POST /analytics/events    a bounded batch of client events

## Why it is not authentication-required, and not anonymous-only

Both, per event class. The acquisition funnel's first two steps happen
before there is an account — a landing view and a registration click are the
whole point — so requiring a session would make F-A unmeasurable. But a
signed-in player firing `share_clicked` should be attributed to their
subject, so the identity is taken when there is one.

`OptionalCurrentUser` is exactly that shape, and it is the same dependency
A64-026.4 used to open the public tournament reads.

## What the handler does not do

It does not decide anything. The event name is checked against the registry,
the properties against their schema, and the identity comes from the
session — all in `ClientEventCollector`, so this function resolves a
service, converts a request and maps an exception. A router with a rule of
its own is a rule that is not in the tests that cover the rule.

## `202`, not `201`

Nothing addressable was created. The caller gets a count and no location,
because there is no resource to point at and a client has no business
reading an analytics row back.
"""

from fastapi import APIRouter, Depends, status

from app.api.openapi import error_response
from app.api.responses import build_response
from app.core.exceptions import ValidationError
from app.core.responses import ApiResponse
from app.modules.analytics.application.services.collector import (
    ClientEventSubmission,
    EventNotAcceptable,
)
from app.modules.analytics.presentation.dependencies import ClientEventCollectorDep
from app.modules.analytics.presentation.rate_limits import (
    enforce_analytics_collect_limit,
)
from app.modules.analytics.presentation.schemas.collect import CollectRequest, CollectResponse
from app.modules.auth.presentation.dependencies import OptionalCurrentUser
from app.platform.metrics import process_metrics

analytics_router = APIRouter(prefix="/analytics", tags=["analytics"])


@analytics_router.post(
    "/events",
    response_model=ApiResponse[CollectResponse],
    status_code=status.HTTP_202_ACCEPTED,
    summary="Record behavioural events",
    response_description="How many events were stored. A retry deduplicates rather than adding.",
    responses=error_response(422, "An event this endpoint does not accept, or invalid properties"),
    dependencies=[Depends(enforce_analytics_collect_limit)],
)
async def collect_events(
    request: CollectRequest,
    collector: ClientEventCollectorDep,
    viewer: OptionalCurrentUser = None,
) -> ApiResponse[CollectResponse]:
    """Stores a batch of client events, or refuses the whole batch.

    The refusal message is deliberately the same for a name outside the
    taxonomy and a name the server owns. A client that could tell them apart
    could enumerate which events are server-authoritative, and an endpoint
    that answers that is an oracle for the taxonomy.
    """
    submissions = [
        ClientEventSubmission(
            name=event.event_name,
            properties=event.properties,
            idempotency_key=event.idempotency_key,
            anonymous_id=event.anonymous_id,
            session_id=event.session_id,
        )
        for event in request.events
    ]

    try:
        accepted = await collector.collect(
            submissions,
            # **From the session, never from the body.** The request schema
            # has no field for this; taking it here is what makes that
            # absence meaningful.
            player_id=viewer.id if viewer is not None else None,
            metrics=process_metrics(),
        )
    except EventNotAcceptable as error:
        raise ValidationError(str(error)) from error

    return build_response(CollectResponse(accepted=accepted))
