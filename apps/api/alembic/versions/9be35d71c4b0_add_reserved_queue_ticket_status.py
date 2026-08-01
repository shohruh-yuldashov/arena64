"""add the reserved queue ticket status and widen the three live predicates

Revision ID: 9be35d71c4b0
Revises: a2d6f81b4c73
Create Date: 2026-08-02 03:58:04.125312

A64-015.3's pairing is two steps that cannot share a transaction — claim
both tickets, then ask `game` for a match — because a cross-context call
inside an open transaction would hold two row locks across another module's
work (services.md BE-05). `reserved` is the status that makes the window
between them visible to every other worker.

## Three predicates move, and they must move together

"Live" stopped meaning `status = 'waiting'` and started meaning
`status IN ('waiting', 'reserved')`. Every object that spelled it out has to
change in the same breath, or the database holds two opinions about whether
a player is queued:

`uq_queue_ticket__one_live_per_player` — QT-1. A reserved player is
    mid-pairing; letting them join a second pool is precisely the
    multi-queueing this index exists to prevent.

`ix_queue_ticket__due` — a worker that dies mid-pairing leaves two reserved
    tickets. Without this the expiry sweep cannot see them, and they occupy
    QT-1's index forever, locking those players out of the queue
    permanently.

`ck_queue_ticket__resolved_iff_terminal` — a reservation is not an outcome.
    Stamping `resolved_at` on one would leave the instant of a match that
    never happened on a ticket that goes back to `waiting`.

`ix_queue_ticket__pool` is deliberately **not** widened: a pairing scan
reads only `waiting`, so a reserved ticket is correctly invisible to every
other worker's next scan. That is the whole mechanism.

## The enum is recreated rather than extended

`ALTER TYPE ... ADD VALUE` is the obvious move and it does not work here.
PostgreSQL permits it inside a transaction block but forbids *using* the new
value in the same transaction, and this migration uses `'reserved'` in three
predicates immediately. The alternatives were:

  - two migrations, one to add the value and one to use it — a revision
    whose only content is a value nothing references, and a deploy that is
    wrong if either half ships alone;
  - `op.execute("COMMIT")` to escape the transaction — giving up atomicity
    on a migration that must not half-apply;
  - create a new type, swap the column, drop the old one. One transaction,
    fully reversible, and the standard recipe.

The third is taken. `USING status::text::` is value-preserving because the
member values are unchanged and only the set grew.

The table is empty in every environment this has run in — `queue_ticket`
shipped two tasks ago and nothing has queued — so the rewrite costs nothing.
On a populated table it would rewrite the column, which is why that is
recorded here rather than discovered later.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "9be35d71c4b0"
down_revision: str | None = "a2d6f81b4c73"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_SCHEMA = "matchmaking"
_TABLE = "queue_ticket"
_ENUM = "queue_ticket_status"

_WITHOUT_RESERVED = ("waiting", "matched", "cancelled", "expired")
_WITH_RESERVED = ("waiting", "reserved", "matched", "cancelled", "expired")

_LIVE_BEFORE = "status = 'waiting'"
_LIVE_AFTER = "status IN ('waiting', 'reserved')"

#: `ix_queue_ticket__pool`'s predicate, which does **not** widen.
#:
#: A pairing scan reads only `waiting`, so a reserved pair is invisible to
#: every other worker's next scan — that is the whole mechanism, and the
#: index is where it is enforced. It is still dropped and rebuilt here,
#: because any expression mentioning `status` blocks the type swap.
_SCANNABLE = "status = 'waiting'"


def _drop_status_dependents() -> None:
    """Remove everything whose definition mentions `status`.

    **Required before the type swap, not tidiness.** `ALTER COLUMN ... TYPE`
    re-plans every expression that references the column, and a partial
    index or a CHECK comparing `status` to a literal of the *old* type fails
    with "operator does not exist: ..._new = ...". All four objects here
    qualify, including `ix_queue_ticket__pool` — whose predicate does not
    change in this migration and which still has to be rebuilt around the
    swap.
    """
    op.drop_constraint(
        "ck_queue_ticket__resolved_iff_terminal", _TABLE, schema=_SCHEMA, type_="check"
    )
    for index in (
        "uq_queue_ticket__one_live_per_player",
        "ix_queue_ticket__due",
        "ix_queue_ticket__pool",
    ):
        op.drop_index(index, table_name=_TABLE, schema=_SCHEMA)


def _create_status_dependents(predicate: str) -> None:
    """Rebuild the CHECK and the three partial indexes over `predicate`.

    All four in one function, so a later edit cannot move two of them and
    leave the database holding different opinions about what "live" means.

    `ix_queue_ticket__pool` is the exception and takes `waiting` literally:
    a pairing scan reads only waiting tickets, which is what makes a
    reserved pair invisible to every other worker's next scan.
    """
    op.create_check_constraint(
        "ck_queue_ticket__resolved_iff_terminal",
        _TABLE,
        sa.text(f"({predicate}) = (resolved_at IS NULL)"),
        schema=_SCHEMA,
    )
    op.create_index(
        "uq_queue_ticket__one_live_per_player",
        _TABLE,
        ["player_id"],
        unique=True,
        schema=_SCHEMA,
        postgresql_where=sa.text(predicate),
    )
    op.create_index(
        "ix_queue_ticket__due",
        _TABLE,
        ["expires_at"],
        schema=_SCHEMA,
        postgresql_where=sa.text(predicate),
    )
    op.create_index(
        "ix_queue_ticket__pool",
        _TABLE,
        ["variant", "queue_type", "region", "entered_at", "id"],
        schema=_SCHEMA,
        postgresql_where=sa.text(_SCANNABLE),
    )


def _replace_status_enum(members: Sequence[str]) -> None:
    """Swap `queue_ticket_status` for one with exactly these members.

    Create-swap-drop-rename, in one transaction. See this module's docstring
    on why `ALTER TYPE ... ADD VALUE` cannot be used.

    The column's `server_default` is dropped and restored around the swap: a
    default is stored as an expression typed against the old enum, and
    PostgreSQL refuses to alter the column out from under one.
    """
    replacement = postgresql.ENUM(*members, name=f"{_ENUM}_new", schema=_SCHEMA)
    replacement.create(op.get_bind(), checkfirst=False)

    op.execute(f"ALTER TABLE {_SCHEMA}.{_TABLE} ALTER COLUMN status DROP DEFAULT")
    op.execute(
        f"ALTER TABLE {_SCHEMA}.{_TABLE} ALTER COLUMN status "
        f"TYPE {_SCHEMA}.{_ENUM}_new USING status::text::{_SCHEMA}.{_ENUM}_new"
    )
    op.execute(f"DROP TYPE {_SCHEMA}.{_ENUM}")
    op.execute(f"ALTER TYPE {_SCHEMA}.{_ENUM}_new RENAME TO {_ENUM}")
    op.execute(
        f"ALTER TABLE {_SCHEMA}.{_TABLE} ALTER COLUMN status "
        f"SET DEFAULT 'waiting'::{_SCHEMA}.{_ENUM}"
    )


def upgrade() -> None:
    _drop_status_dependents()
    _replace_status_enum(_WITH_RESERVED)
    _create_status_dependents(_LIVE_AFTER)


def downgrade() -> None:
    # A reserved ticket cannot survive this — the `status::text::` cast
    # fails on a value the old enum does not have — and that is the honest
    # behaviour: this path is for a deploy rolled back before pairing ran,
    # and a reserved ticket means it did. Resolve or release them first.
    _drop_status_dependents()
    _replace_status_enum(_WITHOUT_RESERVED)
    _create_status_dependents(_LIVE_BEFORE)
