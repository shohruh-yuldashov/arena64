"""add tournaments

A64-019.2. `database.md` §3.1 reserved the `tournaments` schema *"created
empty when the feature is specified"*. It is specified, so here it is.

    tournaments.tournament    one row per tournament
    tournaments.registration  one row per (tournament, player), ever

**No foreign key leaves this schema** (DB-03, R-3). `created_by` and
`player_id` are opaque cross-context identifiers (DM-06); a constraint into
`users` or `game` would make the schemas undeployable apart.

**Capacity has no constraint here**, deliberately: a row-level check cannot
see the other rows. It is enforced by `SELECT ... FOR UPDATE` on the
tournament row plus a count in the same transaction — stated so a reader
looking for the missing constraint finds the reason rather than the gap.

Revision ID: d4b7c3e12f80
Revises: c8f1a2d6e930
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.database.types import UtcDateTime

revision: str = "d4b7c3e12f80"
down_revision: str | None = "c8f1a2d6e930"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "tournaments"


def _enum(*values: str, name: str) -> postgresql.ENUM:
    """`create_type=False` — the types are created once, explicitly, below.

    Without it `create_table` emits `CREATE TYPE` again for the second
    table and fails on the duplicate.
    """
    return postgresql.ENUM(*values, name=name, schema=_SCHEMA, create_type=False)


#: Every format from day one — R-19's argument, which this migration
#: inherits: adding `swiss` after tournaments exist makes every historical
#: query about format wrong. Only `single_elimination` is *runnable*.
_FORMAT = _enum(
    "single_elimination",
    "double_elimination",
    "swiss",
    "round_robin",
    "arena",
    name="tournament_format",
)
_VARIANT = _enum("russian_8x8", name="tournament_variant")
_SPEED_CLASS = _enum(
    "bullet", "blitz", "rapid", "classical", "correspondence", name="tournament_speed_class"
)
_STATUS = _enum(
    "draft",
    "registration_open",
    "registration_closed",
    "in_progress",
    "completed",
    "cancelled",
    name="tournament_status",
)
_REGISTRATION_STATUS = _enum("registered", "withdrawn", name="registration_status")

_ALL_ENUMS = (_FORMAT, _VARIANT, _SPEED_CLASS, _STATUS, _REGISTRATION_STATUS)


def upgrade() -> None:
    op.execute(sa.schema.CreateSchema(_SCHEMA, if_not_exists=True))

    bind = op.get_bind()
    for enum in _ALL_ENUMS:
        enum.create(bind, checkfirst=True)

    op.create_table(
        "tournament",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("format", _FORMAT, nullable=False),
        sa.Column("variant", _VARIANT, nullable=False),
        sa.Column("speed_class", _SPEED_CLASS, nullable=False),
        sa.Column("status", _STATUS, nullable=False),
        sa.Column("rated", sa.Boolean(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("registration_deadline", UtcDateTime(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", UtcDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tournament")),
        sa.CheckConstraint(
            "capacity BETWEEN 2 AND 128", name=op.f("ck_tournament__capacity_in_range")
        ),
        schema=_SCHEMA,
    )

    # The deadline sweep's whole query. Partial: a tournament without a
    # deadline is never claimed, and a closed one never again.
    op.create_index(
        "ix_tournament__overdue",
        "tournament",
        ["registration_deadline"],
        schema=_SCHEMA,
        postgresql_where=sa.text(
            "status = 'registration_open' AND registration_deadline IS NOT NULL"
        ),
    )

    op.create_table(
        "registration",
        sa.Column("tournament_id", sa.UUID(), nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("status", _REGISTRATION_STATUS, nullable=False),
        sa.Column("registered_at", UtcDateTime(), nullable=False),
        sa.Column("withdrawn_at", UtcDateTime(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", UtcDateTime(), nullable=True),
        # **Two columns, not three.** The status is deliberately absent: a
        # withdrawn player cannot re-enter, because the key admits no second
        # row whatever the first one's status is. §4 permits no
        # re-registration, and this makes it structural.
        sa.PrimaryKeyConstraint("tournament_id", "player_id", name=op.f("pk_registration")),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            [f"{_SCHEMA}.tournament.id"],
            name="fk_registration__tournament",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "(status = 'withdrawn') = (withdrawn_at IS NOT NULL)",
            name=op.f("ck_registration__withdrawn_iff_instant"),
        ),
        schema=_SCHEMA,
    )

    # "How many slots are taken" — the count the capacity guard runs inside
    # its lock, over exactly the rows that count.
    op.create_index(
        "ix_registration__active",
        "registration",
        ["tournament_id"],
        schema=_SCHEMA,
        postgresql_where=sa.text("status = 'registered'"),
    )


def downgrade() -> None:
    op.drop_index("ix_registration__active", "registration", schema=_SCHEMA)
    op.drop_table("registration", schema=_SCHEMA)
    op.drop_index("ix_tournament__overdue", "tournament", schema=_SCHEMA)
    op.drop_table("tournament", schema=_SCHEMA)

    bind = op.get_bind()
    for enum in _ALL_ENUMS:
        enum.drop(bind, checkfirst=True)

    op.execute(sa.schema.DropSchema(_SCHEMA, if_exists=True))
