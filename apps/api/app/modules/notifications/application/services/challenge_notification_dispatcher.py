"""`ChallengeNotificationDispatcher` — two moments of a friend challenge,
told to the person waiting on each. A64-022.4 §3, §15, §18.

The fourth outbox consumer this module owns, and it is shaped like the
tournament and game ones rather than like the social one: it composes
`NotificationRecord`s itself and hands them to `DurableNotificationStore`.
`SocialNotificationDispatcher`'s `SocialNotification` carries a rendered
profile and nothing else, and a challenge is not a profile — it is a set of
game settings with a person attached.

## Two events, two recipients, and the mapping is the whole design

    matchmaking.friend_challenge_created   -> friend_challenge_received,
                                              told to the **recipient**
    matchmaking.friend_challenge_accepted  -> friend_challenge_accepted,
                                              told to the **challenger**

In both cases the person told is the one who was *not* acting. That is the
rule `SocialNotificationDispatcher` states for friend requests — "telling
somebody what they just did is not a notification" — and it holds here
without an exception.

The recipient is read off the event, never derived from a request: both ids
are on every challenge payload precisely so a consumer does not have to
re-read a row a retention sweep is allowed to have removed.

## The other three lifecycle events notify nobody, and that is a decision

    friend_challenge_declined   the challenger learns nothing actionable. A
                                decline carries no reason by design, so the
                                notification would say "no", permanently, in
                                a list whose value is that it is short
    friend_challenge_cancelled  a **retraction**. Its consumer is a UI that
                                must stop showing an invitation, not an
                                inbox that must start showing one
    friend_challenge_expired    nothing happened. A row saying so is an
                                inbox entry about the absence of an event

All three are still real events with real consumers coming — A64-022.5's
challenge surface reconciles against them over HTTP. What they are not is
*durable notifications*, and §14's rule is the reason: an inbox filled with
administrative lifecycle noise is an inbox people stop reading.

This consumer therefore does not subscribe to them at all, rather than
subscribing and dropping them. A challenge lifecycle is five events and two
are notifications; the ledger says so.

## Delivery-time suppression, and the two events differ

`friend_challenge_received` is re-checked against the **block list, now** —
the same read, the same symmetry and the same reason as
`SocialNotificationDispatcher._still_reachable`: a player blocked between
the challenge and this tick must not have it delivered, and a delayed push
is exactly the path a block is supposed to close.

`friend_challenge_accepted` is **not** suppressed, and that is deliberate
rather than an omission. Acceptance revalidated the relationship inside its
own transaction (A64-022.3), and it created a match that exists. Withholding
the notification would leave a challenger holding a game they were never
told about, in a ten-minute join window — the same shape of harm
`PendingMatchNotifier` refuses when it withholds a *name* rather than an
*offer*.

An unfriending is not checked in either case, because the existing social
policy does not check one: `_still_reachable` re-reads blocks and nothing
else. Inventing a second rule here would make two social notification paths
disagree about what "may still interact" means.

## Why the other player is rendered at `STRANGER`

`GameNotificationDispatcher`'s argument, and it transfers exactly: the two
people are opponents in a game about to be played, and rendering them any
closer would show a field the subject chose to show only to friends. They
*are* friends — a challenge cannot exist otherwise — and the friends-only
fields are on the profile page. A notification is not the surface to widen
one.

It also costs one read fewer, because there is no relationship to look up.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from app.core.identifiers import generate_uuid7
from app.modules.friends.public import SocialGraphReader
from app.modules.matchmaking.public import FriendChallengeAccepted, FriendChallengeCreated
from app.modules.notifications.application.ports import DurableNotificationStore
from app.modules.notifications.domain.record import (
    CATEGORY_OF,
    ActorSummary,
    ChallengeSummary,
    NavigationTarget,
    NavigationTargetType,
    NotificationRecord,
    NotificationType,
)
from app.modules.profiles.public import ProfileRenderer, PublicProfile
from app.modules.users.public import ViewerRelationship
from app.platform.outbox import OutboxEntry

logger = logging.getLogger(__name__)

#: This consumer's own `platform.processed_event` partition.
#:
#: Separate from `social_notifications`, like every other consumer here: a
#: redelivery one has handled must still reach the others, and none may mark
#: another's work done.
CONSUMER_NAME: Final = "challenge_notifications"

#: The event types this consumer subscribes to. Built from the classes so a
#: renamed event fails to import rather than silently never matching.
SUBSCRIBED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        FriendChallengeCreated.event_type,
        FriendChallengeAccepted.event_type,
    }
)


@dataclass(frozen=True, slots=True)
class _Failed:
    """One entry this consumer could not process — `outbox.ports.EventFailure`."""

    entry_id: UUID
    reason: str


@dataclass(frozen=True, slots=True)
class _Told:
    """One resolved notification, before its subject has been rendered.

    An intermediate rather than a tuple, because the four parts are decided
    in three different places — the recipient and the subject by the event
    type, the settings by the payload, the type by both — and a tuple would
    make positional order the only thing preventing a challenger and a
    recipient being swapped.
    """

    entry: OutboxEntry
    recipient_id: UUID
    subject_id: UUID
    type: NotificationType
    challenge_id: UUID
    time_control_id: str
    variant: str
    rated: bool
    expires_at: datetime | None
    match_id: UUID | None


class ChallengeNotificationDispatcher:
    """Turns friend challenge events into durable notifications.

    Holds three published ports and nothing private to another module:
    `friends`' social graph, `profiles`' renderer, and somewhere durable to
    put the result. No repository, no session and no clock — every instant it
    needs is on the event, which is what makes it correct to run minutes
    after the fact.
    """

    def __init__(
        self,
        *,
        graph: SocialGraphReader,
        profiles: ProfileRenderer,
        store: DurableNotificationStore,
    ) -> None:
        self._graph = graph
        self._profiles = profiles
        self._store = store

    @property
    def consumer(self) -> str:
        return CONSUMER_NAME

    def handles(self, event_type: str) -> bool:
        return event_type in SUBSCRIBED_EVENT_TYPES

    async def handle(self, entries: Sequence[OutboxEntry]) -> Sequence[_Failed]:
        """Delivers a batch.

        Resolution is per entry so one malformed payload does not lose the
        rest, and the **profile render is one call for the whole batch** —
        the N+1 §24 forbids is the only performance mistake available on
        this path, and it is invisible in any test with one challenge.
        """
        failures: list[_Failed] = []
        told: list[_Told] = []

        for entry in entries:
            try:
                resolved = await self._resolve(entry)
            except Exception as error:  # noqa: BLE001 — one event must not fail the batch
                logger.warning(
                    "challenge_notification_resolution_failed",
                    extra={
                        "source_event_type": entry.event_type,
                        "error": type(error).__name__,
                    },
                )
                failures.append(_Failed(entry_id=entry.id, reason=type(error).__name__))
                continue

            if resolved is not None:
                told.append(resolved)

        if not told:
            return failures

        profiles = await self._profiles.render_many(
            sorted({item.subject_id for item in told}),
            relationship=ViewerRelationship.STRANGER,
        )

        await self._store.store([_record(item, profiles) for item in told])
        logger.info(
            "challenge_notifications_composed",
            extra={"events": len(entries), "recipients": len(told)},
        )
        return failures

    async def _resolve(self, entry: OutboxEntry) -> _Told | None:
        """Who is told about this event, and what they are told.

        `None` when a block has since removed the pair, which is the one
        case that is a rule rather than a failure — see the module docstring
        on why an acceptance has no equivalent.
        """
        payload = entry.payload
        challenger_id = _uuid(payload, "challenger_id")
        recipient_id = _uuid(payload, "recipient_id")

        if entry.event_type == FriendChallengeCreated.event_type:
            if await self._blocked(recipient_id, challenger_id):
                logger.info(
                    "challenge_notification_suppressed",
                    extra={"source_event_type": entry.event_type, "reason": "blocked"},
                )
                return None
            return _Told(
                entry=entry,
                recipient_id=recipient_id,
                subject_id=challenger_id,
                type=NotificationType.FRIEND_CHALLENGE_RECEIVED,
                challenge_id=_uuid(payload, "challenge_id"),
                time_control_id=_text(payload, "time_control_id"),
                variant=_text(payload, "variant"),
                rated=bool(payload["rated"]),
                expires_at=datetime.fromisoformat(_text(payload, "expires_at")),
                match_id=None,
            )

        # `matchmaking.friend_challenge_accepted`: the challenger is told,
        # about the recipient who accepted, and the match they now share.
        return _Told(
            entry=entry,
            recipient_id=challenger_id,
            subject_id=recipient_id,
            type=NotificationType.FRIEND_CHALLENGE_ACCEPTED,
            challenge_id=_uuid(payload, "challenge_id"),
            time_control_id=_text(payload, "time_control_id"),
            variant=_text(payload, "variant"),
            rated=bool(payload["rated"]),
            expires_at=None,
            match_id=_uuid(payload, "match_id"),
        )

    async def _blocked(self, viewer_id: UUID, other_id: UUID) -> bool:
        """Whether the pair may no longer interact, read **now**.

        One cached read (`friends:v1:blocked:`), and symmetric — the port
        returns both directions, so which id is asked about does not decide
        the answer.
        """
        return other_id in await self._graph.blocked_ids_for(viewer_id)


def _record(item: _Told, profiles: Mapping[UUID, PublicProfile]) -> NotificationRecord:
    """One durable row.

    `created_at` is the **event's** instant, never now: a relay catching up
    after an outage must not tell somebody that a challenge sent an hour ago
    has just arrived — the row would then outlive the invitation's own
    twenty-four hours by an hour without saying so.
    """
    return NotificationRecord(
        id=generate_uuid7(),
        recipient_id=item.recipient_id,
        type=item.type,
        category=CATEGORY_OF[item.type],
        payload=ChallengeSummary(
            challenge_id=item.challenge_id,
            opponent=_actor(profiles.get(item.subject_id)),
            time_control_id=item.time_control_id,
            variant=item.variant,
            rated=item.rated,
            expires_at=item.expires_at,
            match_id=item.match_id,
        ),
        target=_target_for(item),
        source_event_id=item.entry.id,
        created_at=item.entry.occurred_at,
    )


def _target_for(item: _Told) -> NavigationTarget:
    """Where tapping this notification takes the recipient — §5.

    An **acceptance opens the game**, which is the whole point of the
    notification: the match already exists, both players still have to join
    it, and the join window is ten minutes. `LIVE_GAME` is the existing
    target for exactly this and needed no change.

    A **received invitation** has nowhere of its own to go yet. A64-022.5
    owns the challenge surface, it does not exist, and a target naming a
    route that 404s would be worse than one that lands somewhere true — see
    `NavigationTargetType.FRIENDS` on why the friend list is the closest
    existing truth and what replaces it.
    """
    if item.match_id is not None:
        return NavigationTarget(type=NavigationTargetType.LIVE_GAME, ref=str(item.match_id))
    return NavigationTarget(type=NavigationTargetType.FRIENDS)


def _actor(profile: PublicProfile | None) -> ActorSummary | None:
    if profile is None:
        return None
    identity = profile.identity
    return ActorSummary(
        player_id=identity.id,
        username=identity.username,
        display_name=identity.display_name,
        avatar_object_key=identity.avatar.object_key,
        avatar_version=identity.avatar.version,
    )


def _uuid(payload: Mapping[str, Any], key: str) -> UUID:
    """A required id off a stored payload.

    A malformed payload raises, and the relay records the entry as failed —
    the correct outcome for a producer that changed its contract without
    telling this consumer.
    """
    return UUID(str(payload[key]))


def _text(payload: Mapping[str, Any], key: str) -> str:
    return str(payload[key])


__all__ = [
    "CONSUMER_NAME",
    "SUBSCRIBED_EVENT_TYPES",
    "ChallengeNotificationDispatcher",
]
