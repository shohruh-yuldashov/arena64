"""replace avatar url with object key

Revision ID: 4c911b0abc5f
Revises: dfb52eaf3e2b
Create Date: 2026-08-01 06:41:12.338907

database.md §4.6: "`avatar_object_key` | `text` | **Object-storage key, not
a URL**". A64-010 shipped `avatar_url text` before any storage existed;
A64-012.2 is the task that makes the design's column real.

## Why the URL column is dropped rather than kept

A stored URL bakes the CDN hostname, the bucket name and the URL scheme
into every row. Changing provider, putting a CDN in front, or moving a
bucket then becomes a data migration over the whole `users.user` table
rather than one environment variable — and any row missed keeps pointing at
the old host until somebody notices a broken image.

A key is the object's address and nothing else.
`StorageProvider.get_public_url` composes the rest at render time.

## Why this drops data without a backfill

`avatar_url` is dropped, not converted. There is nothing to convert: no
endpoint has ever written a value that persisted (A64-010 exposed it on
`PATCH /users/{id}`, which A64-012.2 removes), no migration seeded it, and
no object store existed to hold what such a URL would point at. Checked
against the development database before writing this — zero non-null
values.

Had there been rows, this would need a two-phase migration: add the new
columns, backfill by fetching each URL into the store, then drop. The fact
that it does not is worth recording so the shortcut is not copied into a
migration where data exists.

## The three columns are one fact

`avatar_object_key`, `avatar_uploaded_at` and `avatar_version` describe a
single thing, and `ck_user__avatar_reference_is_complete` is the database
refusing to hold half of it: a key with no timestamp renders an avatar
nobody can date, and a timestamp with no key claims an upload that is not
there. `User.set_avatar`/`clear_avatar` move all three together; the CHECK
is what makes that a guarantee rather than a convention (BE-06).

`avatar_version` is `NOT NULL DEFAULT 1`, so every existing row backfills
to 1 in the same statement — correct rather than incidental, since no
cached avatar exists for any of them.

Verified by running upgrade -> downgrade -> upgrade against real
PostgreSQL 17 and inspecting the catalogue at each step.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.database.types import UtcDateTime

# revision identifiers, used by Alembic.
revision: str = "4c911b0abc5f"
down_revision: str | None = "dfb52eaf3e2b"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

SCHEMA = "users"
TABLE = "user"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("avatar_object_key", sa.String(length=512), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        TABLE,
        sa.Column("avatar_uploaded_at", UtcDateTime(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        TABLE,
        # `server_default` backfills every existing row in the same
        # statement, which is what lets the column be `NOT NULL` without a
        # separate update pass. DB-19 keeps the database default as the
        # backstop; the application sets the value explicitly.
        sa.Column("avatar_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        schema=SCHEMA,
    )

    # Autogenerate does not compare CheckConstraints — these are
    # hand-written, and named to match `models.py.__table_args__` so the
    # naming convention resolves them identically. Both are cheap here: the
    # columns were created moments ago, so the scan is over values that
    # cannot fail.
    op.create_check_constraint(
        "avatar_reference_is_complete",
        TABLE,
        "(avatar_object_key IS NULL) = (avatar_uploaded_at IS NULL)",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "avatar_version_positive",
        TABLE,
        "avatar_version >= 1",
        schema=SCHEMA,
    )

    # Last, so the new shape is in place before the old column goes. See
    # this migration's docstring on why no backfill is owed.
    op.drop_column(TABLE, "avatar_url", schema=SCHEMA)


def downgrade() -> None:
    # Restores the column and its nullability, not its contents — there
    # were none. Every avatar uploaded *since* this migration becomes
    # unreachable: the objects survive in the store, but nothing records
    # which player each belongs to.
    #
    # That makes this a one-way door in practice once uploads have
    # happened, and it is worth saying so rather than implying the symmetry
    # a `downgrade` signature suggests. Reversing after real uploads needs
    # the object keys exported first.
    op.add_column(
        TABLE,
        sa.Column("avatar_url", sa.VARCHAR(length=2048), autoincrement=False, nullable=True),
        schema=SCHEMA,
    )
    op.drop_constraint(
        op.f("ck_user__avatar_version_positive"), TABLE, type_="check", schema=SCHEMA
    )
    op.drop_constraint(
        op.f("ck_user__avatar_reference_is_complete"), TABLE, type_="check", schema=SCHEMA
    )
    op.drop_column(TABLE, "avatar_version", schema=SCHEMA)
    op.drop_column(TABLE, "avatar_uploaded_at", schema=SCHEMA)
    op.drop_column(TABLE, "avatar_object_key", schema=SCHEMA)
