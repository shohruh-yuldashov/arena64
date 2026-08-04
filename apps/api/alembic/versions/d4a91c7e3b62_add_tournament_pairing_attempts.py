"""add tournament pairing attempts

A64-019.5, SPEC-TOURNAMENT §6c. A pairing may need **two** `game` matches,
because this platform's games can draw and single elimination needs one
winner per node.

    pairing.id                  a stable surrogate, handed to `game` as
                                `origin_ref` (R-25)
    pairing.match_id            removed — it cannot represent two matches
    pairing.advancement_reason  played | bye | adjudication
    pairing_attempt             one row per match played for one node

**`pairing.match_id` is dropped rather than kept beside the new relation.**
It could no longer truthfully hold "the match this node was played as", and
two competing sources of truth is worse than a migration. The feature is
unreleased, so the model is corrected now.

**The primary key does not change.** `(tournament_id, round_number, slot)`
is what makes a second plan for one slot impossible — §6's immutability —
and the surrogate is added *beside* it, for the one job coordinates cannot
do: crossing a context boundary without publishing this module's own
arithmetic.

**No foreign key leaves this schema** (DB-03). `pairing_attempt.match_id`
is opaque, exactly as `pairing.match_id` was: a tournament is prunable and a
match is permanent, so a constraint would forbid a retention `game` will
need.

Revision ID: d4a91c7e3b62
Revises: f2c8b4e07a91
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.database.types import UtcDateTime

revision: str = "d4a91c7e3b62"
down_revision: str | None = "f2c8b4e07a91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "tournaments"

#: Every member from day one — R-19's argument, which this file inherits: a
#: reason added after brackets have been recorded makes every historical
#: query about *why* somebody advanced wrong and unfixable.
_ADVANCEMENT_REASON = postgresql.ENUM(
    "played",
    "bye",
    "adjudication",
    name="advancement_reason",
    schema=_SCHEMA,
    create_type=False,
)

_ATTEMPT_STATUS = postgresql.ENUM(
    "created",
    "completed",
    name="attempt_status",
    schema=_SCHEMA,
    create_type=False,
)

_ATTEMPT_OUTCOME = postgresql.ENUM(
    "decisive",
    "draw",
    name="attempt_outcome",
    schema=_SCHEMA,
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    _ADVANCEMENT_REASON.create(bind, checkfirst=True)
    _ATTEMPT_STATUS.create(bind, checkfirst=True)
    _ATTEMPT_OUTCOME.create(bind, checkfirst=True)

    # `gen_random_uuid()` is a PostgreSQL 13+ built-in, so no extension is
    # needed. The server default is a backstop for rows written outside the
    # application — a repair script, a backfill; the ORM supplies its own,
    # which is what lets it know the id it inserted without a round trip.
    op.add_column(
        "pairing",
        sa.Column(
            "id",
            sa.UUID(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        schema=_SCHEMA,
    )
    # The FK below needs this, and it is what makes the surrogate an
    # identity rather than a column that happens to hold uuids.
    op.create_unique_constraint(op.f("uq_pairing__id"), "pairing", ["id"], schema=_SCHEMA)

    op.drop_index("uq_pairing__match", "pairing", schema=_SCHEMA)
    op.drop_column("pairing", "match_id", schema=_SCHEMA)

    op.add_column(
        "pairing",
        sa.Column("advancement_reason", _ADVANCEMENT_REASON, nullable=True),
        schema=_SCHEMA,
    )
    # Backfilled to `bye` rather than guessed, and the value is a fact
    # rather than a default: before this revision nothing created a
    # tournament match, so the only advancement any stored bracket can hold
    # is a first-round bye. Without it the constraint below would refuse a
    # developer's existing rows.
    op.execute(
        sa.text(
            f"UPDATE {_SCHEMA}.pairing SET advancement_reason = 'bye' "  # noqa: S608
            "WHERE winner_id IS NOT NULL"
        )
    )
    # Both halves of the fact or neither. A reason without a winner would
    # claim an advancement nobody made; a winner without one loses the
    # distinction between a game, a bye and an adjudication — and that
    # distinction is a competitive fact somebody will ask about.
    #
    # `op.f()` opts the name out of the naming convention, which would
    # otherwise prefix it again and produce `ck_pairing__ck_pairing__…`.
    op.create_check_constraint(
        op.f("ck_pairing__reason_iff_winner"),
        "pairing",
        "(winner_id IS NULL) = (advancement_reason IS NULL)",
        schema=_SCHEMA,
    )

    op.create_table(
        "pairing_attempt",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("pairing_id", sa.UUID(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("match_id", sa.UUID(), nullable=False),
        sa.Column("light_player_id", sa.UUID(), nullable=False),
        sa.Column("dark_player_id", sa.UUID(), nullable=False),
        sa.Column("status", _ATTEMPT_STATUS, nullable=False),
        sa.Column("outcome", _ATTEMPT_OUTCOME, nullable=True),
        sa.Column("winner_id", sa.UUID(), nullable=True),
        sa.Column("completed_at", UtcDateTime(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", UtcDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pairing_attempt")),
        sa.ForeignKeyConstraint(
            ["pairing_id"],
            [f"{_SCHEMA}.pairing.id"],
            name="fk_pairing_attempt__pairing",
            # Within one schema, so the coupling is this module's own. No
            # cascade: a bracket's attempts are part of its permanent
            # record, and deleting a node that has them is a decision.
            ondelete="RESTRICT",
        ),
        # The bound, in the schema — §6c. A third attempt cannot exist even
        # if something above forgets the rule, and an unbounded rematch
        # chain is a tournament that never finishes.
        sa.CheckConstraint(
            "attempt_number BETWEEN 1 AND 2",
            name=op.f("ck_pairing_attempt__number_in_range"),
        ),
        sa.CheckConstraint(
            "(status = 'completed') = (outcome IS NOT NULL) "
            "AND (status = 'completed') = (completed_at IS NOT NULL)",
            name=op.f("ck_pairing_attempt__completed_iff_outcome"),
        ),
        sa.CheckConstraint(
            "winner_id IS NULL OR winner_id = light_player_id OR winner_id = dark_player_id",
            name=op.f("ck_pairing_attempt__winner_played_here"),
        ),
        sa.CheckConstraint(
            "(outcome = 'decisive') = (winner_id IS NOT NULL)",
            name=op.f("ck_pairing_attempt__winner_iff_decisive"),
        ),
        sa.CheckConstraint(
            "light_player_id <> dark_player_id",
            name=op.f("ck_pairing_attempt__distinct_players"),
        ),
        schema=_SCHEMA,
    )
    # **The idempotency guarantee** — §6c. A redelivered `match.completed`
    # cannot create a second rematch, because the row it would insert
    # already exists. Read-then-insert would let two deliveries through.
    op.create_index(
        "uq_pairing_attempt__pairing_number",
        "pairing_attempt",
        ["pairing_id", "attempt_number"],
        unique=True,
        schema=_SCHEMA,
    )
    # One attempt per match — the guarantee `uq_pairing__match` used to
    # hold, moved to where the match now lives.
    op.create_index(
        "uq_pairing_attempt__match",
        "pairing_attempt",
        ["match_id"],
        unique=True,
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("uq_pairing_attempt__match", "pairing_attempt", schema=_SCHEMA)
    op.drop_index("uq_pairing_attempt__pairing_number", "pairing_attempt", schema=_SCHEMA)
    op.drop_table("pairing_attempt", schema=_SCHEMA)

    op.drop_constraint(op.f("ck_pairing__reason_iff_winner"), "pairing", schema=_SCHEMA)
    op.drop_column("pairing", "advancement_reason", schema=_SCHEMA)

    # Restored empty. The matches an attempt row named are not recoverable
    # into one column — that is the whole reason the relation replaced it —
    # so a downgrade returns the shape and loses the second attempt, which
    # is what a downgrade of a model correction always costs.
    op.add_column("pairing", sa.Column("match_id", sa.UUID(), nullable=True), schema=_SCHEMA)
    op.create_index(
        "uq_pairing__match",
        "pairing",
        ["match_id"],
        unique=True,
        schema=_SCHEMA,
        postgresql_where=sa.text("match_id IS NOT NULL"),
    )

    op.drop_constraint(op.f("uq_pairing__id"), "pairing", schema=_SCHEMA)
    op.drop_column("pairing", "id", schema=_SCHEMA)

    bind = op.get_bind()
    _ATTEMPT_OUTCOME.drop(bind, checkfirst=True)
    _ATTEMPT_STATUS.drop(bind, checkfirst=True)
    _ADVANCEMENT_REASON.drop(bind, checkfirst=True)
