"""widen the per-player match indexes to cover active matches

Revision ID: c4e8a1d90f37
Revises: b7d24e08f193
Create Date: 2026-08-05 14:22:11.503914

A64-020.5A. `pending_for` answers "which match is this player in right now",
and until now that meant `pending_acceptance` alone — which left the *first*
of two acceptors unable to learn their own game had started, because the
match activates on the other player's request.

The two player-scoped partial indexes widen by exactly one status to keep
serving that read. Renamed in the same step: an index called `pending` that
carries active matches is a name a reader trusts and should not.

    ix_match__pending_light  ->  ix_match__current_light
    ix_match__pending_dark   ->  ix_match__current_dark

`created_at` is added as a second column because the read now orders by it —
at most one row can match today, and the ordering exists so that if that
invariant were ever broken the answer would still be stable rather than
arbitrary.

## What deliberately does not widen

`ix_match__pending_deadline` and `ck_match__settled_at_iff_not_pending` keep
the narrow predicate, and each for its own reason: an `active` match can
never become overdue, and it is settled. Widening them would put every live
game in the expiry sweep's index and break a constraint that holds today.

## Reversibility

Complete, and cheap: two indexes dropped and two created, on a relation with
no live rows in any environment. The downgrade restores the previous names,
predicate and column list exactly.

Partial indexes rather than a table rewrite, so neither direction takes a
lock beyond the index build.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4e8a1d90f37"
down_revision: str | Sequence[str] | None = "b7d24e08f193"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "game"

#: Restated rather than imported from the live enum, per the convention
#: every migration on this platform follows.
_PENDING = "status = 'pending_acceptance'"
_CURRENT = "status IN ('pending_acceptance', 'active')"


def upgrade() -> None:
    for side in ("light", "dark"):
        op.drop_index(f"ix_match__pending_{side}", "match", schema=_SCHEMA)
        op.create_index(
            f"ix_match__current_{side}",
            "match",
            [f"{side}_player_id", "created_at"],
            schema=_SCHEMA,
            postgresql_where=sa.text(_CURRENT),
        )


def downgrade() -> None:
    for side in ("light", "dark"):
        op.drop_index(f"ix_match__current_{side}", "match", schema=_SCHEMA)
        op.create_index(
            f"ix_match__pending_{side}",
            "match",
            [f"{side}_player_id"],
            schema=_SCHEMA,
            postgresql_where=sa.text(_PENDING),
        )
