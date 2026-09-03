"""add tournament pairings

A64-019.3. Seeds on the registration, and one relation for a round's slots.

**The seed is persisted, not recomputed.** Ratings move; a published
bracket does not. A later phase that re-derived seeding from current
ratings would produce a different order from the one players read, which is
the same class of error as reseeding mid-tournament.

**The pairing's primary key is its immutability.** `(tournament, round,
slot)` cannot hold a second plan, so a retry collides rather than producing
a second bracket — which is how seeding is idempotent without a marker.

Revision ID: e7a2f9c45b13
Revises: d4b7c3e12f80
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.database.types import UtcDateTime

revision: str = "e7a2f9c45b13"
down_revision: str | None = "d4b7c3e12f80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "tournaments"


def upgrade() -> None:
    # Nullable: `NULL` until seeding runs, which is also what makes a
    # second seeding attempt recognisable as one.
    op.add_column(
        "registration", sa.Column("seed_number", sa.Integer(), nullable=True), schema=_SCHEMA
    )

    op.create_table(
        "pairing",
        sa.Column("tournament_id", sa.UUID(), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        # Nullable because a **bye is an empty slot** (§7), not a fake
        # player and not a match.
        sa.Column("light_player_id", sa.UUID(), nullable=True),
        sa.Column("dark_player_id", sa.UUID(), nullable=True),
        sa.Column("light_seed", sa.Integer(), nullable=True),
        sa.Column("dark_seed", sa.Integer(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", UtcDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("tournament_id", "round_number", "slot", name=op.f("pk_pairing")),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            [f"{_SCHEMA}.tournament.id"],
            name="fk_pairing__tournament",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("round_number >= 1", name=op.f("ck_pairing__round_from_one")),
        sa.CheckConstraint("slot >= 0", name=op.f("ck_pairing__slot_not_negative")),
        sa.CheckConstraint(
            "(light_player_id IS NULL) = (light_seed IS NULL)",
            name=op.f("ck_pairing__light_seat_is_complete"),
        ),
        sa.CheckConstraint(
            "(dark_player_id IS NULL) = (dark_seed IS NULL)",
            name=op.f("ck_pairing__dark_seat_is_complete"),
        ),
        sa.CheckConstraint(
            "light_player_id IS NULL OR dark_player_id IS NULL "
            "OR light_player_id <> dark_player_id",
            name=op.f("ck_pairing__distinct_players"),
        ),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("pairing", schema=_SCHEMA)
    op.drop_column("registration", "seed_number", schema=_SCHEMA)
