"""add user bio and country

Revision ID: dfb52eaf3e2b
Revises: 68e5e310737a
Create Date: 2026-08-01 05:28:51.757891

domain-model.md §7's presentational identity — "`UserProfile` owns display
name, avatar reference, country, biography, join date". A64-012.1 needs
the last two for the public profile view; the other three already exist.

## Autogenerate got the columns and missed both constraints

`alembic revision --autogenerate` produced the two `add_column` calls and
nothing else. That is expected rather than a bug — Alembic does not
compare `CheckConstraint`s — and it is exactly the trap the `please
adjust!` banner exists for: the model would have declared two constraints
the database did not have, `alembic check` would have reported no drift,
and the application's validators would have been the only thing enforcing
a rule BE-06 makes the database authoritative for.

Both are therefore hand-written below, named to match
`models.py.__table_args__` so the naming convention resolves them
identically.

## Why a plain `ADD CONSTRAINT` rather than `NOT VALID` + `VALIDATE`

Adding a `CHECK` normally takes an `ACCESS EXCLUSIVE` lock for a full
table scan, and the safe pattern on a live table is to add it `NOT VALID`
and validate separately under a weaker lock.

Not needed here, and it is worth recording why so the shortcut is not
copied into a migration where it matters: both columns are being created
in this same migration, so every row's value is `NULL` and `NULL` passes a
`CHECK` vacuously. The scan is over a table whose relevant columns cannot
fail. A later migration that tightens either rule against populated data
does need the two-step form.

Verified by running upgrade -> downgrade -> upgrade against real
PostgreSQL 17 and inspecting the catalogue at each step.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dfb52eaf3e2b"
down_revision: str | None = "68e5e310737a"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

SCHEMA = "users"
TABLE = "user"


def upgrade() -> None:
    op.add_column(
        TABLE,
        # `varchar(500)` rather than unbounded `text`. The bound is the
        # database's half of a rule the domain also enforces
        # (`BIO_MAX_LENGTH`), and an unbounded free-text column on a table
        # with a row per account is a storage-amplification surface.
        sa.Column("bio", sa.String(length=500), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        TABLE,
        # `char(2)` per database.md §4.6. No foreign key: `reference.country`
        # does not exist yet, and the format CHECK below is what stands in
        # for it until it does.
        sa.Column("country_code", sa.CHAR(length=2), nullable=True),
        schema=SCHEMA,
    )

    op.create_check_constraint(
        "bio_length",
        TABLE,
        "char_length(bio) <= 500",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "country_code_format",
        TABLE,
        "country_code ~ '^[A-Z]{2}$'",
        schema=SCHEMA,
    )


def downgrade() -> None:
    # Reversible and genuinely lossy: dropping these discards every bio and
    # every country on the platform, and unlike a credential table there is
    # no way for a player to regenerate the value — they typed it.
    #
    # Acceptable only because nothing writes either column yet (A64-012.1's
    # brief excludes editing, so every row is `NULL`). Once an edit
    # endpoint exists, reversing this migration is a data-loss event and
    # should be treated as one.
    op.drop_constraint(op.f("ck_user__country_code_format"), TABLE, type_="check", schema=SCHEMA)
    op.drop_constraint(op.f("ck_user__bio_length"), TABLE, type_="check", schema=SCHEMA)
    op.drop_column(TABLE, "country_code", schema=SCHEMA)
    op.drop_column(TABLE, "bio", schema=SCHEMA)
