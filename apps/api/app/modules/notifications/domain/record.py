"""The durable `Notification` — NT-1, and A64-021.1's whole product model.

Framework-free (architecture.md §8), and the aggregate `domain-model.md`
§9.3 has specified since the beginning: *"a durable record that something
happened which the player should know about, independent of whether any
delivery channel worked."*

Until this task, `notifications` had only the *transient* half:
`SocialNotification`, a rendered value handed to a sink. That value's own
docstring predicted this file — *"when NT-1's history arrives, a second sink
persists `Notification` rows. Neither needs this type to change"* — and that
is exactly the shape this takes: a second sink, and no change to the
dispatcher that feeds it.

## A projection, not a second copy of the source

A notification carries only what is needed to **render it and navigate from
it**, and it is a record of what was true when it was written. That has two
consequences worth stating rather than discovering:

  - The actor's display name is the name they had *then*. A notification is
    history; re-resolving it at read time would cost a profile lookup per
    row (§31 forbids exactly that) and would rewrite the past.
  - Nothing here is authoritative about anything. Delete every row and the
    friendship, the request and the match are all still there.

## What is deliberately absent

`dismissed_at`, `expires_at` and `correlation_id` are all in
`database.md` §10.2 and none is here. Dismissal is a second read-state with
no UI to set it, `expires_at` serves NT-3's *delivery* staleness horizon and
there is no delivery channel yet, and a correlation id would be a column
nothing reads — the outbox row named by `source_event_id` already carries
one. Each is additive when its consumer arrives; a column that looks
maintained and is not is worse than an absent one.

## Why the payload is typed rather than free JSON

§3 forbids arbitrary unvalidated JSON. The stored shape is decided by the
`type`, decoded through `payload_of` on the way out, and a row whose payload
does not match its type raises rather than reaching a response — which is
what makes "the frontend supplies presentation, the backend supplies facts"
safe to rely on.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final
from uuid import UUID


class NotificationCategory(StrEnum):
    """The bounded families a future preference switch will address.

    Four, and the set is chosen so that a player muting one is muting
    something they can name. `marketing` is deliberately absent: this
    product defines no such notification, and a category nothing produces is
    a preference that silently does nothing.

    Only `SOCIAL` has a producer today. The other three exist because the
    *category* is the unit `users.notification_preference` will key on
    (database.md §4.9), and adding a category later means migrating rows
    that were written without one.
    """

    SOCIAL = "social"
    GAME = "game"
    TOURNAMENT = "tournament"
    SYSTEM = "system"


class NotificationType(StrEnum):
    """What happened, and therefore which sentence the client renders.

    The template key of `database.md` §10.2 under the name A64-021.1 gives
    it. **No rendered text is stored** — §15.2 of that document explains
    why, and it is the reason this enum is the contract: the backend states
    a fact, the frontend translates it into uz, ru or en.

    Two members, because two source events exist that name their recipient
    unambiguously (§12). Every other candidate — a published round, a
    completed tournament, a registration — needs a source event that either
    does not exist or does not say who to tell, and inventing one to
    populate a list is what §4 forbids.
    """

    FRIEND_REQUEST_RECEIVED = "friend_request_received"
    """Someone sent you a friend request."""

    FRIEND_REQUEST_ACCEPTED = "friend_request_accepted"
    """Someone accepted the request you sent them."""


#: Which family each type belongs to. A mapping rather than a field on the
#: enum, so that a type cannot be added without deciding its category —
#: `CATEGORY_OF[new]` raises at import of the first user rather than
#: defaulting to something plausible.
CATEGORY_OF: Final[Mapping[NotificationType, NotificationCategory]] = {
    NotificationType.FRIEND_REQUEST_RECEIVED: NotificationCategory.SOCIAL,
    NotificationType.FRIEND_REQUEST_ACCEPTED: NotificationCategory.SOCIAL,
}


class NavigationTargetType(StrEnum):
    """Where tapping a notification takes the recipient — §6.

    A closed set of **internal** destinations. No URL is ever stored: an
    event-supplied URL would be an open redirect written into a table, and
    a pre-rendered path would bake this build's routing into rows that
    outlive it. The client maps a target type plus one safe identifier onto
    a route it already owns.

    Two members, for the two types above. `play`, `live_game`, `tournament`
    and `match_replay` are named by §6 and are not here, for the reason the
    types are not: nothing produces them yet.
    """

    PLAYER_PROFILE = "player_profile"
    """`ref` is the player's **username** — the identifier `/players/{username}`
    takes. Usernames are not editable on this platform (`ProfileUpdateRequest`
    offers no field for one), so a stored username stays resolvable."""

    FRIEND_REQUESTS = "friend_requests"
    """The incoming-requests list. `ref` is `None`: the destination is the
    viewer's own page and carries no identifier to get wrong."""


@dataclass(frozen=True, slots=True)
class NavigationTarget:
    """One destination, as a type and at most one safe identifier."""

    type: NavigationTargetType
    ref: str | None = None


