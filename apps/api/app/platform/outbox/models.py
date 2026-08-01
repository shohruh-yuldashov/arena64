"""The `platform` schema — `outbox` and `processed_event`.

database.md §232 puts both here, under an owner that is the platform itself
rather than a bounded context. See `app/platform/__init__.py` on why that
ownership is structural and not clerical.

## DB-18: the one relation designed around its churn

"The outbox is written once, updated once (marked published), and then
dead." All three of its consequences are expressed below:

  **fillfactor 70** — leaves free space on every page so the mark-published
  `UPDATE` is a HOT update: it rewrites the tuple in place and leaves the
  indexes untouched. Without it the platform's highest-churn relation would
  also be its highest index-churn relation, for an update that changes one
  timestamp. Applied by the migration; see `OUTBOX_FILLFACTOR` below on why
  it cannot be declared on the model.

  **a partial index on unpublished rows only** — §12.5. The relay's only
  query is "the oldest unpublished rows", and a full index would carry every
  event ever emitted to answer a question about the few hundred pending.
  Partial, it is *empty* when the relay is healthy, which makes its size a
  direct measure of relay health.

  **range partitioning by `occurred_at`** — the retention mechanism, so
  pruning is a detach rather than a bulk `DELETE`. Not created here:
  database.md §1377 lists it as "designed for, not created yet", and a
  partitioned table with one partition is operational weight bought before
  there is any volume to justify it. What this build owes that future is a
  partition *key* that already leads every index, which `occurred_at` does.

## Two columns database.md does not list, and why

§10.5 specifies `published_at`, `attempt_count`, `claimed_at`, `claimed_by`.
This adds `next_attempt_at` and `last_error`, and the document is updated in
the same change (CLAUDE.md §3.11).

  `next_attempt_at`  bounded retry with backoff (CLAUDE.md §9.10) needs a
                     "not before" instant. Without one, retry is a tight
                     loop against whatever is failing — which is the
                     behaviour that turns a transient outage into an outage
                     plus a thundering herd.
  `last_error`       the failure is otherwise only in a log line, and the
                     row is what an operator queries when asked why an event
                     never arrived. Bounded in length by the writer, and it
                     holds an exception type and message — never a payload.

## No foreign key from `processed_event.event_id` to `outbox.id`

The same reasoning every cross-context reference on this platform uses, plus
one specific to retention: the outbox is partition-pruned, and an FK would
make detaching an old partition fail against ledger rows that outlive it.
The ledger's job is to remember that an id was handled, which does not
require the row it named to still exist.
"""

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import Index, Integer, SmallInteger, String, Text, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.mixins.uuid_pk import UUIDPrimaryKeyMixin
from app.database.types import UtcDateTime

#: database.md §232. Not a module schema — see this module's docstring.
PLATFORM_SCHEMA: Final = "platform"

#: DB-18's HOT-update headroom, applied by the migration rather than here.
#:
#: SQLAlchemy has no declarative form for a table's storage parameters —
#: `postgresql_with` exists on `Index` and not on `Table` — so this is
#: `ALTER TABLE ... SET (fillfactor = ...)` in
#: `alembic/versions/…_create_platform_outbox.py`. Named here so the number
#: has one home and the model documents the decision it cannot express.
#:
#: 70 rather than the default 100, so the mark-published update is a HOT
#: update that reuses the page and leaves the indexes untouched. Not tuned by
#: measurement — there is no production workload yet — but taken from DB-18's
#: explicit instruction.
OUTBOX_FILLFACTOR: Final = 70


class OutboxModel(UUIDPrimaryKeyMixin, Base):
    """The `platform.outbox` row — AD-16.

    **C3 (transactional infrastructure)** in database.md §11.1's
    classification, so it carries `occurred_at` and neither `updated_at` nor
    `deleted_at`. `TimestampMixin` is deliberately not composed: an event's
    instant is when the fact happened, which the *producer* supplies, not
    when the row was inserted — and a `server_default=now()` on it would
    quietly paper over a producer that forgot to pass one.
    """

    __tablename__ = "outbox"
    __table_args__ = (
        # §12.5. `occurred_at` leads because publication order must follow
        # causation order; `id` is the v7 tiebreak, which nearly agrees but
        # is not guaranteed to across generators with clock skew.
        #
        # The predicate covers *unpublished* rows only, and deliberately not
        # "unpublished and not exhausted": an exhausted row must stay in
        # this index so that the backlog metric an operator watches keeps
        # counting it. Filtering it out here is how a permanently failing
        # event becomes invisible.
        Index(
            "ix_outbox__unpublished",
            "occurred_at",
            "id",
            postgresql_where=text("published_at IS NULL"),
        ),
        # A64-014.1's retention scan: "the oldest rows past the horizon".
        #
        # **Unconditional, unlike the index above**, and the asymmetry is
        # the decision rather than an oversight. A partial index on
        # `published_at IS NOT NULL` would match the prune's predicate more
        # closely and would cover nearly the whole table anyway — but it
        # would put `published_at` in a *second* index's definition, and the
        # column's one `UPDATE` is the mark-published write DB-18's
        # fillfactor exists to keep cheap. One index already pays that
        # price; a second would double it for a row-count saving of roughly
        # nothing.
        #
        # `occurred_at` alone, and it is the partition key: when DB-18's
        # range partitioning arrives this index becomes local to each
        # partition and the prune becomes a `DETACH`. Both changes are then
        # confined to the storage layer, which is what "prepare for
        # partitioning" is worth.
        Index("ix_outbox__occurred_at", "occurred_at"),
        {"schema": PLATFORM_SCHEMA},
    )

    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    event_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)

    #: `JSONB`, not `JSON` or `text`: the payload is queried by operators
    #: during incidents ("which events named this player"), and `jsonb`
    #: is the only one of the three that can be indexed if that ever stops
    #: being a rare manual query.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    occurred_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )

    #: The causal chain, propagated from the request that produced the event
    #: (`app/common/context.py`). Nullable because a worker-produced event
    #: has no inbound request to inherit from — and inventing one would make
    #: two unrelated chains look like one.
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    causation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    #: Which worker holds it. Diagnostic only — the claim's correctness comes
    #: from `FOR UPDATE SKIP LOCKED`, not from this column, and a design that
    #: relied on it would be a lease with no way to detect a dead holder.
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProcessedEventModel(Base):
    """The `platform.processed_event` row — domain-model.md §13.6.

    The consumer-side ledger that makes at-least-once safe. Its primary key
    **is** its only access path (§12.5: "needs no secondary index"), because
    the only question ever asked of it is "has this consumer seen this
    event", and both halves are always known.

    No surrogate id, so no `UUIDPrimaryKeyMixin`: the natural key is the
    whole row's meaning, and a generated id beside it would permit two rows
    asserting the same fact.
    """

    __tablename__ = "processed_event"
    __table_args__ = (
        # A64-014.1. §12.5 said this relation "needs no secondary index",
        # and that was true while the only question asked of it was "has
        # this consumer seen this event" — both halves of the key are
        # always known. Retention asks a second question the key cannot
        # answer: *which rows are old*. Without this the prune degrades to
        # a sequential scan of the whole ledger, on exactly the schedule
        # that exists to stop the ledger being whole-scan-sized.
        #
        # Insert-only and pruned by the same column, so the index has the
        # append-at-the-edge access pattern a timestamp gives and none of
        # the churn `ix_outbox__unpublished` carries.
        Index("ix_processed_event__processed_at", "processed_at"),
        {"schema": PLATFORM_SCHEMA},
    )

    consumer: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )
