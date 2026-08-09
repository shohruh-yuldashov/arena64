"""Rename admin.role_assignment's account index to the project convention

Revision ID: f6b3d20a91c4
Revises: e5a1c94f27d8
Create Date: 2026-08-09

`a1c4e7b92f30` wrote the index name by hand — `op.f("ix_role_assignment_account_id")`
— and `op.f` means "this string is already final, do not apply the naming
convention". So the deployed index has one underscore where
`NAMING_CONVENTION`'s ``ix_%(table_name)s__%(column_0_N_name)s`` has two.

The model does not: it declares `index=True` on the column and lets the
convention name it, which yields `ix_role_assignment__account_id`. The two
have disagreed since A64-024.1, and `test_schema_drift.py` has been red
about it ever since — a `remove_index` / `add_index` pair naming exactly
these two spellings.

Nothing was broken by it. The index existed and PostgreSQL used it; only its
name was wrong. What it did break is the drift check itself, which is the
expensive part: an always-red comparison is one nobody reads, so the *next*
real divergence would have arrived into a test that was already failing.

Renaming rather than dropping and recreating: `ALTER INDEX ... RENAME` is a
catalogue update that takes a brief lock and does no work proportional to
the table, where a recreate would rebuild every row's entry for a change
that is purely cosmetic.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f6b3d20a91c4"
down_revision: str | Sequence[str] | None = "e5a1c94f27d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN_SCHEMA = "admin"
DEPLOYED = "ix_role_assignment_account_id"
CONVENTIONAL = "ix_role_assignment__account_id"


def upgrade() -> None:
    op.execute(f"ALTER INDEX {ADMIN_SCHEMA}.{DEPLOYED} RENAME TO {CONVENTIONAL}")


def downgrade() -> None:
    op.execute(f"ALTER INDEX {ADMIN_SCHEMA}.{CONVENTIONAL} RENAME TO {DEPLOYED}")
