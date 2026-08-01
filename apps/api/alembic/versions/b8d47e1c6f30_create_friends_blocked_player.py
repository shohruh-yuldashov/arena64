"""create friends blocked_player table

Revision ID: b8d47e1c6f30
Revises: f5b26d4a8e91
Create Date: 2026-08-01 20:11:38.442901

A64-013.5 gives the `friends` context its third and last relation —
database.md §7.2.

## Deliberately minimal, and hard-deleted on unblock

§7.2's own words. There is no `ended_at`, no soft delete and no
`updated_at`, because "a block has no history worth keeping, and retaining
released blocks would make BL-2's matchmaking filter — already the most
performance-sensitive use of this relation — read rows it must then
exclude."

That is the opposite decision from `friendship`, which is soft-ended, and
the difference is real: a friendship that ended is a fact two people
participated in, while a block that was lifted is one person's private
change of mind.

## Ordered, not canonical

`(blocker_id, blocked_id)` as given. DB-12's canonical-pair pattern applies
to *symmetric* relationships, and a block is not one: A blocking B and B
blocking A are two different facts, both of which can be true and neither of
which implies the other. Canonicalising here would lose the direction, which
is the only thing the row records.

The unique constraint is therefore **not partial**, unlike the friendship and
friend-request ones: those cover only live rows because ended ones must not
prevent a new relationship, and here there are no ended rows to exclude.

## Three indexes for three questions

    uq_blocked_player__pair    one block per ordered pair
    ix_blocked_player__blocker "who have I blocked" — the block list, and
                               the exclusion set search subtracts on every
                               query. Carries the keyset ordering columns
    ix_blocked_player__blocked "who has blocked me" — the other half of the
                               symmetric visibility consequence, and the
                               leg BL-2's matchmaking filter will read on
                               every pairing tick. No ordering columns:
                               nothing pages it, it is consumed as a set

## No foreign keys

To `users.user`, for the reason the other two relations here have none:
cross-context references are opaque `player_id` values (DM-06), and an FK
would make the two schemas undeployable apart (architecture.md §16).
`statistics.player_statistics`, `friends.friend_request` and
`friends.friendship` all make the same choice.

A64-013.5 lists foreign keys among its requirements, so this is a documented
deviation rather than an oversight — the third time this epic has recorded
it, and for the same reason each time.

## Reversibility

Complete. The table is dropped with its indexes; the schema is left in place
because two other relations still live in it.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8d47e1c6f30"
down_revision: str | Sequence[str] | None = "f5b26d4a8e91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "blocked_player",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("blocker_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("blocked_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_blocked_player")),
        sa.CheckConstraint("blocker_id <> blocked_id", name="ck_blocked_player__not_self"),
        schema="friends",
    )

    op.create_index(
        "uq_blocked_player__pair",
        "blocked_player",
        ["blocker_id", "blocked_id"],
        unique=True,
        schema="friends",
    )
    op.create_index(
        "ix_blocked_player__blocker",
        "blocked_player",
        ["blocker_id", "created_at", "id"],
        schema="friends",
    )
    op.create_index(
        "ix_blocked_player__blocked",
        "blocked_player",
        ["blocked_id"],
        schema="friends",
    )


def downgrade() -> None:
    op.drop_index("ix_blocked_player__blocked", "blocked_player", schema="friends")
    op.drop_index("ix_blocked_player__blocker", "blocked_player", schema="friends")
    op.drop_index("uq_blocked_player__pair", "blocked_player", schema="friends")
    op.drop_table("blocked_player", schema="friends")
