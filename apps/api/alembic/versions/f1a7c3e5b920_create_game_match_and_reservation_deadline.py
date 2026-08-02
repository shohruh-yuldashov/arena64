"""create the game schema and match, and add queue_ticket.reserved_until

Revision ID: f1a7c3e5b920
Revises: 9be35d71c4b0
Create Date: 2026-08-02 11:04:18.663214

A64-015.4 makes a pairing durable. Two relations change and they change
together, because the thing being created spans both: a match records the
two queue tickets that produced it, and those tickets record the deadline
the match is answered by.

## One revision, two schemas

Splitting this would produce a deploy that is wrong if either half ships
alone. `PairingService` writes `reserved_until` and sends the same instant
to `game` as `acceptance_deadline` in one pass, so a database with the
column and no table pairs nobody, and one with the table and no column
cannot reserve at all.

## `uq_match__pairing_id` is the load-bearing object here

A64-015.4 §3 requires idempotent match creation and forbids both
in-memory deduplication and check-then-insert. A unique index is the only
mechanism that holds when two pairing workers retry one pairing at the same
instant — both pass any read, and only the database can refuse the second
write. `SqlAlchemyMatchRecordRepository.create` inserts, catches the
violation, and re-reads the winner's row.

Not nullable and not partial: every match comes from a pairing today, and a
direct challenge (domain-model.md §21) will carry its own key into this
column rather than leaving it empty. A nullable unique index would silently
permit any number of matches with no idempotency key at all.

## The two ticket columns are unique too

A queue ticket produces **at most one match**. Already true by construction
— a ticket is reserved once and matched once — and `uq_match__light_ticket`
and `uq_match__dark_ticket` make it true under a bug as well. They are also
the indexes `PairingReconciliationReader` reads, so the constraint that
states the invariant is the index that answers the question it is about.

## No foreign keys, in either direction

`light_player_id` and `dark_player_id` are DM-06's opaque cross-context
identifiers, and the two ticket columns point at a *different schema*. A
foreign key from `game` into `matchmaking.queue_ticket` would make the two
undeployable apart — the seam architecture.md §16 exists to keep open — and
it would outlive its usefulness immediately, because queue tickets are
prunable history and matches are permanent.

## `reserved_until` and its CHECK move together

The column is nullable and the constraint is what gives it meaning: it
exists exactly while `status = 'reserved'`. Without the CHECK a released
ticket could keep the deadline it was reserved under, and the reconciler
would then see a `waiting` row it believes is a stranded reservation.

Adding a nullable column with no default is a catalogue-only change on
PostgreSQL 11+, so this does not rewrite the table. The CHECK is validated
against existing rows, which is correct and cheap here: every live ticket
today is `waiting` with a null `reserved_until`, which satisfies it.

## Reversibility

Complete, and the two halves are not symmetric.

The `matchmaking` half is fully reversible: the index, the constraint and
the column are dropped, and nothing is lost that was not added here — a
reserved ticket survives the downgrade as a reserved ticket with no
deadline, which is exactly the state A64-015.3 left them in.

The `game` half **drops the match table and every match in it**. That is
stated plainly rather than hidden behind "reversible": this path is for a
deploy rolled back before anybody played, and running it on a database
where matches exist destroys the permanent competitive record A-4 is about.
The three enum types are dropped explicitly — `drop_table` does not remove
them, and a re-upgrade would then fail with "type already exists" — and the
schema with them, since nothing else lives in `game` yet.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f1a7c3e5b920"
down_revision: str | Sequence[str] | None = "9be35d71c4b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GAME = "game"
_MATCHMAKING = "matchmaking"

#: Restated rather than imported from the domain enums, per the convention
#: every migration on this platform follows: a revision describes the schema
#: as it was at *this* point, and importing a live enum would make an old
#: migration change meaning the day somebody adds a member.
_VARIANTS = ("russian_8x8",)
_MATCH_STATUSES = ("pending_acceptance", "active", "cancelled", "expired")
_SIDES = ("light", "dark")

_PENDING = "status = 'pending_acceptance'"
_RESERVED = "status = 'reserved'"


def upgrade() -> None:
    _create_game_match()
    _add_reservation_deadline()


def downgrade() -> None:
    _drop_reservation_deadline()
    _drop_game_match()


def _create_game_match() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{_GAME}"')

    bind = op.get_bind()
    variant = postgresql.ENUM(*_VARIANTS, name="match_variant", schema=_GAME)
    status = postgresql.ENUM(*_MATCH_STATUSES, name="match_status", schema=_GAME)
    side = postgresql.ENUM(*_SIDES, name="player_side", schema=_GAME)
    variant.create(bind, checkfirst=True)
    status.create(bind, checkfirst=True)
    side.create(bind, checkfirst=True)

    op.create_table(
        "match",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("pairing_id", sa.Uuid(as_uuid=True), nullable=False),
        # `create_type=False`: the three types are created above, once, and
        # letting the column definitions create them again would attempt a
        # duplicate `CREATE TYPE` inside the same transaction.
        sa.Column(
            "variant",
            postgresql.ENUM(*_VARIANTS, name="match_variant", schema=_GAME, create_type=False),
            nullable=False,
        ),
        sa.Column("rated", sa.Boolean(), nullable=False),
        sa.Column("engine_version", sa.Integer(), nullable=False),
        sa.Column("light_player_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("light_ticket_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("light_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dark_player_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("dark_ticket_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("dark_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acceptance_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(*_MATCH_STATUSES, name="match_status", schema=_GAME, create_type=False),
            server_default="pending_acceptance",
            nullable=False,
        ),
        sa.Column(
            "declined_by",
            postgresql.ENUM(*_SIDES, name="player_side", schema=_GAME, create_type=False),
            nullable=True,
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"({_PENDING}) = (settled_at IS NULL)", name="ck_match__settled_iff_answered"
        ),
        sa.CheckConstraint(
            "(declined_by IS NOT NULL) = (status = 'cancelled')",
            name="ck_match__declined_iff_cancelled",
        ),
        # §4's invariant, held by the database rather than only by the
        # application: a match must not become `active` until both players
        # have accepted.
        sa.CheckConstraint(
            "status <> 'active' OR (light_accepted_at IS NOT NULL "
            "AND dark_accepted_at IS NOT NULL)",
            name="ck_match__active_iff_both_accepted",
        ),
        sa.CheckConstraint(
            "acceptance_deadline > created_at", name="ck_match__acceptance_window_positive"
        ),
        sa.CheckConstraint("engine_version >= 1", name="ck_match__engine_version_positive"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_match")),
        schema=_GAME,
    )

    # Idempotency. See this revision's docstring on why a unique index
    # rather than a check-then-insert.
    op.create_index("uq_match__pairing_id", "match", ["pairing_id"], unique=True, schema=_GAME)
    op.create_index(
        "uq_match__light_ticket", "match", ["light_ticket_id"], unique=True, schema=_GAME
    )
    op.create_index("uq_match__dark_ticket", "match", ["dark_ticket_id"], unique=True, schema=_GAME)

    # "Which match must this player answer" — one per side, both partial, so
    # their size is bounded by how many people are currently being matched
    # rather than by how many games have been played.
    op.create_index(
        "ix_match__pending_light",
        "match",
        ["light_player_id"],
        schema=_GAME,
        postgresql_where=sa.text(_PENDING),
    )
    op.create_index(
        "ix_match__pending_dark",
        "match",
        ["dark_player_id"],
        schema=_GAME,
        postgresql_where=sa.text(_PENDING),
    )
    # The acceptance-expiry sweep's claim. Partial for the same reason
    # `ix_queue_ticket__due` is: a settled match can never become overdue.
    op.create_index(
        "ix_match__pending_deadline",
        "match",
        ["acceptance_deadline"],
        schema=_GAME,
        postgresql_where=sa.text(_PENDING),
    )
    # QT-3's rematch guard reads "this player's most recent match", which is
    # a `DISTINCT ON` over both columns — so each side needs its own index
    # leading with the player and carrying the instant.
    op.create_index(
        "ix_match__light_player_recent", "match", ["light_player_id", "created_at"], schema=_GAME
    )
    op.create_index(
        "ix_match__dark_player_recent", "match", ["dark_player_id", "created_at"], schema=_GAME
    )


def _drop_game_match() -> None:
    for index in (
        "ix_match__dark_player_recent",
        "ix_match__light_player_recent",
        "ix_match__pending_deadline",
        "ix_match__pending_dark",
        "ix_match__pending_light",
        "uq_match__dark_ticket",
        "uq_match__light_ticket",
        "uq_match__pairing_id",
    ):
        op.drop_index(index, table_name="match", schema=_GAME)
    op.drop_table("match", schema=_GAME)

    bind = op.get_bind()
    for name in ("player_side", "match_status", "match_variant"):
        postgresql.ENUM(name=name, schema=_GAME).drop(bind, checkfirst=True)

    op.execute(f'DROP SCHEMA IF EXISTS "{_GAME}"')


def _add_reservation_deadline() -> None:
    op.add_column(
        "queue_ticket",
        sa.Column("reserved_until", sa.DateTime(timezone=True), nullable=True),
        schema=_MATCHMAKING,
    )
    op.create_check_constraint(
        "ck_queue_ticket__reserved_iff_deadline",
        "queue_ticket",
        sa.text(f"({_RESERVED}) = (reserved_until IS NOT NULL)"),
        schema=_MATCHMAKING,
    )
    op.create_index(
        "ix_queue_ticket__stale_reservation",
        "queue_ticket",
        ["reserved_until"],
        schema=_MATCHMAKING,
        postgresql_where=sa.text(_RESERVED),
    )


def _drop_reservation_deadline() -> None:
    op.drop_index(
        "ix_queue_ticket__stale_reservation", table_name="queue_ticket", schema=_MATCHMAKING
    )
    op.drop_constraint(
        "ck_queue_ticket__reserved_iff_deadline",
        "queue_ticket",
        schema=_MATCHMAKING,
        type_="check",
    )
    op.drop_column("queue_ticket", "reserved_until", schema=_MATCHMAKING)
