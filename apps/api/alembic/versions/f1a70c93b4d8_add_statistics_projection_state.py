"""add statistics projection state

Revision ID: f1a70c93b4d8
Revises: e5b3f7a01c94
Create Date: 2026-08-05 21:10:44.812903

A64-020.5F §7. `statistics` shipped as the *reading* half of a projection
— storage, a repository, a service and a published port — because the
writing half is a consumer of `game.match_completed` and there was no
`game` module to emit one. There is now, so this adds the two things a
writer needs.

## `statistics.processed_match`

The exactly-once mechanism, and it is **structural rather than
procedural**: `PRIMARY KEY (match_id, player_id)` makes a second
application impossible. A read-then-check would be a race under two relay
processes, and the platform's `processed_event` ledger cannot serve here
because it is keyed by *event* id and the backfill has no event to key on.

The match and the player is the pair both paths share, so a match counted
live and the same match reached by a backfill collide on the key and the
second is refused. §5's "backfill overlapping with live consumption" is
answered by that collision and by nothing else.

**No foreign keys** — to `game.match` or to `users.user` — for the reason
`player_statistics.player_id` already records: a `statistics` schema that
could not deploy without `game`'s would make architecture.md §16's
extraction seam decorative (DM-06).

## The watermark on `player_statistics`

`counted_at` and `counted_match_id`: the total-order position of the last
match folded into the row. Only the **streak** reads it — the counts
commute, so a late match still belongs in the totals, while a streak is a
statement about the most recent games and folding an older one into it
would describe a sequence that never happened.

Two columns rather than a timestamp because a timestamp alone is not a
total order: two matches finishing in the same instant compare equal, and
"which came last" would then depend on arrival order. A `CHECK` keeps the
pair whole, because a half-set watermark is a row whose ordering question
has no answer.

## No data backfill here

Deliberately, and §10 requires it. Historical matches are replayed by an
explicit operator command — schema migration and historical replay are
different operational concerns, and a migration that counted 74 matches
would be one nobody could re-run, resume or dry-run.

Every existing row keeps its counters untouched and gains a null
watermark, which reads as "has counted nothing" — correct for a table that
is empty in every environment today, and correct for a non-empty one too.

## Reversibility

Fully reversible. `downgrade` drops the relation and the two columns,
returning both to their previous shape. What is lost is the record of
which matches were counted — so a downgrade followed by an upgrade must be
followed by a backfill, which is idempotent and says so.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.database.types import UtcDateTime

revision: str = "f1a70c93b4d8"
down_revision: str | Sequence[str] | None = "e5b3f7a01c94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "statistics"


def upgrade() -> None:
    op.create_table(
        "processed_match",
        sa.Column("match_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("player_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("processed_at", UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("match_id", "player_id", name="pk_processed_match"),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_processed_match__player",
        "processed_match",
        ["player_id", "processed_at"],
        schema=_SCHEMA,
    )

    op.add_column(
        "player_statistics",
        sa.Column("counted_at", UtcDateTime(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        "player_statistics",
        sa.Column("counted_match_id", sa.Uuid(as_uuid=True), nullable=True),
        schema=_SCHEMA,
    )

    # Raw DDL rather than `create_check_constraint`, for the reason
    # `6926ccefaef6` records: the metadata naming convention prefixes the
    # name a second time, so a full name passed to the helper is created as
    # `ck_player_statistics__ck_player_statistics__…` and the downgrade
    # cannot find it.
    op.execute(
        f"ALTER TABLE {_SCHEMA}.player_statistics "
        "ADD CONSTRAINT ck_player_statistics__watermark_is_whole "
        "CHECK ((counted_at IS NULL) = (counted_match_id IS NULL))"
    )


def downgrade() -> None:
    op.execute(
        f"ALTER TABLE {_SCHEMA}.player_statistics "
        "DROP CONSTRAINT ck_player_statistics__watermark_is_whole"
    )
    op.drop_column("player_statistics", "counted_match_id", schema=_SCHEMA)
    op.drop_column("player_statistics", "counted_at", schema=_SCHEMA)
    op.drop_index("ix_processed_match__player", table_name="processed_match", schema=_SCHEMA)
    op.drop_table("processed_match", schema=_SCHEMA)
