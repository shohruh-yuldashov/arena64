"""The admin Notification Operations API — A64-024.7.

    GET  /api/v1/admin/notifications
    GET  /api/v1/admin/notifications/{notification_id}
    POST /api/v1/admin/notifications/{notification_id}/deliveries/{subscription_id}/retry

**There is no send.** No compose, no broadcast, no recipient picker, no
payload, no target, no schedule. The one mutation takes two identifiers from
the path and no body, so there is nothing a caller could supply that changes
*what* is delivered — only whether the platform tries again.

The retry route is spelled for the **delivery**, not the notification,
because that is what it addresses: one message, one device. A route named
`/notifications/{id}/resend` would describe a capability this platform
deliberately does not have.

## Query shape — one page is three statements

    1  the page of notifications      `AdministrativeNotificationDirectory`
    2  every delivery for it          one `IN` over the delivery table
    3  the recipients' names          one batch over `users.public`

Nothing loops a read. A page of fifty notifications owes up to several
hundred deliveries and names up to fifty accounts; each is asked for once.
"""

from collections.abc import Mapping, Sequence
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.core.constants import API_V1_PREFIX
from app.modules.admin.domain.exceptions import RetryUnavailable
from app.modules.admin.presentation.dependencies import CurrentAdmin
from app.modules.admin.presentation.dependencies.notifications import (
    AdminNotificationDirectoryDep,
    NotificationOperationsDep,
)
from app.modules.admin.presentation.dependencies.users import AdminUserDirectoryDep
from app.modules.admin.presentation.schemas.notifications import (
    AdminNotificationDetailResponse,
    AdminNotificationPageResponse,
    AdminNotificationSummary,
    AdminPushDeliveryView,
)
from app.modules.notifications.public import (
    AdminNotificationFilters,
    AdminNotificationRecord,
    AdminPushDelivery,
)
from app.modules.notifications.public.administration import AdminNotificationDetail
from app.modules.users.public import AdminUserRecord

admin_notifications_router = APIRouter(
    prefix=f"{API_V1_PREFIX}/admin/notifications", tags=["admin"]
)

#: The largest page an operator may ask for — the bound every admin list on
#: this console uses. There is no "all".
MAX_PAGE_SIZE = 50
DEFAULT_PAGE_SIZE = 25


