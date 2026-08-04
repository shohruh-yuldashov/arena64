"""add tournament no-show adjudication

A64-019.5H, SPEC-TOURNAMENT §6e. A tournament match is created **already
active** — nobody is asked to accept a fixture they entered a tournament to
play — so the question stops being "did they answer" and becomes "did they
turn up".

    pairing_attempt.no_show_deadline   when it stops waiting
    pairing_attempt.light_present_at   when each player first reached it
    pairing_attempt.dark_present_at
    attempt_outcome                    gains 'no_show'

**The deadline is per attempt, not per deployment.** It is written from
`TOURNAMENT_NO_SHOW_SECONDS` when the match is created and never recomputed,
so a deploy that lengthens the setting cannot reprieve a player whose
deadline already passed and one that shortens it cannot eliminate somebody
who was inside the window they were given.

**Attendance is "first reached", never "connected now".** The two columns are
set once by a guarded `UPDATE ... WHERE ... IS NULL` and never cleared,
because §6e's rule is that a transient disconnect after somebody turned up is
not a no-show — and a liveness flag would make a dropped socket
indistinguishable from an absence.

`ck_pairing_attempt__winner_iff_decisive` is rewritten rather than extended.
It said "decisive implies a winner", which a third outcome that also advances
a player escapes silently; it now says "a draw names nobody, everything else
names somebody", which `no_show` cannot escape.

Revision ID: c3f8a51b7d24
Revises: b6e2f04d19a7
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.database.types import UtcDateTime

revision: str = "c3f8a51b7d24"
down_revision: str | None = "b6e2f04d19a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "tournaments"


def upgrade() -> None:
    # A native enum gains a member by `ALTER TYPE`, which PostgreSQL 12+
    # runs without rewriting the table. `IF NOT EXISTS` so a partially
    # applied run is re-runnable.
    op.execute(f"ALTER TYPE {_SCHEMA}.attempt_outcome ADD VALUE IF NOT EXISTS 'no_show'")

    op.add_column(
        "pairing_attempt",
        sa.Column("no_show_deadline", UtcDateTime(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        "pairing_attempt",
        sa.Column("light_present_at", UtcDateTime(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        "pairing_attempt",
        sa.Column("dark_present_at", UtcDateTime(), nullable=True),
        schema=_SCHEMA,
    )

    # The sweep's whole query. Partial on `outcome IS NULL`, so it indexes
    # exactly the rows that can still be claimed and shrinks as a tournament
    # is played rather than growing with it.
    op.create_index(
        "ix_pairing_attempt__no_show_due",
        "pairing_attempt",
        ["no_show_deadline"],
        schema=_SCHEMA,
        postgresql_where=sa.text("outcome IS NULL AND no_show_deadline IS NOT NULL"),
    )

    op.drop_constraint(
        op.f("ck_pairing_attempt__winner_iff_decisive"), "pairing_attempt", schema=_SCHEMA
    )
    op.create_check_constraint(
        op.f("ck_pairing_attempt__winner_iff_decisive"),
        "pairing_attempt",
        "outcome IS NULL OR (outcome = 'draw') = (winner_id IS NULL)",
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_pairing_attempt__winner_iff_decisive"), "pairing_attempt", schema=_SCHEMA
    )
    op.create_check_constraint(
        op.f("ck_pairing_attempt__winner_iff_decisive"),
        "pairing_attempt",
        "(outcome = 'decisive') = (winner_id IS NOT NULL)",
        schema=_SCHEMA,
    )

    op.drop_index("ix_pairing_attempt__no_show_due", "pairing_attempt", schema=_SCHEMA)
    op.drop_column("pairing_attempt", "dark_present_at", schema=_SCHEMA)
    op.drop_column("pairing_attempt", "light_present_at", schema=_SCHEMA)
    op.drop_column("pairing_attempt", "no_show_deadline", schema=_SCHEMA)

    # **`no_show` is left in the enum.** PostgreSQL cannot drop an enum
    # value, and rebuilding the type would mean rewriting every row of a
    # table whose old rows are fine. The restored constraint above refuses a
    # `no_show` row with no winner, which is the shape that mattered; a
    # member nothing writes is inert.
