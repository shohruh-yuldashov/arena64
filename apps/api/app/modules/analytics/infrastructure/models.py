"""The `analytics` schema — `subject` and `event`.

Two tables, and the split between them is the privacy design rather than
normalisation. `analytics.subject` is the **only** place a person is linked
to their analytics history; `analytics.event` never holds a `player_id`, so
the raw event store cannot be joined to the product database by primary key.
Erasure deletes one row from the first table and the link is gone (§51).

## Relational envelope, JSONB properties

The envelope is queried by every metric — by day, by name, by environment,
by subject — so it is columns. The properties differ per event and are
queried by whichever dimension a metric names, so they are one `jsonb`
column.

That split is deliberate rather than lazy. A column per property would be
twenty mostly-null columns and a migration per new event; a `jsonb` envelope
would put `occurred_at` behind an expression index and make the retention
prune unable to use one. What makes the `jsonb` half safe is that **nothing
untyped reaches it**: a property map is validated against its event's closed
schema before an `AnalyticsEvent` exists, so the column holds only values
some schema admitted.

## No foreign key to `subject`

Deliberate, and it is the point. An `ON DELETE CASCADE` would delete a
person's events with their subject row, which is the opposite of the D3
decision — the non-identifying product facts survive. An `ON DELETE
RESTRICT` would make erasure fail. A nullable FK with `SET NULL` would work
and would also destroy the grouping that keeps aggregates correct.

The absence of the constraint **is** the erasure semantics: rows outlive the
link that named them.
"""

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.mixins.uuid_pk import UUIDPrimaryKeyMixin
from app.database.types import UtcDateTime

ANALYTICS_SCHEMA: Final = "analytics"


class AnalyticsSubjectModel(Base):
    """`analytics.subject` — the one linkage, and the one thing erasure deletes.

    Small: one row per player who has ever produced an analytics event.
    Written once, read on every ingestion, deleted on erasure.
    """

    __tablename__ = "subject"
    __table_args__ = ({"schema": ANALYTICS_SCHEMA},)

    #: The player. **The only `player_id` in the analytics schema**, and the
    #: primary key because a person has exactly one subject key.
    player_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)

    #: Random, never derived. There is no function from `player_id` to this
    #: value, which is what makes deleting the row irreversible rather than
    #: merely inconvenient — see `domain/subject.py`.
    subject_key: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)

    #: Whether this account's events are excluded from product metrics.
    #: Stored here rather than on every event so a seeded e2e account marked
    #: after the fact does not leave a trail of rows claiming otherwise —
    #: though `event.is_synthetic` is what a query filters on, because a
    #: metric must not join to a table erasure can delete.
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )


class AnalyticsEventModel(UUIDPrimaryKeyMixin, Base):
    """`analytics.event` — one row per analytics fact.

    The primary key is the event id itself, inherited from the mixin. That
    is the deduplication mechanism and not merely an identifier: a
    redelivered outbox event derives the same id, the insert conflicts, and
    `ON CONFLICT DO NOTHING` makes at-least-once delivery an exactly-once
    effect **without** depending on the ledger being written, on transaction
    ordering, or on two workers not racing.
    """

    __tablename__ = "event"
    __table_args__ = (
        # --- what every metric filters on first ---------------------------
        #
        # Nearly every query in analytics.md §29 is "this event, in this
        # environment, over this window". Environment leads because it is
        # the one predicate *all* of them carry and its selectivity is
        # near-total in production; name then narrows; `occurred_at` orders
        # and bounds. A64-027.3's funnels and A64-027.4's cohorts both walk
        # this index.
        Index(
            "ix_analytics_event__environment_name_occurred",
            "environment",
            "event_name",
            "occurred_at",
        ),
        # --- per-person questions -----------------------------------------
        #
        # Retention, activation, time-to-first-match and matches-per-player
        # are all "this subject's events in order". Partial on the rows that
        # have a subject, so the anonymous half of the table — which is most
        # of it on a marketing-heavy day — carries no entries at all.
        Index(
            "ix_analytics_event__subject_occurred",
            "subject_key",
            "occurred_at",
            postgresql_where=text("subject_key IS NOT NULL"),
        ),
        # --- the acquisition funnel ---------------------------------------
        #
        # F-A walks one browser's events to the registration that ended
        # them. Partial for the same reason inverted: authenticated product
        # events never have an anonymous id.
        Index(
            "ix_analytics_event__anonymous_occurred",
            "anonymous_id",
            "occurred_at",
            postgresql_where=text("anonymous_id IS NOT NULL"),
        ),
        # --- retention ------------------------------------------------------
        #
        # The prune's only predicate. `occurred_at` alone, and it leads so
        # that when this table is partitioned by it the index becomes local
        # to each partition and the prune becomes a `DETACH` — the same
        # preparation `platform.outbox` already made.
        Index("ix_analytics_event__occurred_at", "occurred_at"),
        # The domain enforces the per-event identity rule (`AnalyticsEvent`
        # checks it against the registry). This is the half SQL can state
        # without knowing the taxonomy: a version is positive, and a row is
        # attached to at most one browser identity.
        CheckConstraint("event_version >= 1", name="ck_analytics_event__version_positive"),
        {"schema": ANALYTICS_SCHEMA},
    )

    #: From the registry's `EventName`. A string rather than a database enum:
    #: adding a taxonomy member would otherwise need a migration, and the
    #: value is already closed by the registry on every write path.
    event_name: Mapped[str] = mapped_column(String(64), nullable=False)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )

    #: `backend` or `frontend`. Server-assigned on both paths.
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)

    subject_key: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    anonymous_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    properties: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    #: The outbox row this was projected from — `None` for a client event.
    #: Not unique: a fan-out projection writes two rows from one outbox row.
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
