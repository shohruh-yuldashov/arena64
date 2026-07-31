"""add user locked_until

Adds the temporary sign-in lock that A64-011.2's `AuthenticationService`
checks. Nullable with no default, so every existing row means "not
locked" without a backfill and without a table rewrite — PostgreSQL adds
a nullable column with no default as a catalogue-only change, so this is
safe on a live table of any size.

Nothing in A64-011.2 *sets* the column. Deliberate: NIST SP 800-63B
prefers throttling to hard lockout, an automatic lock after N failures
is itself a denial-of-service vector against a known address, and rate
limiting is explicitly out of scope for that task. The column exists so
that the check, the tests for it, and the wire contract are in place
before anything starts writing it.

Revision ID: 31528456f438
Revises: 3caf68aa8cfc
Create Date: 2026-08-01 01:20:20.116448
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.database.types import UtcDateTime

# revision identifiers, used by Alembic.
revision: str = "31528456f438"
down_revision: str | None = "3caf68aa8cfc"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "user"
_SCHEMA = "users"
_COLUMN = "locked_until"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, UtcDateTime(), nullable=True),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    # Reversible, and lossy only in the way dropping a column always is:
    # any active lock is forgotten, which fails *open* (people can sign
    # in) rather than closed. That is the correct direction for a
    # rollback — the alternative is a downgrade that locks people out.
    op.drop_column(_TABLE, _COLUMN, schema=_SCHEMA)
