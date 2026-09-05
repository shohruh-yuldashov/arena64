"""add the broadcast audit action

Revision ID: c7a91d4e60b2
Revises: b6e37e8af2d6

A64-027A added `AuditAction.NOTIFICATION_BROADCAST_SENT` to the **Python**
enum. `admin.audit_action` is a PostgreSQL enum type, so without this the
audited half of every broadcast fails at `flush()` against a migrated
database:

    asyncpg.exceptions.InvalidTextRepresentationError:
    invalid input value for enum admin.audit_action:
    "notification.broadcast.send"

This is exactly the failure `e5a1c94f27d8` was written to clean up, and its
docstring explains why it recurs: `alembic revision --autogenerate` does not
detect enum **value** additions, and the contract suite builds its schema
with `Base.metadata.create_all`, which creates the type from the current
Python enum — so the tests run against a database that always has every
value and cannot observe the deployed one missing them.

`tests/contract/test_admin_audit_enums.py` is the guard for precisely this,
and it reads this directory rather than the schema. It failed on the
missing label, which is what sent this file into being.

## Why `downgrade` removes nothing

PostgreSQL has no `DROP VALUE`, and the usual workaround — remap the rows to
a surviving meaning — cannot be applied to `admin.audit_entry`: it is
append-only, enforced by a trigger that raises on `UPDATE`, `DELETE` and
`TRUNCATE`. A downgrade that remapped rows would have to disable the trigger
that exists to stop migrations doing exactly that.

So this downgrade is a no-op and says so. Rolling back past this revision
leaves entries whose action the older code cannot decode; that is the price
of an append-only record, and a downgrade that quietly edited history would
be worse.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c7a91d4e60b2"
down_revision: str | Sequence[str] | None = "b6e37e8af2d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """One statement, with the label spelled out.

    Deliberately not a loop over a tuple: the value a migration applies has
    to be legible **in the file**, both to somebody reading the history and
    to `tests/contract/test_admin_audit_enums.py`, which reads this
    directory as text.

    `IF NOT EXISTS` so a database that has already been repaired by hand is
    not a failed deploy.
    """
    op.execute(
        "ALTER TYPE admin.audit_action ADD VALUE IF NOT EXISTS 'notification.broadcast.send'"
    )


def downgrade() -> None:
    """Intentionally empty — see the module docstring."""
