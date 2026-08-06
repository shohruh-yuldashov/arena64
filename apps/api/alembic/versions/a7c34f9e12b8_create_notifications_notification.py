"""create notifications.notification

Revision ID: a7c34f9e12b8
Revises: f1a70c93b4d8
Create Date: 2026-08-06 21:40:12.554218

A64-021.1 §7. The durable `Notification` — NT-1's "the notification exists
even if every delivery channel fails" — and the first table the
`notifications` context has ever owned.

## `CREATE SCHEMA notifications`

Alembic detects tables in a non-default schema but does not create the
schema itself, so a first migration into one has to. `IF NOT EXISTS` because
this must be re-runnable against a database where an earlier attempt got
this far.

## The unique constraint is the feature, not a safeguard

`(recipient_id, source_event_id, type)` is what makes notification creation
exactly-once under at-least-once event delivery (§11, NT-2). It is the
mechanism rather than a belt on top of one: `NotificationRepository.append`
is `INSERT ... ON CONFLICT DO NOTHING` against this constraint by name, so a
redelivered event, a restarted relay and two concurrent consumer processes
all converge on one row without any of them reading first.

Narrower than `database.md` §10.2's `(recipient_id, event_id, category)`,
and §10.2 is updated in the same change. `category` groups several types, so
keying on it would let one event produce one social notification and then
silently refuse a genuinely different one from the same event.

## Two indexes, and why the second is partial

`ix_notification__recipient_recent` serves the list and nothing else:
`(recipient_id, created_at DESC, id DESC)` is the keyset the cursor encodes,
so a page is a range scan rather than a sort.

`ix_notification__recipient_unread` is `database.md` §12.7's Q9, partial on
`read_at IS NULL`. Partial matters here more than usual: the badge is the
most frequently asked question this table has, and a player who is caught up
has *no rows in the index at all* — so the common case counts nothing.

## No foreign key to `users`

`recipient_id` is an opaque `player_id` (DM-06), like every cross-context
reference on this platform. `source_event_id` has no foreign key to
`platform.outbox` either, and for a second reason: the outbox is
retention-pruned, and a notification must outlive the event that caused it.

## No data migration

Nothing to backfill. Every notification is produced by a source event
through the outbox, and the events that predate this table were consumed and
marked processed long ago — replaying them would mean re-delivering months
of friend requests as though they had just arrived. §14's append-only
retention starts here, empty.

## Reversibility

Fully reversible: `downgrade` drops the table and the schema. What is lost
is every notification, which is history rather than truth — the friendships
and requests they describe are untouched, and `processed_event` still
records that their events were handled, so an upgrade after a downgrade
starts empty rather than replaying.

The schema is dropped without `CASCADE`, so a downgrade fails loudly if
anything else has since been created in it rather than silently taking it
with the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.database.types import UtcDateTime

revision: str = "a7c34f9e12b8"
down_revision: str | Sequence[str] | None = "f1a70c93b4d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "notifications"


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{_SCHEMA}"')

    op.create_table(
        "notification",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("recipient_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_ref", sa.String(length=128), nullable=True),
        sa.Column("source_event_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("read_at", UtcDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_notification"),
        sa.UniqueConstraint(
            "recipient_id",
            "source_event_id",
            "type",
            name="uq_notification__recipient_source_type",
        ),
        schema=_SCHEMA,
    )

    # Raw DDL rather than `create_index`, because the ordering is the point:
    # Alembic's helper takes column names and cannot express `DESC` per
    # column, and an ascending index would answer the query by sorting every
    # page instead of scanning a range.
    op.execute(
        f"CREATE INDEX ix_notification__recipient_recent "
        f"ON {_SCHEMA}.notification (recipient_id, created_at DESC, id DESC)"
    )
    op.create_index(
        "ix_notification__recipient_unread",
        "notification",
        ["recipient_id"],
        unique=False,
        schema=_SCHEMA,
        postgresql_where=sa.text("read_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification__recipient_unread",
        table_name="notification",
        schema=_SCHEMA,
        postgresql_where=sa.text("read_at IS NULL"),
    )
    op.execute(f"DROP INDEX {_SCHEMA}.ix_notification__recipient_recent")
    op.drop_table("notification", schema=_SCHEMA)
    op.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}"')
