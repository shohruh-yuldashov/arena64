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

from app.core.enums import Locale
from app.modules.notifications.application.read_models import (
    MarkReadOutcome,
    NotificationCursor,
    NotificationPage,
)
from app.modules.notifications.domain.email import RenderedEmail
from app.modules.notifications.domain.email_delivery import EmailDeliveryOutcome
from app.modules.notifications.domain.notification import SocialNotification
from app.modules.notifications.domain.preference import (
    DeliveryChannel,
)
from app.modules.notifications.domain.push_delivery import PushDeliveryOutcome
from app.modules.notifications.domain.record import (
    NotificationAnnouncement,
    NotificationCategory,
    NotificationRecord,
    NotificationType,
)
from app.modules.notifications.domain.subscription import PushSubscription
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


class DurableNotificationStore(Protocol):
    """Where a producer that composed its own records puts them —
    A64-021.4 §11.

    The narrower half of `DurableNotificationWriter`, published as a port so
    the tournament and game dispatchers depend on *"somewhere durable"*
    rather than on the class. What they must not be able to do is write a
    row without the preference check, the transaction and the announcement
    that come with it — and a port with exactly one method is the shape that
    makes bypassing it a visible decision rather than a shortcut.

    **May raise**, like `NotificationSink` and for the same reason: a record
    that could not be stored is one the platform promised would exist, so it
    propagates to the dispatcher, which reports the event as failed and lets
    the relay retry it.
    """

    async def store(self, records: Sequence[NotificationRecord]) -> None:
        """Persists a batch, minus whatever preferences suppress. An empty
        batch is a legal no-op."""
        ...


class NotificationEmailRenderer(Protocol):
    """Turns one notification into a message — A64-021.5 §13.

    A port, and the layer rule is why: rendering a subject line in a
    language is `presentation`'s work, and `notifications layers point
    inward` forbids the delivery service from importing it. So the service
    holds this and the composition root supplies `TemplateEmailRenderer`.

    That is not bookkeeping. The renderer holds the configured public origin,
    which is process configuration — a service that imported the templates
    directly would have to thread the origin through every call, and the
    caller that forgot would send links to nowhere.
    """

    def render(self, record: NotificationRecord, *, locale: Locale) -> RenderedEmail:
        """The message for this notification, in this language.

        **Raises** for a type or locale it has no template for, rather than
        returning `None`: reaching it means `supports_email` and the template
        set disagreed, which is a defect and not an outcome.
        """
        ...


@dataclass(frozen=True, slots=True)
class DueEmailDelivery:
    """One delivery a worker has claimed — A64-021.5 §9.

    Everything the worker needs to decide *whether* to send, without reading
    the notification: the recipient whose address and preference to check,
    and the type whose template and category it needs. The notification
    itself is loaded only for the ones that survive those checks, which is
    what keeps a batch of muted recipients cheap.
    """

    notification_id: UUID
    recipient_id: UUID
    notification_type: NotificationType
    attempt_count: int


class EmailDeliveryRepository(Protocol):
    """Storage for the email a notification is owed — A64-021.5 §9, §10, §19.

    Declared here because the port belongs to the layer that needs it
    (AD-06), and because every method is a *use case's* question rather than
    a table's: "what is due", "this one is done", "how much is outstanding".
    """

    async def enqueue(self, deliveries: Sequence[DueEmailDelivery], *, at: datetime) -> int:
        """Records that these notifications are owed an email.

        Returns how many rows were **inserted**. `ON CONFLICT DO NOTHING`
        against the notification's own id, so a redelivered source event
        enqueues nothing — the idempotency §10 asks for, as a constraint
        rather than a check somebody remembered to write.

        Called inside the notification's own transaction, so the intent to
        email is exactly as durable as the record it describes.
        """
        ...

    async def claim_due(self, *, now: datetime, limit: int) -> list[DueEmailDelivery]:
        """Takes up to `limit` deliveries that are due, marking them claimed.

        Claimed rather than merely read: two workers polling the same table
        must not both send the same message, and a `SELECT` followed by an
        `UPDATE` is a race however small the window looks.
        """
        ...

    async def reclaim_stale(self, *, before: datetime, at: datetime) -> int:
        """Returns claims that were never resolved to the pending pool.

        A worker that died between a claim and its result left a delivery
        owed and invisible. Without this it stays that way forever and
        nothing reports it — which is the silent failure a claim-based queue
        has instead of a lost message.
        """
        ...

    async def record(
        self,
        notification_id: UUID,
        *,
        outcome: EmailDeliveryOutcome,
        at: datetime,
        next_attempt_at: datetime | None = None,
        provider_message_id: str | None = None,
    ) -> None:
        """Writes how one attempt ended.

        `next_attempt_at` is set only for a retryable outcome and cleared
        otherwise, so the partial index the claim reads holds exactly the
        rows that are still owed.
        """
        ...

    async def counts_by_status(self) -> Mapping[str, int]:
        """How many deliveries sit in each status — §21's diagnostics.

        Aggregate only. There is no method here that returns a recipient or
        an address, which is what makes the operator surface safe to expose:
        it can report that eleven deliveries are failing and cannot say to
        whom.
        """
        ...


