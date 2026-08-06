"""Wire schemas for the notification endpoints — A64-021.1 §16.

## The backend states facts; the client renders sentences

**No rendered text is stored and none is served.** A notification carries a
`type`, a `category` and typed fields, and the client turns those into a
sentence in uz, ru or en. That is `database.md` §15.2's decision, and it is
what makes a translated notification list possible at all — a message
composed on the server would be composed in one language, at write time, for
a player who may change their language afterwards.

It is also the reason nothing here can carry markup: there is no string a
client would ever render as HTML, so there is nothing to inject into (§20,
§30).

## What is deliberately not on the wire

`source_event_id` — §16. It names a row in `platform.outbox`, a client has
nothing to do with it, and publishing it would let one correlate two
recipients' notifications back to a single social event.

The stored avatar object key — the wire carries a **URL**, composed here
through `AvatarLinkBuilder` exactly as every other endpoint composes one, so
no object key and no bucket layout reaches a client.
"""

import base64
import binascii
from collections.abc import Sequence
from datetime import datetime
from typing import ClassVar
from uuid import UUID

from pydantic import Field

from app.core.dto import BaseResponseDTO
from app.core.error_codes import ErrorCode
from app.core.exceptions import ValidationError
from app.modules.avatars.public import AvatarLinkBuilder
from app.modules.notifications.application.read_models import (
    NotificationCursor,
    NotificationPage,
)
from app.modules.notifications.domain.record import ActorSummary, NotificationRecord
from app.modules.users.public import AvatarReference

#: Separator inside the encoded cursor. A character that cannot appear in an
#: ISO instant or a UUID, so the split is unambiguous.
_CURSOR_SEPARATOR = "|"


class InvalidCursor(ValidationError):
    """The `after` parameter was not a cursor this API issued.

    Its own class rather than `game`'s, because a module does not import
    another module's presentation layer — and the same `ErrorCode`, because
    a client's recovery is the same either way: ask for the first page.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.INVALID_CURSOR


def encode_cursor(cursor: NotificationCursor) -> str:
    raw = f"{cursor.created_at.isoformat()}{_CURSOR_SEPARATOR}{cursor.notification_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(value: str) -> NotificationCursor:
    """A cursor string as the port's value. Raises `InvalidCursor`.

    Every failure mode collapses to one error: bad base64, a missing
    separator, an unparseable instant or id. A client cannot act differently
    on any of them — the answer is always "ask for the first page" — and
    distinguishing them would describe the encoding to whoever is probing it.
    """
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
        instant, _, notification_id = raw.partition(_CURSOR_SEPARATOR)
        return NotificationCursor(
            created_at=datetime.fromisoformat(instant),
            notification_id=UUID(notification_id),
        )
    except (binascii.Error, UnicodeDecodeError, ValueError) as malformed:
        raise InvalidCursor("the pagination cursor is not valid") from malformed


class NotificationActorResponse(BaseResponseDTO):
    """The player a notification is about, as the recipient may see them.

    A **snapshot** taken when the notification was written, through the same
    privacy gate every other surface uses. The display name is the one they
    had then; re-resolving it would cost a profile request per row (§31) and
    would rewrite history.

    Required today because both notification types have an actor. The first
    type without one makes this optional and adds its own key beside it —
    which is one frontend change made at the same time as the renderer that
    type needs anyway.
    """

    player_id: UUID
    username: str
    display_name: str | None = Field(
        description="The player's chosen name, or `null` — clients fall back to `username`."
    )
    thumbnail_url: str | None = Field(
        description="URL of the 128px avatar rendition at the time of writing, or `null`."
    )


class NotificationTargetResponse(BaseResponseDTO):
    """Where tapping this notification goes — §6.

    A **type and at most one identifier**, never a URL: the client owns its
    routes and maps the type onto one it already has. A target type a client
    does not recognise must render as a non-navigable notification rather
    than as a broken link.
    """

    type: str = Field(description="A `NavigationTargetType` value.", examples=["player_profile"])
    ref: str | None = Field(
        description="The one safe identifier the target needs, or `null`.",
        examples=["player_one"],
    )


class NotificationResponse(BaseResponseDTO):
    """One durable notification."""

    id: UUID
    type: str = Field(
        description="A `NotificationType` value. The client renders a translated sentence from it.",
        examples=["friend_request_received"],
    )
    category: str = Field(description="A `NotificationCategory` value.", examples=["social"])
    actor: NotificationActorResponse
    target: NotificationTargetResponse
    created_at: datetime = Field(
        description="When the notified fact happened — not when the row was inserted."
    )
    read_at: datetime | None
    is_read: bool = Field(
        description=(
            "Derived from `read_at`. Present so a client renders state "
            "without comparing against null."
        )
    )

    @classmethod
    def of(
        cls, record: NotificationRecord, *, avatars: AvatarLinkBuilder
    ) -> "NotificationResponse":
        """Renders one record. Field by field, never `model_validate`.

        The discipline is the control rather than a style preference: an
        implicit conversion is how `source_event_id` would appear on a
        response the day somebody adds a field to the record.
        """
        actor: ActorSummary = record.payload
        # `uploaded_at` is `None` rather than stored: the notification's own
        # `created_at` already says when this snapshot was taken, and the
        # link builder uses `version` — not the instant — as its cache
        # buster. Storing a second timestamp nothing reads would be a column
        # that looks maintained.
        links = avatars.links_for(
            AvatarReference(
                object_key=actor.avatar_object_key,
                version=actor.avatar_version,
                uploaded_at=None,
            )
        )
        return cls(
            id=record.id,
            type=record.type.value,
            category=record.category.value,
            actor=NotificationActorResponse(
                player_id=actor.player_id,
                username=actor.username,
                display_name=actor.display_name,
                thumbnail_url=links.thumbnail_url if links else None,
            ),
            target=NotificationTargetResponse(type=record.target.type.value, ref=record.target.ref),
            created_at=record.created_at,
            read_at=record.read_at,
            is_read=record.is_read,
        )


class NotificationPageResponse(BaseResponseDTO):
    """One page, newest first."""

    entries: Sequence[NotificationResponse]
    next_cursor: str | None = Field(
        description=(
            "An opaque cursor for the next page, or `null` on the last page. "
            "Send it back unchanged; never construct or decode one."
        )
    )

    @classmethod
    def of(
        cls, page: NotificationPage, *, avatars: AvatarLinkBuilder
    ) -> "NotificationPageResponse":
        return cls(
            entries=[NotificationResponse.of(record, avatars=avatars) for record in page.entries],
            next_cursor=encode_cursor(page.next_cursor) if page.next_cursor else None,
        )


class UnreadCountResponse(BaseResponseDTO):
    """The badge, and nothing else — §10.

    Its own endpoint and its own shape so that rendering a count never costs
    a page of notifications, and so a client polling it on focus sends the
    cheapest request this API has.
    """

    unread_count: int = Field(ge=0, examples=[3])


class MarkAllReadResponse(BaseResponseDTO):
    """How many notifications the call changed.

    Returned rather than `204` so a client can reconcile its cached count
    without a second request — and so "nothing was unread" is visibly a
    successful no-op rather than an absent answer.
    """

    marked_read: int = Field(ge=0, examples=[3])


__all__ = [
    "InvalidCursor",
    "MarkAllReadResponse",
    "NotificationActorResponse",
    "NotificationPageResponse",
    "NotificationResponse",
    "NotificationTargetResponse",
    "UnreadCountResponse",
    "decode_cursor",
    "encode_cursor",
]
