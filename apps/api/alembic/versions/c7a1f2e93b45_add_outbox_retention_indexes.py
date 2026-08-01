"""add outbox retention indexes

Revision ID: c7a1f2e93b45
Revises: d1e73a95c840
Create Date: 2026-08-01 23:10:04.118203

A64-014.1 bounds `platform.outbox`, which A64-013.7 shipped unbounded. The
policy, the pruner and its schedule are in `app/platform/outbox/retention.py`;
what this revision adds is the two indexes without which the prune is a
sequential scan of exactly the tables it exists to keep small.

## `ix_outbox__occurred_at` — and why it is not partial

The prune's predicate is `occurred_at < cutoff AND published_at IS NOT NULL`,
so a partial index on `published_at IS NOT NULL` would match it more
closely. It is unconditional anyway, for two reasons:

  - **HOT updates.** DB-18 sets `fillfactor = 70` on this table so that the
    mark-published `UPDATE` rewrites the tuple in place without touching an
    index. An update is HOT only when no *indexed* column changes, and
    `published_at` already appears in `ix_outbox__unpublished`'s predicate —
    so that one update already forfeits HOT once. Putting the column in a
    second index's predicate would forfeit it twice, for a row-count saving
    of roughly nothing (in steady state almost every row is published).
  - **It is the partition key.** DB-18 makes range partitioning by
    `occurred_at` the eventual retention mechanism. This index is what that
    partitioning wants, made local per partition; at that point the prune
    becomes `DETACH PARTITION` and both changes stay in the storage layer.

The remaining `published_at IS NOT NULL` is applied as a filter over the
index scan, which is cheap: it excludes the backlog, and the backlog is
small whenever the relay is healthy.

## `ix_processed_event__processed_at`

database.md §12.5 recorded that this relation "needs no secondary index",
and that was true while the only question asked of it was "has this consumer
seen this event" — both halves of the primary key are always known.
Retention asks a second question the key cannot answer: *which rows are
old*. Without this index the ledger prune degrades to a sequential scan of
the whole ledger, on exactly the schedule that exists to stop the ledger
being whole-scan-sized.

Insert-only and pruned by the same column, so the index appends at its
growing edge and carries none of the churn `ix_outbox__unpublished` does.

## No data change, and no lock worth naming

Two `CREATE INDEX` statements. Not `CONCURRENTLY`: Alembic runs each
migration in a transaction and `CREATE INDEX CONCURRENTLY` cannot run in
one, and at this point in the platform's life both relations are small
enough that the `SHARE` lock is measured in milliseconds. On a table with
real volume the correct form is a separate, non-transactional migration —
recorded here so that whoever adds an index to `platform.outbox` at scale
knows this one is not the precedent to copy.

## Reversibility

Complete. Both indexes are dropped and nothing else changes; the pruner
still works after a downgrade, more slowly.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c7a1f2e93b45"
down_revision: str | Sequence[str] | None = "d1e73a95c840"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "platform"


def upgrade() -> None:
    op.create_index(
        "ix_outbox__occurred_at",
        "outbox",
        ["occurred_at"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_processed_event__processed_at",
        "processed_event",
        ["processed_at"],
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_processed_event__processed_at", "processed_event", schema=_SCHEMA)
    op.drop_index("ix_outbox__occurred_at", "outbox", schema=_SCHEMA)
