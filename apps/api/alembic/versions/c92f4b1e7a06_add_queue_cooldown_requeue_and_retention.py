"""add queue_cooldown, requeue provenance, and the retention indexes

Revision ID: c92f4b1e7a06
Revises: f1a7c3e5b920
Create Date: 2026-08-02 16:42:09.331408

A64-015.5 closes the acceptance-failure policy A64-015.4 left open, and
bounds the two relations the handshake fills. Four changes, in three
relations, and they ship together because the policy needs all of them:

    matchmaking.queue_cooldown              new relation — §3
    matchmaking.queue_ticket.source_ticket_id + its unique index — §2
    matchmaking.queue_ticket retention index — §8
    game.match retention index               — §8

## One revision, three relations

Splitting would produce a deploy that is wrong if any half ships alone. The
requeue writes `source_ticket_id` and the cooldown is written in the same
consumer, so a database with one and not the other applies half a policy —
and half of "requeue the accepting player, cool down the decliner" is a
policy that punishes nobody or rewards nobody, depending which half landed.

## `uq_queue_ticket__requeued_from` is the load-bearing object

A64-015.5 §2 requires the requeue to be idempotent. The event ledger
(`platform.processed_event`) already stops a *redelivered* entry reaching the
consumer twice, and it cannot stop two workers processing one entry
concurrently — which is what a partial unique index on `source_ticket_id`
does. Both insert, one wins, and the loser is reported as "already done".

Partial, because almost every ticket has no source: a plain unique on a
mostly-null column would index every row in the relation to constrain a
handful. PostgreSQL treats nulls as distinct in a unique index anyway, so
the predicate is about *size* rather than correctness — and on the
platform's highest-churn queue relation, size is the point.

## `queue_cooldown`'s primary key is the player

Not a surrogate `id`, which is DB-07's convention and every other relation's
choice. The departure is argued in `matchmaking.infrastructure.models`; in
short, a cooldown is a *current fact about a player* of which there is at
most one, and keying on the player makes "a repeated decline extends rather
than accumulates" a single `INSERT ... ON CONFLICT DO UPDATE` instead of a
read-then-write two declines can interleave inside.

## The two retention indexes are partial, and the predicates are the safety

`ix_queue_ticket__retention` is predicated on `resolved_at IS NOT NULL`,
which `ck_queue_ticket__resolved_iff_terminal` makes equivalent to "this
ticket is terminal". `ix_match__abandoned` is predicated on the two statuses
a match reaches without ever being played.

The consequence is worth stating because it is the guarantee §8 asks for: a
**live** queue ticket and an **active** match are not in these indexes, so a
retention sweep cannot reach one however its horizon is configured. A
misconfigured window can delete too much history; it cannot delete a player
out of the queue, and it cannot delete a game.

## No foreign key on `source_ticket_id`

Though it names a row in the same relation. Retention deletes terminal
tickets on a horizon, and a self-referential FK would either block that or
cascade it into the *live* ticket that replaced them — which is the one row
a cleanup job must never touch. Provenance that outlives its subject is
answered with a null rather than a constraint violation.

## Reversibility

Complete, and asymmetric in one place worth naming.

The three `matchmaking` changes reverse cleanly: the indexes drop, the
column drops, and the relation drops with its enum type. Dropping
`queue_cooldown` **discards every cooldown in force** — which is the honest
behaviour, because the downgrade target has no way to enforce one. A player
mid-cooldown may queue immediately after the rollback, and that is a
rollback restoring the previous behaviour rather than a defect.

`ix_match__abandoned` drops with nothing else: it is an index and no data
depends on it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c92f4b1e7a06"
down_revision: str | Sequence[str] | None = "f1a7c3e5b920"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MATCHMAKING = "matchmaking"
_GAME = "game"

#: Restated rather than imported from the domain enum, per the convention
#: every migration on this platform follows: a revision describes the schema
#: as it was at *this* point, and importing a live enum would make an old
#: migration change meaning the day somebody adds a member.
_COOLDOWN_REASONS = ("declined_match",)

_TERMINAL_TICKET = "resolved_at IS NOT NULL"
_REQUEUED = "source_ticket_id IS NOT NULL"
_ABANDONED_MATCH = "status IN ('cancelled', 'expired')"


def upgrade() -> None:
    _create_cooldowns()
    _add_requeue_provenance()
    _add_retention_indexes()


def downgrade() -> None:
    _drop_retention_indexes()
    _drop_requeue_provenance()
    _drop_cooldowns()


def _create_cooldowns() -> None:
    reason = postgresql.ENUM(
        *_COOLDOWN_REASONS, name="queue_cooldown_reason", schema=_MATCHMAKING
    )
    reason.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "queue_cooldown",
        sa.Column("player_id", sa.Uuid(as_uuid=True), nullable=False),
        # `create_type=False`: the type is created above, once, and letting
        # the column definition create it again would attempt a duplicate
        # `CREATE TYPE` inside the same transaction.
        sa.Column(
            "reason",
            postgresql.ENUM(
                *_COOLDOWN_REASONS,
                name="queue_cooldown_reason",
                schema=_MATCHMAKING,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_queue_cooldown__window_positive"
        ),
        # The player, not a surrogate id — see this revision's docstring.
        sa.PrimaryKeyConstraint("player_id", name=op.f("pk_queue_cooldown")),
        schema=_MATCHMAKING,
    )
    # Retention's claim. The eligibility read is by primary key, so this
    # index exists for the sweep rather than for the hot path.
    op.create_index(
        "ix_queue_cooldown__expiry", "queue_cooldown", ["expires_at"], schema=_MATCHMAKING
    )


def _drop_cooldowns() -> None:
    op.drop_index("ix_queue_cooldown__expiry", table_name="queue_cooldown", schema=_MATCHMAKING)
    op.drop_table("queue_cooldown", schema=_MATCHMAKING)
    postgresql.ENUM(name="queue_cooldown_reason", schema=_MATCHMAKING).drop(
        op.get_bind(), checkfirst=True
    )


def _add_requeue_provenance() -> None:
    # A nullable column with no default is a catalogue-only change on
    # PostgreSQL 11+, so this does not rewrite the relation.
    op.add_column(
        "queue_ticket",
        sa.Column("source_ticket_id", sa.Uuid(as_uuid=True), nullable=True),
        schema=_MATCHMAKING,
    )
    op.create_index(
        "uq_queue_ticket__requeued_from",
        "queue_ticket",
        ["source_ticket_id"],
        unique=True,
        schema=_MATCHMAKING,
        postgresql_where=sa.text(_REQUEUED),
    )


def _drop_requeue_provenance() -> None:
    op.drop_index(
        "uq_queue_ticket__requeued_from", table_name="queue_ticket", schema=_MATCHMAKING
    )
    op.drop_column("queue_ticket", "source_ticket_id", schema=_MATCHMAKING)


def _add_retention_indexes() -> None:
    op.create_index(
        "ix_queue_ticket__retention",
        "queue_ticket",
        ["resolved_at"],
        schema=_MATCHMAKING,
        postgresql_where=sa.text(_TERMINAL_TICKET),
    )
    op.create_index(
        "ix_match__abandoned",
        "match",
        ["settled_at"],
        schema=_GAME,
        postgresql_where=sa.text(_ABANDONED_MATCH),
    )


def _drop_retention_indexes() -> None:
    op.drop_index("ix_match__abandoned", table_name="match", schema=_GAME)
    op.drop_index("ix_queue_ticket__retention", table_name="queue_ticket", schema=_MATCHMAKING)
