"""add live game clocks

Revision ID: 076977bf9233
Revises: 6926ccefaef6
Create Date: 2026-08-04

A64-016.5 §1 and §3. Five nullable columns on `game.match`, one on
`game.move`, and the two clock columns on `game.move` that already existed
and were always null now get filled by the move transaction.

## Why every column is nullable

`reference.time_control` (database.md §6.2) does not exist, so `QueuePool`
cannot carry one and `matchmaking` cannot supply one. Every match this
platform has created is therefore **untimed**, and an untimed match must keep
working exactly as it does: no deadline, no flag, null clock columns on its
moves.

Nullable is the honest model rather than a default budget. A placeholder
control with an enormous initial time would be a number somebody eventually
treats as real, and there is no correct value for "this game has no clock".

## No active-side column

It equals the position's side to move, which equals `ply_number` parity from
the opening — LIGHT moves first. A column would be a third copy of one fact
and the one most likely to drift, because it changes on every move.

## Reversibility

Complete and symmetric: five column drops and two constraint drops. Dropping
them **discards every clock**, which is honest — the downgrade target has no
column to hold one, and the durable move log keeps the per-move readings
either way.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.database.types import UtcDateTime

revision: str = "076977bf9233"
down_revision: str | Sequence[str] | None = "6926ccefaef6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "game"

_COLUMNS = (
    ("time_control_initial_ms", sa.Integer()),
    ("time_control_increment_ms", sa.Integer()),
    ("clock_light_ms", sa.Integer()),
    ("clock_dark_ms", sa.Integer()),
    ("clock_turn_started_at", UtcDateTime()),
)


def upgrade() -> None:
    # MT-9's temporal authority on the move log — §3. Nullable because every
    # move written before this revision has none, and null means "before
    # A64-016.5" rather than "unknown when": a backfilled guess would be a
    # flag decision made from a number nobody measured.
    op.add_column("move", sa.Column("received_at", UtcDateTime(), nullable=True), schema=_SCHEMA)

    for name, kind in _COLUMNS:
        op.add_column("match", sa.Column(name, kind, nullable=True), schema=_SCHEMA)

    # BE-06: the database's copy of `MatchRecord.__post_init__`'s pairing
    # check. A budget nothing counts down and a countdown with no budget are
    # both matches nothing can adjudicate.
    op.execute(
        f"""
        ALTER TABLE {_SCHEMA}.match ADD CONSTRAINT ck_match__clock_iff_time_control CHECK (
            (time_control_initial_ms IS NULL) = (clock_light_ms IS NULL)
            AND (time_control_initial_ms IS NULL) = (clock_dark_ms IS NULL)
            AND (time_control_initial_ms IS NULL) = (clock_turn_started_at IS NULL)
            AND (time_control_initial_ms IS NULL) = (time_control_increment_ms IS NULL)
        )
        """
    )
    op.execute(
        f"""
        ALTER TABLE {_SCHEMA}.match ADD CONSTRAINT ck_match__clock_values_sane CHECK (
            time_control_initial_ms IS NULL OR (
                time_control_initial_ms > 0
                AND time_control_increment_ms >= 0
                AND clock_light_ms >= 0
                AND clock_dark_ms >= 0
            )
        )
        """
    )


def downgrade() -> None:
    for constraint in ("ck_match__clock_values_sane", "ck_match__clock_iff_time_control"):
        op.execute(f"ALTER TABLE {_SCHEMA}.match DROP CONSTRAINT {constraint}")

    for name, _ in reversed(_COLUMNS):
        op.drop_column("match", name, schema=_SCHEMA)

    op.drop_column("move", "received_at", schema=_SCHEMA)
