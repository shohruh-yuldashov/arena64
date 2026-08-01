"""add gameplay preferences

Revision ID: b98e4defce66
Revises: a38576f7f4d6
Create Date: 2026-08-01 12:23:10.384684

A64-012.5 gives every account five gameplay settings, stored as one `jsonb`
document on `users.user`.

## Two deviations from database.md, both required by the task

§4.8 specifies a separate `users.player_preference` relation, 1:1 with the
profile, with a typed column per setting. §4.9 argues against `jsonb` for
preference data — no per-key constraint, and no index probe.

A64-012.5 specifies `jsonb` on the profile, and per CLAUDE.md's precedence
rule the task wins. `models.py` records the reasoning in full; in short, the
costs §4.8 and §4.9 describe are bounded here (five keys, empty for an
untouched account, and the notification dispatcher that motivated §4.9 is
deliberately out of scope) and the missing per-key constraint is answered in
the application instead — `extra="forbid"` at the boundary and
`GameplayPreferences.from_document` on read, which is database.md RK-9's own
prescription for `jsonb` payloads.

**The locale preferences do not move.** `preferred_language` and `timezone`
stay the columns they have been since A64-010; A64-012.5 groups them with
gameplay in the *domain* and behind one endpoint, which is a code change
rather than a schema one. That is why this migration adds one column and
touches nothing else.

## Why the default is `'{}'` rather than the full document

A row that has never been touched carries no opinion, so a later change to
a platform default reaches everyone who has not chosen otherwise — which is
the group it should reach, and only that group.

`GameplayPreferences.from_document` fills every absent key with its default,
so an empty object and a complete one are the same thing to a reader. That
is also what makes adding a sixth setting a code change with **no backfill
and no migration**: existing documents simply do not carry the new key, and
every row reads it as its default.

## Why this is safe to run on a populated table

PostgreSQL 11 and later add a `NOT NULL` column with a non-volatile default
without rewriting the table — the default is stored in the catalogue and
materialised on read. Metadata-only whatever the row count, and every
existing account lands on the platform defaults in the same statement.
Verified against PostgreSQL 17 by running upgrade -> downgrade -> upgrade
and inspecting the catalogue at each step.

Reversible: `downgrade` drops a column nothing else references. It discards
whatever each player had chosen, which is data loss in the strict sense —
but a column that no longer exists cannot be half-restored, and the
alternative is not what a downgrade means.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b98e4defce66"
down_revision: str | None = "a38576f7f4d6"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

SCHEMA = "users"
TABLE = "user"
COLUMN = "gameplay_preferences"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(
            COLUMN,
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column(TABLE, COLUMN, schema=SCHEMA)
