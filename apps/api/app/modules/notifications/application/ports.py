"""`notifications`' ports — AD-06.

One published-to-us contract per collaborator, and two of our own:

    PresenceWriter         what the producer needs of `users`' presence:
                           read the current record, write the new one.
                           Narrower than `PresenceService`, which is what
                           makes the transition check the only thing this
                           module can do with presence
    NotificationSink       where a rendered notification goes
    NotificationRepository where a durable notification is stored and read
                           — A64-021.1
    NotificationAnnouncer  who is told that a durable notification now
                           exists — A64-021.2
    NotificationPreferenceRepository
                           where a player's preference overrides are stored
                           — A64-021.3
    NotificationDeliveryPolicy
                           whether a notification may be delivered to this
                           recipient on this channel, asked at delivery time

Everything else this module consumes is already a published port somewhere
else — `friends.public.PresenceAudience`, `profiles.public.ProfileRenderer`,
`platform.outbox.EventPublisher` — and re-declaring them here would be a
second copy of a contract, which is the thing ports exist to prevent.

## Why `PresenceWriter` is declared here and not imported

`users.public` publishes `PresenceProvider` (read) and `PresenceRecorder`
(write), and this module needs *both* plus the guarantee that they address
the same store. `PresenceService` already composes exactly that, but it is
an application service rather than a published port — and publishing it
would hand every future consumer the ability to mark anybody online.

So the consumer declares the shape it needs (AD-06: the port belongs to the
layer that needs it), and the composition root satisfies it with
`PresenceService`. The seam costs one Protocol and buys a producer that
cannot be handed anything wider.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.notifications.application.read_models import (
    MarkReadOutcome,
    NotificationCursor,
    NotificationPage,
)
from app.modules.notifications.domain.notification import SocialNotification
from app.modules.notifications.domain.preference import (
    DeliveryChannel,
)
from app.modules.notifications.domain.record import (
    NotificationAnnouncement,
    NotificationCategory,
    NotificationRecord,
)
from app.modules.users.public import DeviceType, Presence


class PresenceWriter(Protocol):
    """Read-then-write access to one player's presence.

    Satisfied by `users.application.services.PresenceService`.

    The **read** is what makes edge detection possible, and it is the reason
    this is one port rather than two: a producer holding a reader and a
    writer that happened to address different stores would detect
    transitions against one and record them in another, which would look
    like working code and would emit an event on every refresh.
    """

    async def presence_of(self, player_id: UUID) -> Presence | None:
        """This player's current record, or `None` if there is none.

        `None` is "no observation" — never seen, or the window lapsed — and
        is treated as offline by the transition check. It is deliberately not
        distinguished from an explicit offline record: both mean the player
        is not here, and a transition into "here" is the same edge either
        way.
        """
        ...

    async def mark_online(
        self,
        player_id: UUID,
        *,
        session_id: UUID | None = None,
        device_type: DeviceType | None = None,
    ) -> None:
        """Records that the player is present and restarts their window.

        Never raises (`PresenceRecorder`'s contract): a sign-in must not fail
        because Redis was briefly unreachable.
        """
        ...

    async def mark_offline(self, player_id: UUID) -> None:
        """Records that the player has gone. Never raises."""
        ...


class NotificationSink(Protocol):
    """Where a rendered notification goes.

    The seam A64-013.7 stops at. Every transport the brief excludes —
    WebSocket, push, email, mobile — is an implementation of this, and the
    one that exists today writes a log line.

    **Batch-first**, like `EventHandler`: the dispatcher resolves an audience
    and renders once, producing many notifications at a time, and a singular
    method would be called in a loop by every implementation.

    A sink **may raise**. Unlike the presence recorder, whose failures are
    cosmetic and swallowed, a delivery that failed is a delivery the platform
    should retry — so the exception propagates to the dispatcher, which turns
    it into a recorded per-event failure and a backoff. Swallowing it here
    would mark the event published and lose the notification silently.
    """

    async def deliver(self, notifications: list[SocialNotification]) -> None:
        """Delivers a batch. An empty batch is a legal no-op."""
        ...


class NotificationRepository(Protocol):
    """Storage for the durable `Notification` — A64-021.1 §7, §9, §10.

    Declared here rather than in `infrastructure` because the port belongs
    to the layer that needs it (AD-06), and because every method below is a
    *use case's* question rather than a table's: "has this event already
    produced this notification", "what has this recipient not read".

    **Every method is recipient-scoped, without exception.** There is no
    `get(notification_id)` and there never should be: a reader that could
    fetch a row by id alone is one line away from serving somebody else's
    notification, and the recipient is not a filter applied afterwards — it
    is half the key (§30).
    """

    async def append(self, record: NotificationRecord) -> bool:
        """Stores one notification. `True` if it was written, `False` if an
        identical one already existed.

        **Not check-then-insert** (§11). The uniqueness of
        `(recipient_id, source_event_id, type)` is a database constraint and
        this is an upsert that does nothing on conflict, so two relay
        processes handling the same redelivered event concurrently produce
        one row and one `False` — rather than two winners of a race that a
        prior `SELECT` could not see.
        """
        ...

    async def list_for(
        self,
        recipient_id: UUID,
        *,
        after: NotificationCursor | None,
        limit: int,
    ) -> NotificationPage:
        """One page, newest first, keyset-ordered by `(created_at, id)` DESC."""
        ...

    async def count_unread(self, recipient_id: UUID) -> int:
        """How many of this recipient's notifications have no `read_at`.

        One bounded query against the partial index, and **no rows loaded**
        (§10): the badge must not cost a page of notifications to render.
        """
        ...

    async def mark_read(
        self, notification_id: UUID, *, recipient_id: UUID, at: datetime
    ) -> MarkReadOutcome:
        """Marks one notification read, and says what that did.

        Idempotent: an already-read notification keeps its original
        `read_at` and answers `ALREADY_READ`, because "it is read" is the
        state the caller asked for and a second click must not rewrite when
        it happened.

        `NOT_FOUND` covers both "no such notification" and "somebody
        else's", deliberately — the two are indistinguishable to a caller
        (§17), so this port cannot be used to tell them apart either.
        """
        ...

    async def mark_all_read(self, recipient_id: UUID, *, at: datetime) -> int:
        """Marks every unread notification of this recipient read. Returns
        how many changed.

        One statement over the partial index, not a read followed by writes:
        the set is bounded by what is unread, and a recipient who has ignored
        their notifications for a month must not cost a page-by-page walk.
        """
        ...


class NotificationAnnouncer(Protocol):
    """Told that notifications now exist, **after** they are durable —
    A64-021.2 §1, §5.

    The seam A64-021.1 named and left for this phase. Satisfied by
    `app.gateway.notifications.GatewayNotificationSink`, and by
    `NullNotificationAnnouncer` where there is no fleet to announce into.

    ## It is an accelerator, and the contract says so

    **Never raises.** A deliberate departure from `NotificationSink`, whose
    "a sink may raise" exists so a real delivery failure is retried. Here a
    retry would re-announce a notification the client can already read, and
    raising would fail a relay tick — undoing nothing, since the row is
    already committed, and holding up every other event in the batch.

    So every failure mode is tolerable by construction, because the durable
    answer is `GET /notifications`:

        nobody connected     the ordinary state of a player who is not
                             looking at the app
        a socket dropped     the frame is lost and the next read recovers it
        another node         forwarded through the existing bus
        the publish raised   counted, not raised

    ## Called after the commit, never before

    A64-021.1 §13: *"do not emit realtime delivery before durable
    persistence commits"*. The writer calls this once its unit of work has
    committed, with the records it **actually inserted** — so a redelivered
    event, which inserts nothing, announces nothing.
    """

    async def announce(self, announcements: Sequence[NotificationAnnouncement]) -> None:
        """Announces a batch. An empty batch is a legal no-op."""
        ...


@dataclass(frozen=True, slots=True)
class DeliveryRequest:
    """One prospective delivery: who, and about what kind of thing.

    A pair rather than two parallel sequences, so a batch cannot be
    assembled with its recipients and categories out of step — the failure
    mode of a fan-out that silently checks the wrong person's preference.
    """

    recipient_id: UUID
    category: NotificationCategory


class NotificationPreferenceRepository(Protocol):
    """Where a player's preference **overrides** are stored — A64-021.3 §6.

    Overrides, not preferences: a row exists only where somebody has
    departed from the default, so a new account has none and
    `domain.preference.effective` fills the rest in. See that module on why
    sparse.

    Every method is scoped to a user id the caller resolved from
    `CurrentUser`. There is no "read everybody's preferences" method and
    there should not be one.
    """

    async def overrides_for(
        self, user_id: UUID
    ) -> Mapping[tuple[NotificationCategory, DeliveryChannel], bool]:
        """This player's stored overrides. Empty for an untouched account."""
        ...

    async def replace(
        self,
        user_id: UUID,
        *,
        changes: Sequence[tuple[NotificationCategory, DeliveryChannel, bool]],
        at: datetime,
    ) -> None:
        """Applies every change, or none of them.

        An upsert on `(user_id, category, channel)` — **not** a delete and
        insert, and not a read followed by a write. Two tabs saving at once
        must produce one row per pair rather than a unique violation, which
        is what `ON CONFLICT DO UPDATE` gives and what a check-then-insert
        cannot (§8's "race-safe upsert").
        """
        ...

    async def permitted(
        self, requests: Sequence[DeliveryRequest], *, channel: DeliveryChannel
    ) -> frozenset[DeliveryRequest]:
        """The subset of `requests` this channel may deliver to.

        **One query for the whole batch** — §11. A friend request has one
        recipient today, but a published tournament round has as many as it
        has entrants, and a port shaped for a single lookup would make that
        an N+1 nobody notices until the first large bracket.
        """
        ...


class NotificationDeliveryPolicy(Protocol):
    """Whether a notification may be delivered — asked at **delivery** time.

    The rule `SocialNotificationDispatcher` already applies to audience and
    privacy, applied to preferences: *"re-read current state; do not trust
    enqueue-time state"*. A player who muted a category between the event
    and its delivery must not receive it, and the only way that holds is if
    the question is asked here rather than baked into the event.

    Declared in this layer because this is the layer that needs it (AD-06).
    Satisfied by `PreferenceDeliveryPolicy` over the repository above, and
    supplied by the composition root — so the durable writer never learns
    where a preference is stored, and `notifications` never reaches into
    another module for one.
    """

    async def permitted(
        self, requests: Sequence[DeliveryRequest], *, channel: DeliveryChannel
    ) -> frozenset[DeliveryRequest]:
        """The subset that may be delivered. An empty input is a legal no-op."""
        ...
