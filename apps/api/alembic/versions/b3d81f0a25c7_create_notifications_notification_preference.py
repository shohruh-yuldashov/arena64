"""create notifications.notification_preference

Revision ID: b3d81f0a25c7
Revises: a7c34f9e12b8
Create Date: 2026-08-07 09:12:44.108233

A64-021.3 §6. The durable preference: which categories a player wants, on
which channels. The second table the `notifications` context owns.

## The schema is `notifications`, not `users`

`database.md` §4.9 placed this table in the `users` schema; it is corrected
in the same change, along with `domain-model.md` §9.3 (CLAUDE.md §3.11). The
reasoning is in `domain/preference.py`, and the short version is that §4.9's
own column types are named `notifications.notification_category` and
`notifications.delivery_channel` — the vocabulary is this module's, and
putting the table in `users` would make the platform's base module import
`notifications.public` to describe it.

`CREATE SCHEMA` is not repeated here: `a7c34f9e12b8` created it, and this
migration cannot run before that one.

## Sparse, so there is nothing to backfill

A row exists only where a player has **overridden** a default. Every
existing account therefore starts correct with zero rows — in-app on,
email and push off — and this migration inserts nothing.

That is the whole reason there is no data migration, and it is worth being
explicit that the alternative was considered: materialising the matrix for
every account would be a dozen rows per player written now, a data migration
over every user for each future category, and the permanent loss of the
difference between "chose the default" and "never looked".

## The primary key is the identity

`(user_id, category, channel)`, no surrogate. One preference per triple,
with no identity apart from which one it is. It is also the constraint
`ON CONFLICT DO UPDATE` names, which is what lets two tabs saving at once
converge on one row without either reading first.

## One index, ordered for the fan-out

`(channel, user_id)` serves the delivery-time question — "for these
recipients, on this channel, what did they choose?" — which asks about one
channel and many recipients. `user_id` first would be a probe per recipient
where this is one range scan (§11).

The settings screen's read is served by the primary key, so it needs no
index of its own.

## No foreign key to `users`

`user_id` is an opaque player id (DM-06), like every cross-context reference
on this platform. §4.9 specified `FK, cascade`; here that would be a
cross-schema foreign key of the kind DB-03 forbids. Deleting an account
leaves these rows behind, keyed on an id that will never be reissued — see
`NotificationPreferenceModel` on why that is `users`' erasure path to clean
up rather than another schema's constraint.

## Reversibility

Fully reversible: `downgrade` drops the index and the table. What is lost is
every player's overrides, so a downgrade and re-upgrade silently returns
everybody to the defaults — which for `in_app` means *on*. Anyone who had
muted a category would begin receiving it again. That is stated rather than
mitigated: a downgrade of a preference table cannot preserve preferences,
and an operator needs to know it is a consent-affecting operation before
running it, not afterwards.

The schema is deliberately **not** dropped: `notifications.notification`
still lives in it, and this migration did not create it.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.database.types import UtcDateTime

revision: str = "b3d81f0a25c7"
down_revision: str | Sequence[str] | None = "a7c34f9e12b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "notifications"


def upgrade() -> None:
    op.create_table(
        "notification_preference",
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint(
            "user_id", "category", "channel", name="pk_notification_preference"
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_notification_preference__channel_user",
        "notification_preference",
        ["channel", "user_id"],
        unique=False,
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_preference__channel_user",
        table_name="notification_preference",
        schema=_SCHEMA,
    )
    op.drop_table("notification_preference", schema=_SCHEMA)
