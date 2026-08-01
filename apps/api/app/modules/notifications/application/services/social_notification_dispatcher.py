"""`SocialNotificationDispatcher` — the outbox's one consumer, and the place
every rule A64-013.7 states about *delivery* is enforced.

An `EventHandler` (`platform.outbox.ports`), so the relay routes to it and
the ledger keeps it idempotent. Its whole job, per batch:

    1. group the batch by event type
    2. resolve who may be told — **now**, not at enqueue
    3. render the subject through `PublicProfileComposer` — once per
       audience, not once per recipient
    4. hand the result to the sink

## Re-reading, and why it is the point rather than an optimisation gap

"Before delivering every notification: re-read current relationship state.
Do NOT trust enqueue-time state."

Everything about who receives a notification is resolved here, seconds or
minutes after the event was recorded, because that interval is exactly where
the interesting cases live. A player blocked between an accept and its
delivery must not be told; a friendship ended in the same window must not
produce a notification about a friend who is no longer one. The event
payload deliberately carries nothing but identity so that there is no stale
copy of the answer to compete with the live read.

## Audience membership is not permission

`PresenceAudience.observers_of` guarantees exactly one thing: nobody in the
set is blocked. It says nothing about whether a given recipient may *see*
the field being pushed — that is `VisibilityLevel` against
`ViewerRelationship`, and only `PublicProfileComposer` applies it.

So every payload here is rendered through `ProfileRenderer`, which cannot
skip the gate. A subject who set `online_status: nobody` produces a
`PublicProfile` with no presence in it, delivered to friends who learn
nothing they should not — and a subject with no public profile at all
(deactivated between event and delivery) produces no notification, because
the renderer omits them.

## Who receives what, and the three events that notify nobody

    friend_request_accepted  the **requester**. The addressee performed the
                             acceptance; telling them what they just did is
                             not a notification
    presence_online          the subject's audience — friends minus blocked
    presence_offline         the same audience
    friend_removed           **nobody**. FS-2 makes removal unilateral and
                             silent: "the other person is not told" is the
                             rule, and a notification would be the one way
                             to break it
    player_blocked           **nobody**. BL-1 keeps a block invisible to its
                             subject; a notification would be an invitation
                             to retaliate from a second account
    player_unblocked         **nobody**. A lifted block is as invisible as
                             the block was — telling somebody they have been
                             unblocked tells them they were blocked

Those three are still *subscribed to*, not merely ignored, and the
distinction matters: they are marked processed in this consumer's ledger, so
the record says this consumer considered them and delivered nothing. An
event nobody subscribed to would be indistinguishable from one a consumer
silently dropped.

## Failure is per event

`handle` returns the entries it could not process rather than raising, so
one player whose profile read fails does not hold back the rest of a batch.
See `OutboxRelay` on what the relay does with the return value.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from app.modules.friends.public import FriendRemoved as FriendRemovedEvent
from app.modules.friends.public import (
    FriendRequestAccepted,
    PlayerBlocked,
    PlayerUnblocked,
    PresenceAudience,
    SocialGraphReader,
)
from app.modules.notifications.application.ports import NotificationSink
from app.modules.notifications.domain.notification import NotificationKind, SocialNotification
from app.modules.profiles.public import ProfileRenderer
from app.modules.users.public import PresenceOffline, PresenceOnline, ViewerRelationship
from app.platform.outbox import OutboxEntry

logger = logging.getLogger(__name__)

#: This consumer's name in `platform.processed_event`. A constant rather than
#: a literal at the two sites that use it, because renaming it re-delivers
#: every retained event — see `EventHandler.consumer`.
CONSUMER_NAME = "social_notifications"

#: The event types this consumer subscribes to. Built from the classes rather
#: than from strings, so a renamed event fails to import instead of silently
#: never matching.
#:
#: Public because the relay asks `handles()` per entry and the adapter that
#: scopes this dispatcher to a session must answer without building one —
#: constructing repositories to evaluate a set membership test would open a
#: connection for every event the consumer does not want.
SUBSCRIBED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        FriendRequestAccepted.event_type,
        FriendRemovedEvent.event_type,
        PlayerBlocked.event_type,
        PlayerUnblocked.event_type,
        PresenceOnline.event_type,
        PresenceOffline.event_type,
    }
)

#: Event type -> what a recipient is told. Absent means "notifies nobody",
#: which is a rule rather than an omission — see this module's docstring.
_KINDS: Mapping[str, NotificationKind] = {
    FriendRequestAccepted.event_type: NotificationKind.FRIEND_REQUEST_ACCEPTED,
    PresenceOnline.event_type: NotificationKind.FRIEND_ONLINE,
    PresenceOffline.event_type: NotificationKind.FRIEND_OFFLINE,
}


@dataclass(frozen=True, slots=True)
class _Failed:
    """One entry this consumer could not process — `outbox.ports.EventFailure`."""

    entry_id: UUID
    reason: str


@dataclass(frozen=True, slots=True)
class _Audience:
    """One event's resolved delivery: who, about whom, as what.

    An intermediate rather than a tuple, because the three parts are decided
    in different places — recipients by the social graph, the subject by the
    payload, the relationship by the event type — and a tuple would make the
    order of those three the only thing preventing a mix-up.
    """

    entry: OutboxEntry
    subject_id: UUID
    recipient_ids: frozenset[UUID]
    kind: NotificationKind
    relationship: ViewerRelationship


class SocialNotificationDispatcher:
    """Turns social events into delivered notifications.

    Holds four published ports and nothing private to another module: the
    social graph (`friends`), the presence audience (`friends`), the profile
    renderer (`profiles`) and the sink. It has no repository, no session and
    no clock — every instant it needs is on the event, which is what makes it
    correct to run minutes after the fact.
    """

    def __init__(
        self,
        *,
        audience: PresenceAudience,
        graph: SocialGraphReader,
        profiles: ProfileRenderer,
        sink: NotificationSink,
    ) -> None:
        self._audience = audience
        self._graph = graph
        self._profiles = profiles
        self._sink = sink

    @property
    def consumer(self) -> str:
        return CONSUMER_NAME

    def handles(self, event_type: str) -> bool:
        return event_type in SUBSCRIBED_EVENT_TYPES

    async def handle(self, entries: Sequence[OutboxEntry]) -> Sequence[_Failed]:
        """Delivers a batch. Returns the entries that failed, or empty.

        Resolution is per event and rendering is per *audience*, which is
        what keeps this batch-shaped: an event with fifty recipients costs
        one audience read and one render, not fifty of each. Two events about
        the same subject still cost two renders — deduplicating those would
        mean holding one recipient's view across two subjects' privacy
        settings, and the saving is not worth a shared mutable render cache
        in a consumer that must be provably correct about permissions.
        """
        failures: list[_Failed] = []
        delivered = 0

        for entry in entries:
            try:
                audience = await self._resolve(entry)
            except Exception as error:  # noqa: BLE001 — one event must not fail the batch
                logger.warning(
                    "notification_resolution_failed",
                    extra={
                        "event_id": str(entry.id),
                        "event_type": entry.event_type,
                        "error": type(error).__name__,
                    },
                )
                failures.append(_Failed(entry.id, type(error).__name__))
                continue

            if audience is None or not audience.recipient_ids:
                # Nobody to tell. Counted as processed, not as failed — see
                # this module's docstring on the three events that notify
                # nobody by rule.
                continue

            try:
                delivered += await self._deliver(audience)
            except Exception as error:  # noqa: BLE001 — a failed delivery is retried, not lost
                logger.warning(
                    "notification_delivery_failed",
                    extra={
                        "event_id": str(entry.id),
                        "event_type": entry.event_type,
                        "error": type(error).__name__,
                    },
                )
                failures.append(_Failed(entry.id, type(error).__name__))

        logger.info(
            "event_processed",
            extra={
                "consumer": CONSUMER_NAME,
                "event_count": len(entries),
                "notifications_delivered": delivered,
                "failed": len(failures),
            },
        )
        return failures

    async def _resolve(self, entry: OutboxEntry) -> _Audience | None:
        """Who may be told about this event, read **now**.

        `None` for every event that notifies nobody, which is three of the
        six and is a domain rule rather than a gap.
        """
        kind = _KINDS.get(entry.event_type)
        if kind is None:
            return None

        if entry.event_type in (PresenceOnline.event_type, PresenceOffline.event_type):
            subject_id = UUID(str(entry.payload["player_id"]))
            # The live audience: friends minus blocked, both directions, read
            # at delivery. Every member is a friend by construction, which is
            # what lets one render serve the whole set.
            return _Audience(
                entry=entry,
                subject_id=subject_id,
                recipient_ids=await self._audience.observers_of(subject_id),
                kind=kind,
                relationship=ViewerRelationship.FRIEND,
            )

        # `friends.friend_request_accepted`: the requester is told, about the
        # addressee who accepted.
        requester_id = UUID(str(entry.payload["requester_id"]))
        addressee_id = UUID(str(entry.payload["addressee_id"]))
        return _Audience(
            entry=entry,
            subject_id=addressee_id,
            recipient_ids=await self._still_reachable(addressee_id, requester_id),
            kind=kind,
            relationship=ViewerRelationship.FRIEND,
        )

    async def _still_reachable(self, subject_id: UUID, recipient_id: UUID) -> frozenset[UUID]:
        """The recipient, unless the pair has been blocked since the event.

        The named-recipient counterpart to `observers_of`, and the reason it
        exists at all: an acceptance names its recipient, so there is no
        audience to filter — but the block that arrived in the meantime must
        still remove them. Skipping this because "the payload already says
        who" is exactly the enqueue-time trust A64-013.7 forbids.

        One read, and it is the cached one (`friends:v1:blocked:`), so this
        costs nothing on the hot path a busy relay creates.
        """
        blocked = await self._graph.blocked_ids_for(subject_id)
        if recipient_id in blocked:
            logger.info(
                "notification_suppressed_blocked",
                extra={"reason": "blocked", "subject_id": str(subject_id)},
            )
            return frozenset()
        return frozenset({recipient_id})

    async def _deliver(self, audience: _Audience) -> int:
        """Renders once and delivers to everyone. Returns how many were sent.

        **One render per event**, not per recipient: every member of the
        audience stands in the same `ViewerRelationship` to the subject, and
        `PublicProfile` is a function of (identity, relationship) — so the
        rendered view is identical for all of them. That is what makes the
        privacy gate free to apply here rather than a per-recipient cost the
        design would be tempted to skip.

        An absent render means the subject has no public profile any more —
        deactivated between event and delivery — and produces no
        notification rather than an empty one.
        """
        rendered = await self._profiles.render_many(
            [audience.subject_id], relationship=audience.relationship
        )
        subject = rendered.get(audience.subject_id)
        if subject is None:
            logger.info(
                "notification_suppressed_no_profile",
                extra={"event_id": str(audience.entry.id)},
            )
            return 0

        notifications = [
            SocialNotification(
                event_id=audience.entry.id,
                recipient_id=recipient_id,
                kind=audience.kind,
                subject=subject,
                occurred_at=_as_utc(audience.entry.occurred_at),
            )
            for recipient_id in sorted(audience.recipient_ids)
        ]
        await self._sink.deliver(notifications)
        return len(notifications)


def _as_utc(instant: datetime) -> datetime:
    """The event's instant, guaranteed timezone-aware (DM-14).

    `UtcDateTime` stores and returns aware datetimes, so this is a belt on
    top of braces — but a naive instant reaching a client renders as local
    time in whatever zone the client guesses, which is the class of bug that
    is only ever noticed by somebody in the wrong hemisphere.
    """
    return instant if instant.tzinfo is not None else instant.replace(tzinfo=UTC)
