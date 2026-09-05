"""The `notifications` ORM model — `notifications.notification`.

Owned exclusively by this module (database.md DB-03/DB-04), in its own
schema, and carrying **no foreign key to `users`**: cross-context references
are opaque `player_id` values (DM-06), and a foreign key here would make the
two schemas undeployable apart.

## What this table is

A **recipient-owned projection** (database.md C5-adjacent): every row is
derived from an event that is itself durable in `platform.outbox`, nothing
here is the system of record for anything, and deleting the whole relation
loses history rather than truth. That is what makes append-only acceptable
for now (§14) and what would make a rebuild possible if one were ever
needed.

## Three documented deviations from database.md §10.2

The document specifies `template_key`, `params`, `event_id`,
`correlation_id`, `dismissed_at` and `expires_at`. A64-021.1 names the first
three differently and excludes the last three, and §10.2 is updated in the
same change (CLAUDE.md §3.11).

**1. `type`, `payload`, `source_event_id`** — the task's names for
`template_key`, `params` and `event_id`. Same columns, same meanings; the
names are the ones the API, the frontend and the tests all use, and three
spellings of one concept is worse than one deviation.

**2. No `correlation_id`.** The outbox row named by `source_event_id`
carries one, so a second copy here would be a column nothing reads and
nothing maintains — which looks maintained, and is the failure
`statistics.player_statistics` documents for `source_watermark`.

**3. No `dismissed_at`, no `expires_at`.** Dismissal is a second read-state
with no product rule and no control to set it. `expires_at` serves NT-3's
*delivery* staleness horizon, and there is no delivery channel yet: added
now it would be a horizon nothing enforces. Both are additive.

## The unique key is the exactly-once guarantee — §11

`(recipient_id, source_event_id, type)`. Narrower than §10.2's
`(recipient_id, event_id, category)` on purpose: `category` groups several
types, so keying on it would let one event produce a `friend_request_received`
and then silently refuse a genuinely different social notification from the
same event. The type is the finest grain at which "the same notification"
is still the same notification.

Structural rather than checked: `append` is an upsert that does nothing on
conflict, so a redelivered event, a restarted relay and two concurrent
consumer processes all converge on one row without any of them reading
first.

## Indexes

`ix_notification__recipient_recent` serves the list (`(created_at, id)` DESC
keyset) and nothing else. `ix_notification__recipient_unread` is **partial**
on `read_at IS NULL`, which is database.md §12.7's Q9 index and the reason
the badge is an index-only scan: it shrinks as a player reads, so the
common case — a caught-up player — costs almost nothing to count.
"""

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    Boolean,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.mixins.uuid_pk import UUIDPrimaryKeyMixin
from app.database.types import UtcDateTime

NOTIFICATIONS_SCHEMA: Final = "notifications"

#: The unique constraint's name, referenced by the repository's `ON CONFLICT`
#: clause. A constant so the migration, the model and the upsert cannot drift
#: — a renamed constraint would turn every duplicate into an `IntegrityError`
#: at the exact moment a redelivery happened.
NOTIFICATION_SOURCE_UNIQUE: Final = "uq_notification__recipient_source_type"

#: The broadcast idempotency constraint's name, referenced by the
#: repository's `ON CONFLICT`. A constant for the same reason as above.
BROADCAST_IDEMPOTENCY_UNIQUE: Final = "uq_notification_broadcast__admin_key"


