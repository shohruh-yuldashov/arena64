"""add friend challenges

Revision ID: 1ba6f5d18023
Revises: e26a0159c56b
Create Date: 2026-08-07 16:04:12.550916

A64-022.1 §13, §14. One table in the `matchmaking` schema: a direct
invitation from one player to a friend, and the settings it fixes.

## Why `matchmaking` and not a schema of its own

`domain-model.md` §10.3 places `Challenge` in this context beside
`QueueTicket`, and its own comparison table says why: both are intentions to
play that resolve into a `Match`, differing in who chooses the opponent and
how long the intention lives rather than in what they are.

## The unique index is the product rule

`uq_friend_challenge__live_pair` is "one live challenge per pair, whichever
direction" (§6, policy A) expressed as a constraint. It is keyed on
`least(challenger_id, recipient_id), greatest(...)` so the pair is
**unordered** — a plain unique on the two columns would permit exactly the
opposite-direction case it exists to prevent, and an application check would
lose the race between two friends challenging each other at the same moment.

Partial on `pending`, for the reason `uq_queue_ticket__one_live` is: a plain
unique would mean two players could challenge each other once ever. The rule
is about the live state.

## Three `CHECK`s, and each is an invariant the domain already enforces

They exist for the second writer — a future backfill, an operator's `UPDATE`,
a repository written next year — not because the aggregate is untrusted.

## No foreign keys

`challenger_id` and `recipient_id` are opaque cross-context identifiers
(DM-06), like every player reference on this platform, so the schemas stay
deployable apart. `created_match_id` has none either, and that one is
retention: a challenge is the record of an invitation and must outlive the
game it produced.

## No backfill

Nothing existing is a challenge. The table starts empty, and
`created_match_id` starts `NULL` on every row and stays that way until
A64-022.3 — the column exists now so that adding acceptance is not a schema
change on a live table.

## Reversibility

Fully reversible: `downgrade` drops the table and its three enum types.
What is lost is every challenge, which at this revision is nothing — and
after it, a set of invitations whose loss costs each player one re-send. No
`Match`, no notification and no friendship is affected in either direction:
nothing references this table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.database.types import UtcDateTime

revision: str = "1ba6f5d18023"
down_revision: str | Sequence[str] | None = "e26a0159c56b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "matchmaking"

#: Values are the enum members', written out rather than imported.
#:
#: A migration is a historical record: importing the enum would make this
#: revision describe whatever the code says *today*, so a member added next
#: year would silently change what this migration claims to have created.
_STATUSES = ("pending", "accepted", "declined", "cancelled", "expired")
_VARIANTS = ("russian_8x8",)
_TIME_CONTROLS = ("bullet_1_0", "blitz_3_2", "rapid_10_0", "classical_30_0")

#: `create_type=False`: this migration manages the types' lifecycle itself,
#: in `upgrade` and `downgrade` below. Without it `create_table` emits a
#: second `CREATE TYPE` for every enum column and the migration fails on its
#: own types — the classic double-create, and the repository already writes
#: it this way in `00debbb7452d`.
_STATUS_ENUM = postgresql.ENUM(
    *_STATUSES, name="challenge_status", schema=_SCHEMA, create_type=False
)
_VARIANT_ENUM = postgresql.ENUM(
    *_VARIANTS, name="challenge_variant", schema=_SCHEMA, create_type=False
)
_TIME_CONTROL_ENUM = postgresql.ENUM(
    *_TIME_CONTROLS, name="challenge_time_control", schema=_SCHEMA, create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    # Its own types rather than the queue's: a shared type is a shared
    # migration, so adding a challenge status would `ALTER TYPE` a column
    # belonging to `queue_ticket`.
    for enum in (_STATUS_ENUM, _VARIANT_ENUM, _TIME_CONTROL_ENUM):
        enum.create(bind, checkfirst=True)

    op.create_table(
        "friend_challenge",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("challenger_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("recipient_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("time_control_id", _TIME_CONTROL_ENUM, nullable=False),
        sa.Column("variant", _VARIANT_ENUM, nullable=False),
        sa.Column("rated", sa.Boolean(), nullable=False),
        sa.Column("status", _STATUS_ENUM, nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("expires_at", UtcDateTime(), nullable=False),
        sa.Column("responded_at", UtcDateTime(), nullable=True),
        sa.Column("created_match_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_friend_challenge"),
        # A challenge to oneself is a row that cannot mean anything.
        sa.CheckConstraint(
            "challenger_id <> recipient_id", name="ck_friend_challenge__distinct_players"
        ),
        # A terminal challenge has a response time and a pending one does not.
        sa.CheckConstraint(
            "(status = 'pending') = (responded_at IS NULL)",
            name="ck_friend_challenge__responded_when_settled",
        ),
        # Only an accepted challenge may name a match. A `created_match_id`
        # on a declined row would be a game nobody agreed to play.
        sa.CheckConstraint(
            "created_match_id IS NULL OR status = 'accepted'",
            name="ck_friend_challenge__match_only_when_accepted",
        ),
        schema=_SCHEMA,
    )

    # The product rule — see the module docstring.
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_friend_challenge__live_pair "
            f"ON {_SCHEMA}.friend_challenge "
            "(least(challenger_id, recipient_id), greatest(challenger_id, recipient_id)) "
            "WHERE status = 'pending'"
        )
    )

    # "Who has invited me" and "what have I sent" — two screens, two players
    # in the predicate, so two indexes rather than one composite. Partial, so
    # a table that accumulates answered history costs nothing to scan.
    op.create_index(
        "ix_friend_challenge__recipient_pending",
        "friend_challenge",
        ["recipient_id"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_friend_challenge__challenger_pending",
        "friend_challenge",
        ["challenger_id"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
        schema=_SCHEMA,
    )
    # The expiry sweep's claim query (A64-022.6): pending rows whose window
    # has closed.
    op.create_index(
        "ix_friend_challenge__expiring",
        "friend_challenge",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_friend_challenge__expiring",
        table_name="friend_challenge",
        schema=_SCHEMA,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index(
        "ix_friend_challenge__challenger_pending",
        table_name="friend_challenge",
        schema=_SCHEMA,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index(
        "ix_friend_challenge__recipient_pending",
        table_name="friend_challenge",
        schema=_SCHEMA,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.execute(sa.text(f"DROP INDEX IF EXISTS {_SCHEMA}.uq_friend_challenge__live_pair"))
    op.drop_table("friend_challenge", schema=_SCHEMA)

    bind = op.get_bind()
    for enum in (_TIME_CONTROL_ENUM, _VARIANT_ENUM, _STATUS_ENUM):
        enum.drop(bind, checkfirst=True)