@admin_notifications_router.get(
    "",
    response_model=AdminNotificationPageResponse,
    summary="List notifications and their push standing",
)
async def list_notifications(
    admin: CurrentAdmin,
    directory: AdminNotificationDirectoryDep,
    accounts: AdminUserDirectoryDep,
    response: Response,
    recipient_id: Annotated[
        UUID | None,
        Query(description="Everything one account was sent. Index-backed."),
    ] = None,
    failed_push_only: Annotated[
        bool,
        Query(
            description="Only notifications with at least one failed push. "
            "Index-backed, and the question this console exists to answer."
        ),
    ] = False,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> AdminNotificationPageResponse:
    """One page, newest first.

    `admin` is unused in the body and named anyway: it is the guard, and a
    route whose protection lived only in a router-level dependency would be
    one whose protection is invisible in its own signature.
    """
    _no_store(response)

    page = await directory.list_notifications(
        filters=AdminNotificationFilters(
            recipient_id=recipient_id, failed_push_only=failed_push_only
        ),
        limit=limit,
        cursor=cursor,
    )

    deliveries = await directory.deliveries_for([record.id for record in page.records])
    named = await accounts.accounts_by_ids([record.recipient_id for record in page.records])

    return AdminNotificationPageResponse(
        items=[_summary(record, deliveries.get(record.id, ()), named) for record in page.records],
        next_cursor=page.next_cursor,
    )


@admin_notifications_router.get(
    "/{notification_id}",
    response_model=AdminNotificationDetailResponse,
    summary="One notification, with every device's delivery",
    responses={404: {"description": "No such notification."}},
)
async def read_notification(
    notification_id: UUID,
    admin: CurrentAdmin,
    directory: AdminNotificationDirectoryDep,
    accounts: AdminUserDirectoryDep,
    response: Response,
) -> AdminNotificationDetailResponse:
    """One notification and its deliveries — two statements plus one name."""
    _no_store(response)

    detail = await directory.find_notification(notification_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such notification.")

    named = await accounts.accounts_by_ids([detail.notification.recipient_id])
    return _detail(detail, named)


@admin_notifications_router.post(
    "/{notification_id}/deliveries/{subscription_id}/retry",
    response_model=AdminPushDeliveryView,
    summary="Re-arm one exhausted push delivery",
    responses={
        404: {"description": "No such notification."},
        409: {"description": "That delivery cannot be retried."},
    },
)
async def retry_delivery(
    notification_id: UUID,
    subscription_id: UUID,
    admin: CurrentAdmin,
    operations: NotificationOperationsDep,
    directory: AdminNotificationDirectoryDep,
    response: Response,
) -> AdminPushDeliveryView:
    """Queues one more attempt at an already-recorded delivery.

    **No request body.** There is nothing to decide: the recipient, the
    type, the payload and the destination are all already stored, and this
    endpoint changes none of them. A body would be a place for one of them
    to arrive.

    Eligibility is decided by the guarded `UPDATE` rather than by a read
    taken a moment earlier — so a worker settling the row, a second
    administrator, or a state that was never eligible all resolve to the
    same `409` with nothing changed. Each refusal writes a `FAILED` audit
    entry, per `specs/admin.md` §6.12's policy.
    """
    _no_store(response)

    if await directory.find_notification(notification_id) is None:
        # Read first so a retry against an id that matches nothing is a
        # `404` rather than a conflict about a delivery that never existed.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such notification.")

    try:
        delivery = await operations.retry_delivery(
            notification_id=notification_id,
            subscription_id=subscription_id,
            actor_id=admin.id,
        )
    except RetryUnavailable:
        await operations.record_refusal(
            notification_id=notification_id,
            actor_id=admin.id,
            refusal="delivery_not_retryable",
        )
        raise

    return _delivery_view(delivery)


def _summary(
    record: AdminNotificationRecord,
    deliveries: Sequence[AdminPushDelivery],
    named: Mapping[UUID, AdminUserRecord],
) -> AdminNotificationSummary:
    return AdminNotificationSummary(
        id=record.id,
        recipient_id=record.recipient_id,
        recipient_username=_username(record.recipient_id, named),
        type=record.type.value,
        category=record.category.value,
        created_at=record.created_at,
        read_at=record.read_at,
        push_capable=record.push_capable,
        push_summary=_push_summary(record, deliveries),
        delivery_count=len(deliveries),
    )


def _push_summary(record: AdminNotificationRecord, deliveries: Sequence[AdminPushDelivery]) -> str:
    """The page's one-word push standing.

    Derived rather than stored, and **worst-first**: a notification that
    reached two devices and failed on a third is reported as `failed`,
    because the third is the one an operator has to do something about. A
    "mostly fine" summary would hide exactly the row this console exists to
    surface.

    `none` covers both "this type is not pushed" and "no device was
    subscribed", which are the same fact to an operator: no push was owed,
    and nothing is wrong.
    """
    if not deliveries:
        return "none"
    statuses = {delivery.status.value for delivery in deliveries}
    for worst in ("failed", "pending", "skipped", "sent"):
        if worst in statuses:
            return worst
    return "none"


def _detail(
    detail: AdminNotificationDetail, named: Mapping[UUID, AdminUserRecord]
) -> AdminNotificationDetailResponse:
    record = detail.notification
    return AdminNotificationDetailResponse(
        id=record.id,
        recipient_id=record.recipient_id,
        recipient_username=_username(record.recipient_id, named),
        type=record.type.value,
        category=record.category.value,
        target_type=record.target_type.value,
        target_ref=record.target_ref,
        source_event_id=record.source_event_id,
        created_at=record.created_at,
        read_at=record.read_at,
        push_capable=record.push_capable,
        deliveries=[_delivery_view(delivery) for delivery in detail.deliveries],
    )


def _delivery_view(delivery: AdminPushDelivery) -> AdminPushDeliveryView:
    return AdminPushDeliveryView(
        subscription_id=delivery.subscription_id,
        status=delivery.status.value,
        outcome=delivery.outcome.value if delivery.outcome else None,
        attempt_count=delivery.attempt_count,
        next_attempt_at=delivery.next_attempt_at,
        last_attempt_at=delivery.last_attempt_at,
        # Renamed on the way out: the column is `delivered_at` and the fact
        # is "a push service accepted it". The response says the fact.
        accepted_at=delivery.delivered_at,
        created_at=delivery.created_at,
        can_retry=delivery.is_retryable,
        device_first_seen_at=delivery.subscription_created_at,
        device_last_seen_at=delivery.subscription_last_seen_at,
        device_revoked_at=delivery.subscription_revoked_at,
    )


def _username(account_id: UUID, named: Mapping[UUID, AdminUserRecord]) -> str | None:
    """The recipient's handle, or `None` for an account that no longer
    exists — a fact, where a fabricated name would not be."""
    record = named.get(account_id)
    return None if record is None else record.username


def _no_store(response: Response) -> None:
    """Privileged answers are never reused from a cache — §15."""
    response.headers["Cache-Control"] = "no-store"


__all__ = [
    "MAX_PAGE_SIZE",
    "admin_notifications_router",
    "list_notifications",
    "read_notification",
    "retry_delivery",
]
