"""create admin.role_assignment — A64-024.1

Administrative authority becomes data, as `database.md` §10.4 specified
before anything needed it.

**Nothing is promoted.** The table is created empty, so every existing
account remains an ordinary player and no migration path can make one an
administrator. The first grant is an explicit operator command
(`python -m app.operator.admin grant …`), which is the only writer that may
create a grant with no granter.

Revision ID: a1c4e7b92f30
Revises: 1ba6f5d18023
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1c4e7b92f30"
down_revision: str | Sequence[str] | None = "1ba6f5d18023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN_SCHEMA = "admin"


def upgrade() -> None:
    op.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{ADMIN_SCHEMA}"'))

    role = postgresql.ENUM("admin", name="admin_role", schema=ADMIN_SCHEMA, create_type=False)
    role.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "role_assignment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("role", role, nullable=False),
        # Nullable for exactly one row per deployment: the first grant,
        # which cannot have been made by an administrator because there was
        # none. Every later grant names one — `AdminRoleService.grant`
        # requires it and `bootstrap` refuses once a holder exists.
        sa.Column("granted_by", sa.Uuid(), nullable=True),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_role_assignment")),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= granted_at",
            name=op.f("ck_role_assignment__revoked_after_granted"),
        ),
        sa.CheckConstraint(
            "granted_by IS NULL OR granted_by <> account_id",
            name=op.f("ck_role_assignment__not_self_granted"),
        ),
        schema=ADMIN_SCHEMA,
        # No foreign key to `users.user`: DB-03 forbids cross-schema
        # references, so an account that no longer exists leaves a grant
        # resolving to nobody — which confers nothing, because the guard
        # reads the account as well as the grant.
    )

    op.create_index(
        op.f("ix_role_assignment_account_id"),
        "role_assignment",
        ["account_id"],
        unique=False,
        schema=ADMIN_SCHEMA,
    )

    # At most one **live** grant per account and role. Partial rather than
    # total, because the same account may legitimately hold the same role
    # twice over time — granted, revoked, granted again — and the history
    # of who held authority when is the point of the table.
    op.create_index(
        "uq_role_assignment__live",
        "role_assignment",
        ["account_id", "role"],
        unique=True,
        schema=ADMIN_SCHEMA,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_role_assignment__live", table_name="role_assignment", schema=ADMIN_SCHEMA)
    op.drop_index(
        op.f("ix_role_assignment_account_id"), table_name="role_assignment", schema=ADMIN_SCHEMA
    )
    op.drop_table("role_assignment", schema=ADMIN_SCHEMA)
    postgresql.ENUM(name="admin_role", schema=ADMIN_SCHEMA).drop(op.get_bind(), checkfirst=True)
    # The schema itself is left in place: `DROP SCHEMA` would take anything
    # a later migration put beside this table, and an empty schema costs
    # nothing.