@dataclass(frozen=True, slots=True)
class DuePushDelivery:
    """One device's delivery a worker has claimed — A64-021.6 §10.

    Everything the worker needs to decide *whether* to send, without reading
    the notification: whose preference to check, which type it is, and which
    subscription to encrypt for. The notification itself is never loaded —
    unlike email, whose body is rendered from it — because a push payload is
    two identifiers this row already carries.
    """

    notification_id: UUID
    subscription_id: UUID
    recipient_id: UUID
    notification_type: NotificationType
    attempt_count: int


class PushSubscriptionRepository(Protocol):
    """Storage for browsers that asked to be notified — A64-021.6 §2, §3, §9.

    **No method takes an endpoint from a caller as a lookup key.** An
    endpoint is a bearer capability, and a repository that could be asked
    "who owns this endpoint" is one call away from an enumeration surface.
    `register` takes one because the browser that issued it is submitting
    it; nothing reads by one.
    """

    async def register(self, subscription: PushSubscription) -> PushSubscription:
        """Stores a browser's subscription, or takes over an existing one.

        `ON CONFLICT (endpoint) DO UPDATE`, which is the §23 ownership rule
        as a single statement: a browser re-subscribing keeps working, and
        one that now belongs to a different account is **re-bound** rather
        than duplicated or refused. Returns the stored row, whose `id` may
        be the pre-existing one.

        An upsert rather than read-then-write, so two tabs registering
        concurrently produce one row rather than a race whose loser gets a
        unique-violation.
        """
        ...

    async def live_for(self, user_id: UUID) -> list[PushSubscription]:
        """Every browser this account can currently be reached on.

        One indexed read per notification rather than a lookup per device —
        §9's "avoid N+1" is this method's signature, not a caching layer.
        """
        ...

    async def live_for_many(
        self, user_ids: Sequence[UUID]
    ) -> Mapping[UUID, list[PushSubscription]]:
        """The same, for a batch of recipients, in **one** query.

        The fan-out read. A tournament round publishes to a hundred and
        twenty-eight players at once, and calling `live_for` in a loop
        inside the notification's transaction would be a hundred and
        twenty-eight round trips holding a write lock — §27's N+1, in the
        one place it would actually hurt.

        Recipients with no live subscription are **absent** from the map
        rather than present with an empty list, so a caller iterating it
        touches only accounts that can be reached.
        """
        ...

    async def get_for(self, subscription_id: UUID, *, user_id: UUID) -> PushSubscription | None:
        """One subscription, **scoped to its owner**.

        The `user_id` is not a filter applied afterwards; it is half the
        question. A lookup by id alone would serve one account another's
        capability, which is exactly what §25 forbids.
        """
        ...

    async def revoke(self, subscription_id: UUID, *, at: datetime) -> bool:
        """Marks one subscription undeliverable. `True` if it was live.

        Not a delete — see `domain.subscription.PushSubscription.revoked_at`
        on why the row survives. Idempotent: revoking an already-revoked
        subscription is `False` and not an error, because the caller's
        intent ("this must not be deliverable") already holds.
        """
        ...

    async def revoke_by_endpoint(self, endpoint: str, *, user_id: UUID, at: datetime) -> bool:
        """Revokes the caller's own subscription for one endpoint.

        The one method that takes an endpoint, and it is scoped to the
        owner: it serves *"this browser is signing out"*, where the browser
        knows its own endpoint and the session says whose it is. A caller
        cannot revoke somebody else's device by guessing a URL.
        """
        ...


