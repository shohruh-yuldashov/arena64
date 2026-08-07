"""add web push subscriptions and deliveries

Revision ID: e26a0159c56b
Revises: d1f6a83c04e2
Create Date: 2026-08-07 13:12:44.108327

A64-021.6 §2, §10, §19, §31. Two tables in the `notifications` schema: the
browsers that asked to be notified, and the pushes each one is owed.

## Why two tables and not columns on the email delivery

`notification_email_delivery` is keyed on the notification alone, because a
person has one address. A push is owed **per browser**, so the key is
`(notification_id, subscription_id)` — and that difference is not something
a column could express on the existing relation.

## The endpoint is unique, and that is the ownership rule — §23

`uq_push_subscription__endpoint` spans live *and* revoked rows. Registration
is `ON CONFLICT (endpoint) DO UPDATE`, so a browser whose endpoint already
belongs to another account is **re-bound** in one statement with no window
in which two rows claim it. A constraint scoped to live rows only would let
a revoked row reappear as a second live one, and one browser would be pushed
twice.

## No foreign keys, in either direction

`push_subscription.user_id` has none, like every table in this schema:
cross-context references are opaque identifiers (DM-06), so the two schemas
stay deployable apart.

`notification_push_delivery.subscription_id` has none either, and that is a
*retention* decision rather than a boundary one — both tables live here. The
delivery row is the operational answer to "did we try", and it must outlive
the device it describes; a cascade would delete the audit along with the
browser somebody is asking about.

## No backfill — §31

Nothing existing generates a push. There are no subscriptions on the day
this runs, so there is nothing to fan out to and no delivery row to write,
and notifications already stored stay exactly as they are. The first push
this platform sends is for a notification created after somebody enabled it.

## Reversibility

Fully reversible: `downgrade` drops both tables. What is lost is every
registered browser and every delivery record — after which each person must
re-enable push and subscribe again, which their browser does on the next app
start. No durable notification is affected in either direction: push is
secondary to the record, and the record lives in `notification`.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.database.types import UtcDateTime

revision: str = "e26a0159c56b"
down_revision: str | Sequence[str] | None = "d1f6a83c04e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "notifications"


def upgrade() -> None:
    op.create_table(
        "push_subscription",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("endpoint", sa.String(length=2048), nullable=False),
        # Exact lengths, fixed by RFC 8291. A value of any other length
        # cannot encrypt anything, so the boundary refuses it and the column
        # records that it did.
        sa.Column("p256dh", sa.LargeBinary(length=65), nullable=False),
        sa.Column("auth", sa.LargeBinary(length=16), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("last_seen_at", UtcDateTime(), nullable=False),
        sa.Column("revoked_at", UtcDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_push_subscription"),
        sa.UniqueConstraint("endpoint", name="uq_push_subscription__endpoint"),
        schema=_SCHEMA,
    )
    # The fan-out read, exactly: every live subscription for one recipient.
    # Partial, so a table that accumulates revoked history costs nothing to
    # fan out over.
    op.create_index(
        "ix_push_subscription__user_live",
        "push_subscription",
        ["user_id"],
        unique=False,
        postgresql_where=sa.text("revoked_at IS NULL"),
        schema=_SCHEMA,
    )

    op.create_table(
        "notification_push_delivery",
        sa.Column("notification_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("subscription_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("recipient_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("notification_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", UtcDateTime(), nullable=True),
        sa.Column("last_attempt_at", UtcDateTime(), nullable=True),
        sa.Column("delivered_at", UtcDateTime(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        # The pair is the key, and it is also §19's idempotency: enqueueing
        # is `ON CONFLICT DO NOTHING`, so a redelivered source event that
        # reached the fan-out twice converges on the same rows.
        sa.PrimaryKeyConstraint(
            "notification_id", "subscription_id", name="pk_notification_push_delivery"
        ),
        schema=_SCHEMA,
    )
    # The worker's claim query, exactly: pending rows whose time has come,
    # oldest first. Partial on `PENDING`, so a table that is mostly
    # delivered history costs nothing to scan for work.
    op.create_index(
        "ix_notification_push_delivery__due",
        "notification_push_delivery",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_push_delivery__due",
        table_name="notification_push_delivery",
        schema=_SCHEMA,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("notification_push_delivery", schema=_SCHEMA)
    op.drop_index(
        "ix_push_subscription__user_live",
        table_name="push_subscription",
        schema=_SCHEMA,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.drop_table("push_subscription", schema=_SCHEMA)
