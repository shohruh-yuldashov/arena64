"""index notifications for the operations console — A64-024.7

Two indexes, no columns and no data change. Each exists because the admin
console makes exactly one query that would otherwise be a sequential scan of
a table that only grows.

`ix_notification__created_at_id` — the console's default page is every
notification, newest first. The existing `ix_notification__recipient_recent`
leads with `recipient_id`, so it cannot serve an unfiltered ordering: without
this the first page of the console would sort the whole relation. Its shape
matches `ix_user__created_at_id`, which the Users console added for the same
reason.

`ix_notification_push_delivery__failed` — "which pushes are failing" is the
question this console exists to answer, and the existing
`ix_notification_push_delivery__due` is partial on `pending`, which is the
opposite set. Partial on `failed`, so it holds only the rows an operator
looks for and stays small while the delivered history grows.

No `admin_retry` column and no new state: a retry is the existing row
returning to `pending`, which the delivery model already represents.

Revision ID: d4f2b83c05a1
Revises: c3e7a95d61b8
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4f2b83c05a1"
down_revision: str | Sequence[str] | None = "c3e7a95d61b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOTIFICATIONS_SCHEMA = "notifications"


def upgrade() -> None:
    op.create_index(
        "ix_notification__created_at_id",
        "notification",
        [sa.text("created_at DESC"), sa.text("id DESC")],
        schema=NOTIFICATIONS_SCHEMA,
    )
    op.create_index(
        "ix_notification_push_delivery__failed",
        "notification_push_delivery",
        ["notification_id"],
        postgresql_where=sa.text("status = 'failed'"),
        schema=NOTIFICATIONS_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_push_delivery__failed",
        table_name="notification_push_delivery",
        schema=NOTIFICATIONS_SCHEMA,
    )
    op.drop_index(
        "ix_notification__created_at_id",
        table_name="notification",
        schema=NOTIFICATIONS_SCHEMA,
    )
