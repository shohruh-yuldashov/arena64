"""add the cooldown audit trail and the pairing reconciliation timeline

Revision ID: d5e81a3c9f47
Revises: c92f4b1e7a06
Create Date: 2026-08-02 19:08:52.104773

A64-015.6 §3 and §4. Two relations, both **append-only**, both answering a
question an operator or a support agent asks about something that has already
finished — which is why neither can be a column on the operational row it
describes.

## `queue_cooldown_audit`, and why it is not `queue_cooldown`

A64-015.5 keyed the enforcement row on the player and made a second decline
*extend* it with `GREATEST`. That is the right shape for the join path — a
primary-key lookup and one upsert — and it discards history, which that task
recorded:

> "What it costs is history: a second decline overwrites the first's
> `expires_at` and nothing records that there were two."

Merging the two would mean keeping that behaviour or turning the queue-join
read into a scan-and-`max()` over a player's whole history. The hot read and
the audit read want opposite shapes, so they get two relations.

**It is not a Sanction.** §3 forbids reusing the moderation model, and the
schema shows why the distinction is real: there is no actor column, no
severity, no note and no escalation count. A cooldown is a mechanical
consequence of one action with a duration from a settings file.

## `pairing_timeline`, and why a projection rather than a log query

`matchmaking.pairing_reconciled` has been published since A64-015.5 and read
by nobody. The log line beside it is aggregated per tick — it says *five
tickets were settled* and not which — sits on the log pipeline's retention
rather than the platform's, and cannot be joined to a ticket id, which is the
only identifier a support conversation starts from.

AD-19 makes every projection rebuildable from PostgreSQL, and this one is:
`event_id` is the join back to the outbox rows it was built from.

## The two unique indexes are the idempotency, and they are not decoration

Both writers are outbox consumers under AD-16's at-least-once contract, so a
redelivered event reaches them twice **by design**. The `processed_event`
ledger stops the ordinary case and cannot stop two relays delivering
concurrently, so a check-then-insert would pass for both and write two rows —
which for an audit trail means two different answers to one question.

    uq_queue_cooldown_audit__source  partial unique on (player_id,
                                     source_match_id) where the match is not
                                     null. Partial because the column is
                                     nullable and reserved for a future
                                     non-match reason; nulls are distinct in
                                     a unique index anyway, so the predicate
                                     is about size rather than correctness
    uq_pairing_timeline__event       plain unique on the outbox entry id

## `ix_pairing_timeline__pairing` indexes a column that is always null

Deliberate, and stated rather than left to be discovered. §4 requires the
timeline to be queryable by pairing identifier; `PairingReconciled` carries a
*ticket*, because the reconciler claims whatever bounded batch it locks and
may hold one half of a pair without the other. The column and its partial
index cost one catalogue entry and no pages while it stays null, and adding
them now is cheaper than a migration on a populated relation later.

## No foreign keys on any of the five identifier columns

`player_id` and `source_match_id` are DM-06's opaque cross-context values, and
`source_match_id` names a row in the `game` schema which is pruned on its own
horizon. `ticket_id` names a row in this schema, pruned at 72 hours.

Every one of those is a relation with a *shorter* horizon than the audit row
that references it — which is the whole point of an audit row — so a foreign
key would either block retention or cascade a deletion into the record of what
happened. Provenance that outlives its subject is answered with a dangling
identifier, not with a constraint.

## Reversibility

Complete and symmetric. Both relations drop with their indexes, and the one
enum type this revision creates (`reconciliation_action`) drops with them —
`drop_table` does not remove it, and a re-upgrade would then fail with "type
already exists".

Dropping them **discards every audit row**, which is the honest behaviour: the
downgrade target has no relation to hold them, and the events they were
projected from are still in the outbox until its own horizon passes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d5e81a3c9f47"
down_revision: str | Sequence[str] | None = "c92f4b1e7a06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "matchmaking"

#: Restated rather than imported from the domain enum, per the convention
#: every migration on this platform follows: a revision describes the schema
#: as it was at *this* point, and importing a live enum would make an old
#: migration change meaning the day somebody adds a member.
_ACTIONS = (
    "settled",
    "released",
    "expired",
    "requeued",
    "pending_match_cancelled",
    "no_action",
    "reconciliation_failed",
)

_HAS_SOURCE = "source_match_id IS NOT NULL"
_HAS_PAIRING = "pairing_id IS NOT NULL"


def upgrade() -> None:
    _create_cooldown_audit()
    _create_pairing_timeline()


def downgrade() -> None:
    _drop_pairing_timeline()
    _drop_cooldown_audit()


def _create_cooldown_audit() -> None:
    op.create_table(
        "queue_cooldown_audit",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("player_id", sa.Uuid(as_uuid=True), nullable=False),
        # Reuses `queue_cooldown_reason`, created by c92f4b1e7a06. Sharing
        # the type is the point: a record whose reason the enforcement row
        # cannot hold would be a record of something that never happened.
        sa.Column(
            "reason",
            postgresql.ENUM(name="queue_cooldown_reason", schema=_SCHEMA, create_type=False),
            nullable=False,
        ),
        sa.Column("source_match_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extended_existing", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "expires_at > applied_at", name="ck_queue_cooldown_audit__window_positive"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_queue_cooldown_audit")),
        schema=_SCHEMA,
    )
    # Idempotency under concurrent delivery — see this revision's docstring.
    op.create_index(
        "uq_queue_cooldown_audit__source",
        "queue_cooldown_audit",
        ["player_id", "source_match_id"],
        unique=True,
        schema=_SCHEMA,
        postgresql_where=sa.text(_HAS_SOURCE),
    )
    # The support query, ordered so PostgreSQL walks the index and stops at
    # the limit rather than sorting a history.
    op.create_index(
        "ix_queue_cooldown_audit__player",
        "queue_cooldown_audit",
        ["player_id", "applied_at"],
        schema=_SCHEMA,
    )
    # Retention's claim, which is player-blind and so leads with the instant.
    op.create_index(
        "ix_queue_cooldown_audit__retention",
        "queue_cooldown_audit",
        ["applied_at"],
        schema=_SCHEMA,
    )


def _drop_cooldown_audit() -> None:
    for index in (
        "ix_queue_cooldown_audit__retention",
        "ix_queue_cooldown_audit__player",
        "uq_queue_cooldown_audit__source",
    ):
        op.drop_index(index, table_name="queue_cooldown_audit", schema=_SCHEMA)
    op.drop_table("queue_cooldown_audit", schema=_SCHEMA)


def _create_pairing_timeline() -> None:
    action = postgresql.ENUM(*_ACTIONS, name="reconciliation_action", schema=_SCHEMA)
    action.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "pairing_timeline",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("event_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("ticket_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("player_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "action",
            postgresql.ENUM(
                *_ACTIONS, name="reconciliation_action", schema=_SCHEMA, create_type=False
            ),
            nullable=False,
        ),
        sa.Column("match_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("pairing_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pairing_timeline")),
        schema=_SCHEMA,
    )
    op.create_index(
        "uq_pairing_timeline__event",
        "pairing_timeline",
        ["event_id"],
        unique=True,
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_pairing_timeline__ticket",
        "pairing_timeline",
        ["ticket_id", "occurred_at"],
        schema=_SCHEMA,
    )
    # Partial on a column nothing populates yet — see this revision's
    # docstring on why it ships empty rather than later.
    op.create_index(
        "ix_pairing_timeline__pairing",
        "pairing_timeline",
        ["pairing_id", "occurred_at"],
        schema=_SCHEMA,
        postgresql_where=sa.text(_HAS_PAIRING),
    )
    op.create_index(
        "ix_pairing_timeline__retention",
        "pairing_timeline",
        ["occurred_at"],
        schema=_SCHEMA,
    )


def _drop_pairing_timeline() -> None:
    for index in (
        "ix_pairing_timeline__retention",
        "ix_pairing_timeline__pairing",
        "ix_pairing_timeline__ticket",
        "uq_pairing_timeline__event",
    ):
        op.drop_index(index, table_name="pairing_timeline", schema=_SCHEMA)
    op.drop_table("pairing_timeline", schema=_SCHEMA)

    postgresql.ENUM(name="reconciliation_action", schema=_SCHEMA).drop(
        op.get_bind(), checkfirst=True
    )
