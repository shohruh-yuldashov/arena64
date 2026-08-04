"""add tournament bracket

A64-019.4. Rounds get a relation, and a pairing becomes a bracket node by
gaining a winner and a match.

**`winner_id` is a compare-and-set target.** Advancement is
`UPDATE … SET winner_id = :w WHERE … AND winner_id IS NULL`, so two workers
processing the same completed match cannot both write. Read-then-write would
let both through, and the second would silently replace the first.

**`match_id` has no foreign key** into `game` (DB-03, R-25). The uniqueness
is local: one node per match, so one result cannot advance two nodes.

Revision ID: f2c8b4e07a91
Revises: e7a2f9c45b13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.database.types import UtcDateTime

revision: str = "f2c8b4e07a91"
down_revision: str | None = "e7a2f9c45b13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "tournaments"

_ROUND_STATUS = postgresql.ENUM(
    "pending",
    "published",
    "in_progress",
    "completed",
    name="round_status",
    schema=_SCHEMA,
    create_type=False,
)


def upgrade() -> None:
    _ROUND_STATUS.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "round",
        sa.Column("tournament_id", sa.UUID(), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("status", _ROUND_STATUS, nullable=False),
        sa.Column("published_at", UtcDateTime(), nullable=True),
        sa.Column("started_at", UtcDateTime(), nullable=True),
        sa.Column("completed_at", UtcDateTime(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", UtcDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("tournament_id", "round_number", name=op.f("pk_round")),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            [f"{_SCHEMA}.tournament.id"],
            name="fk_round__tournament",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("round_number >= 1", name=op.f("ck_round__number_from_one")),
        sa.CheckConstraint(
            "(status = 'pending') = (published_at IS NULL)",
            name=op.f("ck_round__published_iff_instant"),
        ),
        schema=_SCHEMA,
    )

    op.add_column("pairing", sa.Column("winner_id", sa.UUID(), nullable=True), schema=_SCHEMA)
    op.add_column("pairing", sa.Column("match_id", sa.UUID(), nullable=True), schema=_SCHEMA)

    # The database's half of `BracketSlot.with_winner`: a backfill cannot
    # advance somebody who was never in the node.
    # `op.f()` opts the name out of the naming convention, which would
    # otherwise prefix it again and produce `ck_pairing__ck_pairing__…`.
    op.create_check_constraint(
        op.f("ck_pairing__winner_played_here"),
        "pairing",
        "winner_id IS NULL OR winner_id = light_player_id OR winner_id = dark_player_id",
        schema=_SCHEMA,
    )
    # One node per match — two nodes claiming one match would be two
    # advancements from one result.
    op.create_index(
        "uq_pairing__match",
        "pairing",
        ["match_id"],
        unique=True,
        schema=_SCHEMA,
        postgresql_where=sa.text("match_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_pairing__match", "pairing", schema=_SCHEMA)
    op.drop_constraint(op.f("ck_pairing__winner_played_here"), "pairing", schema=_SCHEMA)
    op.drop_column("pairing", "match_id", schema=_SCHEMA)
    op.drop_column("pairing", "winner_id", schema=_SCHEMA)
    op.drop_table("round", schema=_SCHEMA)
    _ROUND_STATUS.drop(op.get_bind(), checkfirst=True)
