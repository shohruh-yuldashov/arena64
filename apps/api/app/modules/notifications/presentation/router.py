"""`/notifications` — the in-app read surface. A64-021.1 §15.

Six routes, all authenticated, all scoped to the caller:

    GET   /notifications                     one page, newest first
    GET   /notifications/unread-count        the badge
    POST  /notifications/{id}/read           mark one
    POST  /notifications/read-all            mark every unread one
    GET   /notifications/preferences         the whole matrix — A64-021.3
    PATCH /notifications/preferences         change what you receive

`/preferences` sits under this prefix rather than under `/profile`, where
the platform's other settings live, because it is this module's contract:
its vocabulary is `NotificationCategory` and `DeliveryChannel`, and the
delivery path that honours it is here. A settings *screen* may show it
beside the profile ones; that is a frontend arrangement, not an API one.

## The recipient is `CurrentUser`, and there is no way to say otherwise

No route takes a recipient id, in the path, the query or the body. That is
the whole of §30's first three bullets: a client cannot ask for somebody
else's notifications because there is no parameter in which to ask.

## Why a notification nobody owns is a `404` and never a `403`

A `403` confirms the notification exists, which is enough to probe for other
players' notifications one id at a time and learn how much social activity
somebody has. So an id that was never issued and an id belonging to another
player produce the **same** answer: same status, same body, same path
through this file.

## `POST` rather than `PATCH`, for marking read

Marking read is an action with no request body, not a partial replacement of
a resource. `PATCH /notifications/{id}` with `{"read": true}` would invite
`{"read": false}`, and unreading is not a behaviour this product defines
(§9). The verb is the narrower contract.

## There is no route that creates a notification

§15, and it is structural rather than a matter of not having written one:
`NotificationService` has no create method, and the only writer is a source
event's consumer.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.openapi import error_response
from app.api.responses import build_response
from app.core.responses import ApiResponse
from app.modules.auth.presentation.dependencies import CurrentUser, VerifiedUser
from app.modules.avatars.presentation.dependencies import AvatarLinkBuilderDep
from app.modules.notifications.application.services import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.modules.notifications.presentation.dependencies import (
    NotificationPreferenceServiceDep,
    NotificationServiceDep,
    PushSubscriptionServiceDep,
)
from app.modules.notifications.presentation.rate_limits import (
    enforce_notification_preferences_update_limit,
    enforce_push_subscription_limit,
)
from app.modules.notifications.presentation.schemas import (
    MarkAllReadResponse,
    NotificationPageResponse,
    NotificationPreferencesResponse,
    PushStatusResponse,
    PushSubscriptionResponse,
    RegisterPushSubscriptionRequest,
    RemovePushSubscriptionRequest,
    UnreadCountResponse,
    UpdateNotificationPreferencesRequest,
    decode_cursor,
)

notifications_router = APIRouter(prefix="/notifications", tags=["notifications"])


@notifications_router.get(
    "",
    response_model=ApiResponse[NotificationPageResponse],
    status_code=status.HTTP_200_OK,
    summary="Your notifications",
    # 422, not 400: `InvalidCursor` is a `ValidationError`, and the
    # platform maps that family to `422` (`api/exception_handlers.py`).
    responses=error_response(422, "The pagination cursor is not valid"),
)
async def list_notifications(
    user: CurrentUser,
    notifications: NotificationServiceDep,
    avatars: AvatarLinkBuilderDep,
    after: Annotated[
        str | None, Query(description="An opaque cursor from a previous page.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ApiResponse[NotificationPageResponse]:
    """Your notifications, newest first, keyset-paginated.

    **One request per page and none per row** — §31. The actor's name and
    avatar were stored with the notification, so a page of fifty costs one
    query and no profile lookups.
    """
    page = await notifications.list_for(
        user.id,
        after=decode_cursor(after) if after else None,
        limit=limit,
    )
    return build_response(NotificationPageResponse.of(page, avatars=avatars))


@notifications_router.get(
    "/unread-count",
    response_model=ApiResponse[UnreadCountResponse],
    status_code=status.HTTP_200_OK,
    summary="How many notifications you have not read",
)
async def unread_count(
    user: CurrentUser, notifications: NotificationServiceDep
) -> ApiResponse[UnreadCountResponse]:
    """The badge. One `COUNT` over a partial index, and no rows loaded — §10.

    Its own route so that rendering a number never costs a page of
    notifications, and so a client may refetch it on focus without paying
    for the list it is not showing.
    """
    return build_response(
        UnreadCountResponse(unread_count=await notifications.unread_count(user.id))
    )


@notifications_router.post(
    "/read-all",
    response_model=ApiResponse[MarkAllReadResponse],
    status_code=status.HTTP_200_OK,
    summary="Mark every unread notification read",
)
async def mark_all_read(
    user: CurrentUser, notifications: NotificationServiceDep
) -> ApiResponse[MarkAllReadResponse]:
    """Marks everything currently unread as read.

    **Registered before `/{notification_id}/read`** and it does not have to
    be: `read-all` is one segment and the other is two, so no request a
    client can send matches both. Kept adjacent anyway, because the two are
    read together.

    Returns how many changed, so a client reconciles its cached count
    without a second request. Zero is a successful no-op.
    """
    marked = await notifications.mark_all_read(user.id)
    return build_response(MarkAllReadResponse(marked_read=marked))


@notifications_router.get(
    "/preferences",
    response_model=ApiResponse[NotificationPreferencesResponse],
    status_code=status.HTTP_200_OK,
    summary="Your notification preferences",
)
async def get_preferences(
    user: CurrentUser, preferences: NotificationPreferenceServiceDep
) -> ApiResponse[NotificationPreferencesResponse]:
    """Every category on every channel, defaults already resolved — §7.

    Unlimited, unlike the write beneath it: one indexed read of at most a
    dozen of the caller's own rows, with no parameter that could name
    anybody else's. See `presentation.rate_limits`.
    """
    return build_response(
        NotificationPreferencesResponse.of(await preferences.effective_for(user.id))
    )


@notifications_router.patch(
    "/preferences",
    response_model=ApiResponse[NotificationPreferencesResponse],
    status_code=status.HTTP_200_OK,
    summary="Change your notification preferences",
    dependencies=[Depends(enforce_notification_preferences_update_limit)],
    responses={
        **error_response(422, "A change was refused, or the request was malformed"),
        **error_response(429, "Too many preference updates"),
    },
)
async def update_preferences(
    user: CurrentUser,
    preferences: NotificationPreferenceServiceDep,
    body: UpdateNotificationPreferencesRequest,
) -> ApiResponse[NotificationPreferencesResponse]:
    """Applies every change, or none — §9.

    **`PATCH`, and a list of changes rather than the whole matrix.** A save
    names only the switches that moved, so a client cannot overwrite a
    category it never rendered and a second tab cannot silently revert one
    it did not touch.

    Returns the resulting matrix, exactly what a fresh `GET` would say, so a
    save costs one request and the screen cannot disagree with the server.

    One illegal change rejects the whole request and writes nothing: a
    locked or unavailable pair answers `422` with the code that says which
    (`notification_preference_locked`, `notification_channel_unavailable`),
    and the table never moved.
    """
    return build_response(
        NotificationPreferencesResponse.of(
            await preferences.apply(
                user.id, changes=[change.to_change() for change in body.changes]
            )
        )
    )


@notifications_router.post(
    "/{notification_id}/read",
    response_model=ApiResponse[MarkAllReadResponse],
    status_code=status.HTTP_200_OK,
    summary="Mark one notification read",
    responses=error_response(404, "No such notification of yours"),
)
async def mark_read(
    user: CurrentUser,
    notifications: NotificationServiceDep,
    notification_id: Annotated[UUID, Path(description="Which notification to mark read.")],
) -> ApiResponse[MarkAllReadResponse]:
    """Marks one notification read.

    Idempotent: marking an already-read notification succeeds and leaves its
    original `read_at` alone, so a double click is one outcome. The response
    is the same shape as `read-all` so a client has one reconciliation path
    — `marked_read` is `1` for a notification this call changed and `0` for
    one that was already read.

    A notification that does not exist and one belonging to somebody else
    produce the same `404` — see this module's docstring.
    """
    changed = await notifications.mark_read(notification_id, recipient_id=user.id)
    return build_response(MarkAllReadResponse(marked_read=1 if changed else 0))


@notifications_router.post(
    "/push/subscriptions",
    dependencies=[Depends(enforce_push_subscription_limit)],
    response_model=ApiResponse[PushSubscriptionResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Register this browser for push notifications",
    responses=error_response(422, "The subscription is malformed, or push is unavailable"),
)
async def register_push_subscription(
    user: VerifiedUser,
    push: PushSubscriptionServiceDep,
    body: RegisterPushSubscriptionRequest,
) -> ApiResponse[PushSubscriptionResponse]:
    """Registers, re-registers or takes over one browser's subscription — §3, §4.

    **The account comes from the session.** There is no `user_id` on the
    request body and no path parameter — a caller cannot subscribe somebody
    else's account to their own browser, and the schema's `extra="forbid"`
    means an attempt to supply one is a `422` rather than a silently ignored
    field.

    Called on enabling push *and* on each app start, so re-registering is the
    normal case. An endpoint already registered — by this account or another
    — is **taken over** rather than rejected: a browser is the only thing
    that can tell this platform its previous binding is stale, and refusing
    the claim would leave a shared laptop bound to whoever used it first
    (§23).

    `VerifiedUser` rather than `CurrentUser` — §24. The verified-email policy
    A64-021.5H established governs every outward-facing write, and a push
    subscription is one: an unverified throwaway account could otherwise
    accumulate endpoints this platform will POST to on a schedule.

    `201` on both a create and a takeover. The distinction is not the
    client's business — it asked for "this browser is subscribed", and it is.
    """
    subscription = await push.register(
        user.id,
        endpoint=body.endpoint,
        p256dh=body.decoded_p256dh(),
        auth=body.decoded_auth(),
    )
    return build_response(PushSubscriptionResponse.of(subscription))


@notifications_router.post(
    "/push/subscriptions/remove",
    dependencies=[Depends(enforce_push_subscription_limit)],
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove this browser's push subscription",
)
async def remove_push_subscription(
    user: CurrentUser,
    push: PushSubscriptionServiceDep,
    body: RemovePushSubscriptionRequest,
) -> None:
    """Removes the calling browser's own subscription — §22, §23.

    The browser is identified by the endpoint it submits — the one thing it
    can produce about itself without having been told anything. It is not an
    authorization token: the session decides whose device this is, so an
    endpoint belonging to another account removes nothing.

    ## Why `POST .../remove` and not `DELETE`

    §4 offers `DELETE /push/subscriptions/current` "or equivalent", and the
    equivalent is chosen for one reason: the endpoint has to travel, and
    both `DELETE` shapes are worse places to put it.

        in the path or query   the endpoint is a bearer capability, and a
                               URL lands in access logs, proxy logs and
                               browser history (§25)
        in a DELETE body       permitted by HTTP and stripped in practice by
                               enough intermediaries that it is not
                               dependable

    A body on a `POST` is neither. The cost is a verb that does not name the
    effect, which the path does instead.

    **Always `204`.** An endpoint that was never registered, one already
    removed, and one belonging to somebody else all answer identically —
    distinguishing them would answer *"does this endpoint belong to another
    account"* about a value that is a bearer capability.

    `CurrentUser` rather than `VerifiedUser`, deliberately and unlike the
    register above: this is the path a **sign-out** takes (§23), and a
    person whose verification lapsed must still be able to stop their
    devices being pushed to. Removing a capability is never the operation to
    gate.
    """
    await push.remove(user.id, endpoint=body.endpoint)


@notifications_router.get(
    "/push/status",
    response_model=ApiResponse[PushStatusResponse],
    status_code=status.HTTP_200_OK,
    summary="Whether push works here, and how many browsers are registered",
)
async def push_status(
    user: CurrentUser,
    push: PushSubscriptionServiceDep,
) -> ApiResponse[PushStatusResponse]:
    """What the settings screen needs — §20.

    Answers `available: false` with a `null` key on a server with no VAPID
    pair, rather than `404` or an error: "push is not available here" is a
    state the UI renders, not a failure it handles.

    Carries no rate limit, like the preference read it sits beside: it is
    one indexed read of at most a handful of the caller's own rows, and a
    caller who repeats it learns how many devices they have.

    The **public** key only. The one that signs never appears in a response,
    a log, or a `VITE_` variable.
    """
    return build_response(PushStatusResponse.of(await push.status(user.id)))


__all__ = ["notifications_router"]
