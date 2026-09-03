"""add match origin

A64-019.0, `domain-model.md` R-25. `game.match` gains where a match came
from and an **opaque** reference to the context that produced it.

    origin      queue | challenge | rematch | tournament
    origin_ref  uuid, nullable, no foreign key

`services.md` §11.3 and `database.md` §18.3 both claim tournaments need no
new mechanism *because* this reference exists. It did not; this is it.

**No foreign key**, deliberately (DB-03). A constraint into `tournaments`
would make the two schemas undeployable apart — the seam `architecture.md`
§16 keeps open — and would outlive its usefulness anyway: a tournament is
prunable and a match is permanent.

`server_default = 'queue'` rather than a backfill. Every match written
before this column existed came from the queue, so the default states a
fact rather than guessing one, and the migration needs no data pass.

Revision ID: c8f1a2d6e930
Revises: a3c1d9f47b20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c8f1a2d6e930"
down_revision: str | None = "a3c1d9f47b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "game"

#: All four members from day one — `domain-model.md` R-19's argument, which
#: this file inherits: adding `tournament` after months of matches were
#: recorded as `queue` makes every historical query about origin wrong and
#: unfixable.
_ORIGIN = postgresql.ENUM(
    "queue",
    "challenge",
    "rematch",
    "tournament",
    name="match_origin",
    schema=_SCHEMA,
    create_type=False,
)


def upgrade() -> None:
    _ORIGIN.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "match",
        sa.Column("origin", _ORIGIN, nullable=False, server_default=sa.text("'queue'")),
        schema=_SCHEMA,
    )
    op.add_column("match", sa.Column("origin_ref", sa.Uuid(), nullable=True), schema=_SCHEMA)

    # "Every match this context produced" — the read a tournament makes to
    # reconcile its own round. Partial, because `origin_ref` is null for the
    # queue matches that are almost all of the table today.
    op.create_index(
        "ix_match__origin_ref",
        "match",
        ["origin", "origin_ref"],
        schema=_SCHEMA,
        postgresql_where=sa.text("origin_ref IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_match__origin_ref", "match", schema=_SCHEMA)
    op.drop_column("match", "origin_ref", schema=_SCHEMA)
    op.drop_column("match", "origin", schema=_SCHEMA)
    _ORIGIN.drop(op.get_bind(), checkfirst=True)