class NotificationModel(UUIDPrimaryKeyMixin, Base):
    """One durable notification — `domain.record.NotificationRecord`.

    Composes `UUIDPrimaryKeyMixin` and **not** `TimestampMixin`: `created_at`
    here is when the notification came into being, which the writer supplies
    from the source event's own instant, and an `updated_at` would record
    when it was read — which `read_at` already says, better.
    """

    __tablename__ = "notification"

    __table_args__ = (
        UniqueConstraint(
            "recipient_id",
            "source_event_id",
            "type",
            name=NOTIFICATION_SOURCE_UNIQUE,
        ),
        # The list query, exactly: one recipient's rows, newest first.
        # `created_at` leads and `id` breaks ties, which is the keyset the
        # cursor encodes — an index in a different order would still answer
        # the query and would sort every time.
        Index(
            "ix_notification__recipient_recent",
            "recipient_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        # A64-024.7. The admin console's default page is every notification,
        # newest first — an ordering the index above cannot serve, because it
        # leads with `recipient_id`. Without this the first page of the
        # operations console sorts the whole relation.
        Index(
            "ix_notification__created_at_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        # database.md §12.7's Q9. Partial, so it holds only what is unread
        # and shrinks as a player catches up — which is what makes the badge
        # count cheap for the player who checks it most often.
        Index(
            "ix_notification__recipient_unread",
            "recipient_id",
            postgresql_where=text("read_at IS NULL"),
        ),
        {"schema": NOTIFICATIONS_SCHEMA},
    )

    recipient_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    """Whose notification this is. Opaque `player_id` — DM-06."""

    type: Mapped[str] = mapped_column(String(64), nullable=False)
    """`domain.record.NotificationType`. Stored as text rather than as a
    PostgreSQL enum: adding a notification type must be a code change and a
    migration of *rows*, never an `ALTER TYPE` that locks the relation."""

    category: Mapped[str] = mapped_column(String(32), nullable=False)
    """`domain.record.NotificationCategory`. Denormalised from the type
    rather than derived at read time, because it is what a future
    per-category preference query filters on — and a filter over a value
    computed in Python cannot use an index."""

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """The typed payload, as `domain.record.payload_as_json` writes it.

    `JSONB` and not columns, because the shape is per type and there will be
    more types: a column per field of every future payload is a table that
    is mostly `NULL`. It is not free-form — `payload_of` decodes it against
    the row's own `type` and refuses a row that does not match."""

    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    """Where tapping this goes, as a closed type plus at most one safe
    identifier. **Never a URL** — see `domain.record.NavigationTargetType`."""

    source_event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    """The `platform.outbox` row that caused this. No foreign key: the outbox
    is retention-pruned and this must outlive it, which is the same reasoning
    `platform.processed_event` uses for the same reference."""

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    """When the notified fact happened — the source event's `occurred_at`,
    not the insert time. A relay catching up after an outage must not tell a
    player that a week-old friend request arrived just now."""

    read_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    """When the recipient read it, or `NULL`. Server time, set once: a
    second mark-read keeps the original instant."""


class NotificationBroadcastModel(UUIDPrimaryKeyMixin, Base):
    """One administrative announcement — `domain.broadcast.Broadcast`.

    The **system of record**, unlike `NotificationModel` above: nothing else
    holds what an administrator sent, so this table is the answer to "who
    told the platform that, and when". §23 requires that answer to survive
    the notifications it produced.

    ## Why the recipients are a column and not a table

    `SPECIFIC_PLAYERS` carries at most `MAX_NAMED_RECIPIENTS` ids and the
    list is written once and read once. A join table would be the right
    shape for an unbounded audience that is queried, filtered and reported
    on; for a bounded array nothing indexes, it would be a second table to
    migrate, to clean up and to keep in step for no query it makes possible.

    `ALL_PLAYERS` stores nothing here at all — the audience is resolved by
    the expander at delivery time, which is what makes an account created
    mid-broadcast either included or not on a rule rather than by accident.
    """

    __tablename__ = "notification_broadcast"

    __table_args__ = (
        # A double-submitted composer is the same broadcast, not a second
        # one. Scoped to the administrator so two of them cannot collide on
        # a client-generated value.
        UniqueConstraint(
            "created_by",
            "idempotency_key",
            name=BROADCAST_IDEMPOTENCY_UNIQUE,
        ),
        # The worker's claim query: the oldest unfinished broadcast.
        # Partial, so the index holds only work and shrinks to nothing when
        # there is none.
        Index(
            "ix_notification_broadcast__pending",
            "created_at",
            postgresql_where=text("status IN ('queued', 'sending')"),
        ),
        # The console's history: newest first, over everything.
        Index(
            "ix_notification_broadcast__created_at_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        {"schema": NOTIFICATIONS_SCHEMA},
    )

    title: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(String(600), nullable=False)

    locale: Mapped[str] = mapped_column(String(8), nullable=False)
    """Which language the administrator wrote in. Not a translation key —
    nothing translates this text; a client marks it with `lang` so a screen
    reader pronounces it correctly."""

    audience: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    """The administrator. Opaque `player_id` — DM-06, no foreign key."""

    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)

    recipients: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    audience_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """`NULL` until counted. Never a zero standing in for "not counted yet",
    which would read as a broadcast that reached nobody."""

    delivered: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)

    failure_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    cursor: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    """The last recipient written. A keyset rather than an offset: accounts
    are created while a broadcast is delivering, and an offset would skip or
    repeat them."""


class NotificationPreferenceModel(Base):
    """The `notifications.notification_preference` row — A64-021.3 §6.

    **One row per override, not per player.** A player who has never opened
    the settings screen has none, and `domain.preference.effective` resolves
    their whole matrix from the defaults. Materialising sixteen rows per
    account would make every future category or channel a data migration
    over every user, and would lose the distinction between "chose this" and
    "never looked" — the question a later change of default has to ask.

    ## The key is the whole identity

    `PRIMARY KEY (user_id, category, channel)`, with no surrogate. There is
    exactly one preference per triple and it has no identity apart from
    which one it is; a UUID here would be a second key nothing joins on —
    the same reasoning `statistics.player_statistics` records for
    `player_id`.

    It is also what makes the write race-safe: `ON CONFLICT DO UPDATE`
    against this key means two tabs saving at once produce one row rather
    than a unique violation, without anybody reading first (§8).

    ## No foreign key to `users`

    `user_id` is an opaque player id (DM-06), like every cross-context
    reference on this platform. `database.md` §4.9 specified `FK, cascade`
    when it placed this table in the `users` schema; here that would be a
    cross-schema foreign key of exactly the kind DB-03 forbids, and it would
    make the two schemas undeployable apart.

    The cascade it bought is worth naming: deleting an account leaves these
    rows behind. They are preferences about notifications that no longer
    have a recipient, they are keyed on an id that will never be reissued,
    and account deletion is `users`' erasure path to clean up — not a
    foreign key that decides another schema's retention.

    ## Text, not a PostgreSQL enum

    Adding a category or a channel must be a code change and a migration of
    *rows*, never an `ALTER TYPE` that locks the relation — the same choice
    `notification.type` made and for the same reason. The vocabulary is
    enforced by `domain.preference` on the way in and on the way out.
    """

    __tablename__ = "notification_preference"

    __table_args__ = (
        PrimaryKeyConstraint("user_id", "category", "channel", name="pk_notification_preference"),
        # The delivery-time question, exactly: "for these recipients, on this
        # channel, what did they choose?" `channel` leads because a fan-out
        # asks about one channel at a time and many recipients at once, so a
        # `user_id`-first index would be one probe per recipient where this
        # is one range scan — §11's "future fan-out must not force an N+1".
        Index("ix_notification_preference__channel_user", "channel", "user_id"),
        {"schema": NOTIFICATIONS_SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    """The player's answer. Only ever the *opposite* of the default in
    practice, but stored as the value rather than as a flag: a default that
    changes later must not silently flip everybody who had agreed with the
    old one."""

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    """When this override was last written. `TimestampMixin` is deliberately
    not composed: it brings a surrogate-key-shaped set of defaults this
    relation does not want, and both instants here are supplied by the
    service from one clock read so a create and its update agree."""


class NotificationEmailDeliveryModel(Base):
    """The `notifications.notification_email_delivery` row — A64-021.5 §9, §19.

    One row per notification that is **owed** an email, written in the same
    transaction as the notification itself. Not one per attempt: §10 is
    explicit that a retry reuses the record, and a row per attempt would make
    "has this been sent" a `MAX(...)` over a history nobody reads.

    ## The primary key is the notification

    `notification_id`, and nothing else. A notification belongs to exactly
    one recipient (that is what makes the durable row a row) and email is one
    channel, so `(notification_id, channel)` and
    `(notification_id, recipient_id, channel)` would both be the same key
    with columns that cannot vary. Adding them would invite a second row for
    the same message the day somebody passed a different channel.

    It is also the idempotency §10 asks for, structurally: enqueueing is
    `INSERT ... ON CONFLICT DO NOTHING`, so a redelivered source event that
    inserts no notification inserts no delivery either, and one that somehow
    tried twice converges on one row without reading first.

    ## No foreign key to `notification`

    Deliberate, and not for the cross-schema reason the other tables give —
    both live here. It is retention: `notification` is append-only today
    (§14) and will not stay that way, and a delivery record is the
    *operational* answer to "did we try", which must outlive the message it
    describes. A cascade would delete the audit with the artefact.

    ## What is not stored

    The rendered subject and body. §13 prefers rendering from the typed
    payload at send time, and the reason is a retry: a body frozen at enqueue
    time would be sent in whatever locale the recipient had *then*, and would
    keep being sent after a template fixed a mistake in it.

    The recipient's email address. §5 is explicit — it is resolved at
    delivery time from the authoritative account, so a change of address
    between enqueue and send reaches the right inbox, and so this table is
    not a list of email addresses.

    A provider's response body. `last_error_code` is a **bounded label this
    platform chose** (`EmailDeliveryOutcome`), never vendor text, which is
    what keeps the column safe to read and safe to put on a metric.
    """

    __tablename__ = "notification_email_delivery"

    __table_args__ = (
        PrimaryKeyConstraint("notification_id", name="pk_notification_email_delivery"),
        # The worker's claim query, exactly: pending rows whose time has
        # come, oldest first. Partial on `PENDING`, so the index holds only
        # what is owed — a table that is mostly delivered history costs
        # nothing to scan for work.
        Index(
            "ix_notification_email_delivery__due",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
        {"schema": NOTIFICATIONS_SCHEMA},
    )

    notification_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    recipient_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    """Whose address to resolve at send time. Denormalised from the
    notification rather than joined, because the claim query must not read a
    second table to decide what it holds — and because the delivery record
    outlives the notification."""

    notification_type: Mapped[str] = mapped_column(String(64), nullable=False)
    """`domain.record.NotificationType`. Carried so the worker can refuse an
    unsupported type without loading the notification, and so a metric can be
    labelled by type without a join."""

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """`domain.email_delivery.EmailDeliveryOutcome`, or `NULL` while a row
    has never been attempted. Text rather than a PostgreSQL enum, like every
    other closed vocabulary here: adding an outcome must be a code change and
    a migration of rows, never an `ALTER TYPE` that locks the relation."""

    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    """When this becomes due. `NULL` on a terminal row, so the partial index
    above never holds one."""

    last_attempt_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """What the provider called it. **Infrastructure metadata, never API
    data** (§19): it is how an operator correlates a complaint with a
    vendor's dashboard, and it appears on no response and no metric label."""

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class PushSubscriptionModel(UUIDPrimaryKeyMixin, Base):
    """The `notifications.push_subscription` row — A64-021.6 §2, §3.

    One row per **browser**, not per person and not per device: a laptop
    with two browsers is two subscriptions, and a browser whose site data
    was cleared becomes a new one. That is the only granularity the Push API
    offers — there is no device identifier — and pretending otherwise would
    invent an identity nothing can maintain.

    ## The endpoint is unique, and that is the ownership rule

    `uq_push_subscription__endpoint` is what makes "this browser belongs to
    this account" a database fact rather than a service check. A browser
    that re-subscribes with an endpoint another account already registered
    **takes it over** (`ON CONFLICT DO UPDATE`), which is §23's requirement:
    two people sharing a laptop must not inherit each other's
    notifications, and the browser saying "this endpoint is now mine" is the
    signal that the previous binding is stale.

    Unique across live *and* revoked rows, deliberately. A revoked row
    reappearing as a second live one for the same endpoint would push twice
    to one browser.

    ## No foreign key to `users`

    The same rule as every table here: cross-context references are opaque
    `user_id` values (DM-06), so the two schemas stay deployable apart.

    ## What is stored, and how it must be handled

    `endpoint`, `p256dh` and `auth` together are a **capability**: anyone
    holding them can push to that browser, subject to VAPID. They are never
    returned to a client, never logged, and never accepted as a lookup key a
    caller supplies. The keys are `LargeBinary` rather than base64 text
    because that is what the encryption takes — storing the encoded form
    would mean decoding on every delivery and would let a malformed value
    survive the write.
    """

    __tablename__ = "push_subscription"

    __table_args__ = (
        UniqueConstraint("endpoint", name="uq_push_subscription__endpoint"),
        # The fan-out query, exactly: every live subscription for one
        # recipient. Partial on live rows, so a table that accumulates
        # revoked history costs nothing to fan out over — and §9's "avoid
        # N+1" is a single indexed read per notification rather than a
        # lookup per device.
        Index(
            "ix_push_subscription__user_live",
            "user_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        {"schema": NOTIFICATIONS_SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    endpoint: Mapped[str] = mapped_column(String(2048), nullable=False)
    """`domain.subscription.MAX_ENDPOINT_LENGTH`. Long because push services
    choose these and some are long; bounded so the column cannot be used as
    storage."""

    p256dh: Mapped[bytes] = mapped_column(LargeBinary(65), nullable=False)
    auth: Mapped[bytes] = mapped_column(LargeBinary(16), nullable=False)
    """Exact lengths, fixed by RFC 8291. A value of any other length cannot
    encrypt anything, so the boundary refuses it and the column records that
    it did."""

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    """When this browser last re-registered. The only "is this device still
    real" signal available — a push service reports nothing until a delivery
    fails."""

    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    """Set rather than deleted, so an operator asking *why did this device
    stop receiving notifications* gets an answer. Two things set it: a push
    service answering `404`/`410`, and signing out on that browser."""


class NotificationPushDeliveryModel(Base):
    """The `notifications.notification_push_delivery` row — A64-021.6 §10, §19.

    ## Why the key is a pair, where email's is not

    `(notification_id, subscription_id)`. A person has one address and may
    have three browsers, so one notification owes **one** email and *n*
    pushes — and each succeeds or fails on its own (§9: one dead device must
    not prevent the others).

    That pair is also §19's idempotency, structurally: enqueueing is
    `INSERT ... ON CONFLICT DO NOTHING`, so a redelivered source event that
    inserts no notification inserts no deliveries, and a relay retry that
    somehow reached the fan-out twice converges on the same rows without
    reading first.

    ## No foreign key to `push_subscription`, despite both living here

    Retention, the same argument the email delivery table makes: the
    delivery record is the *operational* answer to "did we try", and it must
    outlive the subscription it describes. A cascade would delete the audit
    with the device — which is precisely the history somebody investigating
    "my phone stopped getting these" needs.

    ## What is not stored

    The endpoint or the keys. They are read from `push_subscription` at send
    time, so a browser that re-subscribed between enqueue and send is pushed
    at its *current* address, and so this table is not a second copy of a
    capability.

    The payload. It is two identifiers derived from the notification at send
    time (`domain.push.PushPayload`), and freezing it here would send a
    stale type after a correction.

    A push service's response body. `outcome` is a **bounded label this
    platform chose**, never vendor text, which is what keeps it safe to read
    and safe on a metric.
    """

    __tablename__ = "notification_push_delivery"

    __table_args__ = (
        PrimaryKeyConstraint(
            "notification_id", "subscription_id", name="pk_notification_push_delivery"
        ),
        # The worker's claim query, exactly: pending rows whose time has
        # come, oldest first. Partial on `PENDING`, so a table that is
        # mostly delivered history costs nothing to scan for work.
        Index(
            "ix_notification_push_delivery__due",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
        # A64-024.7. "Which pushes are failing" — the question the operations
        # console exists to answer, and the exact complement of the index
        # above. Partial on `failed`, so it holds only what an operator looks
        # for and stays small while delivered history accumulates.
        Index(
            "ix_notification_push_delivery__failed",
            "notification_id",
            postgresql_where=text("status = 'failed'"),
        ),
        {"schema": NOTIFICATIONS_SCHEMA},
    )

    notification_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    subscription_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    recipient_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    """Whose preference to read at send time. Denormalised from the
    notification rather than joined, because the claim query must not read a
    second table to decide what it holds — and because the delivery record
    outlives the notification."""

    notification_type: Mapped[str] = mapped_column(String(64), nullable=False)
    """`domain.record.NotificationType`. Carried so the worker can refuse an
    unsupported type without loading the notification, and so a metric can be
    labelled by type without a join."""

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """`domain.push_delivery.PushDeliveryOutcome`, or `NULL` while a row has
    never been attempted. Text rather than a PostgreSQL enum, like every
    other closed vocabulary here: adding an outcome must be a code change and
    a migration of rows, never an `ALTER TYPE` that locks the relation."""

    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    """When this becomes due. `NULL` on a terminal row, so the partial index
    above never holds one."""

    last_attempt_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


__all__ = [
    "BROADCAST_IDEMPOTENCY_UNIQUE",
    "NOTIFICATIONS_SCHEMA",
    "NOTIFICATION_SOURCE_UNIQUE",
    "NotificationBroadcastModel",
    "NotificationEmailDeliveryModel",
    "NotificationModel",
    "NotificationPreferenceModel",
    "NotificationPushDeliveryModel",
    "PushSubscriptionModel",
]
