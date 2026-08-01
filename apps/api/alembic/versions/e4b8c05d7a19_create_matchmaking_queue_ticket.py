"""create matchmaking schema and queue_ticket

Revision ID: e4b8c05d7a19
Revises: c7a1f2e93b45
Create Date: 2026-08-01 23:22:47.902551

A64-014.1 creates the `matchmaking` schema — database.md §222's one schema
per bounded context — and its first relation.

## This relation contradicts database.md §8.1 as it was written

§8.1 said "**Queue tickets are absent from PostgreSQL entirely.**" That
sentence has been rewritten in the same change (CLAUDE.md §3.11), and the
argument is recorded in full in
`app/modules/matchmaking/infrastructure/models.py`. In short: QT-4's atomic
claim and QT-1's one-live-ticket rule are both *constraints under
concurrency*, and A64-014.1 forbids inventing a concurrency mechanism to
hold them — `SELECT ... FOR UPDATE SKIP LOCKED` and a partial unique index
are the platform's proven answers to exactly those two problems, and both
exist only here.

## The three enum types

`queue_type`, `queue_region` and `queue_ticket_status`, all native
PostgreSQL enums in this schema (DB-15). Every member of each is declared
now, including the two statuses nothing writes yet (`matched`) and the six
regions nothing selects yet: `ALTER TYPE ... ADD VALUE` against a type used
by a live table is a migration nobody should have to schedule in order to
ship pairing — the same reasoning `friendship_end_reason` records.

`queue_region` is reference data wearing an enum's clothes, knowingly. DB-08
puts it in a `reference` schema alongside variants and time controls; no
such schema exists in code, and creating one for a single closed list would
be speculative. The values are chosen to survive that move unchanged.

## `uq_queue_ticket__one_live_per_player` is the load-bearing object here

Partial unique on `player_id` where `status = 'waiting'`. Two properties,
and both are the reason this table exists:

  - It keys on the player **alone**, not on the pool. QT-1 is "one live
    ticket per player across all pools", and a key including `queue_type`
    would permit exactly the multi-queueing that pairs somebody into two
    simultaneous matches.
  - It is **partial**, for the reason `uq_friendship__pair` is: a plain
    unique would mean a player could queue once, ever.

It is also the index `active_ticket` reads, which is why this table carries
no separate index on `player_id`.

## The two other indexes, and why they are separate

`ix_queue_ticket__pool` leads with `(queue_type, region)` because every
snapshot and every future pairing scan names one pool and reads it
oldest-first. `ix_queue_ticket__due` leads with `expires_at` because the
expiry sweep is deliberately pool-*blind* — one worker drains every pool,
and a scan that had to lead with `queue_type` would need one pass per pool
per tick.

Both are partial on `waiting`, so their size is bounded by concurrency
rather than by history — the same property that makes
`ix_outbox__unpublished` a direct measure of relay health.

## No foreign key on `player_id`

DM-06: cross-context references are opaque `uuid` values. A foreign key into
`users` would make the two schemas undeployable apart, which is the seam
architecture.md §16 exists to keep open. `friends` and `statistics` make the
identical choice.

## No `fillfactor`, unlike `platform.outbox`

The churn shape is the same — written once, updated once, then dead — so
DB-18 looks like it applies. It does not: `fillfactor` buys HOT updates, an
update is HOT only when no indexed column changes, and all three indexes
above are predicated on `status`, which is the one column the one update
writes. Reserving free space would cost storage and buy nothing.

## Reversibility

Complete. The table, its indexes and all three enum types are dropped, and
the schema with them — nothing else lives in `matchmaking` yet, so leaving
it behind would leave an empty schema no downgrade would ever remove.
Dropping the enum types explicitly matters: `drop_table` does not remove
them, and a re-upgrade would then fail with "type already exists".
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e4b8c05d7a19"
down_revision: str | Sequence[str] | None = "c7a1f2e93b45"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "matchmaking"

#: Restated rather than imported from the domain enums, per the convention
#: every migration on this platform follows: a revision describes the schema
#: as it was at *this* point, and importing a live enum would make an old
#: migration change meaning the day somebody adds a member.
_QUEUE_TYPES = ("ranked", "casual")
_REGIONS = (
    "global",
    "europe",
    "north_america",
    "south_america",
    "asia",
    "africa",
    "oceania",
)
_STATUSES = ("waiting", "matched", "cancelled", "expired")


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{_SCHEMA}"')

    queue_type = postgresql.ENUM(*_QUEUE_TYPES, name="queue_type", schema=_SCHEMA)
    region = postgresql.ENUM(*_REGIONS, name="queue_region", schema=_SCHEMA)
    status = postgresql.ENUM(*_STATUSES, name="queue_ticket_status", schema=_SCHEMA)

    bind = op.get_bind()
    queue_type.create(bind, checkfirst=True)
    region.create(bind, checkfirst=True)
    status.create(bind, checkfirst=True)

    op.create_table(
        "queue_ticket",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("player_id", sa.Uuid(as_uuid=True), nullable=False),
        # `create_type=False`: the three types are created above, once, and
        # letting the column definitions create them again would attempt a
        # duplicate `CREATE TYPE` inside the same transaction.
        sa.Column(
            "queue_type",
            postgresql.ENUM(*_QUEUE_TYPES, name="queue_type", schema=_SCHEMA, create_type=False),
            nullable=False,
        ),
        sa.Column(
            "region",
            postgresql.ENUM(*_REGIONS, name="queue_region", schema=_SCHEMA, create_type=False),
            nullable=False,
        ),
        sa.Column("rating_snapshot", sa.Integer(), nullable=False),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                *_STATUSES, name="queue_ticket_status", schema=_SCHEMA, create_type=False
            ),
            server_default="waiting",
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'waiting') = (resolved_at IS NULL)",
            name="ck_queue_ticket__resolved_iff_terminal",
        ),
        sa.CheckConstraint("expires_at > entered_at", name="ck_queue_ticket__window_positive"),
        sa.CheckConstraint("rating_snapshot >= 0", name="ck_queue_ticket__rating_non_negative"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_queue_ticket")),
        schema=_SCHEMA,
    )

    # QT-1. See this revision's docstring on why the key is the player alone
    # and why it is partial.
    op.create_index(
        "uq_queue_ticket__one_live_per_player",
        "queue_ticket",
        ["player_id"],
        unique=True,
        schema=_SCHEMA,
        postgresql_where=sa.text("status = 'waiting'"),
    )
    op.create_index(
        "ix_queue_ticket__pool",
        "queue_ticket",
        ["queue_type", "region", "entered_at", "id"],
        schema=_SCHEMA,
        postgresql_where=sa.text("status = 'waiting'"),
    )
    op.create_index(
        "ix_queue_ticket__due",
        "queue_ticket",
        ["expires_at"],
        schema=_SCHEMA,
        postgresql_where=sa.text("status = 'waiting'"),
    )


def downgrade() -> None:
    op.drop_index("ix_queue_ticket__due", "queue_ticket", schema=_SCHEMA)
    op.drop_index("ix_queue_ticket__pool", "queue_ticket", schema=_SCHEMA)
    op.drop_index("uq_queue_ticket__one_live_per_player", "queue_ticket", schema=_SCHEMA)
    op.drop_table("queue_ticket", schema=_SCHEMA)

    bind = op.get_bind()
    for name in ("queue_ticket_status", "queue_region", "queue_type"):
        postgresql.ENUM(name=name, schema=_SCHEMA).drop(bind, checkfirst=True)

    op.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}"')
