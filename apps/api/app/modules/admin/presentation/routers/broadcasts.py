"""The admin Broadcast API — A64-027A §12–§23.

    GET  /api/v1/admin/broadcasts/audience/{audience}
    POST /api/v1/admin/broadcasts
    GET  /api/v1/admin/broadcasts
    GET  /api/v1/admin/broadcasts/{broadcast_id}

## Why the prefix is `/admin/broadcasts` and not `/admin/notifications/...`

`routers/notifications.py` already owns `GET /admin/notifications/{id}`, and
a sibling path segment under the same prefix is captured by it: `broadcasts`
arrives as a malformed `notification_id` and the request is refused with a
UUID parse error. Registering this router first would "fix" it, and would
make correctness depend on the order of two `include_router` calls in a file
neither module owns. A distinct prefix cannot be shadowed at all.

The console still presents both under one Notifications workspace — that is
an information-architecture decision (§13) and has never needed the two to
share a URL prefix.

A **separate router** from `routers/notifications.py`, whose docstring opens
with "there is no send". That statement was true and is the reason this is a
new file rather than a fourth route in that one: the operations API reads
deliveries and re-arms them, and nothing about it should quietly become a
composer. What is delivered is still not something the retry route can
choose; what is delivered *here* is chosen, and the two deserve to be read
separately.

## Authorization is the server's, in one word

Every route names `CurrentAdmin`. §18 and §33: a console that hid the
composer behind a role check in the frontend would be a console whose
protection is a JavaScript bundle. The guard is the same one every other
admin route uses, so "is this route protected" stays one word in a
signature.

## The request creates work; it does not do it

`POST` writes one row and returns `202`. §19 forbids looping an audience
inside an HTTP request, and `202 Accepted` is the honest status for it: the
platform has taken the instruction and has not yet carried it out. A `200`
would claim delivery that has not happened.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from app.modules.admin.application.services.audit_recorder import AuditRecorder
from app.modules.admin.domain.audit import AuditAction, AuditSubjectType
from app.modules.admin.presentation.dependencies import CurrentAdmin
from app.modules.admin.presentation.dependencies.broadcasts import (
    AuditRecorderDep,
    BroadcastServiceDep,
)
from app.modules.admin.presentation.schemas.broadcasts import (
    AudienceSizeResponse,
    BroadcastCreateRequest,
    BroadcastPageResponse,
    BroadcastResponse,
)
from app.modules.notifications.application.services.broadcast_service import BroadcastRequest
from app.modules.notifications.domain.broadcast import Broadcast, BroadcastAudience

admin_broadcasts_router = APIRouter(prefix="/admin/broadcasts", tags=["admin", "notifications"])

#: One page of history. The console shows a list, not an archive.
MAX_PAGE_SIZE = 50


@admin_broadcasts_router.get("/audience/{audience}", response_model=AudienceSizeResponse)
async def audience_size(
    audience: BroadcastAudience,
    admin: CurrentAdmin,
    service: BroadcastServiceDep,
) -> AudienceSizeResponse:
    """How many accounts this audience reaches right now.

    Guarded like every other route here. It is a count and not a list, but
    an unguarded count of verified accounts is still a fact about the
    platform that a stranger has no business reading.
    """
    return AudienceSizeResponse(
        audience=audience.value,
        size=await service.preview_audience_size(audience),
    )


@admin_broadcasts_router.post(
    "",
    response_model=BroadcastResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_broadcast(
    request: BroadcastCreateRequest,
    admin: CurrentAdmin,
    service: BroadcastServiceDep,
    recorder: AuditRecorderDep,
) -> BroadcastResponse:
    """Queues one announcement. Idempotent on the request's key.

    ## The audit entry is written for the attempt, not for the delivery

    §23. What is recorded is that an administrator addressed an audience,
    which is true the moment the row exists; whether every recipient
    received it is the broadcast's own status and is on the row itself.

    The entry carries the audience *category* and the broadcast id, and no
    recipient. A named audience of a hundred players would otherwise put a
    hundred identities into a table operators grep — §23 forbids exactly
    that, and the broadcast row remains the place to look.
    """
    broadcast = await service.create(
        BroadcastRequest(
            title=request.title,
            body=request.body,
            locale=request.locale,
            audience=request.audience,
            idempotency_key=request.idempotency_key,
            recipients=tuple(request.recipients),
        ),
        created_by=admin.id,
    )
    await _audit(recorder, admin_id=admin.id, broadcast=broadcast)
    return BroadcastResponse.of(broadcast)


@admin_broadcasts_router.get("", response_model=BroadcastPageResponse)
async def list_broadcasts(
    admin: CurrentAdmin,
    service: BroadcastServiceDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
    before: datetime | None = None,
) -> BroadcastPageResponse:
    """The history, newest first — §20."""
    items = await service.history(limit=limit, before=before)
    return BroadcastPageResponse(items=[BroadcastResponse.of(item) for item in items])


@admin_broadcasts_router.get("/{broadcast_id}", response_model=BroadcastResponse)
async def read_broadcast(
    broadcast_id: UUID,
    admin: CurrentAdmin,
    service: BroadcastServiceDep,
) -> BroadcastResponse:
    broadcast = await service.get(broadcast_id)
    if broadcast is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such broadcast.")
    return BroadcastResponse.of(broadcast)


async def _audit(recorder: AuditRecorder, *, admin_id: UUID, broadcast: Broadcast) -> None:
    await recorder.record_administrator(
        actor_id=admin_id,
        action=AuditAction.NOTIFICATION_BROADCAST_SENT,
        subject_type=AuditSubjectType.NOTIFICATION,
        subject_ref=str(broadcast.id),
        after={
            "audience": broadcast.audience.value,
            "channel": broadcast.channel.value,
            # The title, so an operator can recognise it. Not the body: the
            # broadcast row is the system of record and still holds it.
            "title": broadcast.title,
            "named_recipients": len(broadcast.recipients),
        },
    )


__all__ = ["admin_broadcasts_router"]
