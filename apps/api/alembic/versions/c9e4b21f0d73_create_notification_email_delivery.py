"""create notifications.notification_email_delivery

Revision ID: c9e4b21f0d73
Revises: b3d81f0a25c7
Create Date: 2026-08-07 11:04:52.331109

A64-021.5 §9, §25. The durable record of an email this platform owes, so a
send survives a restart, a provider timeout and a worker crash.

## Why a table rather than a fire-and-forget task

§9 is explicit, and the reason is the failure it prevents: an in-process
task holding "send this in five minutes" is on one node, and a deploy takes
every one it held with it. The emails then never send and nothing records
that they were owed. A row is claimable by any worker and a restart loses
nothing — the same argument `platform.outbox` and `tournament`'s no-show
deadline both make.

## One row per notification

`PRIMARY KEY (notification_id)`, no surrogate. A notification has exactly
one recipient and email is one channel, so `(notification_id, channel)`
would be a key with a column that cannot vary. Enqueueing is `INSERT ...
ON CONFLICT DO NOTHING` against it, which is §10's idempotency as a
constraint rather than as a check somebody remembered.

## One index, partial on what is owed

`(next_attempt_at) WHERE status = 'pending'` serves the worker's claim and
nothing else. Partial matters more here than usual: this table is
append-only in practice and becomes mostly delivered history, so an index
over all of it would grow forever while the query only ever wants the few
rows that are due.

## No foreign key to `notification`

Not the cross-schema reason the sibling tables give — both live here. It is
retention: a delivery record is the operational answer to *"did we try, and
why did it stop"*, and it must outlive the message it describes. A cascade
would delete the audit along with the artefact.

## No backfill — §25

Every existing notification predates the channel. Enqueueing rows for them
would email people about tournaments that finished weeks ago, which is the
one thing a first email send must not do. The table starts empty and only
notifications written after this point are ever owed an email.

## Reversibility

Fully reversible: `downgrade` drops the index and the table. What is lost is
the delivery *history* — no notification, no preference and no account is
touched. An operator should know that a downgrade discards the record of
which emails were attempted, and that re-upgrading starts empty rather than
re-sending: the enqueue happens with the notification, so nothing already
written is ever queued again.

The schema is deliberately **not** dropped: two other tables live in it.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.database.types import UtcDateTime

revision: str = "c9e4b21f0d73"
down_revision: str | Sequence[str] | None = "b3d81f0a25c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "notifications"


def upgrade() -> None:
    op.create_table(
        "notification_email_delivery",
        sa.Column("notification_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("recipient_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("notification_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", UtcDateTime(), nullable=True),
        sa.Column("last_attempt_at", UtcDateTime(), nullable=True),
        sa.Column("delivered_at", UtcDateTime(), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("notification_id", name="pk_notification_email_delivery"),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_notification_email_delivery__due",
        "notification_email_delivery",
        ["next_attempt_at"],
        unique=False,
        schema=_SCHEMA,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_email_delivery__due",
        table_name="notification_email_delivery",
        schema=_SCHEMA,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("notification_email_delivery", schema=_SCHEMA)
