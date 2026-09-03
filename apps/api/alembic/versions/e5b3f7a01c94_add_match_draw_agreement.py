"""add draw agreement to game.match

Revision ID: e5b3f7a01c94
Revises: c4e8a1d90f37
Create Date: 2026-08-05 16:42:11.204518

A64-020.5C-pre §4: a pending draw offer is durable Match state. It must
survive a process restart, a socket reconnect and a page refresh, which
rules out Redis — the only role that could hold it is `cache`, which is
configured to evict — and rules out the move log, because no move records
an offer.

Five columns on the match row, and the split between them is the point:

    draw_offer_by           whose offer stands, or three nulls for none
    draw_offer_ply          the ply it was made on
    draw_offer_created_at   when, for display and audit

    light_draw_offer_from_ply   the earliest ply at which each side may
    dark_draw_offer_from_ply    open a *new* offer — §3's spam rule

## Why not a table of its own

At most one offer exists per match at a time (§1), so a child table would
be one row per match reached by a join on every snapshot — to answer a
question the parent row can hold. No audit requirement asks for the history
of offers that were declined, and §4 asks for the smallest coherent schema.

## Why the thresholds are plies and not instants

§3 forbids a wall-clock cooldown, a Redis TTL and an in-process timer, and
the reason each is wrong is the same: none survives a restart, so a player
who reconnects would find their spam allowance refreshed. The ply is
already durable, already monotonic and already under the match row's lock,
so the rule reloads identically after any failure.

## Why this needs no backfill

The two thresholds are `NOT NULL DEFAULT 0`, and zero is a *total* "no
restriction" because `ply_number` is never negative. The three offer
columns are nullable and default to null, which is exactly "no offer
stands". So every historical match — played, abandoned or still running —
already has the correct value, and there is nothing for a backfill to
guess. This is deliberately unlike a default that reclassifies history:
nothing here asserts anything about a past game.

## Reversibility

Fully reversible. `downgrade` drops the four constraints and the five
columns, which returns the table to its previous shape exactly — no data
belonging to any other feature passes through them.

The information lost on a downgrade is any offer standing at that moment,
which is the correct loss: an offer is a live negotiation, and a deployment
that removes the feature removes the ability to answer one.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.database.types import UtcDateTime

revision: str = "e5b3f7a01c94"
down_revision: str | Sequence[str] | None = "c4e8a1d90f37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "game"
_TABLE = "match"

#: Reused rather than redeclared. `create_type=False` because
#: `game.player_side` already exists — it types `declined_by` and `winner` —
#: and a second `CREATE TYPE` would fail.
_SIDE_ENUM = sa.Enum("light", "dark", name="player_side", schema=_SCHEMA, create_type=False)

#: The four invariants, in creation order. Dropped in reverse, so the pair
#: is one list rather than two that can drift apart.
#:
#: `status::text` rather than `status`, matching `6926ccefaef6`: the
#: predicate never names the value as an enum literal, and the comparison is
#: identical because the enum's stored form *is* its value.
_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    (
        "ck_match__draw_offer_fields_agree",
        "(draw_offer_by IS NULL) = (draw_offer_ply IS NULL) "
        "AND (draw_offer_by IS NULL) = (draw_offer_created_at IS NULL)",
    ),
    ("ck_match__draw_offer_iff_active", "draw_offer_by IS NULL OR status::text = 'active'"),
    ("ck_match__draw_offer_ply_non_negative", "draw_offer_ply IS NULL OR draw_offer_ply >= 0"),
    (
        "ck_match__draw_offer_thresholds_non_negative",
        "light_draw_offer_from_ply >= 0 AND dark_draw_offer_from_ply >= 0",
    ),
)


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("draw_offer_by", _SIDE_ENUM, nullable=True), schema=_SCHEMA)
    op.add_column(_TABLE, sa.Column("draw_offer_ply", sa.Integer(), nullable=True), schema=_SCHEMA)
    op.add_column(
        _TABLE,
        sa.Column("draw_offer_created_at", UtcDateTime(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column("light_draw_offer_from_ply", sa.Integer(), nullable=False, server_default="0"),
        schema=_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column("dark_draw_offer_from_ply", sa.Integer(), nullable=False, server_default="0"),
        schema=_SCHEMA,
    )

    # Raw DDL rather than `create_check_constraint`, for the reason
    # `6926ccefaef6` already records against this same table: the metadata
    # naming convention prefixes the name a *second* time, so a full name
    # passed to the helper is created as `ck_match__ck_match__…` and the
    # downgrade then cannot find it. Spelling the DDL out keeps the name
    # that ships identical to the name in `models.py`.
    #
    # BE-06: each of these is the database's copy of a check
    # `MatchRecord.__post_init__` or `DrawAgreement.__post_init__` already
    # makes, so a row written by a repair script is bound by the same rules
    # as one written by the aggregate.
    for name, predicate in _CONSTRAINTS:
        op.execute(f"ALTER TABLE {_SCHEMA}.{_TABLE} ADD CONSTRAINT {name} CHECK ({predicate})")


def downgrade() -> None:
    # Raw DDL on the way down for the same reason: `drop_constraint`
    # applies the naming convention and would look for a name that was
    # never created.
    for name, _ in reversed(_CONSTRAINTS):
        op.execute(f"ALTER TABLE {_SCHEMA}.{_TABLE} DROP CONSTRAINT {name}")

    op.drop_column(_TABLE, "dark_draw_offer_from_ply", schema=_SCHEMA)
    op.drop_column(_TABLE, "light_draw_offer_from_ply", schema=_SCHEMA)
    op.drop_column(_TABLE, "draw_offer_created_at", schema=_SCHEMA)
    op.drop_column(_TABLE, "draw_offer_ply", schema=_SCHEMA)
    op.drop_column(_TABLE, "draw_offer_by", schema=_SCHEMA)
