"""add the durable move log and match settlement

Revision ID: 6926ccefaef6
Revises: d5e81a3c9f47
Create Date: 2026-08-04 19:20:42.559767

A64-016.4 §1, §2 and §6. AD-18's second half: "Live position lives in Redis.
**Moves are appended durably to PostgreSQL.**" A64-016.3 shipped the first
half and recorded the gap — until this revision, a Redis failure lost an
in-flight game with nothing to replay from, which is the mitigation AD-19
depends on.

## Two changes, one revision

They are one change: a move log whose matches cannot record a result would
be an archive of games that never ended, and a settled match with no log
would be a result nothing can verify. Splitting them would leave a
deployable state in which neither is useful.

## What this is, against `database.md` §8.4

§8.4 specifies `game.move` as it becomes at scale: partitioned monthly on
`match_created_at`, `path` as `smallint[]` of PDN square numbers, plus
`client_move_id` and `received_at`, with non-null clock columns. This is the
subset that ships, documented as `game.match` §8.2a already documents its
own. See `MoveLogModel` for each divergence and why — the short form is that
the parent is not partitioned, the engine has no PDN numbering, and clocks
arrive in A64-016.5.

## The one asymmetry, stated plainly

`match_status` gains `completed` via `ADD VALUE`, and the downgrade **leaves
the member in the type**. PostgreSQL has no `DROP VALUE`, so removing it
means rebuilding the type — and rebuilding it means dropping and recreating
seven dependent objects, because three partial indexes and three `CHECK`
constraints on `game.match` are predicated on a status.

That was written, run, and abandoned: the `USING` cast fails on every one of
those predicates, and making it work means restating seven load-bearing
definitions inside a migration where they can silently diverge from the model
— including `ck_match__active_iff_both_accepted`, which is the invariant
A64-015.4 §4 exists for.

So the trade is: an orphan enum member after a rollback, against seven
chances to write a subtly different constraint. The member is **unreachable**
rather than merely unused — the downgrade refuses to run while any row says
`completed`, and the code at the downgrade target never writes it. Nothing
observable differs; a catalogue listing shows one member the schema cannot produce.

The downgrade's refusal is the part that matters and is not a shortcut:
silently mapping a played game onto some other status would be data loss
disguised as a rollback.

## Reversibility

Complete and symmetric. The two enums this revision creates are dropped on
the way down — `drop_table` does not remove a type, and a re-upgrade would
then fail with "type already exists".

Dropping `game.move` **discards every move log**, which is the honest
behaviour: the downgrade target has no relation to hold them. That is stated
rather than mitigated, because a migration that quietly preserved rows in a
side table would be a second, undocumented archive.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.database.types import UtcDateTime

revision: str = "6926ccefaef6"
down_revision: str | Sequence[str] | None = "d5e81a3c9f47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "game"

#: Restated rather than imported from the domain enums, per the convention
#: every migration on this platform follows: a revision describes the schema
#: as it was at *this* point, and importing a live enum would make an old
#: migration change meaning the day somebody adds a member.
_MATCH_STATUS = ("pending_acceptance", "active", "completed", "cancelled", "expired")
_MATCH_STATUS_BEFORE = ("pending_acceptance", "active", "cancelled", "expired")

#: The column default, restated for the same reason the members are: it has
#: to be dropped and restored around the type swap, and reading it from the
#: live model would make this revision change meaning.
_DEFAULT_STATUS = "pending_acceptance"

_OUTCOMES = ("win", "draw", "none")

_TERMINATION_REASONS = (
    "no_legal_moves",
    "all_pieces_captured",
    "resignation",
    "abort",
    "agreed_draw",
    "repetition",
    "move_limit",
    "flag",
    "flag_insufficient_material",
    "abandonment",
    "adjudication",
)


def upgrade() -> None:
    _add_completed_status()
    _create_result_enums()
    _add_settlement_columns()
    _create_move_log()


def downgrade() -> None:
    _drop_move_log()
    _drop_settlement_columns()
    _drop_result_enums()
    _require_no_completed_matches()
    # `completed` is deliberately left in the type — see this file's
    # docstring. `_require_no_completed_matches` above guarantees no row
    # holds it, so the member is unreachable rather than merely unused.


def _add_completed_status() -> None:
    """Adds `completed` to `game.match_status`.

    `ADD VALUE` rather than rebuilding the type, and the choice is a real
    trade rather than the lazy option — see this file's docstring on why
    the downgrade is asymmetric.

    Rebuilding would mean dropping and recreating **seven** dependent
    objects: three partial indexes predicated on `pending_acceptance`, one
    on the abandoned pair, and three `CHECK` constraints that name a
    status. Every one of them is load-bearing — `ck_match__active_iff_both_
    accepted` is the invariant A64-015.4 §4 exists for — and recreating
    them from a migration is seven chances to write a subtly different
    predicate than the model declares.

    Safe inside a transaction on PostgreSQL 12 and later provided the new
    value is not *used* in the same transaction, which nothing here does.
    """
    op.execute(f"ALTER TYPE {_SCHEMA}.match_status ADD VALUE IF NOT EXISTS 'completed'")


def _require_no_completed_matches() -> None:
    """Refuses the downgrade while a played match exists.

    Without it the `USING` cast fails with PostgreSQL's own message, which
    names a type and a column and not the thing an operator needs to know:
    that rolling back would have to discard games that were played. Failing
    first with a sentence is the difference between a rollback somebody
    understands and one they retry.
    """
    op.execute(
        sa.text(
            f"""
            DO $$
            DECLARE played bigint;
            BEGIN
                SELECT count(*) INTO played
                FROM {_SCHEMA}.match WHERE status = 'completed';

                IF played > 0 THEN
                    RAISE EXCEPTION
                        'Cannot downgrade: % match(es) are completed. Rolling back would '
                        'discard games that were played; settle them elsewhere first.',
                        played;
                END IF;
            END $$;
            """
        )
    )


def _create_result_enums() -> None:
    postgresql.ENUM(*_OUTCOMES, name="match_outcome", schema=_SCHEMA).create(
        op.get_bind(), checkfirst=True
    )
    postgresql.ENUM(*_TERMINATION_REASONS, name="match_termination_reason", schema=_SCHEMA).create(
        op.get_bind(), checkfirst=True
    )


def _drop_result_enums() -> None:
    for name in ("match_termination_reason", "match_outcome"):
        postgresql.ENUM(name=name, schema=_SCHEMA).drop(op.get_bind(), checkfirst=True)


def _add_settlement_columns() -> None:
    """The played half of a match's life — §6.

    `ply_number` carries a server default so the column can be `NOT NULL`
    on a table that already has rows: every match written before this
    revision has had no moves played, and `0` is the truth rather than a
    placeholder.
    """
    op.add_column(
        "match",
        sa.Column("ply_number", sa.Integer(), nullable=False, server_default="0"),
        schema=_SCHEMA,
    )
    op.add_column(
        "match",
        sa.Column(
            "outcome",
            postgresql.ENUM(name="match_outcome", schema=_SCHEMA, create_type=False),
            nullable=True,
        ),
        schema=_SCHEMA,
    )
    op.add_column(
        "match",
        sa.Column(
            "termination_reason",
            postgresql.ENUM(name="match_termination_reason", schema=_SCHEMA, create_type=False),
            nullable=True,
        ),
        schema=_SCHEMA,
    )
    op.add_column(
        "match",
        sa.Column(
            "winner",
            postgresql.ENUM(name="player_side", schema=_SCHEMA, create_type=False),
            nullable=True,
        ),
        schema=_SCHEMA,
    )
    op.add_column("match", sa.Column("ended_at", UtcDateTime(), nullable=True), schema=_SCHEMA)

    # BE-06: each of these is the database's copy of a check
    # `MatchRecord.__post_init__` already makes, so a row written by a
    # repair script is bound by the same rules as one written by the
    # aggregate.
    # Raw DDL rather than `create_check_constraint`, for two reasons that
    # both bite here. The naming convention would prefix the name a second
    # time (`ck_match__ck_match__…`), and — the one that actually fails —
    # `status = 'completed'` compares against an enum value added earlier in
    # *this* transaction, which PostgreSQL refuses until it is committed.
    #
    # Casting to `text` sidesteps the second: the predicate never names the
    # value as an enum literal, and the comparison is identical because the
    # enum's stored form is its value (`values_callable`).
    for name, predicate in (
        ("ck_match__outcome_iff_completed", "(status::text = 'completed') = (outcome IS NOT NULL)"),
        ("ck_match__ended_at_iff_outcome", "(outcome IS NOT NULL) = (ended_at IS NOT NULL)"),
        # A winner exactly for a decisive result. The one corruption that
        # would otherwise survive every read path and reach a rating.
        ("ck_match__winner_iff_decisive", "(winner IS NOT NULL) = (outcome::text = 'win')"),
        ("ck_match__ply_non_negative", "ply_number >= 0"),
    ):
        op.execute(f"ALTER TABLE {_SCHEMA}.match ADD CONSTRAINT {name} CHECK ({predicate})")


def _drop_settlement_columns() -> None:
    # Raw DDL on the way down too, and for the first of the two reasons
    # above: `drop_constraint` applies the naming convention and would look
    # for `ck_match__ck_match__…`, which is not what was created.
    for constraint in (
        "ck_match__ply_non_negative",
        "ck_match__winner_iff_decisive",
        "ck_match__ended_at_iff_outcome",
        "ck_match__outcome_iff_completed",
    ):
        op.execute(f"ALTER TABLE {_SCHEMA}.match DROP CONSTRAINT {constraint}")

    for column in ("ended_at", "winner", "termination_reason", "outcome", "ply_number"):
        op.drop_column("match", column, schema=_SCHEMA)


def _create_move_log() -> None:
    op.create_table(
        "move",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("match_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("ply_number", sa.Integer(), nullable=False),
        sa.Column(
            "seat",
            postgresql.ENUM(name="player_side", schema=_SCHEMA, create_type=False),
            nullable=False,
        ),
        sa.Column("path", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("captured", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("promoted_to", sa.Text(), nullable=True),
        sa.Column("position_hash", sa.Text(), nullable=False),
        sa.Column("engine_version", sa.Integer(), nullable=False),
        sa.Column("think_time_ms", sa.Integer(), nullable=True),
        sa.Column("remaining_clock_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint("ply_number >= 1", name=op.f("ck_move__ply_positive")),
        sa.CheckConstraint("array_length(path, 1) >= 2", name=op.f("ck_move__path_is_a_move")),
        sa.CheckConstraint("position_hash <> ''", name=op.f("ck_move__position_hash_present")),
        sa.CheckConstraint(
            "think_time_ms IS NULL OR think_time_ms >= 0",
            name=op.f("ck_move__think_time_non_negative"),
        ),
        sa.CheckConstraint(
            "remaining_clock_ms IS NULL OR remaining_clock_ms >= 0",
            name=op.f("ck_move__remaining_clock_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["match_id"], [f"{_SCHEMA}.match.id"], name=op.f("fk_move__match"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_move")),
        schema=_SCHEMA,
    )
    # **The concurrency mechanism** — §2 and §8. Two moves submitted for the
    # same expected ply produce one row and one integrity error, decided by
    # the index rather than by a check either writer would pass.
    op.create_index("uq_move__ply", "move", ["match_id", "ply_number"], unique=True, schema=_SCHEMA)
    # "Replay this match": every move in order, read from the index without
    # touching the heap for the ordering.
    op.create_index("ix_move__replay", "move", ["match_id", "ply_number"], schema=_SCHEMA)


def _drop_move_log() -> None:
    for index in ("ix_move__replay", "uq_move__ply"):
        op.drop_index(index, table_name="move", schema=_SCHEMA)
    op.drop_table("move", schema=_SCHEMA)
