"""add time control to matchmaking.queue_ticket

Revision ID: b7d24e08f193
Revises: a3f91c7d5e42
Create Date: 2026-08-05 10:31:55.640217

A64-020.5A-pre §6 and §7: a queue pool is `(variant, mode, time control,
region)`, and a ticket carries a **snapshot** of the control it was entered
with rather than a reference to a catalogue row somebody may later edit.

Four columns, and the split between them is the point:

    time_control_id            pool identity — part of ix_queue_ticket__pool
    time_control_base_ms       the snapshot: what this player actually
    time_control_increment_ms  chose, fixed at entry
    speed_class                which rating a result would move

## No foreign key to `reference.time_control`

DM-06's opaque cross-context reference, and one reason specific to this
column: the snapshot exists precisely so that this row does not depend on
that one. A constraint asserting a dependency the design denies would also
make a *retired* control unable to keep its already-waiting tickets
pairable.

## Why this is not a nullable column with a backfill

The columns are `NOT NULL` with no server default and no backfill, which is
only safe because `matchmaking.queue_ticket` holds **no live rows in any
environment**: a ticket's whole lifetime is at most
`MATCHMAKING_TICKET_TTL_SECONDS` (ten minutes by default) and no release has
run a public queue. The table is created empty on every environment this
revision will meet.

Stating that rather than defending against it is the honest choice. A
`server_default` would be A64-020.5A-pre §16's "production-facing ambiguous
default" written into DDL — every historical ticket silently reclassified as
some control nobody chose — and there are no historical tickets to protect.

If this revision ever meets a non-empty table, `ALTER TABLE ... SET NOT
NULL` fails loudly, which is the correct outcome: the rows would have to be
resolved by a human who knows what those players asked for.

## Reversibility

Complete. The four columns are dropped, the index is restored to its
previous five-column shape, and the two enum types created here go with
them. The check constraint is dropped explicitly — `drop_column` removes a
constraint that names only dropped columns on PostgreSQL, but naming it
keeps the downgrade readable and independent of that behaviour.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b7d24e08f193"
down_revision: str | Sequence[str] | None = "a3f91c7d5e42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "matchmaking"

#: Restated rather than imported from the live enums, per the convention
#: every migration on this platform follows.
_TIME_CONTROL_IDS = ("bullet_1_0", "blitz_3_2", "rapid_10_0", "classical_30_0")
_SPEED_CLASSES = ("bullet", "blitz", "rapid", "classical", "correspondence")


def upgrade() -> None:
    bind = op.get_bind()

    time_control_id = postgresql.ENUM(*_TIME_CONTROL_IDS, name="queue_time_control", schema=_SCHEMA)
    speed_class = postgresql.ENUM(*_SPEED_CLASSES, name="queue_speed_class", schema=_SCHEMA)
    time_control_id.create(bind, checkfirst=True)
    speed_class.create(bind, checkfirst=True)

    op.add_column(
        "queue_ticket",
        sa.Column(
            "time_control_id",
            postgresql.ENUM(
                *_TIME_CONTROL_IDS,
                name="queue_time_control",
                schema=_SCHEMA,
                create_type=False,
            ),
            nullable=False,
        ),
        schema=_SCHEMA,
    )
    op.add_column(
        "queue_ticket",
        sa.Column("time_control_base_ms", sa.Integer(), nullable=False),
        schema=_SCHEMA,
    )
    op.add_column(
        "queue_ticket",
        sa.Column("time_control_increment_ms", sa.Integer(), nullable=False),
        schema=_SCHEMA,
    )
    op.add_column(
        "queue_ticket",
        sa.Column(
            "speed_class",
            postgresql.ENUM(
                *_SPEED_CLASSES, name="queue_speed_class", schema=_SCHEMA, create_type=False
            ),
            nullable=False,
        ),
        schema=_SCHEMA,
    )

    op.create_check_constraint(
        "ck_queue_ticket__time_control_sane",
        "queue_ticket",
        "time_control_base_ms > 0 AND time_control_increment_ms >= 0",
        schema=_SCHEMA,
    )

    # The pool index gains the new component in the position
    # `QueuePool.identifier()` puts it — before `region`. Dropped and
    # recreated rather than added beside, because two pool indexes would let
    # the planner choose the one that does not narrow by clock.
    op.drop_index("ix_queue_ticket__pool", "queue_ticket", schema=_SCHEMA)
    op.create_index(
        "ix_queue_ticket__pool",
        "queue_ticket",
        ["variant", "queue_type", "time_control_id", "region", "entered_at", "id"],
        schema=_SCHEMA,
        postgresql_where=sa.text("status = 'waiting'"),
    )


def downgrade() -> None:
    op.drop_index("ix_queue_ticket__pool", "queue_ticket", schema=_SCHEMA)
    op.create_index(
        "ix_queue_ticket__pool",
        "queue_ticket",
        ["variant", "queue_type", "region", "entered_at", "id"],
        schema=_SCHEMA,
        postgresql_where=sa.text("status = 'waiting'"),
    )

    op.drop_constraint(
        "ck_queue_ticket__time_control_sane", "queue_ticket", type_="check", schema=_SCHEMA
    )
    for column in (
        "speed_class",
        "time_control_increment_ms",
        "time_control_base_ms",
        "time_control_id",
    ):
        op.drop_column("queue_ticket", column, schema=_SCHEMA)

    bind = op.get_bind()
    for name in ("queue_speed_class", "queue_time_control"):
        postgresql.ENUM(name=name, schema=_SCHEMA).drop(bind, checkfirst=True)
