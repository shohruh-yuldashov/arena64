"""add profile privacy settings

Revision ID: a38576f7f4d6
Revises: 3f4f9d8844ec
Create Date: 2026-08-01 11:47:02.084521

A64-012.4 gives every account five controls over what a stranger may see.
domain-model.md §7.1 puts privacy preferences inside `UserProfile` — "settings
are inside the profile, not beside it" — so they are columns on `users.user`
rather than a table of their own.

## Why five boolean columns and not one `jsonb`

`jsonb` is the obvious shortcut for a settings bag and is the wrong shape
here. Each of these is read on the platform's most-served endpoint, each
needs a database-level default that applies to rows this application did not
insert, and each will eventually be a predicate (`WHERE show_online_status`
on a "who is online" listing). A `jsonb` column gives up the default, the
NOT NULL and the plain b-tree index in exchange for schema-less growth this
table does not want — the same argument database.md DB-15 makes for a native
enum over a varchar. A sixth flag costs one migration, which is the right
amount of friction for a new disclosure.

## Why every column is NOT NULL with a server default

"No answer" is not a state a privacy control may be in. A nullable flag
would need a fallback on every read path, the fallbacks would drift, and the
first one written the wrong way round would publish something.

The defaults are `domain/privacy.py`'s constants, interpolated by
`models.py` rather than retyped — BE-06's rule that the database's
authoritative value cannot be allowed to disagree with the application's.
`show_last_seen` is the one that defaults to **false**: it is the only flag
covering an observed behavioural timestamp rather than a declared fact, and
a last-seen time published continuously is a sleep schedule.

## Why this is safe to run on a populated table

PostgreSQL 11 and later add a `NOT NULL` column with a non-volatile default
without rewriting the table — the default is stored in the catalogue and
materialised on read. So this is a metadata-only change on `users.user`
whatever its size, and every existing account lands on the platform defaults
in the same statement. Verified against PostgreSQL 17 by running
upgrade -> downgrade -> upgrade and inspecting `information_schema.columns`
at each step.

Genuinely reversible: `downgrade` drops five columns that nothing else
references. It does discard whatever each player had chosen, which is data
loss in the strict sense — but a column that no longer exists cannot be
half-restored, and the alternative (keeping the columns and un-wiring the
code) is not what a downgrade means.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a38576f7f4d6"
down_revision: str | None = "3f4f9d8844ec"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

SCHEMA = "users"
TABLE = "user"

#: `(column, default)`, in the order A64-012.4 lists them. A table rather
#: than five near-identical `op.add_column` calls, so that the one default
#: which differs — `show_last_seen` — is visible as a difference instead of
#: being buried in the middle of a wall of `true`s.
_PRIVACY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("show_country", "true"),
    ("show_last_seen", "false"),
    ("show_statistics", "true"),
    ("show_online_status", "true"),
    ("show_activity", "true"),
)


def upgrade() -> None:
    for column, default in _PRIVACY_COLUMNS:
        op.add_column(
            TABLE,
            sa.Column(column, sa.Boolean(), server_default=sa.text(default), nullable=False),
            schema=SCHEMA,
        )


def downgrade() -> None:
    # Reverse order, so the sequence of statements mirrors the upgrade even
    # though column drops are independent of each other. Costs nothing and
    # makes the two halves readable as a pair.
    for column, _ in reversed(_PRIVACY_COLUMNS):
        op.drop_column(TABLE, column, schema=SCHEMA)
