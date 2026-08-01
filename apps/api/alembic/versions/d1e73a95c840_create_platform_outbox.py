"""create platform outbox and processed_event

Revision ID: d1e73a95c840
Revises: b8d47e1c6f30
Create Date: 2026-08-01 22:04:11.309772

A64-013.7 creates the platform's **first non-module schema** and AD-16's two
relations — database.md §10.5 and §232.

## Why a schema of its own

Every schema so far belongs to a bounded context (`users`, `auth`,
`friends`, `statistics`). `platform` does not, and that is the point: the
outbox has one producer today and will have `game`, `ratings` and `chat`
tomorrow. Putting it in whichever context happened to need it first would
make every future producer depend on that context, and §232 already assigns
it an owner that is nobody in particular.

## `fillfactor = 70` — DB-18

The outbox is "written once, updated once (marked published), and then
dead", and at the platform's projected volume it is the primary bloat
source. A fillfactor below 100 leaves free space on every page so the
mark-published `UPDATE` is a **HOT** update: the new tuple version goes on
the same page and the indexes are not touched at all.

Applied with `ALTER TABLE` rather than in the model because SQLAlchemy has
no declarative form for a table's storage parameters. `app/platform/outbox/
models.py` names the number in `OUTBOX_FILLFACTOR` so it has one home.

Not applied to `processed_event`: that relation is insert-only, so there is
no update for the free space to serve — reserving 30% of every page for
nothing would cost storage and buy nothing.

## `ix_outbox__unpublished` — the index that disappears

Partial on `published_at IS NULL`, ordered `(occurred_at, id)`. database.md
§12.5: the relay's only query is "the oldest unpublished rows", so a full
index would carry every event ever emitted to answer a question about the
few hundred that are pending. Partial, it is effectively empty when the
relay is healthy — which makes its size a direct, graphable measure of relay
health.

`occurred_at` leads rather than `id` because publication order must follow
causation order; `id` is UUIDv7 and nearly agrees, but "nearly" is not a
guarantee across generators with clock skew.

## No range partitioning yet

DB-18 makes partitioning by `occurred_at` the retention mechanism, and
database.md §1377 lists `platform.outbox` as "designed for, not created
yet". A partitioned table with a single partition is operational weight
bought before there is volume to justify it. What this migration owes that
future is a partition key that already leads the index, which `occurred_at`
does — so the conversion is a table rewrite and not an index redesign.

## No foreign key on `processed_event.event_id`

To `outbox.id`, deliberately. The outbox is partition-pruned as its
retention mechanism, and a foreign key would make detaching an old partition
fail against ledger rows that outlive it. The ledger's job is to remember
that an id was handled, which does not require the row it named to still
exist.

## Reversibility

Complete. Both tables are dropped with their indexes, and the schema with
them — unlike the module schemas, nothing else lives in `platform`, so
leaving it behind would leave an empty schema no downgrade would ever
remove.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d1e73a95c840"
down_revision: str | Sequence[str] | None = "b8d47e1c6f30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "platform"

#: Mirrors `app.platform.outbox.models.OUTBOX_FILLFACTOR`. Restated rather
#: than imported: a migration must describe the schema as it was at this
#: revision, and importing a constant would make an old migration change
#: meaning when somebody tunes the current one.
_FILLFACTOR = 70


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{_SCHEMA}"')

    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("event_version", sa.SmallInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("causation_id", sa.String(length=64), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
        schema=_SCHEMA,
    )

    op.execute(f"ALTER TABLE {_SCHEMA}.outbox SET (fillfactor = {_FILLFACTOR})")

    op.create_index(
        "ix_outbox__unpublished",
        "outbox",
        ["occurred_at", "id"],
        schema=_SCHEMA,
        postgresql_where=sa.text("published_at IS NULL"),
    )

    op.create_table(
        "processed_event",
        sa.Column("consumer", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # The natural key is the whole meaning of the row — see the model on
        # why there is no surrogate id beside it.
        sa.PrimaryKeyConstraint("consumer", "event_id", name=op.f("pk_processed_event")),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("processed_event", schema=_SCHEMA)
    op.drop_index("ix_outbox__unpublished", "outbox", schema=_SCHEMA)
    op.drop_table("outbox", schema=_SCHEMA)
    op.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}"')
