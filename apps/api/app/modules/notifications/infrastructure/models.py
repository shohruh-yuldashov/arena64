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


__all__ = [
    "NOTIFICATIONS_SCHEMA",
    "NOTIFICATION_SOURCE_UNIQUE",
    "NotificationModel",
    "NotificationPreferenceModel",
]