@dataclass(frozen=True, slots=True)
class ActorSummary:
    """The player a social notification is *about*, as the recipient may see
    them at the moment the notification was written.

    **Composed through `PublicProfileComposer`**, never read from a table
    directly — so a field the actor withheld is not here, and there is no
    path from this record back to an unfiltered identity. That gate is
    applied by `SocialNotificationDispatcher`, which is also where the block
    list is re-read; this type only holds the result.

    The avatar is carried as an **object key and a version**, not a URL.
    Composing the URL needs the deployment's storage provider
    (`avatars.public.AvatarLinkBuilder`), and a stored URL would freeze one
    CDN hostname into every historical row — the same reason
    `users.public.AvatarReference` carries no URL either.
    """

    player_id: UUID
    username: str
    display_name: str | None
    avatar_object_key: str | None
    avatar_version: int


#: The payload union. One member today; the alias is the seam a second type
#: with a different shape arrives through, and it is what `payload_of`
#: dispatches on.
NotificationPayload = ActorSummary


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    """One durable notification, owned by its recipient.

    Frozen: read state is changed by writing a row, not by mutating a value
    somebody is holding.
    """

    id: UUID
    recipient_id: UUID
    type: NotificationType
    category: NotificationCategory
    payload: NotificationPayload
    target: NavigationTarget
    source_event_id: UUID
    """The outbox entry that caused this — the durable half of exactly-once.

    Never published on the API (§16): it names an internal record, and a
    client has nothing to do with it.
    """

    created_at: datetime
    read_at: datetime | None = None

    @property
    def is_read(self) -> bool:
        return self.read_at is not None


@dataclass(frozen=True, slots=True)
class NotificationAnnouncement:
    """That a notification now exists — A64-021.2 §2.

    The value that crosses into `app.gateway`, and it is deliberately the
    **smallest** thing a client needs in order to know it should re-read.

    ## Why it carries three fields and not the notification

    §2 allows `notification_id`, `type` and `created_at`, and forbids
    everything else — no actor, no username, no avatar, no rendered text.
    That is not miserliness: a pushed payload is a **second copy** of a
    record the client is about to fetch anyway, and a second copy is a
    second thing that can be stale, be wrong, or leak. The frame says
    *something happened*; `GET /notifications` says what.

    It is also what makes the frame safe to send to a socket whose owner
    may have signed out, changed their privacy settings, or blocked the
    actor between the write and the push: none of those can change what
    these three fields mean.

    `recipient_id` is the **address**, not payload. The gateway uses it to
    choose a socket and never puts it on the wire — a client already knows
    who it is.
    """

    notification_id: UUID
    recipient_id: UUID
    type: NotificationType
    created_at: datetime

    @classmethod
    def of(cls, record: "NotificationRecord") -> "NotificationAnnouncement":
        """The announcement for a record that was **actually written**.

        A classmethod rather than a constructor call at the call site, so
        the projection is defined once and a field added to
        `NotificationRecord` does not silently appear on the wire.
        """
        return cls(
            notification_id=record.id,
            recipient_id=record.recipient_id,
            type=record.type,
            created_at=record.created_at,
        )


class MalformedNotification(ValueError):
    """A stored row whose payload does not match its type.

    Raised on the way *out*, so a row written by an older or broken producer
    fails loudly at the one place that can still refuse it, rather than
    reaching a client as a half-rendered card. §17 maps it to a stable code
    and never exposes what was wrong with it.
    """


def payload_as_json(payload: NotificationPayload) -> dict[str, Any]:
    """The stored form. Explicit, field by field.

    `dataclasses.asdict` would be shorter and would serialise whatever the
    dataclass grows next — including something that should not be stored.
    Writing the keys out means the stored contract changes only when
    somebody edits this function.
    """
    return {
        "actor_player_id": str(payload.player_id),
        "actor_username": payload.username,
        "actor_display_name": payload.display_name,
        "actor_avatar_object_key": payload.avatar_object_key,
        "actor_avatar_version": payload.avatar_version,
    }


def payload_of(type_: NotificationType, stored: Mapping[str, Any]) -> NotificationPayload:
    """Decodes a stored payload against its type. Raises `MalformedNotification`.

    The dispatch is on `type_` even though both members decode identically
    today, because that is the whole point of the seam: the second payload
    shape is a branch here, not a new column and not an `if` at every reader.
    """
    if type_ in (
        NotificationType.FRIEND_REQUEST_RECEIVED,
        NotificationType.FRIEND_REQUEST_ACCEPTED,
    ):
        return _actor_of(stored)
    # Unreachable while the enum has two members and both are handled. Kept
    # so that adding a member without a decoder fails at the row rather than
    # silently returning the wrong shape.
    raise MalformedNotification(f"no payload decoder for {type_}")


def _actor_of(stored: Mapping[str, Any]) -> ActorSummary:
    try:
        return ActorSummary(
            player_id=UUID(str(stored["actor_player_id"])),
            username=str(stored["actor_username"]),
            display_name=_optional_str(stored["actor_display_name"]),
            avatar_object_key=_optional_str(stored["actor_avatar_object_key"]),
            avatar_version=int(stored["actor_avatar_version"]),
        )
    except (KeyError, TypeError, ValueError) as malformed:
        # The cause is chained (CLAUDE.md §9.4) so an operator sees which
        # key was wrong; the message that reaches a client does not.
        raise MalformedNotification("stored payload does not match its type") from malformed


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


__all__ = [
    "CATEGORY_OF",
    "ActorSummary",
    "NotificationAnnouncement",
    "MalformedNotification",
    "NavigationTarget",
    "NavigationTargetType",
    "NotificationCategory",
    "NotificationPayload",
    "NotificationRecord",
    "NotificationType",
    "payload_as_json",
    "payload_of",
]
