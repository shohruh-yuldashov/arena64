"""create admin.audit_entry — A64-024.8

`database.md` §10.4 specified this table and `domain-model.md` §13.4 said why
it is an entity rather than a log line. This creates it as specified.

**Append-only is enforced by the database.** A trigger raises on `UPDATE` and
`DELETE`, so the guarantee survives a repository bug, a migration, an
operator with `psql`, and an administrator who reached the connection. A rule
only the application keeps is a rule the application can forget — and an
audit trail that can be edited by the party it audits is not one.

The trigger is deliberately not `REVOKE`: privileges are a deployment's to
grant, and a role that happens to own the schema would bypass them. A trigger
is owned by the table.

Revision ID: b2d5f8a41c70
Revises: a1c4e7b92f30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b2d5f8a41c70"
down_revision: str | Sequence[str] | None = "a1c4e7b92f30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN_SCHEMA = "admin"

_GUARD = f"""
CREATE OR REPLACE FUNCTION {ADMIN_SCHEMA}.audit_entry_is_append_only()
RETURNS trigger AS $$
BEGIN
    -- Built by concatenation rather than a format placeholder: this same DDL
    -- is executed through SQLAlchemy, where the percent sign is the driver's
    -- own parameter marker and would be consumed before PostgreSQL saw it.
    RAISE EXCEPTION USING
        ERRCODE = 'restrict_violation',
        MESSAGE = 'admin.audit_entry is append-only (attempted ' || TG_OP || ')';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{ADMIN_SCHEMA}"'))

    actor_type = postgresql.ENUM(
        "administrator", "operator", name="audit_actor_type", schema=ADMIN_SCHEMA,
        create_type=False,
    )
    action = postgresql.ENUM(
        "admin.role.grant", "admin.role.revoke", name="audit_action", schema=ADMIN_SCHEMA,
        create_type=False,
    )
    subject_type = postgresql.ENUM(
        "account", name="audit_subject_type", schema=ADMIN_SCHEMA, create_type=False
    )
    outcome = postgresql.ENUM(
        "succeeded", "failed", name="audit_outcome", schema=ADMIN_SCHEMA, create_type=False
    )
    for enum in (actor_type, action, subject_type, outcome):
        enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "audit_entry",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_type", actor_type, nullable=False),
        # Null exactly when the actor is an operator process — the first
        # `admin.role.grant` on a deployment has no administrator behind it,
        # and recording a fabricated account there would be worse than
        # recording none.
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("action", action, nullable=False),
        sa.Column("subject_type", subject_type, nullable=False),
        sa.Column("subject_ref", sa.Text(), nullable=False),
        sa.Column("outcome", outcome, nullable=False),
        sa.Column("before", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("after", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_entry")),
        # The domain refuses both of these at construction; this is the copy
        # the database keeps for a row that did not go through it.
        sa.CheckConstraint(
            "(actor_type = 'administrator' AND actor_id IS NOT NULL) "
            "OR (actor_type = 'operator' AND actor_id IS NULL)",
            name=op.f("ck_audit_entry__actor_matches_type"),
        ),
        schema=ADMIN_SCHEMA,
    )

    op.create_index(
        "ix_audit_entry__created_at_id",
        "audit_entry",
        ["created_at", "id"],
        schema=ADMIN_SCHEMA,
    )
    op.create_index(
        "ix_audit_entry__actor", "audit_entry", ["actor_id", "created_at"], schema=ADMIN_SCHEMA
    )
    op.create_index(
        "ix_audit_entry__action", "audit_entry", ["action", "created_at"], schema=ADMIN_SCHEMA
    )
    op.create_index(
        "ix_audit_entry__subject",
        "audit_entry",
        ["subject_type", "subject_ref", "created_at"],
        schema=ADMIN_SCHEMA,
    )

    op.execute(sa.text(_GUARD))
    op.execute(
        sa.text(
            f"CREATE TRIGGER audit_entry_append_only "
            f"BEFORE UPDATE OR DELETE ON {ADMIN_SCHEMA}.audit_entry "
            f"FOR EACH ROW EXECUTE FUNCTION {ADMIN_SCHEMA}.audit_entry_is_append_only()"
        )
    )
    # `TRUNCATE` fires no row trigger, so a row-level guard alone leaves the
    # one statement that empties the whole trail wide open. Statement-level,
    # separately, because that is the only level `TRUNCATE` has.
    op.execute(
        sa.text(
            f"CREATE TRIGGER audit_entry_no_truncate "
            f"BEFORE TRUNCATE ON {ADMIN_SCHEMA}.audit_entry "
            f"FOR EACH STATEMENT EXECUTE FUNCTION {ADMIN_SCHEMA}.audit_entry_is_append_only()"
        )
    )


def downgrade() -> None:
    for trigger in ("audit_entry_no_truncate", "audit_entry_append_only"):
        op.execute(
            sa.text(f"DROP TRIGGER IF EXISTS {trigger} ON {ADMIN_SCHEMA}.audit_entry")
        )
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {ADMIN_SCHEMA}.audit_entry_is_append_only()"))
    for name in (
        "ix_audit_entry__subject",
        "ix_audit_entry__action",
        "ix_audit_entry__actor",
        "ix_audit_entry__created_at_id",
    ):
        op.drop_index(name, table_name="audit_entry", schema=ADMIN_SCHEMA)
    op.drop_table("audit_entry", schema=ADMIN_SCHEMA)
    for name in ("audit_outcome", "audit_subject_type", "audit_action", "audit_actor_type"):
        postgresql.ENUM(name=name, schema=ADMIN_SCHEMA).drop(op.get_bind(), checkfirst=True)
