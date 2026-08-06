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
from app.modules.auth.presentation.dependencies import CurrentUser
from app.modules.avatars.presentation.dependencies import AvatarLinkBuilderDep
from app.modules.notifications.application.services import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.modules.notifications.presentation.dependencies import (
    NotificationPreferenceServiceDep,
    NotificationServiceDep,
)
from app.modules.notifications.presentation.rate_limits import (
    enforce_notification_preferences_update_limit,
)
from app.modules.notifications.presentation.schemas import (
    MarkAllReadResponse,
    NotificationPageResponse,
    NotificationPreferencesResponse,
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


__all__ = ["notifications_router"]
