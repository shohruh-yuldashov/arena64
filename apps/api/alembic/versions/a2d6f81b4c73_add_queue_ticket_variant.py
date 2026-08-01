"""add queue_ticket.variant and re-lead the pool index

Revision ID: a2d6f81b4c73
Revises: e4b8c05d7a19
Create Date: 2026-08-02 10:41:18.336204

A64-015.2 gives a queue pool its third component. A64-015.1's pool was
`(queue_type, region)`, which was complete while one variant existed and
nothing scanned; a pool is really `(variant, mode, region)`, and a ticket
that does not record which game it is waiting for cannot be excluded from
the wrong pairing scan.

## Why now rather than with pairing

Because it is free now. No ticket exists in production — the table shipped
one task ago and nothing has queued — so this adds a column with a default
and drops the default in the same breath. The same change against live rows
would be a backfill, and against a running pairing scan it would be a
backfill plus a deploy order.

## `queue_variant` is a new enum with one member

Not `board_variant`, and not a text column. The type holds exactly the
variants a *player* may choose, which is `game.public.ProductVariant` —
one member today. The engine also plays `english_8x8`, deliberately kept
off the menu as a testing fixture (`specs/game-engine/audit.md` §9), and a
type that cannot express it is a stronger guarantee than a check somewhere
in the application.

Adding a second member later is `ALTER TYPE ... ADD VALUE`, which is
online in PostgreSQL 12+ and is the ordinary cost of shipping a variant.
This is the one enum in this schema whose members are *not* all declared up
front, and the reason is the inverse of the usual one: declaring
`english_8x8` here would make the fixture storable, which is precisely what
the type exists to prevent.

## The index leads with the variant

`ix_queue_ticket__pool` becomes `(variant, queue_type, region, entered_at,
id)`. A pairing scan names one whole pool, so the leading column should be
the widest partition — and a scan for one variant must never touch another
variant's rows even incidentally.

`ix_queue_ticket__due` is untouched: the expiry sweep is deliberately
pool-blind (one worker drains every pool), so a variant column at its front
would force one pass per variant per tick for no benefit.

`uq_queue_ticket__one_live_per_player` is untouched, and that is the point
of QT-1: one live ticket per player **across every pool**, so a player
cannot queue for Russian and international at once and be paired into two
matches.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a2d6f81b4c73"
down_revision: str | Sequence[str] | None = "e4b8c05d7a19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "matchmaking"

_VARIANT = postgresql.ENUM(
    "russian_8x8",
    name="queue_variant",
    schema=_SCHEMA,
    create_type=False,
)


def upgrade() -> None:
    _VARIANT.create(op.get_bind(), checkfirst=False)

    # A server default only so the column can be NOT NULL without a
    # backfill step, then dropped immediately: `variant` is a domain value
    # the application always supplies, and a default left in place is a
    # default somebody eventually relies on by forgetting to set it.
    op.add_column(
        "queue_ticket",
        sa.Column(
            "variant",
            _VARIANT,
            nullable=False,
            server_default=sa.text("'russian_8x8'::matchmaking.queue_variant"),
        ),
        schema=_SCHEMA,
    )
    op.alter_column("queue_ticket", "variant", server_default=None, schema=_SCHEMA)

    op.drop_index("ix_queue_ticket__pool", table_name="queue_ticket", schema=_SCHEMA)
    op.create_index(
        "ix_queue_ticket__pool",
        "queue_ticket",
        ["variant", "queue_type", "region", "entered_at", "id"],
        schema=_SCHEMA,
        postgresql_where=sa.text("status = 'waiting'"),
    )


def downgrade() -> None:
    op.drop_index("ix_queue_ticket__pool", table_name="queue_ticket", schema=_SCHEMA)
    op.create_index(
        "ix_queue_ticket__pool",
        "queue_ticket",
        ["queue_type", "region", "entered_at", "id"],
        schema=_SCHEMA,
        postgresql_where=sa.text("status = 'waiting'"),
    )

    op.drop_column("queue_ticket", "variant", schema=_SCHEMA)
    _VARIANT.drop(op.get_bind(), checkfirst=False)
