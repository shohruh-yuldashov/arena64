"""create reference schema and time_control catalogue

Revision ID: a3f91c7d5e42
Revises: e91b47c05fa3
Create Date: 2026-08-05 10:14:02.118440

A64-020.5A-pre creates the `reference` schema — database.md DB-08's home for
variants, time controls, regions and locales — and its first and only
relation.

Five files across three modules recorded this table's absence in the same
words, and each declined to invent a local substitute: `QueuePool`,
`game.domain.clock`, `game.public.matches`,
`matchmaking.presentation.schemas.matches` and `rating.domain.keys`. This
revision is what they were waiting for; the flow that consumes it lands in
the same change.

## `delay_ms` is deliberately absent

database.md §6.2 gives `reference.time_control` a `delay_ms` alongside
`base_time_ms` and `increment_ms`. It is not created here, and the
divergence is stated rather than silent (CLAUDE.md §3.11): `game`'s clock
implements Fischer increment only, so a delay column would be a documented
promise the adjudicator would ignore. When simple or Bronstein delay ships
it is a column and a `TimeControl` field, in one change.

## The seed is part of the migration

Four rows, written here rather than by an application bootstrap, for the
reason a schema object is: reference data the platform *is defined by* must
exist the instant the schema does. A queue join against an empty catalogue
is a `422` for every player, and an environment that has run its migrations
should not be able to reach that state.

Written as literals rather than imported from `TimeControlId` and friends,
per the convention every migration on this platform follows: a revision
describes the schema as it was at *this* point, and importing a live enum
would make an old migration change meaning the day somebody adds a member.
`tests/integration/test_time_control_catalogue.py` is what holds the two in
step.

## Reversibility

Complete. The table, its enum types and the schema are dropped — nothing
else lives in `reference`, so leaving it behind would leave an empty schema
no downgrade would ever remove. Dropping the enum types explicitly matters:
`drop_table` does not remove them, and a re-upgrade would then fail with
"type already exists".

The seeded rows go with the table, which is the honest downgrade: they are
catalogue data, not user data, and re-upgrading recreates them identically.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a3f91c7d5e42"
down_revision: str | Sequence[str] | None = "e91b47c05fa3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "reference"

#: Restated rather than imported — see this revision's docstring.
_TIME_CONTROL_IDS = ("bullet_1_0", "blitz_3_2", "rapid_10_0", "classical_30_0")

#: Every member of `rating`'s `SpeedClass`, including `correspondence`,
#: which no seeded row uses. Declared now because `ALTER TYPE ... ADD VALUE`
#: against a type used by a live table is a migration nobody should have to
#: schedule in order to ship a correspondence control — the same reasoning
#: `queue_region` records.
_SPEED_CLASSES = ("bullet", "blitz", "rapid", "classical", "correspondence")

#: The catalogue A64-020.5A-pre §2 approves, in display order.
#:
#: `(id, label, base_time_ms, increment_ms, speed_class, display_order)`.
#: Milliseconds throughout — `game.domain.clock` on why never seconds.
_SEED = (
    ("bullet_1_0", "1+0", 60_000, 0, "bullet", 0),
    ("blitz_3_2", "3+2", 180_000, 2_000, "blitz", 1),
    ("rapid_10_0", "10+0", 600_000, 0, "rapid", 2),
    ("classical_30_0", "30+0", 1_800_000, 0, "classical", 3),
)


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{_SCHEMA}"')

    time_control_id = postgresql.ENUM(*_TIME_CONTROL_IDS, name="time_control_id", schema=_SCHEMA)
    speed_class = postgresql.ENUM(*_SPEED_CLASSES, name="speed_class", schema=_SCHEMA)

    bind = op.get_bind()
    time_control_id.create(bind, checkfirst=True)
    speed_class.create(bind, checkfirst=True)

    time_control = op.create_table(
        "time_control",
        # `create_type=False`: the two types are created above, once, and
        # letting the column definitions create them again would attempt a
        # duplicate `CREATE TYPE` inside the same transaction.
        sa.Column(
            "id",
            postgresql.ENUM(
                *_TIME_CONTROL_IDS, name="time_control_id", schema=_SCHEMA, create_type=False
            ),
            nullable=False,
        ),
        sa.Column("label", sa.String(length=32), nullable=False),
        sa.Column("base_time_ms", sa.Integer(), nullable=False),
        sa.Column("increment_ms", sa.Integer(), nullable=False),
        sa.Column(
            "speed_class",
            postgresql.ENUM(*_SPEED_CLASSES, name="speed_class", schema=_SCHEMA, create_type=False),
            nullable=False,
        ),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.CheckConstraint("base_time_ms > 0", name="ck_time_control__base_time_positive"),
        sa.CheckConstraint("increment_ms >= 0", name="ck_time_control__increment_not_negative"),
        sa.CheckConstraint(
            "display_order >= 0", name="ck_time_control__display_order_not_negative"
        ),
        sa.CheckConstraint("length(label) > 0", name="ck_time_control__label_not_blank"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_time_control")),
        sa.UniqueConstraint("display_order", name="uq_time_control__display_order"),
        schema=_SCHEMA,
    )

    op.bulk_insert(
        time_control,
        [
            {
                "id": control_id,
                "label": label,
                "base_time_ms": base_time_ms,
                "increment_ms": increment_ms,
                "speed_class": speed,
                "display_order": display_order,
                "is_active": True,
            }
            for control_id, label, base_time_ms, increment_ms, speed, display_order in _SEED
        ],
    )


def downgrade() -> None:
    op.drop_table("time_control", schema=_SCHEMA)

    bind = op.get_bind()
    for name in ("speed_class", "time_control_id"):
        postgresql.ENUM(name=name, schema=_SCHEMA).drop(bind, checkfirst=True)

    op.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}"')
