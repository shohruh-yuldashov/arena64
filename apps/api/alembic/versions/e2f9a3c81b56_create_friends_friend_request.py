"""create friends friend_request table

Revision ID: e2f9a3c81b56
Revises: c4e8b1a29f37
Create Date: 2026-08-01 16:48:19.337204

A64-013.2 gives the `friends` bounded context its first relation —
database.md §7.1.

## `CREATE SCHEMA friends`

Alembic detects tables in a non-default schema but does not create the
schema itself, so this is hand-added exactly as `7ed700e67f2a` did for
`users`, `00debbb7452d` for `auth` and `b0f336b06542` for `statistics`.

## The enum carries three values nothing writes

`expired` and `declined_by_block` are in the type from the first release,
and neither has a producer: expiry is excluded from A64-013.2 and blocking
is A64-013.5. They are here because `ALTER TYPE ... ADD VALUE` on an enum
used by an indexed column of a live table is a migration nobody should have
to schedule in order to ship a feature — and because, until PostgreSQL 12,
it could not run inside a transaction at all. Declaring them now costs
nothing.

## No foreign keys to `users.user`

Deliberate, and the one thing here most likely to look like an omission —
A64-013.2 lists foreign keys among its requirements, so this is a documented
deviation rather than a miss.

Cross-context references are opaque `player_id` values (DM-06), and an FK
from `friends` into `users` would make the two schemas undeployable apart,
which is precisely the extraction seam architecture.md §16 exists to keep
open. `statistics.player_statistics` makes the same choice for the same
reason, and database.md §1611 goes further: `player_id` survives erasure as
a tombstone, which a cascade would delete.

## Why the pending uniqueness is a partial index

database.md §7.1 states it outright: a plain unique on `(requester_id,
addressee_id)` would permit only one request *ever* between two players, so
a friendship that ended could never be re-requested. The partial index
constrains the live state — which is what FR-1 actually says — and leaves
the historical rows that FR-5's decline cooldown reads untouched.

## Reversibility

Complete. `downgrade` drops the table, then the enum type, then the schema.
The schema drop is unqualified rather than `CASCADE`: if another table has
appeared in `friends` by then, this migration must fail loudly rather than
take it with it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e2f9a3c81b56"
down_revision: str | Sequence[str] | None = "c4e8b1a29f37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The six members, in the order the type declares them. Named once so the
#: `CREATE TYPE` below and the column that references it cannot disagree.
_STATUS_VALUES = (
    "pending",
    "accepted",
    "declined",
    "cancelled",
    "expired",
    "declined_by_block",
)

#: **`create_type=False` is load-bearing**, and it must be the PostgreSQL
#: `ENUM` rather than `sa.Enum` for it to have any effect — the flag is a
#: dialect-specific one, and `sa.Enum` accepts it silently and ignores it.
#:
#: Without it, `op.create_table` emits its own `CREATE TYPE` for the column
#: on top of the explicit one below, and the second fails with
#: `DuplicateObjectError`.
#:
#: Creating the type explicitly rather than letting the table do it is what
#: makes `downgrade` symmetrical: `drop_table` does not drop a type it
#: created, so relying on the implicit creation would leave the type behind
#: and break the next upgrade.
_STATUS = postgresql.ENUM(
    *_STATUS_VALUES,
    name="friend_request_status",
    schema="friends",
    create_type=False,
)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS friends")
    members = ", ".join(f"'{value}'" for value in _STATUS_VALUES)
    op.execute(f"CREATE TYPE friends.friend_request_status AS ENUM ({members})")

    op.create_table(
        "friend_request",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("requester_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("addressee_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("status", _STATUS, server_default="pending", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_friend_request")),
        sa.CheckConstraint("requester_id <> addressee_id", name="ck_friend_request__not_self"),
        # `responded_at` set exactly when the status is not pending. The
        # aggregate sets both in one statement, so this is BE-06's
        # authoritative copy: a row written by a repair script or a future
        # expiry sweep cannot record an outcome without its instant.
        sa.CheckConstraint(
            "(status = 'pending') = (responded_at IS NULL)",
            name="ck_friend_request__responded_iff_resolved",
        ),
        schema="friends",
    )

    # FR-1, partial — see this module's docstring.
    op.create_index(
        "uq_friend_request__one_pending_per_pair",
        "friend_request",
        ["requester_id", "addressee_id"],
        unique=True,
        schema="friends",
        postgresql_where=sa.text("status = 'pending'"),
    )
    # One index per list endpoint. The filter column leads and the ordering
    # key follows, so PostgreSQL walks the index backwards for the
    # newest-first keyset and stops at the page size instead of sorting the
    # whole result.
    op.create_index(
        "ix_friend_request__addressee",
        "friend_request",
        ["addressee_id", "created_at", "id"],
        schema="friends",
    )
    op.create_index(
        "ix_friend_request__requester",
        "friend_request",
        ["requester_id", "created_at", "id"],
        schema="friends",
    )


def downgrade() -> None:
    op.drop_index("ix_friend_request__requester", "friend_request", schema="friends")
    op.drop_index("ix_friend_request__addressee", "friend_request", schema="friends")
    op.drop_index("uq_friend_request__one_pending_per_pair", "friend_request", schema="friends")
    op.drop_table("friend_request", schema="friends")
    op.execute("DROP TYPE friends.friend_request_status")
    # Unqualified, not CASCADE: a second table in this schema means
    # something else was added, and this migration must fail rather than
    # delete it.
    op.execute("DROP SCHEMA IF EXISTS friends")
