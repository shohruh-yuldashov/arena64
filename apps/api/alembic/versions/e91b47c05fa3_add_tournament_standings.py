"""add tournament standings

A64-019.6, SPEC-TOURNAMENT §6f. A completed tournament's final placement,
materialised once and never recomputed.

    tournaments.standing         one row per entrant of a completed tournament
    tournament.started_at        when play began
    tournament.completed_at      when the result was recorded
    ix_registration__by_player   the player-history keyset

**A C1 relation** (database.md DB-02): `created_at` alone, no `updated_at`.
A standing is a snapshot of a bracket that can no longer change, so
recomputing it on every read would make a published result depend on code
that can change, and a mutable column would invite exactly the admin
correction A64-019.6 defers (OQ-1).

**Exactly one champion** is a partial unique index rather than a `CHECK`,
because a row-level constraint cannot see the other rows — the same argument
capacity makes one level up. Everything else the shape guarantees is a
`CHECK`: the champion is the one player with no elimination, the two
elimination columns travel together, and `final_status` cannot disagree with
`final_rank`.

**Ranks are not dense.** Two players knocked out in the same round share a
rank, so nobody is fourth in an eight-player bracket. Nothing here enforces
density, deliberately — see `domain/standings.py` on why tied tiers are not
broken.

No foreign key into `game` or `users` (DB-03, DM-06). The only reference
that leaves a row is `eliminated_by_player_id`, which names another
participant of the same tournament.

Revision ID: e91b47c05fa3
Revises: c3f8a51b7d24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.database.types import UtcDateTime

revision: str = "e91b47c05fa3"
down_revision: str | None = "c3f8a51b7d24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "tournaments"

#: Every member from day one — R-19. `withdrawn` is unreachable in v0.x
#: because a withdrawal is only permitted while registration is open, so a
#: withdrawn player is never seeded; it exists so that the day mid-tournament
#: withdrawal is decided it is a *use* of this type rather than a change to
#: it, which would rewrite every historical row.
_FINAL_STATUS = postgresql.ENUM(
    "champion",
    "runner_up",
    "eliminated",
    "withdrawn",
    name="final_status",
    schema=_SCHEMA,
    create_type=False,
)


def upgrade() -> None:
    _FINAL_STATUS.create(op.get_bind(), checkfirst=True)

    # Both nullable and both unbackfilled. A tournament that has not started
    # has no start instant, and inventing one from `created_at` would assert
    # a time nobody observed — the same reason `withdrawn_at` is nullable
    # rather than defaulted.
    op.add_column(
        "tournament", sa.Column("started_at", UtcDateTime(), nullable=True), schema=_SCHEMA
    )
    op.add_column(
        "tournament", sa.Column("completed_at", UtcDateTime(), nullable=True), schema=_SCHEMA
    )

    op.create_table(
        "standing",
        sa.Column("tournament_id", sa.UUID(), nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("final_rank", sa.Integer(), nullable=False),
        sa.Column("seed_number", sa.Integer(), nullable=False),
        sa.Column("elimination_round", sa.Integer(), nullable=True),
        sa.Column("eliminated_by_player_id", sa.UUID(), nullable=True),
        sa.Column("wins", sa.Integer(), nullable=False),
        sa.Column("losses", sa.Integer(), nullable=False),
        sa.Column("draws", sa.Integer(), nullable=False),
        sa.Column("adjudicated_advancements", sa.Integer(), nullable=False),
        sa.Column("final_status", _FINAL_STATUS, nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        # The key is the pair, so a second result for one player is
        # impossible — §6's idempotency, made structural.
        sa.PrimaryKeyConstraint("tournament_id", "player_id", name=op.f("pk_standing")),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            [f"{_SCHEMA}.tournament.id"],
            name="fk_standing__tournament",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("final_rank >= 1", name=op.f("ck_standing__rank_from_one")),
        sa.CheckConstraint("seed_number >= 1", name=op.f("ck_standing__seed_from_one")),
        sa.CheckConstraint(
            "wins >= 0 AND losses >= 0 AND draws >= 0 AND adjudicated_advancements >= 0",
            name=op.f("ck_standing__counts_not_negative"),
        ),
        sa.CheckConstraint(
            "(final_rank = 1) = (elimination_round IS NULL)",
            name=op.f("ck_standing__champion_is_not_eliminated"),
        ),
        sa.CheckConstraint(
            "(elimination_round IS NULL) = (eliminated_by_player_id IS NULL)",
            name=op.f("ck_standing__elimination_is_complete"),
        ),
        sa.CheckConstraint(
            "eliminated_by_player_id IS NULL OR eliminated_by_player_id <> player_id",
            name=op.f("ck_standing__nobody_eliminates_themselves"),
        ),
        sa.CheckConstraint(
            "(final_status = 'champion') = (final_rank = 1)",
            name=op.f("ck_standing__champion_iff_first"),
        ),
        sa.CheckConstraint(
            "(final_status = 'runner_up') = (final_rank = 2)",
            name=op.f("ck_standing__runner_up_iff_second"),
        ),
        schema=_SCHEMA,
    )

    # The published ordering, as an index. The standings endpoint pages over
    # this relation, and a read that had to sort would sort a whole
    # tournament's results per request.
    op.create_index(
        "ix_standing__placement",
        "standing",
        ["tournament_id", "final_rank", "seed_number", "player_id"],
        schema=_SCHEMA,
    )
    # **Exactly one champion**, which a row-level `CHECK` cannot express.
    op.create_index(
        "uq_standing__one_champion",
        "standing",
        ["tournament_id"],
        unique=True,
        schema=_SCHEMA,
        postgresql_where=sa.text("final_rank = 1"),
    )

    # "Which tournaments has this player entered, newest first" — §12's
    # keyset. Descending on both keys because that is the order the endpoint
    # pages in, and an ascending index would make every page a backwards scan.
    op.create_index(
        "ix_registration__by_player",
        "registration",
        ["player_id", sa.text("registered_at DESC"), sa.text("tournament_id DESC")],
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("tournament", "completed_at", schema=_SCHEMA)
    op.drop_column("tournament", "started_at", schema=_SCHEMA)
    op.drop_index("ix_registration__by_player", "registration", schema=_SCHEMA)
    op.drop_index("uq_standing__one_champion", "standing", schema=_SCHEMA)
    op.drop_index("ix_standing__placement", "standing", schema=_SCHEMA)
    op.drop_table("standing", schema=_SCHEMA)
    _FINAL_STATUS.drop(op.get_bind(), checkfirst=True)
