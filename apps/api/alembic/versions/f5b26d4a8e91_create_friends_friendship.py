"""create friends friendship table

Revision ID: f5b26d4a8e91
Revises: e2f9a3c81b56
Create Date: 2026-08-01 18:04:52.118970

A64-013.3 gives the `friends` context its second relation — database.md
§7.3, the canonical-pair pattern.

## One row per unordered pair, never two mirrored ones

DB-12. "Two rows for one relationship is two facts that can disagree, and
when they do, neither is authoritative — there is no principled repair." The
read convenience mirroring would buy is bought instead with two indexes on
one row (§12.3), which costs index space rather than correctness.

`ck_friendship__canonical_order` is what makes the invariant real rather
than conventional: "without it, `(B, A)` is insertable and the unique
constraint does not fire, so the invariant fails exactly once — silently, in
production, under the concurrency that produced the out-of-order write."

## Why the unique index is partial

The same reason `uq_friend_request__one_pending_per_pair` is. A plain unique
on the pair would mean a friendship that ended could never be formed again,
which is not what FS-1 says — it constrains the *live* state. The partial
index leaves ended rows alone, and those rows are kept because
database.md §1221 calls a friendship that ended "a fact with a date; the row
is history, not debris".

## The two directional indexes

§12.3: reads ask "friendships of player X" without knowing which side X is
on, so both directions are indexed, each partial on live rows. `created_at`
and `id` follow the player column because every read of this relation is a
keyset page ordered by them — so PostgreSQL walks each leg in order and
stops at the page size instead of sorting the whole result.

## No foreign keys

To `users.user` for the reason `friend_request` has none: cross-context
references are opaque `player_id` values (DM-06), and an FK would make the
two schemas undeployable apart (architecture.md §16).

To `friends.friend_request` — which *is* in this schema, so the argument is
different — because `source_request_id` is nullable audit provenance. An FK
would force a retention policy that purges resolved requests to choose
between deleting friendships and keeping requests forever.

## Reversibility

Complete. `downgrade` drops the indexes, the table and then the enum type.
The schema is left in place, because `friend_request` still lives in it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f5b26d4a8e91"
down_revision: str | Sequence[str] | None = "e2f9a3c81b56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Both members from the first release even though only `removed` has a
#: producer — `ALTER TYPE ... ADD VALUE` on a type used by a live table is a
#: migration nobody should have to schedule in order to ship blocking.
_END_REASON_VALUES = ("removed", "blocked")

#: `create_type=False`, and it must be the PostgreSQL `ENUM` rather than
#: `sa.Enum` for the flag to have any effect — it is dialect-specific, and
#: `sa.Enum` accepts it silently and ignores it. Without it `create_table`
#: emits its own `CREATE TYPE` on top of the explicit one below and the
#: second fails. See `e2f9a3c81b56`, which learned this the same way.
_END_REASON = postgresql.ENUM(
    *_END_REASON_VALUES,
    name="friendship_end_reason",
    schema="friends",
    create_type=False,
)


def upgrade() -> None:
    members = ", ".join(f"'{value}'" for value in _END_REASON_VALUES)
    op.execute(f"CREATE TYPE friends.friendship_end_reason AS ENUM ({members})")

    op.create_table(
        "friendship",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("player_low_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("player_high_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("source_request_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_reason", _END_REASON, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_friendship")),
        sa.CheckConstraint("player_low_id < player_high_id", name="ck_friendship__canonical_order"),
        # `ended_at` and `ended_reason` are set together or not at all, so a
        # row cannot record an end without saying why (BE-06).
        sa.CheckConstraint(
            "(ended_at IS NULL) = (ended_reason IS NULL)", name="ck_friendship__ended_pairing"
        ),
        schema="friends",
    )

    op.create_index(
        "uq_friendship__pair",
        "friendship",
        ["player_low_id", "player_high_id"],
        unique=True,
        schema="friends",
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index(
        "ix_friendship__low",
        "friendship",
        ["player_low_id", "created_at", "id"],
        schema="friends",
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index(
        "ix_friendship__high",
        "friendship",
        ["player_high_id", "created_at", "id"],
        schema="friends",
        postgresql_where=sa.text("ended_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_friendship__high", "friendship", schema="friends")
    op.drop_index("ix_friendship__low", "friendship", schema="friends")
    op.drop_index("uq_friendship__pair", "friendship", schema="friends")
    op.drop_table("friendship", schema="friends")
    op.execute("DROP TYPE friends.friendship_end_reason")
    # The schema is deliberately left in place — `friend_request` still
    # lives in it, and `e2f9a3c81b56` owns dropping it.
