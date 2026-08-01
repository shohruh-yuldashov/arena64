"""narrow display name to 3 50

Revision ID: 3f4f9d8844ec
Revises: 4c911b0abc5f
Create Date: 2026-08-01 07:14:52.118034

A64-012.3 makes `display_name` editable and specifies 3-50 characters.
A64-010's column was `varchar(64)` with no lower bound and no CHECK, which
was adequate while registration was the only writer and is not adequate for
a field any account can rewrite daily.

## Why the bound moves in the database and not only in the validator

BE-06 makes the database the authoritative check, and this repository
already applies that to `username` — `3caf68aa8cfc` moved its CHECK when
the domain bound changed. A validator-only narrowing would leave PostgreSQL
accepting a 64-character name the application refuses: a disagreement that
surfaces as a constraint nobody can explain the first time a value arrives
from a path that skips the validator.

## Why the length CHECK is added rather than relying on `varchar(50)`

`varchar(50)` bounds the maximum and says nothing about the minimum, and
the minimum is the half that matters here: a one-character display name
renders as near-nothing beside an avatar, which is an impersonation shape
rather than a formatting quirk. `ck_user__display_name_length` carries
both, interpolated from the domain's own constants so the two cannot
drift. `NULL` passes — "no display name" stays a legitimate state.

## Why this is safe to run without a backfill

Checked against the development database before writing: zero rows hold a
`display_name` outside 3-50, so the type change truncates nothing and the
CHECK validates against values that already conform.

That will not be true forever. A deployment with real data must confirm
the same query returns zero before applying, and shorten or clear the
offending rows first if it does not. `ALTER TYPE` to a shorter `varchar`
**errors** on over-length rows rather than truncating them, so the failure
is loud — but it is still a failed migration rather than a clean one.

Verified by running upgrade -> downgrade -> upgrade against real
PostgreSQL 17 and inspecting the catalogue at each step.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3f4f9d8844ec"
down_revision: str | None = "4c911b0abc5f"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

SCHEMA = "users"
TABLE = "user"


def upgrade() -> None:
    op.alter_column(
        TABLE,
        "display_name",
        existing_type=sa.VARCHAR(length=64),
        type_=sa.String(length=50),
        existing_nullable=True,
        schema=SCHEMA,
    )
    # Autogenerate does not compare CheckConstraints — hand-written, and
    # named to match `models.py.__table_args__` so the naming convention
    # resolves it identically.
    op.create_check_constraint(
        "display_name_length",
        TABLE,
        "display_name IS NULL OR char_length(display_name) BETWEEN 3 AND 50",
        schema=SCHEMA,
    )


def downgrade() -> None:
    # Widening back is lossless: every value that fits 3-50 also fits 64,
    # and dropping the CHECK only stops future rows being validated.
    # Genuinely reversible, unlike the avatar migration before it.
    op.drop_constraint(op.f("ck_user__display_name_length"), TABLE, type_="check", schema=SCHEMA)
    op.alter_column(
        TABLE,
        "display_name",
        existing_type=sa.String(length=50),
        type_=sa.VARCHAR(length=64),
        existing_nullable=True,
        schema=SCHEMA,
    )