class PushDeliveryRepository(Protocol):
    """Storage for the pushes a notification is owed — A64-021.6 §10, §19.

    The email repository's shape, with one difference that propagates
    everywhere: a row is `(notification_id, subscription_id)`, because one
    notification is owed one push **per device**.
    """

    async def enqueue(self, deliveries: Sequence[DuePushDelivery], *, at: datetime) -> int:
        """Records that these notifications are owed a push, per device.

        Returns how many rows were **inserted**. `ON CONFLICT DO NOTHING` on
        the pair, so a redelivered source event enqueues nothing — §19's
        idempotency as a constraint rather than a check somebody remembered.

        Called inside the notification's own transaction, so the intent to
        push is exactly as durable as the record it describes.
        """
        ...

    async def claim_due(self, *, now: datetime, limit: int) -> list[DuePushDelivery]:
        """Takes up to `limit` deliveries that are due, marking them claimed.

        Claimed rather than merely read: two workers polling the same table
        must not both push the same message to the same device.
        """
        ...

    async def reclaim_stale(self, *, before: datetime, at: datetime) -> int:
        """Returns claims that were never resolved to the pending pool.

        A worker that died between a claim and its result left a delivery
        owed and invisible; without this it stays that way forever.
        """
        ...

    async def record(
        self,
        notification_id: UUID,
        subscription_id: UUID,
        *,
        outcome: PushDeliveryOutcome,
        at: datetime,
        next_attempt_at: datetime | None = None,
    ) -> None:
        """Writes how one device's attempt ended.

        `next_attempt_at` is set only for a retryable outcome and cleared
        otherwise, so the partial index the claim reads holds exactly the
        rows that are still owed.
        """
        ...

    async def counts_by_status(self) -> Mapping[str, int]:
        """How many deliveries sit in each status — the operator diagnostic.

        Aggregate only. Nothing here returns a recipient, a subscription or
        an endpoint, which is what makes the surface safe to expose: it can
        report that eleven pushes are failing and cannot say to whom.
        """
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

    async def for_recipient(
        self, notification_id: UUID, *, recipient_id: UUID
    ) -> NotificationRecord | None:
        """One notification, **scoped to its recipient** — A64-021.5.

        Added for the email worker, which has to render a message from the
        record a delivery row names. Recipient-scoped like every other method
        here and for the same reason: A64-021.1 §30 makes "there is no
        `get(id)`" a structural property, and a worker that could read any
        notification by id would be the first hole in it.

        The scoping is free at the call site — a delivery row carries the
        recipient — and it means a delivery row whose recipient was somehow
        wrong renders nothing rather than somebody else's notification.
        """
        ...

    async def count_unread(self, recipient_id: UUID) -> int:
        """How many of this recipient's notifications have no `read_at`.

        One bounded query against the partial index, and **no rows loaded**
        (§10): the badge must not cost a page of notifications to render.
        """
        ...

    async def target_refs_for(self, claims: Sequence[tuple[UUID, UUID]]) -> Mapping[UUID, str]:
        """The navigation `ref` of each named notification, by notification
        id — A64-022.4 §10.

        Added for the push worker, whose payload now carries the identifier
        its click target needs. **Batch-only**, which is the point: the
        alternative was `for_recipient` in a loop, and that is one query per
        device per notification on the one path a tournament fans out
        across.

        `claims` are `(notification_id, recipient_id)` pairs, because
        recipient scoping is half the key here as it is on every other
        method (§30) — a delivery row whose recipient was somehow wrong
        contributes no key rather than somebody else's destination.

        A notification whose target carries no identifier is **absent from
        the result** rather than present with `None`, so a caller reads one
        shape: a key exists exactly when there is a ref to send.
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


class NotificationRetentionStore(Protocol):
    """The four bounded deletes retention needs — A64-028.7, closing P2-7.

    A port of its own rather than four methods added to the repositories
    above, because retention is not something any of them do: they read and
    write one aggregate for a request, and this deletes rows nobody is
    looking at on a schedule. `CLAUDE.md` §2.1's single responsibility, and
    the same split `OutboxRepository`/`OutboxPruner` already makes.

    Every method takes a `limit` and returns how many rows it removed. The
    caller stops when a batch comes back short, which is how a first run
    against years of history stays bounded.
    """

    async def delete_notifications(self, *, before: datetime, limit: int) -> int:
        """Notifications created before the horizon.

        Deleted **after** their delivery rows — there is no foreign key
        between them, so a notification removed first leaves orphans nothing
        else would ever clean up.
        """
        ...

    async def delete_email_deliveries(self, *, before: datetime, limit: int) -> int:
        """Email delivery audit rows past their own, shorter horizon."""
        ...

    async def delete_push_deliveries(self, *, before: datetime, limit: int) -> int:
        """Push delivery audit rows past their own, shorter horizon."""
        ...

    async def delete_revoked_subscriptions(self, *, before: datetime, limit: int) -> int:
        """Push subscriptions revoked before the horizon.

        Only revoked ones. A live subscription is current state and has no
        horizon: a player who has not opened the site for a year still
        expects their notifications when they do.
        """
        ...
