"""add users.external_identity

Revision ID: f5444391dfec
Revises: c7a91d4e60b2
Create Date: 2026-10-22 04:00:00.000000

A64-031.E1 — "Continue with Google". One table, additive, and the only
schema change the checkpoint needs.

## Why a table, and why this one

Signing in with Google needs a durable answer to "which Arena64 account
is this Google account?", keyed on Google's stable subject (`sub`) —
never on the address, which Google can change and recycle. Nothing in
the existing schema can hold it: `users.user` has one `email` and one
`password_hash` and no place for a provider identity, and keying on
`email` is precisely the account-takeover path ADR-008 rules out.

`database.md` §4.3 drew this as an `oauth` row in `auth.credential`.
That table was never built — credentials live on `users.user` (see
`users.infrastructure.models`, "two documented deviations") — and the
link must commit in the same transaction as an account created through
Google, so it lives beside the user, in `users`.

## The constraints are the invariant

`(provider, subject)` unique: one Google account reaches one Arena64
account. `(user_id, provider)` unique: one Arena64 account holds one
Google account — and, as its leading column, it is the index the
`ON DELETE CASCADE` from `users.user` probes. Both are enforced here
rather than by a pre-check, because two first sign-ins for one Google
account can reach `api-1` and `api-2` together.

## Rollback

`downgrade` drops the table. Every Google-only account keeps its
`users.user` row and becomes reachable by password reset alone, which is
the documented recovery path for those accounts anyway; nothing else
references this table.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f5444391dfec"
down_revision: str | Sequence[str] | None = "c7a91d4e60b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_identity",
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_external_identity")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user.id"],
            name=op.f("fk_external_identity__user_id"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "provider", "subject", name=op.f("uq_external_identity__provider_subject")
        ),
        sa.UniqueConstraint(
            "user_id", "provider", name=op.f("uq_external_identity__user_provider")
        ),
        sa.CheckConstraint(
            "provider IN ('google')", name=op.f("ck_external_identity__provider_known")
        ),
        sa.CheckConstraint(
            "char_length(subject) BETWEEN 1 AND 255",
            name=op.f("ck_external_identity__subject_length"),
        ),
        schema="users",
    )


def downgrade() -> None:
    op.drop_table("external_identity", schema="users")
