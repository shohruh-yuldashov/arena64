"""add the audit enum values A64-024.5H/.6/.7 shipped without

Revision ID: e5a1c94f27d8
Revises: d4f2b83c05a1

`admin.audit_action` and `admin.audit_subject_type` were created by
`b2d5f8a41c70` (A64-024.8) with the two actions and the one subject that
existed then. Three later phases added members to the **Python** enums and
none of them added a migration:

    A64-024.6   admin.sanction.apply, admin.sanction.lift
    A64-024.7   notification.delivery.retry, subject `notification`
    A64-024.5H  tournament.create, tournament.registration_open,
                tournament.registration_close, tournament.start,
                tournament.transition_refused, subject `tournament`

So every audited mutation added after A64-024.8 fails at `flush()` against a
migrated database:

    asyncpg.exceptions.InvalidTextRepresentationError:
    invalid input value for enum admin.audit_action: "tournament.create"

Moderation restrict/restore and the push-delivery retry were equally dead;
only the tournament path had been exercised against a real database.

## Why nothing caught it

`alembic revision --autogenerate` does not detect enum **value** additions —
`7989d2c17008`'s docstring already recorded that and it held again here.

And the contract suite builds its schema with `Base.metadata.create_all`,
which creates the type from the *current* Python enum. So the tests ran
against a database that always had every value, and could not have observed
the deployed one missing them. `tests/contract/test_admin_audit_enums.py`
is the guard that closes it, and it reads the migrations rather than the
schema for exactly that reason.

## Why `downgrade` removes nothing

PostgreSQL has no `DROP VALUE`, and `7989d2c17008`'s convention — leave the
label, remap the rows to a surviving meaning — **cannot be applied here**.
`admin.audit_entry` is append-only, enforced by a trigger that raises on
`UPDATE`, `DELETE` and `TRUNCATE` (A64-024.8). A downgrade that remapped
rows would have to disable that trigger, which is precisely the guarantee
the trigger exists to keep against migrations.

So this downgrade is a **no-op**, and says so rather than pretending. The
consequence is stated plainly: rolling back past this revision leaves
entries whose action the older code cannot decode, and reading the trail
would raise. That is the price of an append-only record, and the honest
alternative — a downgrade that quietly edited history — is worse.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e5a1c94f27d8"
down_revision: str | Sequence[str] | None = "d4f2b83c05a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Every label spelled out, one statement each.

    Deliberately not a loop over a tuple. A migration is a snapshot of an
    intent, and the value it applies has to be legible **in the file** —
    both to somebody reading the history and to
    `tests/contract/test_admin_audit_enums.py`, which reads this directory
    to learn what the deployed schema contains. An f-string over a list
    leaves `'{action}'` in the source and tells neither of them anything.

    `IF NOT EXISTS` makes a re-run a no-op, and makes this safe against a
    database built with `create_all`, which already has every label.

    `ADD VALUE` may not be *used* later in the transaction that adds it.
    Nothing here inserts, so the plain statements are safe on PostgreSQL 12+.
    """
    # A64-024.6 — moderation.
    op.execute("ALTER TYPE admin.audit_action ADD VALUE IF NOT EXISTS 'admin.sanction.apply'")
    op.execute("ALTER TYPE admin.audit_action ADD VALUE IF NOT EXISTS 'admin.sanction.lift'")

    # A64-024.7 — notification operations.
    op.execute(
        "ALTER TYPE admin.audit_action ADD VALUE IF NOT EXISTS 'notification.delivery.retry'"
    )
    op.execute("ALTER TYPE admin.audit_subject_type ADD VALUE IF NOT EXISTS 'notification'")

    # A64-024.5H — tournament administration.
    op.execute("ALTER TYPE admin.audit_action ADD VALUE IF NOT EXISTS 'tournament.create'")
    op.execute(
        "ALTER TYPE admin.audit_action ADD VALUE IF NOT EXISTS 'tournament.registration_open'"
    )
    op.execute(
        "ALTER TYPE admin.audit_action ADD VALUE IF NOT EXISTS 'tournament.registration_close'"
    )
    op.execute("ALTER TYPE admin.audit_action ADD VALUE IF NOT EXISTS 'tournament.start'")
    op.execute(
        "ALTER TYPE admin.audit_action ADD VALUE IF NOT EXISTS 'tournament.transition_refused'"
    )
    op.execute("ALTER TYPE admin.audit_subject_type ADD VALUE IF NOT EXISTS 'tournament'")


def downgrade() -> None:
    """Deliberately empty — see the module docstring.

    The labels stay because PostgreSQL cannot drop one, and the rows stay
    because `admin.audit_entry` refuses `UPDATE` by trigger. Removing either
    would mean editing an append-only record from a migration.
    """
