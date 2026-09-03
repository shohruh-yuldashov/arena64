"""create admin.moderation_case and admin.sanction — A64-024.6

`database.md` §10.4 specified both, `domain-model.md` §13.2/§13.3 said what
each is for, and DM-12 said why they are separate tables rather than one.
This creates them as specified.

Two tables rather than one because `sanction.case_id` is a `NOT NULL`
foreign key in §10.4 and §13.3 states the rule in words: a sanction names
the case that authorised it. Shipping the sanction alone would mean adding
that column to a populated table later and backfilling fabricated cases for
live restrictions.

**No backfill and no default restriction.** Both tables are created empty.
Nothing about an existing account changes, and in particular no existing
`users.user` row is read or written — `admin` never writes account rows
(domain-model.md §6).

Revision ID: c3e7a95d61b8
Revises: b2d5f8a41c70
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c3e7a95d61b8"
down_revision: str | Sequence[str] | None = "b2d5f8a41c70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN_SCHEMA = "admin"


def upgrade() -> None:
    category = postgresql.ENUM(
        "cheating",
        "abuse",
        "account_compromise",
        "policy_violation",
        "other",
        name="moderation_category",
        schema=ADMIN_SCHEMA,
        create_type=False,
    )
    case_status = postgresql.ENUM(
        "closed", name="moderation_case_status", schema=ADMIN_SCHEMA, create_type=False
    )
    kind = postgresql.ENUM(
        "suspended", name="sanction_kind", schema=ADMIN_SCHEMA, create_type=False
    )
    for enum in (category, case_status, kind):
        enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "moderation_case",
        sa.Column("id", sa.Uuid(), nullable=False),
        # DM-06's opaque player reference. No FK to `users` — DB-03 forbids
        # the cross-schema reference, and the case must outlive the account.
        sa.Column("subject_player_id", sa.Uuid(), nullable=False),
        sa.Column("category", category, nullable=False),
        sa.Column("status", case_status, nullable=False),
        sa.Column("opened_by", sa.Uuid(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("reverses_case_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_moderation_case")),
        sa.ForeignKeyConstraint(
            ["reverses_case_id"],
            [f"{ADMIN_SCHEMA}.moderation_case.id"],
            name="fk_moderation_case__reverses",
        ),
        sa.CheckConstraint(
            "opened_by <> subject_player_id",
            name=op.f("ck_moderation_case__not_self_opened"),
        ),
        sa.CheckConstraint(
            "closed_at >= opened_at",
            name=op.f("ck_moderation_case__closed_after_opened"),
        ),
        schema=ADMIN_SCHEMA,
    )
    op.create_index(
        "ix_moderation_case__subject",
        "moderation_case",
        ["subject_player_id", "opened_at"],
        schema=ADMIN_SCHEMA,
    )

    op.create_table(
        "sanction",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("player_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("kind", kind, nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lifted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lifted_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sanction")),
        sa.ForeignKeyConstraint(
            ["case_id"],
            [f"{ADMIN_SCHEMA}.moderation_case.id"],
            name="fk_sanction__case_id",
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > starts_at",
            name=op.f("ck_sanction__expires_after_start"),
        ),
        sa.CheckConstraint(
            "(lifted_at IS NULL) = (lifted_by IS NULL)",
            name=op.f("ck_sanction__lift_is_attributed"),
        ),
        schema=ADMIN_SCHEMA,
    )

    # database.md §12.6 — the hot authorization index. A partial predicate
    # must be immutable, so `now()` cannot appear in it; `lifted_at IS NULL`
    # is the immutable half and the expiry comparison is a filter on the
    # handful of rows returned.
    op.create_index(
        "ix_sanction__player_expiry",
        "sanction",
        ["player_id", "expires_at"],
        postgresql_where=sa.text("lifted_at IS NULL"),
        schema=ADMIN_SCHEMA,
    )
    op.create_index(
        "ix_sanction__created_at_id", "sanction", ["created_at", "id"], schema=ADMIN_SCHEMA
    )
    # At most one unlifted sanction of a kind per account — what makes two
    # administrators acting at once end in an integrity error rather than
    # in two live restrictions that disagree (BE-06).
    op.create_index(
        "uq_sanction__live_kind",
        "sanction",
        ["player_id", "kind"],
        unique=True,
        postgresql_where=sa.text("lifted_at IS NULL"),
        schema=ADMIN_SCHEMA,
    )


def downgrade() -> None:
    for name in (
        "uq_sanction__live_kind",
        "ix_sanction__created_at_id",
        "ix_sanction__player_expiry",
    ):
        op.drop_index(name, table_name="sanction", schema=ADMIN_SCHEMA)
    op.drop_table("sanction", schema=ADMIN_SCHEMA)

    op.drop_index("ix_moderation_case__subject", table_name="moderation_case", schema=ADMIN_SCHEMA)
    op.drop_table("moderation_case", schema=ADMIN_SCHEMA)

    for name in ("sanction_kind", "moderation_case_status", "moderation_category"):
        postgresql.ENUM(name=name, schema=ADMIN_SCHEMA).drop(op.get_bind(), checkfirst=True)
