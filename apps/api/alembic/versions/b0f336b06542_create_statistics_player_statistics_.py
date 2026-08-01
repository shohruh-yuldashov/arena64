"""create statistics player statistics table

Revision ID: b0f336b06542
Revises: b98e4defce66
Create Date: 2026-08-01 12:50:32.676157

A64-012.6 gives the `statistics` bounded context its first relation.

## `CREATE SCHEMA statistics`

Alembic detects tables in a non-default schema but does not create the
schema itself, so autogenerate produced a `create_table` that would fail on
a database where `statistics` does not exist. The `op.execute` below is
hand-added, exactly as `7ed700e67f2a` did for `users` and `00debbb7452d`
for `auth`.

`IF NOT EXISTS` because the schema may already have been created by a
previous partial run; creating a schema is idempotent in intent even though
PostgreSQL's bare `CREATE SCHEMA` is not.

## What this table is

A **projection** (domain-model.md DM-03, §11.5; database.md C5): every
column is a count or a peak derived from match history, nothing here is the
system of record for anything, and the whole relation is truncatable and
rebuildable. Two consequences visible in the DDL:

  - **No row means "no matches played."** A row is written the first time a
    result is folded in, so an account that has never played simply has
    none, and the reader returns the empty record rather than treating
    absence as an error. There is no backfill in this migration and none is
    needed.
  - **The CHECK constraints are arithmetic, not policy.** They restate what
    `PlayerStatistics.__post_init__` enforces, so a row written by a
    rebuild script — which does not go through the entity — cannot be
    inconsistent either (BE-06). `games_played = wins + losses + draws` is
    the load-bearing one: it is what stops a win rate above 100% from ever
    reaching a screen.

## No foreign key to `users.user`

Deliberate, and the one thing in this file most likely to look like an
omission. Cross-context references are opaque `player_id` values (DM-06),
and a foreign key from `statistics` into `users` would make the two schemas
undeployable apart — which is precisely the extraction seam architecture.md
§16 exists to keep real. database.md §1611 goes further and keeps
`player_id` here as a *tombstone* after erasure, which a cascade would
delete.

## Two documented deviations from database.md §9.5

The primary key is `player_id`, not `(player_id, rating_category_id)`, and
there is no `player_statistics_termination`, `head_to_head`,
`source_watermark` or `rebuilt_at`. Both are required by A64-012.6's flat
nine-field model and its exclusion of game result processing;
`infrastructure/models.py` records the reasoning and the migration path in
full.

## Reversibility

Genuinely reversible: `downgrade` drops the index and the table. It does
**not** drop the schema — another module may add a relation to
`statistics` later, and a downgrade that removed a namespace it did not
exclusively own would take that with it. An empty schema costs nothing.

Dropping the table discards the projection, which is data loss in the
strict sense and is the one case where that is genuinely fine: this is
rebuildable from match history by definition (database.md C5 lists it among
the relations that may be `TRUNCATE`d outright).

Verified against PostgreSQL 17 by running upgrade -> downgrade -> upgrade
and inspecting the catalogue at each step, and by driving a deliberately
inconsistent INSERT through each CHECK.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.database.types import UtcDateTime

# revision identifiers, used by Alembic.
revision: str = "b0f336b06542"
down_revision: str | None = "b98e4defce66"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

SCHEMA = "statistics"
TABLE = "player_statistics"
RATING_INDEX = "ix_player_statistics__current_rating"


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    op.create_table(
        TABLE,
        sa.Column("player_id", sa.Uuid(), nullable=False),
        sa.Column("games_played", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("wins", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("losses", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("draws", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("current_rating", sa.Integer(), server_default=sa.text("1500"), nullable=False),
        sa.Column("highest_rating", sa.Integer(), server_default=sa.text("1500"), nullable=False),
        sa.Column("current_streak", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("best_win_streak", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", UtcDateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=True),
        sa.CheckConstraint(
            "best_win_streak >= GREATEST(current_streak, 0)",
            name=op.f("ck_player_statistics__best_win_streak_covers_current"),
        ),
        sa.CheckConstraint(
            "games_played = wins + losses + draws",
            name=op.f("ck_player_statistics__counts_sum_to_games_played"),
        ),
        sa.CheckConstraint(
            "highest_rating >= current_rating",
            name=op.f("ck_player_statistics__highest_rating_is_a_peak"),
        ),
        sa.CheckConstraint(
            "wins >= 0 AND losses >= 0 AND draws >= 0 AND games_played >= 0",
            name=op.f("ck_player_statistics__counts_are_not_negative"),
        ),
        sa.PrimaryKeyConstraint("player_id", name=op.f("pk_player_statistics")),
        schema=SCHEMA,
    )

    # Serves the ordering a future leaderboard projection builds over this
    # column. Descending because every reader of a rating reads it from the
    # top; not unique, because two players sharing a rating is ordinary.
    op.create_index(
        RATING_INDEX,
        TABLE,
        [sa.literal_column("current_rating DESC")],
        unique=False,
        schema=SCHEMA,
        postgresql_using="btree",
    )


def downgrade() -> None:
    op.drop_index(RATING_INDEX, table_name=TABLE, schema=SCHEMA, postgresql_using="btree")
    op.drop_table(TABLE, schema=SCHEMA)
    # The schema itself is deliberately left in place — see this module's
    # docstring.
